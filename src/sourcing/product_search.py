"""Attribute-based product search for the manual order form and the unified
Productos tab (no LLM for the SQL legs).

One read-only use case that queries BOTH product sources the owner can order
from and returns them in the UI contract order:

1. LOCAL hits first — ``catalogo`` (the pricing LOCAL catalog) left-joined with
   the canonical ``inventory`` counter, so every local row shows its stock.
2. RAG hits after — ``catalogo_productos_rag`` (indexed supplier catalogs,
   owned by the rag-api service; read-only here, never migrated/written).

Filter semantics mirror the Productos tab consultation (exact normalized
proveedor, folded-accent substrings for marca/categoría/subcategoría/código,
text over the free-text column). Same article may appear twice (once per
source): there is NO dedup across sources — the owner chooses which line
completes the order and may add both when local stock does not cover the
demand.

The unified entry point (``search_products_unified``) adds:

- ``scope``: ``"local"`` / ``"prov"`` / ``"both"`` (default) — which legs run.
- ``subcategoria``: independent folded substring on the ``subcategoria``
  column of each leg (the existing ``categoria`` filter keeps its
  categoria/subcategoria OR semantics for the Pedidos chain).
- ``nombre``: folded substring on ``nombre_oficial`` for the LOCAL leg, and a
  VECTOR query through the rag-api (``RagProductClient.query()``) for the PROV
  leg; field filters set together with ``nombre`` post-filter the vector
  results in Python with the same folded-substring semantics. When the vector
  service is unavailable the leg degrades to the SQL rag-table path (nombre as
  folded substring over ``text_content``) and reports a note to the caller.

The search never writes and never commits: it takes the caller's ``Session``
and returns plain rows. Pure SQL for both legs; the only network I/O is the
injected rag-api vector query.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Any, Protocol

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

from src.backoffice.sku_mappings import primary_supplier_codes
from src.config import get_settings
from src.db.models import Catalogo, ExchangeRate, Inventory, Supplier, SupplierSkuMapping
from src.pricing.engine import compute_base
from src.supplier.rag_catalog import RagProduct, RagProductError

_SEARCH_MAX_LIMIT = 1000

_CENT = Decimal("0.01")

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


def _folded_contains(haystack: str | None, needle: str) -> bool:
    """Accent- and case-insensitive substring check (vector post-filter)."""
    return _fold_accents(needle).lower() in _fold_accents(haystack or "").lower()


class RagVectorClient(Protocol):
    """Structural contract for the injected vector-search client.

    Defined here so the L1 use case never imports the L2 ``src.integrations``
    adapter directly (import-linter layer contract): the caller (backoffice)
    builds the client and passes it in. Only ``query`` is required.
    """

    def query(self, text: str) -> tuple[RagProduct, ...]: ...


@dataclass(frozen=True)
class ProductSearchHit:
    """One searchable product row, tagged with its source for the order form.

    ``stock`` is only meaningful for LOCAL hits (None for RAG). ``price`` is
    the list base price for LOCAL and the original-denomination offer price
    for RAG (the currency lives in ``moneda``).

    ``display_code`` is what the UI shows in the results grid: for LOCAL hits
    the product's primary mapped supplier code (``codigo_interno`` is opaque;
    fallback to it when the product is unmapped), for RAG hits the RAG
    ``codigo_producto``. Order lines are always built from ``sku`` — the
    display code never leaks into storage.

    Extended fields (all optional, appended after ``display_code`` so the
    Pedidos chain keeps its exact previous shape) feed the unified Productos
    grid: ``subcategoria`` plus the LOCAL pricing snapshot (``costo``,
    ``margen_pct`` stored as percentage points, display-time ``precio_lista_ars``
    and the stored ``moneda_original``). They stay None for RAG hits.
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
    display_code: str
    subcategoria: str | None = None
    costo: Decimal | None = None
    margen_pct: Decimal | None = None
    precio_lista_ars: Decimal | None = None
    moneda_original: str | None = None


