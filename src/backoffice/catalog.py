"""Backoffice catalog & stock editor (task 3.7).

Pure DB operations behind the Gradio catalog tab: browse products, edit stock,
price or margin. Margin edits recompute the list base price through the pure
pricing engine (base = cost × (1 + margin)) so the backoffice never diverges
from the pricing rules. Also exposes read-only queries over the RAG catalog
table (supplier catalogs indexed by the rag-api service) so the Productos tab
can consult indexed products with field filters — no LLM call involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache

from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table, and_, func, or_, select
from sqlalchemy.orm import Session

from src.backoffice.sku_mappings import find_product_by_any_supplier_code, primary_supplier_codes
from src.config import get_settings
from src.db.models import Catalogo, Inventory, Supplier
from src.pricing.engine import compute_base

_CENT = Decimal("0.01")

_RAG_SEARCH_MAX_LIMIT = 1000

# Accent folding for text filters: ILIKE does not ignore accents, so a user
# typing "griferias" would never match "Griferías". Both sides are folded
# with translate() (Postgres char-by-char mapping); "ñ" is kept distinct to
# avoid false positives (caño vs cano).
_ACCENTED = "áéíóúüÁÉÍÓÚÜ"
_PLAIN = "aeiouuAEIOUU"


def _fold_accents(value: str) -> str:
    return value.translate(str.maketrans(_ACCENTED, _PLAIN))


def _folded(column) -> object:
    return func.translate(column, _ACCENTED, _PLAIN)


def list_products(session: Session) -> list[dict[str, object]]:
    """Every product row for the catalog grid.

    ``codigo_interno`` is an OPAQUE internal identifier (user-approved design):
    the grid leads with the SUPPLIER code + the product's primary mapped
    supplier code instead. Stock comes from the canonical
    ``Inventory.quantity_on_hand`` (ADR 0002); a product with no Inventory row
    displays zero. The primary supplier code is the mapping with the highest
    confidence (earliest id on ties), fetched in one extra query — empty
    string when the product is unmapped.
    """
    results = session.execute(
        select(Catalogo, Inventory.quantity_on_hand, Supplier.code)
        .join(Catalogo.supplier)  # every catalog row has a supplier (non-null FK)
        .outerjoin(Inventory, Inventory.sku_id == Catalogo.codigo_interno)
        .order_by(Catalogo.codigo_interno)
    )
    products = list(results)
    primary_codes = primary_supplier_codes(session, [p.codigo_interno for p, _, _ in products])
    rows = []
    for product, on_hand, supplier_code in products:
        rows.append(
            {
                "codigo_interno": product.codigo_interno,
                "supplier_code": supplier_code,
                "supplier_sku_code": primary_codes.get(product.codigo_interno, ""),
                "codigo_barras": product.codigo_barras or "",
                "nombre_oficial": product.nombre_oficial,
                "costo_proveedor": str(product.costo_proveedor),
                "margen_aplicado_pct": str(product.margen_aplicado_pct),
                "precio_lista_base": str(product.precio_lista_base),
                "on_hand": int(on_hand or 0),
            }
        )
    return rows


def resolve_product_code(session: Session, code: str) -> Catalogo:
    """Resolve a typed code to a catalog product for the Productos edit box.

    Resolution order: (a) exact ``codigo_interno`` match first (cheap, keeps
    the internal identifier usable as a fallback), else (b) the code as a
    mapped supplier code across ALL suppliers. Raises ``KeyError`` when
    neither matches, with a message that mentions both options (surfaced in
    the UI status box).
    """
    text = (code or "").strip()
    product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == text)) if text else None
    if product is None:
        product = find_product_by_any_supplier_code(session, text)
    if product is None:
        raise KeyError(
            "Producto no encontrado: ingresá el SKU interno o el código de proveedor del producto."
        )
    return product


def _product_by_sku(session: Session, sku: str) -> Catalogo:
    product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == sku))
    if product is None:
        raise KeyError(f"unknown sku: {sku}")
    return product


def update_stock(session: Session, sku: str, stock: int) -> Catalogo:
    """Set the on-hand stock of a product (audited adjustments live in Phase 3+).

    ``Inventory.quantity_on_hand`` is the single on-hand source (ADR 0002);
    this edit writes it directly.
    """
    if stock < 0:
        raise ValueError("stock cannot be negative")
    product = _product_by_sku(session, sku)
    inventory_row = session.scalar(select(Inventory).where(Inventory.sku_id == sku))
    if inventory_row is not None:
        inventory_row.quantity_on_hand = stock
        inventory_row.updated_at = datetime.now(UTC)
    else:
        session.add(Inventory(sku_id=sku, quantity_on_hand=stock))
    session.flush()
    return product


def update_price(session: Session, sku: str, price: Decimal | float) -> Catalogo:
    """Set the list base price directly."""
    if Decimal(str(price)) < 0:
        raise ValueError("price cannot be negative")
    product = _product_by_sku(session, sku)
    product.precio_lista_base = Decimal(str(price)).quantize(_CENT)
    session.flush()
    return product


def update_margin(session: Session, sku: str, margin: Decimal | float) -> Catalogo:
    """Set the applied margin and recompute the list base price."""
    if Decimal(str(margin)) < 0:
        raise ValueError("price cannot be negative")
    product = _product_by_sku(session, sku)
    product.margen_aplicado_pct = Decimal(str(margin)).quantize(_CENT)
    product.precio_lista_base = compute_base(product.costo_proveedor, margin)
    session.flush()
    return product


# ---------------------------------------------------------------- RAG catalog query


@lru_cache(maxsize=4)
def _rag_products_table(table_name: str) -> Table:
    """Lightweight read-only mapping of the RAG catalog table.

    The table is created and owned by the rag-api ingestion service; it is never
    created, migrated or written from the backoffice. Declared in its own
    ``MetaData`` so it stays out of ``Base.metadata`` (Alembic/create_all must
    not touch it) and only the columns this query needs are mapped — in
    particular the ``embedding`` vector column is intentionally absent.
    """
    return Table(
        table_name,
        MetaData(),
        Column("node_id", String, primary_key=True),
        Column("codigo_producto", String),
        Column("codigo_orig", String),
        Column("nombre_proveedor", String),
        Column("codigo_proveedor", String),
        Column("marca", String),
        Column("categoria_padre", String),
        Column("categoria", String),
        Column("subcategoria", String),
        Column("precio", Numeric),
        Column("moneda", String),
        Column("pagina_origen", Integer),
        Column("archivo_origen", String),
        Column("text_content", String),
    )


def search_rag_products(
    session: Session,
    *,
    codigo_proveedor: str | None = None,
    marca: str | None = None,
    categoria: str | None = None,
    codigo: str | None = None,
    texto: str | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Filter rows of the RAG catalog table with plain SQL — no LLM call.

    Filter semantics (all optional, combined with AND):
    - ``codigo_proveedor``: exact match after strip/upper (3-char supplier code).
    - ``marca``: case- and accent-insensitive substring.
    - ``categoria``: case- and accent-insensitive substring over ``categoria``
      and ``categoria_padre``.
    - ``codigo``: case- and accent-insensitive substring over
      ``codigo_producto`` and ``codigo_orig``.
    - ``texto``: case- and accent-insensitive substring over ``text_content``.
    - ``limit``: clamped to [1, 1000]; default 100.

    Raises ``ValueError`` when no filter is supplied, so the UI can never
    trigger a full-table scan by accident.
    """
    filters = {
        "codigo_proveedor": (codigo_proveedor or "").strip(),
        "marca": (marca or "").strip(),
        "categoria": (categoria or "").strip(),
        "codigo": (codigo or "").strip(),
        "texto": (texto or "").strip(),
    }
    if not any(filters.values()):
        raise ValueError("Especificá al menos un filtro (proveedor, marca, categoría, código o texto).")

    table = _rag_products_table(get_settings().rag_table_name)
    conditions = []
    if filters["codigo_proveedor"]:
        conditions.append(table.c.codigo_proveedor == filters["codigo_proveedor"].upper())
    if filters["marca"]:
        pattern = f"%{_fold_accents(filters['marca'])}%"
        conditions.append(_folded(table.c.marca).ilike(pattern))
    if filters["categoria"]:
        pattern = f"%{_fold_accents(filters['categoria'])}%"
        conditions.append(
            or_(_folded(table.c.categoria).ilike(pattern), _folded(table.c.categoria_padre).ilike(pattern))
        )
    if filters["codigo"]:
        pattern = f"%{_fold_accents(filters['codigo'])}%"
        conditions.append(
            or_(_folded(table.c.codigo_producto).ilike(pattern), _folded(table.c.codigo_orig).ilike(pattern))
        )
    if filters["texto"]:
        conditions.append(_folded(table.c.text_content).ilike(f"%{_fold_accents(filters['texto'])}%"))

    stmt = (
        select(
            table.c.codigo_producto,
            table.c.codigo_orig,
            table.c.codigo_proveedor,
            table.c.nombre_proveedor,
            table.c.marca,
            table.c.categoria,
            table.c.subcategoria,
            table.c.precio,
            table.c.moneda,
            table.c.pagina_origen,
            table.c.archivo_origen,
        )
        .where(and_(*conditions))
        .order_by(table.c.codigo_proveedor, table.c.codigo_producto)
        .limit(max(1, min(int(limit), _RAG_SEARCH_MAX_LIMIT)))
    )

    rows: list[dict[str, object]] = []
    for row in session.execute(stmt):
        rows.append(
            {
                "codigo": row.codigo_producto,
                "codigo_orig": row.codigo_orig,
                "proveedor": row.codigo_proveedor,
                "nombre_proveedor": row.nombre_proveedor,
                "marca": row.marca,
                "categoria": row.categoria,
                "subcategoria": row.subcategoria,
                "precio": float(row.precio) if row.precio is not None else None,
                "moneda": row.moneda,
                "pagina": row.pagina_origen,
                "archivo": row.archivo_origen,
            }
        )
    return rows
