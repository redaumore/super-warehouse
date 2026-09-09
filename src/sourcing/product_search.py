"""Attribute-based product search for the manual order form (no LLM).

One read-only use case that queries BOTH product sources the owner can order
from and returns them in the UI contract order:

1. LOCAL hits first — ``catalogo`` (the pricing LOCAL catalog) left-joined with
   the canonical ``inventory`` counter, so every local row shows its stock.
2. RAG hits after — ``catalogo_productos_rag`` (indexed supplier catalogs,
   owned by the rag-api service; read-only here, never migrated/written).

Filter semantics mirror the Productos tab RAG consultation (exact normalized
proveedor, folded-accent substrings for marca/categoría/código, text over the
free-text column). Same article may appear twice (once per source): there is
NO dedup across sources — the owner chooses which line completes the order and
may add both when local stock does not cover the demand.

The search never writes and never commits: it takes the caller's ``Session``
and returns plain rows. Pure SQL, no LLM call involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import Catalogo, Inventory, Supplier

_SEARCH_MAX_LIMIT = 1000

# Accent folding for text filters: ILIKE does not ignore accents, so a user
# typing "griferias" would never match "Griferías". Both sides are folded with
# translate() (Postgres char-by-char mapping); "ñ" stays distinct to avoid
# false positives (caño vs cano). Same convention as the Productos tab.
_ACCENTED = "áéíóúüÁÉÍÓÚÜ"
_PLAIN = "aeiouuAEIOUU"


def _fold_accents(value: str) -> str:
    return value.translate(str.maketrans(_ACCENTED, _PLAIN))


def _folded(column: Any) -> Any:
    return func.translate(column, _ACCENTED, _PLAIN)


@dataclass(frozen=True)
class ProductSearchHit:
    """One searchable product row, tagged with its source for the order form.

    ``stock`` is only meaningful for LOCAL hits (None for RAG). ``price`` is
    the list base price for LOCAL and the original-denomination offer price
    for RAG (the currency lives in ``moneda``).
    """

    sku: str
    name: str
    source: str  # "LOCAL" | "RAG"
    stock: int | None
    marca: str | None
    categoria: str | None
    supplier: str | None
    price: Decimal | None
    moneda: str | None


def _compose_rag_name(marca: str | None, categoria: str | None, subcategoria: str | None) -> str:
    """Best-effort display name from the RAG row metadata (no clean name column)."""
    return " ".join(part for part in (marca, categoria, subcategoria) if part).strip()


@lru_cache(maxsize=4)
def rag_products_table(table_name: str) -> Table:
    """Lightweight read-only mapping of the RAG catalog table.

    Mirror of the backoffice mapping (src/backoffice/catalog.py): the table is
    created and owned by the rag-api ingestion service, never created or
    written from here, and the ``embedding`` column is intentionally absent.
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


def search_order_products(
    session: Session,
    *,
    proveedor: str | None = None,
    marca: str | None = None,
    categoria: str | None = None,
    codigo: str | None = None,
    texto: str | None = None,
    limit: int = 100,
) -> list[ProductSearchHit]:
    """Search LOCAL inventory and the RAG catalog; LOCAL hits come first.

    Filter semantics (all optional, combined with AND, per source):
    - ``proveedor``: exact match after strip/upper — supplier code for LOCAL,
      ``codigo_proveedor`` for RAG.
    - ``marca``: case- and accent-insensitive substring.
    - ``categoria``: case- and accent-insensitive substring (LOCAL over
      ``categoria``/``subcategoria``; RAG over ``categoria``/``categoria_padre``).
    - ``codigo``: substring over the internal SKU + barcode (LOCAL) or over
      ``codigo_producto``/``codigo_orig`` (RAG).
    - ``texto``: substring over ``nombre_oficial`` (LOCAL) or ``text_content``
      (RAG).
    - ``limit``: clamped to [1, 1000] and applied PER LEG so one source cannot
      crowd the other out of the results grid.

    Raises ``ValueError`` when no filter is supplied, so the UI can never
    trigger a full scan by accident. No dedup across sources: the same article
    may appear once per source.
    """
    filters = {
        "proveedor": (proveedor or "").strip(),
        "marca": (marca or "").strip(),
        "categoria": (categoria or "").strip(),
        "codigo": (codigo or "").strip(),
        "texto": (texto or "").strip(),
    }
    if not any(filters.values()):
        raise ValueError("Especificá al menos un filtro (proveedor, marca, categoría, código o texto).")

    effective_limit = max(1, min(int(limit), _SEARCH_MAX_LIMIT))
    hits = [
        *_search_local(session, filters, effective_limit),
        *_search_rag(session, filters, effective_limit),
    ]
    return hits