def _list_price_ars(product: Catalogo, usd_rate: Decimal | None) -> Decimal | None:
    """Display-time AR$ list price — mirror of ``backoffice.catalog._list_price_ars``.

    The reverse import (sourcing -> backoffice) is forbidden by the layer
    contract, so the same conversion (cost × (1 + margin) × currency rate,
    with the margin stored as percentage points) is mirrored here for the
    unified Productos grid. ``precio_lista_base`` is never mutated.
    """
    if product.costo_proveedor is None:
        return None
    currency = (product.moneda or "ARS").strip().upper()
    rate = Decimal(1)
    if currency == "USD" and usd_rate is not None:
        rate = usd_rate
    margin = product.margen_aplicado_pct
    if margin is not None and margin.copy_abs() > 1:
        margin = margin / Decimal(100)
    base = compute_base(product.costo_proveedor, margin)
    return (base * rate).quantize(_CENT, rounding=ROUND_HALF_UP)


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

    Backward-compatible entry for the Pedidos chain: delegates to
    ``search_products_unified`` with the default scope (both legs) and no
    ``nombre``/``subcategoria`` filters, so its behavior is unchanged.

    Filter semantics (all optional, combined with AND, per source):
    - ``proveedor``: exact match after strip/upper — supplier code for LOCAL,
      ``codigo_proveedor`` for RAG.
    - ``marca``: case- and accent-insensitive substring.
    - ``categoria``: case- and accent-insensitive substring (LOCAL over
      ``categoria``/``subcategoria``; RAG over ``categoria``/``categoria_padre``).
    - ``codigo``: substring over the internal SKU + barcode, or over any mapped
      supplier code of the product (LOCAL); over ``codigo_producto``/
      ``codigo_orig`` (RAG).
    - ``texto``: substring over ``nombre_oficial`` (LOCAL) or ``text_content``
      (RAG).
    - ``limit``: clamped to [1, 1000] and applied PER LEG so one source cannot
      crowd the other out of the results grid.

    Raises ``ValueError`` when no filter is supplied, so the UI can never
    trigger a full scan by accident. No dedup across sources: the same article
    may appear once per source.
    """
    hits, _notes = search_products_unified(
        session,
        proveedor=proveedor,
        marca=marca,
        categoria=categoria,
        codigo=codigo,
        texto=texto,
        limit=limit,
    )
    return hits


def search_products_unified(
    session: Session,
    *,
    proveedor: str | None = None,
    marca: str | None = None,
    categoria: str | None = None,
    subcategoria: str | None = None,
    codigo: str | None = None,
    nombre: str | None = None,
    texto: str | None = None,
    scope: str = "both",
    limit: int = 100,
    rag_client: RagVectorClient | None = None,
) -> tuple[list[ProductSearchHit], list[str]]:
    """Unified search for the Productos tab: LOCAL + PROV legs, scoped.

    Same filter semantics as ``search_order_products`` plus:

    - ``subcategoria``: independent folded substring on the ``subcategoria``
      column of each leg (``categoria`` keeps its OR semantics).
    - ``nombre``: folded substring on ``nombre_oficial`` (LOCAL); for PROV it
      resolves through the injected ``rag_client`` vector query (field filters
      post-filter the vector hits in Python). Without a client — or when the
      vector call fails — the PROV leg degrades to the SQL rag-table path with
      ``nombre`` as a folded substring over ``text_content`` and the fallback
      is reported in the returned notes.
    - ``scope``: ``"local"`` | ``"prov"`` | ``"both"`` (default), case- and
      space-insensitive; anything else raises ``ValueError``.

    Returns ``(hits, notes)``: LOCAL hits first (when its leg runs), RAG hits
    after; ``notes`` carries degradation messages for the UI status box. The
    per-leg ``limit`` and the no-filter guard behave exactly as in
    ``search_order_products``.
    """
    filters = {
        "proveedor": (proveedor or "").strip(),
        "marca": (marca or "").strip(),
        "categoria": (categoria or "").strip(),
        "subcategoria": (subcategoria or "").strip(),
        "codigo": (codigo or "").strip(),
        "nombre": (nombre or "").strip(),
        "texto": (texto or "").strip(),
    }
    if not any(filters.values()):
        raise ValueError(
            "Especificá al menos un filtro (proveedor, marca, categoría, "
            "subcategoría, código o nombre)."
        )

    normalized_scope = _normalize_scope(scope)
    effective_limit = max(1, min(int(limit), _SEARCH_MAX_LIMIT))
    hits: list[ProductSearchHit] = []
    notes: list[str] = []
    if normalized_scope in ("both", "local"):
        hits.extend(_search_local(session, filters, effective_limit))
    if normalized_scope in ("both", "prov"):
        rag_hits, rag_notes = _search_rag(session, filters, effective_limit, rag_client)
        hits.extend(rag_hits)
        notes.extend(rag_notes)
    return hits, notes


def _normalize_scope(scope: str | None) -> str:
    """Normalize the scope selector; anything but the three values is refused."""
    value = (scope or "both").strip().lower()
    if value not in ("local", "prov", "both"):
        raise ValueError("Ámbito inválido: usá 'Local', 'Prov' o 'Ambas'.")
    return value


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
    if filters["subcategoria"]:
        pattern = f"%{_fold_accents(filters['subcategoria'])}%"
        conditions.append(_folded(Catalogo.subcategoria).ilike(pattern))
    if filters["codigo"]:
        pattern = f"%{_fold_accents(filters['codigo'])}%"
        # A product also matches when one of its supplier's codes (the
        # supplier_sku_mappings table) matches the typed code: codigo_interno
        # is opaque, so the owner searches by the code on the price list.
        # The mapping EXISTS is correlated to the row's own supplier only when
        # a proveedor filter is active; otherwise any supplier's mapping counts.
        mapping_match = and_(
            SupplierSkuMapping.internal_sku == Catalogo.codigo_interno,
            _folded(SupplierSkuMapping.supplier_sku_code).ilike(pattern),
        )
        if filters["proveedor"]:
            mapping_match = and_(mapping_match, SupplierSkuMapping.supplier_id == Supplier.id)
        conditions.append(
            or_(
                _folded(Catalogo.codigo_interno).ilike(pattern),
                _folded(Catalogo.codigo_barras).ilike(pattern),
                select(SupplierSkuMapping.id).where(mapping_match).exists(),
            )
        )
    if filters["texto"]:
        pattern = f"%{_fold_accents(filters['texto'])}%"
        conditions.append(_folded(Catalogo.nombre_oficial).ilike(pattern))
    if filters["nombre"]:
        # Same folded substring semantics as ``texto``: the unified grid's
        # nombre filter narrows the LOCAL leg by official name.
        pattern = f"%{_fold_accents(filters['nombre'])}%"
        conditions.append(_folded(Catalogo.nombre_oficial).ilike(pattern))

    usd_rate = session.scalar(
        select(ExchangeRate.rate_to_ars).where(ExchangeRate.currency == "USD")
    )
    stock = func.coalesce(Inventory.quantity_on_hand, 0)
    stmt = (
        select(Catalogo, stock)
        .join(Catalogo.supplier)  # every catalog row has a supplier (non-null FK)
        .join(Inventory, Inventory.sku_id == Catalogo.codigo_interno, isouter=True)
        .where(and_(*conditions))
        .order_by(Catalogo.codigo_interno)
        .limit(limit)
    )
    local_rows = list(session.execute(stmt))
    # One extra query for every returned SKU (no N+1): the displayed code is
    # the product's primary mapped supplier code, falling back to the opaque
    # internal SKU when the product has no mapping (never silently blanked).
    primary_codes = primary_supplier_codes(session, [p.codigo_interno for p, _ in local_rows])
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
            display_code=primary_codes.get(product.codigo_interno, product.codigo_interno),
            subcategoria=product.subcategoria,
            costo=product.costo_proveedor,
            margen_pct=product.margen_aplicado_pct,
            precio_lista_ars=_list_price_ars(product, usd_rate),
            moneda_original=product.moneda,
        )
        for product, row_stock in local_rows
    ]


def _search_rag(
    session: Session,
    filters: dict[str, str],
    limit: int,
    rag_client: RagVectorClient | None,
) -> tuple[list[ProductSearchHit], list[str]]:
    """RAG leg: vector query for ``nombre`` when available, SQL filters otherwise.

    ``nombre`` is the only filter the indexed table cannot answer well (the
    product name lives inside free text), so it drives the vector search; the
    remaining field filters post-filter the vector hits in Python with the
    same folded-substring semantics as the SQL path. An unavailable vector
    service degrades to the SQL path and reports a note — never a crash.
    """
    if filters["nombre"] and rag_client is not None:
        try:
            return _search_rag_vector(rag_client, filters, limit), []
        except RagProductError as exc:
            note = (
                f"Búsqueda por similitud no disponible ({exc}); "
                "se usó el catálogo indexado (SQL)."
            )
            return _search_rag_sql(session, filters, limit), [note]
    return _search_rag_sql(session, filters, limit), []


def _search_rag_vector(
    client: RagVectorClient, filters: dict[str, str], limit: int
) -> list[ProductSearchHit]:
    """Vector leg: ``nombre`` as the similarity query, field filters in Python."""
    hits: list[ProductSearchHit] = []
    for product in client.query(filters["nombre"]):
        if not _vector_matches(product, filters):
            continue
        hits.append(
            ProductSearchHit(
                sku=product.sku,
                name=product.name,
                source="RAG",
                stock=None,
                marca=product.brand,
                categoria=product.categoria,
                supplier=product.codigo_proveedor,
                price=Decimal(str(product.price)) if product.price is not None else None,
                moneda=product.currency,
                display_code=product.sku,
                subcategoria=product.subcategoria,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _vector_matches(product: RagProduct, filters: dict[str, str]) -> bool:
    """Post-filter one vector hit with the SQL-leg filter semantics."""
    proveedor_code = (product.codigo_proveedor or "").upper()
    return not (
        (
            filters["proveedor"]
            and proveedor_code != filters["proveedor"].upper()
        )
        or (filters["marca"] and not _folded_contains(product.brand, filters["marca"]))
        or (
            filters["categoria"]
            and not (
                _folded_contains(product.categoria, filters["categoria"])
                or _folded_contains(product.categoria_padre, filters["categoria"])
            )
        )
        or (
            filters["subcategoria"]
            and not _folded_contains(product.subcategoria, filters["subcategoria"])
        )
        or (filters["codigo"] and not _folded_contains(product.sku, filters["codigo"]))
    )


def _search_rag_sql(
    session: Session, filters: dict[str, str], limit: int
) -> list[ProductSearchHit]:
    """RAG leg over the indexed table: same filters/conventions as before."""
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
    if filters["subcategoria"]:
        pattern = f"%{_fold_accents(filters['subcategoria'])}%"
        conditions.append(_folded(table.c.subcategoria).ilike(pattern))
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
    if filters["nombre"]:
        # Degrade path for the vector query: the name lives inside free text.
        conditions.append(
            _folded(table.c.text_content).ilike(f"%{_fold_accents(filters['nombre'])}%")
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
            display_code=row.codigo_producto,
            subcategoria=row.subcategoria,
        )
        for row in session.execute(stmt)
    ]