def _search_local(
    session: Session, filters: dict[str, str], limit: int
) -> list[ProductSearchHit]:
    """LOCAL leg: catalogo ⋈ inventory, priced from the list base snapshot."""
    conditions = []
    if filters["proveedor"]:
        conditions.append(func.upper(Supplier.code) == filters["proveedor"].upper())
    if filters["marca"]:
        pattern = f"%{_fold_accents(filters['marca'])}%"
        conditions.append(_folded(Catalogo.marca).ilike(pattern))
    if filters["categoria"]:
        pattern = f"%{_fold_accents(filters['categoria'])}%"
        conditions.append(
            or_(_folded(Catalogo.categoria).ilike(pattern), _folded(Catalogo.subcategoria).ilike(pattern))
        )
    if filters["codigo"]:
        pattern = f"%{_fold_accents(filters['codigo'])}%"
        conditions.append(
            or_(
                _folded(Catalogo.codigo_interno).ilike(pattern),
                _folded(Catalogo.codigo_barras).ilike(pattern),
            )
        )
    if filters["texto"]:
        pattern = f"%{_fold_accents(filters['texto'])}%"
        conditions.append(_folded(Catalogo.nombre_oficial).ilike(pattern))

    stock = func.coalesce(Inventory.quantity_on_hand, 0)
    stmt = (
        select(Catalogo, stock)
        .join(Catalogo.supplier)  # every catalog row has a supplier (non-null FK)
        .join(Inventory, Inventory.sku_id == Catalogo.codigo_interno, isouter=True)
        .where(and_(*conditions))
        .order_by(Catalogo.codigo_interno)
        .limit(limit)
    )
    return [
        ProductSearchHit(
            sku=product.codigo_interno,
            name=product.nombre_oficial,
            source="LOCAL",
            stock=int(row_stock),
            marca=product.marca,
            categoria=product.categoria,
            supplier=product.supplier.code if product.supplier else None,
            price=product.precio_lista_base,
            moneda="ARS",
        )
        for product, row_stock in session.execute(stmt)
    ]


def _search_rag(
    session: Session, filters: dict[str, str], limit: int
) -> list[ProductSearchHit]:
    """RAG leg: same filters/conventions as the Productos tab consultation."""
    table = rag_products_table(get_settings().rag_table_name)
    conditions = []
    if filters["proveedor"]:
        conditions.append(table.c.codigo_proveedor == filters["proveedor"].upper())
    if filters["marca"]:
        pattern = f"%{_fold_accents(filters['marca'])}%"
        conditions.append(_folded(table.c.marca).ilike(pattern))
    if filters["categoria"]:
        pattern = f"%{_fold_accents(filters['categoria'])}%"
        conditions.append(
            or_(
                _folded(table.c.categoria).ilike(pattern),
                _folded(table.c.categoria_padre).ilike(pattern),
            )
        )
    if filters["codigo"]:
        pattern = f"%{_fold_accents(filters['codigo'])}%"
        conditions.append(
            or_(
                _folded(table.c.codigo_producto).ilike(pattern),
                _folded(table.c.codigo_orig).ilike(pattern),
            )
        )
    if filters["texto"]:
        conditions.append(
            _folded(table.c.text_content).ilike(f"%{_fold_accents(filters['texto'])}%")
        )

    stmt = (
        select(
            table.c.codigo_producto,
            table.c.marca,
            table.c.categoria,
            table.c.subcategoria,
            table.c.codigo_proveedor,
            table.c.precio,
            table.c.moneda,
        )
        .where(and_(*conditions))
        .order_by(table.c.codigo_producto)
        .limit(limit)
    )
    return [
        ProductSearchHit(
            sku=row.codigo_producto,
            name=_compose_rag_name(row.marca, row.categoria, row.subcategoria),
            source="RAG",
            stock=None,
            marca=row.marca,
            categoria=row.categoria,
            supplier=row.codigo_proveedor,
            price=row.precio,
            moneda=row.moneda,
        )
        for row in session.execute(stmt)
    ]
