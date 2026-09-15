"""Attribute-based product search + source-aware manual order lines.

The manual order form now finds products in BOTH sources (local inventory
catalog and the RAG supplier-catalog table) and adds source-tagged lines to a
DRAFT order — creating it or modifying an existing one. The search use case
(``src/sourcing/product_search.py``) is read-only SQL with LOCAL hits first and
NO dedup across sources; the draft mutation (``update_manual_order``) prices
every new line via ``compute_order`` (never leaves a 0) and recomputes totals.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from src.backoffice.app import (
    _load_manual_draft,
    _manual_order_add_line,
    _manual_order_add_selected,
    _manual_product_search,
    _save_manual_order_changes,
)
from src.backoffice.customer_orders import (
    create_manual_order_action,
    update_manual_order_action,
)
from src.config import get_settings
from src.db.models import (
    AppSetting,
    Catalogo,
    Cliente,
    ExchangeRate,
    Inventory,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    Supplier,
)
from src.db.session import SessionLocal
from src.integrations.rag import RagProduct, RagProductError
from src.sourcing.draft_order import (
    ManualLineInput,
    ManualOrderError,
    create_manual_order,
    update_manual_order,
)
from src.sourcing.product_search import (
    rag_products_table,
    search_order_products,
    search_products_unified,
)


def _postgres_up() -> bool:
    try:
        engine = create_engine(
            get_settings().sqlalchemy_database_url, connect_args={"connect_timeout": 2}
        )
        with engine.connect():
            pass
        engine.dispose()
        return True
    except (OperationalError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _postgres_up(), reason="Postgres not running (make db-up)")


@pytest.fixture(autouse=True)
def _clean_schema(clean_schema):
    yield


_CREATE_RAG_TABLE = """
CREATE TABLE IF NOT EXISTS catalogo_productos_rag (
    node_id varchar PRIMARY KEY,
    codigo_producto varchar,
    codigo_orig varchar,
    nombre_proveedor varchar,
    codigo_proveedor varchar,
    marca varchar,
    categoria_padre varchar,
    categoria varchar,
    subcategoria varchar,
    precio numeric,
    moneda varchar,
    pagina_origen int,
    archivo_origen varchar,
    documento_id varchar,
    es_tabla boolean,
    text_content text
)
"""


@pytest.fixture()
def rag_table(db_engine):
    """Create the minimal RAG table on the test database and drop it after."""
    with db_engine.begin() as conn:
        conn.execute(text(_CREATE_RAG_TABLE))
    yield
    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS catalogo_productos_rag"))


@pytest.fixture
def shop_ctx(db_session):
    """Seed price lists, customers, two suppliers, catalog products, inventory."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Cliente(
            customer_id=1,
            nombre_comercial="Ferretería Don Juan",
            telefono_norm="+5491155551234",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.add(
        Supplier(id=1, code="SUP", business_name="Supplier", default_margin_pct=Decimal(0))
    )
    db_session.add(
        Supplier(id=2, code="AMX", business_name="Tornimax", default_margin_pct=Decimal(0))
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="CLV-001",
            supplier_id=1,
            nombre_oficial="Grifería monocomando de cocina",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            sinonimos=[],
            marca="FV",
            categoria="Griferías",
            subcategoria="Cocina",
        )
    )
    db_session.add(
        Catalogo(
            id=2,
            codigo_interno="AT-5044",
            supplier_id=2,
            nombre_oficial="Tarugo de nylon 8mm",
            costo_proveedor=Decimal("50.00"),
            margen_aplicado_pct=Decimal("0.20"),
            precio_lista_base=Decimal("60.00"),
            sinonimos=[],
            marca="Fischer",
            categoria="Fijaciones",
            subcategoria="Tarugos",
        )
    )
    db_session.add(
        Catalogo(
            id=3,
            codigo_interno="LLV-9",
            supplier_id=1,
            nombre_oficial="Llave paso 1/2",
            costo_proveedor=Decimal("10.00"),
            margen_aplicado_pct=Decimal("0.50"),
            precio_lista_base=Decimal("15.00"),
            sinonimos=[],
        )
    )
    db_session.add(Inventory(sku_id="CLV-001", quantity_on_hand=3))
    db_session.add(Inventory(sku_id="AT-5044", quantity_on_hand=3))
    db_session.add(Inventory(sku_id="LLV-9", quantity_on_hand=7))
    db_session.flush()
    return db_session


def _seed_rag(session) -> None:
    """Seed indexed RAG rows, including one shared article with the LOCAL leg."""
    rows = [
        # shared article: same codigo in inventory AND RAG (dual result)
        ("node-1", "AT-5044", "AT-5044", "Tornimax", "AMX", "Fischer", "Fijaciones",
         "Tarugos", "Plástico", 80.50, "ARS", 12, "lista-amx.pdf", "tarugo de nylon 8mm"),
        # USD row: exercises the exchange-rate guard on the manual pricing path
        ("node-2", "SM 483-8", "SM 483-8", "Sanitarios del Centro", "SCO", "GENERICA",
         "Sanitarios y Grifería", "Griferías", "Monocomandos", 15.0, "USD", 58,
         "lista-sco.pdf", "monocomando de cocina acero"),
        # doubled provider prefix: the stored SKU must collapse it
        ("node-3", "AMX-AMX-AT-9999", "AT-9999", "Tornimax", "AMX", "Fischer",
         "Fijaciones", "Tarugos", "Plástico", 10.0, "ARS", 5, "lista-amx.pdf",
         "tarugo 10mm"),
        # extra AMX row: proves the per-leg limit cap
        ("node-4", "AMX-AT-7777", "AT-7777", "Tornimax", "AMX", "Fischer",
         "Fijaciones", "Tarugos", "Plástico", 9.0, "ARS", 6, "lista-amx.pdf",
         "tarugo 7mm"),
    ]
    columns = (
        "node_id", "codigo_producto", "codigo_orig", "nombre_proveedor", "codigo_proveedor",
        "marca", "categoria_padre", "categoria", "subcategoria", "precio", "moneda",
        "pagina_origen", "archivo_origen", "text_content",
    )
    table = rag_products_table(get_settings().rag_table_name)
    session.execute(table.insert().values([dict(zip(columns, row, strict=True)) for row in rows]))
    session.flush()


def _seed_rag_named_row(session) -> None:
    """Seed one RAG row whose ``text_content`` carries the literal name line.

    Mirrors the real chunker output: the ``nombre:`` line inside
    ``text_content`` holds the source-file name the owner expects to see.
    """
    table = rag_products_table(get_settings().rag_table_name)
    session.execute(
        table.insert().values(
            node_id="node-mech-1",
            codigo_producto="FDN-MECH-1",
            codigo_orig="FDN-MECH-1",
            nombre_proveedor="Ferretera del Norte",
            codigo_proveedor="FDN",
            marca="Genérico",
            categoria_padre="Herramientas",
            categoria="Accesorios para herramientas",
            subcategoria="Mechas de widia para mampostería",
            precio=900.0,
            moneda="ARS",
            pagina_origen=4,
            archivo_origen="lista-fdn.pdf",
            text_content=(
                "proveedor: Ferretera del Norte S.R.L.\n"
                "nombre: MECHA DE WIDEA 10 *130 x 10 unid\n"
                "descripcion: Mecha de widia 10 x 130 mm, paquete x 10 unidades\n"
            ),
        )
    )
    session.flush()


# --------------------------------------------------------- search use case


def test_search_orders_local_first_and_tags_source(rag_table, shop_ctx):
    """LOCAL hits come first with stock; RAG hits follow without stock."""
    _seed_rag(shop_ctx)
    hits = search_order_products(shop_ctx, marca="fischer")

    # RAG rows are ordered by codigo_producto ("AMX..." sorts before "AT...").
    assert [(h.source, h.sku) for h in hits] == [
        ("LOCAL", "AT-5044"),
        ("RAG", "AMX-AMX-AT-9999"),
        ("RAG", "AMX-AT-7777"),
        ("RAG", "AT-5044"),
    ]
    local = hits[0]
    assert local.stock == 3
    assert local.price == Decimal("60.00")
    assert local.moneda == "ARS"
    assert local.supplier == "AMX"
    assert all(h.stock is None for h in hits[1:])


def test_search_returns_same_article_in_both_sources_without_dedup(rag_table, shop_ctx):
    """The shared article appears once per source so the owner can pick either."""
    _seed_rag(shop_ctx)
    hits = search_order_products(shop_ctx, codigo="AT-5044")

    assert [(h.source, h.sku, h.stock) for h in hits] == [
        ("LOCAL", "AT-5044", 3),
        ("RAG", "AT-5044", None),
    ]


def test_search_filters_by_proveedor_exact_and_normalized(rag_table, shop_ctx):
    """The proveedor filter is exact and case/space-insensitive on both legs."""
    _seed_rag(shop_ctx)
    hits = search_order_products(shop_ctx, proveedor=" amx ")

    # LOCAL AT-5044 (supplier AMX) + all three RAG AMX rows, sorted per leg.
    assert [h.sku for h in hits] == [
        "AT-5044",
        "AMX-AMX-AT-9999",
        "AMX-AT-7777",
        "AT-5044",
    ]
    hits_sup = search_order_products(shop_ctx, proveedor="SUP")
    assert [h.sku for h in hits_sup] == ["CLV-001", "LLV-9"]


def test_search_filters_by_texto_per_source(rag_table, shop_ctx):
    """Texto matches nombre_oficial (LOCAL) and text_content (RAG)."""
    _seed_rag(shop_ctx)
    hits = search_order_products(shop_ctx, texto="monocomando")

    assert [(h.source, h.sku) for h in hits] == [("LOCAL", "CLV-001"), ("RAG", "SM 483-8")]


def test_search_limit_applies_per_leg(rag_table, shop_ctx):
    """Each leg is capped separately so one source cannot crowd out the other."""
    _seed_rag(shop_ctx)
    hits = search_order_products(shop_ctx, proveedor="AMX", limit=2)

    # LOCAL AMX has 1 row; RAG AMX has 3 rows but the leg is capped at 2.
    assert [(h.source, h.sku) for h in hits] == [
        ("LOCAL", "AT-5044"),
        ("RAG", "AMX-AMX-AT-9999"),
        ("RAG", "AMX-AT-7777"),
    ]


def test_search_requires_at_least_one_filter(shop_ctx):
    """No filters is a guard error, never a full-table scan."""
    with pytest.raises(ValueError, match="al menos un filtro"):
        search_order_products(shop_ctx)


def test_search_with_no_matches_returns_empty_list(rag_table, shop_ctx):
    """Empty results come back as an empty list for the UI to handle."""
    _seed_rag(shop_ctx)
    assert search_order_products(shop_ctx, codigo="INEXISTENTE-99") == []


def test_search_rag_hit_shows_literal_name_from_text_content(rag_table, shop_ctx):
    """RAG display name is the literal catalog name from the nombre: line."""
    _seed_rag(shop_ctx)
    _seed_rag_named_row(shop_ctx)

    hits = search_order_products(shop_ctx, codigo="FDN-MECH-1")
    assert [h.name for h in hits] == ["MECHA DE WIDEA 10 *130 x 10 unid"]

    # Rows without a ``nombre:`` line keep the composed metadata fallback.
    fallback = [
        h
        for h in search_order_products(shop_ctx, marca="fischer")
        if h.source == "RAG" and h.sku == "AT-5044"
    ]
    assert [h.name for h in fallback] == ["Fischer Tarugos Plástico"]


def test_manual_order_rag_line_uses_literal_name_from_text_content(rag_table, shop_ctx):
    """A manual RAG line snapshots the literal name, not the metadata."""
    _seed_rag(shop_ctx)
    _seed_rag_named_row(shop_ctx)
    order = create_manual_order(
        shop_ctx, 1, [ManualLineInput(sku="FDN-MECH-1", cantidad=1, source="RAG")]
    )

    item = shop_ctx.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.name == "MECHA DE WIDEA 10 *130 x 10 unid"


# ------------------------------------------------- unified search (Productos)


class _FakeVectorRag:
    """RagProductClient stand-in with canned vector products (records queries)."""

    def __init__(self, products: tuple[RagProduct, ...]) -> None:
        self.products = tuple(products)
        self.calls: list[str] = []

    def query(self, text: str) -> tuple[RagProduct, ...]:
        self.calls.append(text)
        return self.products


class _FailingVectorRag:
    """RagProductClient stand-in whose vector query is unavailable."""

    def query(self, text: str) -> tuple[RagProduct, ...]:
        raise RagProductError("rag query failed for 'x': down")


def _rag_product(**overrides) -> RagProduct:
    """Canned RAG hit shape mirroring a real rag-api structured response."""
    base = {
        "sku": "SM 483-8",
        "name": "Monocomando de cocina acero",
        "codigo_proveedor": "SCO",
        "brand": "GENERICA",
        "price": 15.0,
        "currency": "USD",
        "categoria_padre": "Sanitarios y Grifería",
        "categoria": "Griferías",
        "subcategoria": "Monocomandos",
    }
    base.update(overrides)
    return RagProduct(**base)


def test_unified_search_filters_by_subcategoria_independently(rag_table, shop_ctx):
    """Subcategoría is its own folded-substring filter (not the categoria OR)."""
    _seed_rag(shop_ctx)

    # LOCAL AT-5044 (subcategoria "Tarugos"); RAG rows have subcategoria
    # "Plástico" even though their categoria column is "Tarugos" — the
    # subcategoria filter must NOT reuse the categoria OR semantics.
    hits, _notes = search_products_unified(shop_ctx, subcategoria="tarugos")
    assert [(h.source, h.sku) for h in hits] == [("LOCAL", "AT-5044")]

    hits, _notes = search_products_unified(shop_ctx, subcategoria="plastico")
    assert [h.source for h in hits] == ["RAG"] * 3
    assert all(h.subcategoria == "Plástico" for h in hits)


def test_unified_search_scope_local_runs_only_local_leg(rag_table, shop_ctx):
    """Scope LOCAL never touches the RAG table: only LOCAL hits come back."""
    _seed_rag(shop_ctx)
    hits, notes = search_products_unified(shop_ctx, marca="fischer", scope="local")
    assert [(h.source, h.sku) for h in hits] == [("LOCAL", "AT-5044")]
    assert notes == []


def test_unified_search_scope_prov_runs_only_rag_leg(rag_table, shop_ctx):
    """Scope PROV never touches the LOCAL catalog; hits keep source 'RAG'."""
    _seed_rag(shop_ctx)
    hits, notes = search_products_unified(shop_ctx, proveedor="amx", scope="prov")
    assert [(h.source, h.sku) for h in hits] == [
        ("RAG", "AMX-AMX-AT-9999"),
        ("RAG", "AMX-AT-7777"),
        ("RAG", "AT-5044"),
    ]
    assert notes == []


def test_unified_search_rejects_unknown_scope(shop_ctx):
    """Anything but local/prov/both is refused before any query runs."""
    with pytest.raises(ValueError, match="Ámbito inválido"):
        search_products_unified(shop_ctx, marca="x", scope="todo")


def test_unified_search_guard_counts_nombre_and_subcategoria(shop_ctx):
    """Zero filters is a guard error; nombre alone is a valid LOCAL filter."""
    with pytest.raises(ValueError, match="al menos un filtro"):
        search_products_unified(shop_ctx)

    hits, notes = search_products_unified(shop_ctx, nombre="monocomando", scope="local")
    assert [(h.source, h.sku) for h in hits] == [("LOCAL", "CLV-001")]
    assert notes == []


def test_unified_search_prov_nombre_uses_vector_client(rag_table, shop_ctx):
    """With nombre set the PROV leg queries the vector service, not the table."""
    _seed_rag(shop_ctx)
    fake = _FakeVectorRag(
        (
            _rag_product(),
            _rag_product(
                sku="AT-9999",
                name="Tarugo plástico 8mm",
                codigo_proveedor="AMX",
                brand="Fischer",
                price=9.0,
                currency="ARS",
                categoria_padre="Fijaciones",
                categoria="Tarugos",
                subcategoria="Plástico",
            ),
        )
    )
    hits, notes = search_products_unified(
        shop_ctx, nombre="monocomando", scope="prov", rag_client=fake
    )

    assert fake.calls == ["monocomando"]
    assert [(h.source, h.sku, h.price, h.moneda) for h in hits] == [
        ("RAG", "SM 483-8", Decimal("15.0"), "USD"),
        ("RAG", "AT-9999", Decimal("9.0"), "ARS"),
    ]
    assert all(h.stock is None for h in hits)
    assert notes == []


def test_unified_search_vector_hits_postfiltered_by_fields(rag_table, shop_ctx):
    """Field filters set together with nombre post-filter the vector results."""
    _seed_rag(shop_ctx)
    fake = _FakeVectorRag((_rag_product(), _rag_product(sku="AT-9999", brand="Fischer")))

    hits, _notes = search_products_unified(
        shop_ctx, nombre="monocomando", marca="GENERICA", scope="prov", rag_client=fake
    )
    assert [h.sku for h in hits] == ["SM 483-8"]

    hits, _notes = search_products_unified(
        shop_ctx, nombre="monocomando", subcategoria="plasticos", scope="prov", rag_client=fake
    )
    assert [h.sku for h in hits] == []  # fake hits carry subcategoria "Monocomandos"

    hits, _notes = search_products_unified(
        shop_ctx, nombre="monocomando", codigo="483", scope="prov", rag_client=fake
    )
    assert [h.sku for h in hits] == ["SM 483-8"]


def test_unified_search_nombre_local_sql_and_prov_vector(rag_table, shop_ctx):
    """Ambas scope with nombre: LOCAL SQL substring first, vector hits after."""
    _seed_rag(shop_ctx)
    fake = _FakeVectorRag((_rag_product(),))
    hits, _notes = search_products_unified(
        shop_ctx, nombre="monocomando", scope="both", rag_client=fake
    )
    assert [(h.source, h.sku) for h in hits] == [("LOCAL", "CLV-001"), ("RAG", "SM 483-8")]


def test_unified_search_vector_failure_falls_back_to_sql_with_note(rag_table, shop_ctx):
    """A down vector service degrades to the SQL table and reports a note."""
    _seed_rag(shop_ctx)
    hits, notes = search_products_unified(
        shop_ctx, nombre="monocomando", scope="prov", rag_client=_FailingVectorRag()
    )
    assert [(h.source, h.sku) for h in hits] == [("RAG", "SM 483-8")]
    assert len(notes) == 1
    assert "no disponible" in notes[0]


def test_unified_search_local_hit_carries_pricing_snapshot(shop_ctx):
    """LOCAL hits expose cost, margin and the display AR$ list price."""
    hits, _notes = search_products_unified(shop_ctx, codigo="CLV-001", scope="local")
    hit = hits[0]
    assert hit.costo == Decimal("100.00")
    assert hit.margen_pct == Decimal("0.35")
    assert hit.precio_lista_ars == Decimal("135.00")  # 100.00 × 1.35
    assert hit.subcategoria == "Cocina"


def test_unified_search_local_usd_hit_converts_list_price(shop_ctx):
    """A USD catalog product converts its list price like the catalog grid."""
    shop_ctx.add(ExchangeRate(currency="USD", rate_to_ars=Decimal("1000.0000")))
    shop_ctx.add(
        Catalogo(
            id=4,
            codigo_interno="USD-1",
            supplier_id=1,
            nombre_oficial="Producto importado en USD",
            costo_proveedor=Decimal("2.00"),
            margen_aplicado_pct=Decimal("0.50"),
            precio_lista_base=Decimal("3.00"),
            sinonimos=[],
            moneda="USD",
        )
    )
    shop_ctx.flush()

    hits, _notes = search_products_unified(shop_ctx, codigo="USD-1", scope="local")
    hit = hits[0]
    assert hit.precio_lista_ars == Decimal("3000.00")  # (2.00 × 1.50) × 1000
    assert hit.moneda_original == "USD"
    assert hit.moneda == "ARS"  # Pedidos contract keeps its fixed LOCAL label


# ------------------------------------------- RAG display pricing (unified grid)


def test_unified_search_rag_hit_carries_supplier_margin_and_list_price(rag_table, shop_ctx):
    """RAG hits show the adoption margin and the AR$ list price of the offer."""
    _seed_rag(shop_ctx)
    shop_ctx.get(Supplier, 2).default_margin_pct = Decimal("0.30")
    shop_ctx.flush()
    hits, _notes = search_products_unified(shop_ctx, proveedor="amx", scope="prov")

    hit = next(h for h in hits if h.sku == "AT-5044")
    assert hit.margen_pct == Decimal("0.30")
    assert hit.precio_lista_ars == Decimal("104.65")  # 80.50 × 1.30


def test_unified_search_rag_usd_hit_converts_list_price(rag_table, shop_ctx):
    """A USD RAG offer converts cost × margin × rate at display time."""
    _seed_rag(shop_ctx)
    shop_ctx.add(
        Supplier(
            id=3, code="SCO", business_name="Sanitarios del Centro",
            default_margin_pct=Decimal("0.25"),
        )
    )
    shop_ctx.add(ExchangeRate(currency="USD", rate_to_ars=Decimal("1000.0000")))
    shop_ctx.flush()
    hits, _notes = search_products_unified(shop_ctx, codigo="SM 483-8", scope="prov")

    hit = hits[0]
    assert hit.margen_pct == Decimal("0.25")
    assert hit.precio_lista_ars == Decimal("18750.00")  # (15.00 × 1.25) × 1000


def test_unified_search_rag_unregistered_supplier_falls_back_to_global_margin(
    rag_table, shop_ctx
):
    """A RAG hit whose supplier is not registered uses the global default 20%."""
    _seed_rag_named_row(shop_ctx)  # codigo_proveedor FDN has no Supplier row
    hits, _notes = search_products_unified(shop_ctx, codigo="FDN-MECH-1", scope="prov")

    hit = hits[0]
    assert hit.margen_pct == Decimal(20)  # percentage points (points > 1 → /100)
    assert hit.precio_lista_ars == Decimal("1080.00")  # 900 × 1.20


def test_unified_search_rag_fallback_reads_default_margin_setting(rag_table, shop_ctx):
    """The configured global default margin overrides the built-in 20% fallback."""
    shop_ctx.add(AppSetting(key="default_margin_pct", value="50.00"))
    _seed_rag_named_row(shop_ctx)
    shop_ctx.flush()
    hits, _notes = search_products_unified(shop_ctx, codigo="FDN-MECH-1", scope="prov")

    hit = hits[0]
    assert hit.margen_pct == Decimal("50.00")
    assert hit.precio_lista_ars == Decimal("1350.00")  # 900 × 1.50


def test_unified_search_vector_hit_carries_display_pricing(rag_table, shop_ctx):
    """Vector RAG hits carry the same margin/list-price snapshot as SQL hits."""
    shop_ctx.add(
        Supplier(
            id=3, code="SCO", business_name="Sanitarios del Centro",
            default_margin_pct=Decimal("0.25"),
        )
    )
    shop_ctx.add(ExchangeRate(currency="USD", rate_to_ars=Decimal("1000.0000")))
    shop_ctx.flush()
    fake = _FakeVectorRag((_rag_product(),))
    hits, _notes = search_products_unified(
        shop_ctx, nombre="monocomando", scope="prov", rag_client=fake
    )

    assert hits[0].margen_pct == Decimal("0.25")
    assert hits[0].precio_lista_ars == Decimal("18750.00")  # (15.00 × 1.25) × 1000


def test_unified_search_rag_price_none_leaves_pricing_columns_empty(rag_table, shop_ctx):
    """A RAG row without an offer price keeps the pricing columns empty."""
    table = rag_products_table(get_settings().rag_table_name)
    shop_ctx.execute(
        table.insert().values(
            node_id="node-free-1",
            codigo_producto="FREE-1",
            codigo_orig="FREE-1",
            nombre_proveedor="Ferretera del Norte",
            codigo_proveedor="AMX",
            precio=None,
            moneda=None,
            text_content="nombre: Producto sin precio",
        )
    )
    shop_ctx.flush()
    hits, _notes = search_products_unified(shop_ctx, codigo="FREE-1", scope="prov")

    hit = hits[0]
    assert hit.price is None
    assert hit.precio_lista_ars is None  # grid renders empty, never a fake 0


# ------------------------------------- source-aware manual creation (domain)


def test_create_manual_order_mixed_local_and_rag_lines(rag_table, shop_ctx):
    """A draft can mix LOCAL and RAG lines; every line is priced with snapshots."""
    _seed_rag(shop_ctx)
    order = create_manual_order(
        shop_ctx,
        1,
        [
            ManualLineInput(sku="AT-5044", cantidad=3, source="LOCAL"),
            ManualLineInput(sku="AT-5044", cantidad=5, source="RAG"),
        ],
    )

    items = shop_ctx.scalars(
        select(OrderItem).where(OrderItem.order_id == order.order_id).order_by(OrderItem.source)
    ).all()
    assert [(i.source, i.sku, i.cantidad) for i in items] == [
        ("LOCAL", "AT-5044", 3),
        ("RAG", "AT-5044", 5),
    ]
    local, rag = items
    # LOCAL: catalog cost × margin (60.00); RAG: offer price converted (ARS, ×1).
    assert local.base_price == Decimal("60.00")
    assert local.final_price == Decimal("60.00")
    assert local.precio_original == Decimal("50.0000")
    assert rag.base_price == Decimal("80.50")
    assert rag.final_price == Decimal("80.50")
    assert rag.precio_original == Decimal("80.5000")
    assert rag.supplier == "AMX"
    # Composed fallback: the seeded text_content has no ``nombre:`` line.
    assert rag.name == "Fischer Tarugos Plástico"
    assert order.subtotal == Decimal("582.50")  # 3 × 60 + 5 × 80.50
    assert order.total == Decimal("582.50")
    assert order.conversion_pending is False


def test_create_manual_order_rag_prefix_collapses_in_stored_sku(rag_table, shop_ctx):
    """The RAG line's stored SKU collapses the doubled provider prefix."""
    _seed_rag(shop_ctx)
    order = create_manual_order(
        shop_ctx, 1, [ManualLineInput(sku="AMX-AMX-AT-9999", cantidad=1, source="RAG")]
    )

    item = shop_ctx.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.sku == "AMX-AT-9999"


def test_create_manual_order_rag_usd_without_rate_is_refused(rag_table, shop_ctx):
    """A USD RAG line without an exchange rate is a friendly domain error."""
    _seed_rag(shop_ctx)
    with pytest.raises(ManualOrderError, match="exchange rate"):
        create_manual_order(shop_ctx, 1, [ManualLineInput(sku="SM 483-8", cantidad=1, source="RAG")])
    assert shop_ctx.scalar(select(func.count(Order.order_id))) == 0


def test_create_manual_order_rag_usd_with_rate_prices_conversion(rag_table, shop_ctx):
    """With the rate loaded, the USD RAG line converts to ARS like the chat flow."""
    _seed_rag(shop_ctx)
    shop_ctx.add(ExchangeRate(currency="USD", rate_to_ars=Decimal("1300.0000")))
    shop_ctx.flush()
    order = create_manual_order(
        shop_ctx, 1, [ManualLineInput(sku="SM 483-8", cantidad=2, source="RAG")]
    )

    item = shop_ctx.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.base_price == Decimal("19500.00")  # 15.00 × 1300
    assert item.moneda == "USD"
    assert item.precio_original == Decimal("15.0000")
    assert order.total == Decimal("39000.00")


def test_create_manual_order_local_usd_catalog_prices_with_rate(rag_table, shop_ctx):
    """A LOCAL line from a USD catalog product converts after the markup.

    Cost 2.00 USD × margin 0.50 = 3.00 USD → × rate 1000 = 3000.00 AR$; the
    stored snapshot keeps the product's own currency and the USD cost.
    """
    shop_ctx.add(
        Catalogo(
            id=4,
            codigo_interno="USD-1",
            supplier_id=1,
            nombre_oficial="Producto importado en USD",
            costo_proveedor=Decimal("2.00"),
            margen_aplicado_pct=Decimal("0.50"),
            precio_lista_base=Decimal("3.00"),
            sinonimos=[],
            moneda="USD",
        )
    )
    shop_ctx.add(ExchangeRate(currency="USD", rate_to_ars=Decimal("1000.0000")))
    shop_ctx.flush()
    order = create_manual_order(shop_ctx, 1, [ManualLineInput(sku="USD-1", cantidad=2)])

    item = shop_ctx.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.base_price == Decimal("3000.00")  # (2.00 × 1.50) × 1000
    assert item.final_price == Decimal("3000.00")
    assert item.moneda == "USD"
    assert item.precio_original == Decimal("2.0000")
    assert order.total == Decimal("6000.00")


def test_create_manual_order_local_usd_catalog_without_rate_is_refused(rag_table, shop_ctx):
    """A LOCAL USD catalog line without an exchange rate is refused, no writes."""
    shop_ctx.add(
        Catalogo(
            id=4,
            codigo_interno="USD-1",
            supplier_id=1,
            nombre_oficial="Producto importado en USD",
            costo_proveedor=Decimal("2.00"),
            margen_aplicado_pct=Decimal("0.50"),
            precio_lista_base=Decimal("3.00"),
            sinonimos=[],
            moneda="USD",
        )
    )
    shop_ctx.flush()
    with pytest.raises(ManualOrderError, match="exchange rate"):
        create_manual_order(shop_ctx, 1, [ManualLineInput(sku="USD-1", cantidad=1)])
    assert shop_ctx.scalar(select(func.count(Order.order_id))) == 0


def test_create_manual_order_unknown_rag_product_is_refused(rag_table, shop_ctx):
    """An RAG SKU missing from the indexed table is a domain error, no writes."""
    _seed_rag(shop_ctx)
    with pytest.raises(ManualOrderError, match="unknown RAG product: NOPE"):
        create_manual_order(
            shop_ctx, 1, [ManualLineInput(sku="NOPE", cantidad=1, source="RAG")]
        )
    assert shop_ctx.scalar(select(func.count(Order.order_id))) == 0


def test_create_manual_order_merges_duplicate_same_source_lines(rag_table, shop_ctx):
    """Repeated inputs for the same (sku, source) become one accumulated line."""
    _seed_rag(shop_ctx)
    create_manual_order(
        shop_ctx,
        1,
        [
            ManualLineInput(sku="AT-5044", cantidad=2, source="LOCAL"),
            ManualLineInput(sku="AT-5044", cantidad=1, source="LOCAL"),
        ],
    )

    items = shop_ctx.scalars(select(OrderItem)).all()
    assert [(i.source, i.sku, i.cantidad) for i in items] == [("LOCAL", "AT-5044", 3)]


# ---------------------------------------- manual draft modification (domain)


def test_update_manual_order_adds_priced_rag_line_and_recomputes_totals(rag_table, shop_ctx):
    """Modifying a DRAFT adds the RAG line priced at save-time (never 0)."""
    _seed_rag(shop_ctx)
    order = create_manual_order(shop_ctx, 1, [("AT-5044", 2)])  # LOCAL tuple contract
    order_id = order.order_id

    updated = update_manual_order(
        shop_ctx,
        order,
        [
            ManualLineInput(sku="AT-5044", cantidad=2, source="LOCAL"),
            ManualLineInput(sku="AT-5044", cantidad=5, source="RAG"),
        ],
    )

    items = shop_ctx.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.source)
    ).all()
    assert [(i.source, i.cantidad, i.base_price) for i in items] == [
        ("LOCAL", 2, Decimal("60.00")),
        ("RAG", 5, Decimal("80.50")),
    ]
    assert updated.estado is OrderEstado.DRAFT
    assert updated.total == Decimal("522.50")  # 2 × 60 + 5 × 80.50
    assert updated.conversion_pending is False


def test_update_manual_order_moves_quantity_keeping_frozen_snapshots(rag_table, shop_ctx):
    """Quantity changes keep the stored per-unit snapshots untouched."""
    _seed_rag(shop_ctx)
    order = create_manual_order(shop_ctx, 1, [("AT-5044", 2)])
    stored = shop_ctx.scalar(select(OrderItem)).base_price

    update_manual_order(shop_ctx, order, [ManualLineInput(sku="AT-5044", cantidad=7)])

    item = shop_ctx.scalar(select(OrderItem))
    assert item.cantidad == 7
    assert item.base_price == stored
    assert order.total == Decimal("420.00")  # 7 × 60


def test_update_manual_order_deletes_removed_lines(rag_table, shop_ctx):
    """Lines dropped from the form disappear from the draft; the rest survive."""
    _seed_rag(shop_ctx)
    order = create_manual_order(
        shop_ctx, 1, [("AT-5044", 2), ("CLV-001", 1)]
    )

    update_manual_order(shop_ctx, order, [ManualLineInput(sku="CLV-001", cantidad=1)])

    items = shop_ctx.scalars(select(OrderItem)).all()
    assert [(i.sku, i.cantidad) for i in items] == [("CLV-001", 1)]
    assert order.total == Decimal("135.00")


def test_update_manual_order_rejects_non_draft_order(rag_table, shop_ctx):
    """Only borradores can be modified: a confirmed order is refused."""
    _seed_rag(shop_ctx)
    order = create_manual_order(shop_ctx, 1, [("AT-5044", 2)])
    order.estado = OrderEstado.CONFIRMED
    shop_ctx.flush()

    with pytest.raises(ManualOrderError, match="not a draft"):
        update_manual_order(shop_ctx, order, [ManualLineInput(sku="AT-5044", cantidad=3)])


def test_update_manual_order_rejects_pending_conversion(rag_table, shop_ctx):
    """A pending-conversion draft (no ARS snapshots) refuses manual sync."""
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, conversion_pending=True)
    shop_ctx.add(order)
    shop_ctx.flush()

    with pytest.raises(ManualOrderError, match="pending currency conversion"):
        update_manual_order(shop_ctx, order, [("AT-5044", 2)])


def test_update_manual_order_unknown_rag_line_fails_without_writes(rag_table, shop_ctx):
    """A failed sync leaves the draft's lines and totals exactly as they were."""
    _seed_rag(shop_ctx)
    order = create_manual_order(shop_ctx, 1, [("AT-5044", 2)])
    total_before = order.total

    with pytest.raises(ManualOrderError, match="unknown RAG product"):
        update_manual_order(
            shop_ctx,
            order,
            [
                ManualLineInput(sku="AT-5044", cantidad=2),
                ManualLineInput(sku="NOPE", cantidad=1, source="RAG"),
            ],
        )

    item = shop_ctx.scalar(select(OrderItem))
    assert (item.sku, item.cantidad) == ("AT-5044", 2)
    assert order.total == total_before


# ------------------------------------------ backoffice actions (po.py pattern)


def test_update_manual_order_action_commits(rag_table, shop_ctx):
    """The action wrapper commits: a fresh session sees the priced line."""
    _seed_rag(shop_ctx)
    shop_ctx.commit()
    created = create_manual_order_action(shop_ctx, 1, [("AT-5044", 2)])

    update_manual_order_action(
        shop_ctx,
        created.order_id,
        [
            ManualLineInput(sku="AT-5044", cantidad=2),
            ManualLineInput(sku="AT-5044", cantidad=5, source="RAG"),
        ],
    )

    with SessionLocal() as fresh:
        order = fresh.get(Order, created.order_id)
        assert order.total == Decimal("522.50")
        sources = sorted(item.source for item in order.items)
        assert sources == ["LOCAL", "RAG"]


# ------------------------------------------------------- UI-level handlers


def test_manual_order_add_line_accumulates_per_source():
    """Same (sku, origen) accumulates; same SKU with a different origen appends."""
    rows, _grid, _sku, _qty, status = _manual_order_add_line([], "AT-5044", 3, "LOCAL")
    assert rows == [["AT-5044", 3, "LOCAL"]]
    assert "LOCAL" in status

    rows, _grid, _sku, _qty, status = _manual_order_add_line(rows, "AT-5044", 5, "RAG")
    assert rows == [["AT-5044", 3, "LOCAL"], ["AT-5044", 5, "RAG"]]
    assert "RAG" in status

    rows, _grid, _sku, _qty, _status = _manual_order_add_line(rows, "AT-5044", 1, "LOCAL")
    assert rows[0] == ["AT-5044", 4, "LOCAL"]

    rows, _grid, _sku, _qty, status = _manual_order_add_line(rows, "AT-5044", 0, "LOCAL")
    assert "mayor que cero" in status
    assert len(rows) == 2  # invalid input never mutates


def test_manual_order_add_selected_uses_result_source():
    """The selected search row drives the source; bad selections are no-ops."""
    hits = [
        {"source": "LOCAL", "sku": "AT-5044", "stock": 3},
        {"source": "RAG", "sku": "AT-5044", "stock": None},
    ]
    rows, _grid, _qty, _status = _manual_order_add_selected(None, hits, [], 2)
    assert rows == []

    rows, _grid, _qty, status = _manual_order_add_selected(0, hits, [], 3)
    assert rows == [["AT-5044", 3, "LOCAL"]]
    assert "LOCAL" in status

    rows, _grid, _qty, _status = _manual_order_add_selected(1, hits, rows, 5)
    assert rows == [["AT-5044", 3, "LOCAL"], ["AT-5044", 5, "RAG"]]

    rows, _grid, _qty, _status = _manual_order_add_selected(99, hits, rows, 1)
    assert len(rows) == 2  # out-of-range selection never mutates


def test_manual_product_search_requires_filter(shop_ctx, rag_table):
    """The search handler surfaces the guard error in the status box."""
    _seed_rag(shop_ctx)
    shop_ctx.commit()
    grid, hits, status = _manual_product_search("", "", "", "", "", 100)
    assert grid == [] and hits == []
    assert status.startswith("Error:")
    assert "al menos un filtro" in status


def test_manual_product_search_lists_local_first(rag_table, shop_ctx):
    """The results grid shows source, stock and prices, LOCAL rows first."""
    _seed_rag(shop_ctx)
    shop_ctx.commit()
    grid, hits, status = _manual_product_search("AMX", "", "", "", "", 100)

    # LOCAL AT-5044 first, then the three RAG AMX rows.
    assert [row[0] for row in grid] == ["LOCAL", "RAG", "RAG", "RAG"]
    assert grid[0][1] == "AT-5044" and grid[0][8] == 3
    assert hits[0]["source"] == "LOCAL"
    assert "LOCAL primero" in status


def test_load_manual_draft_loads_lines_and_refuses_non_draft(rag_table, shop_ctx):
    """Load fills the form with the draft lines; confirmed orders are refused."""
    _seed_rag(shop_ctx)
    shop_ctx.commit()
    created = create_manual_order_action(
        shop_ctx, 1, [ManualLineInput(sku="AT-5044", cantidad=3, source="LOCAL")]
    )
    shop_ctx.commit()

    lines, _grid, loaded_id, customer_id, status = _load_manual_draft(created.order_id)
    assert lines == [["AT-5044", 3, "LOCAL"]]
    assert loaded_id == created.order_id
    assert customer_id == 1
    assert "cargado" in status

    created.estado = OrderEstado.CONFIRMED
    shop_ctx.commit()
    lines, _grid, loaded_id, _customer, status = _load_manual_draft(created.order_id)
    assert lines == [] and loaded_id is None
    assert "solo borradores" in status


def test_save_manual_order_changes_persists_and_clears_form(rag_table, shop_ctx):
    """The save handler syncs the loaded draft and clears the form on success."""
    _seed_rag(shop_ctx)
    shop_ctx.commit()
    created = create_manual_order_action(shop_ctx, 1, [("AT-5044", 2)])
    shop_ctx.commit()

    status, _orders_grid, lines = _save_manual_order_changes(
        created.order_id,
        [["AT-5044", 2, "LOCAL"], ["AT-5044", 5, "RAG"]],
    )
    assert "actualizado (borrador)" in status
    assert "522.50" in status
    assert lines == []  # form cleared

    with SessionLocal() as fresh:
        order = fresh.get(Order, created.order_id)
        assert order.total == Decimal("522.50")

    status, _orders_grid, lines = _save_manual_order_changes(None, [["AT-5044", 1, "LOCAL"]])
    assert "Cargá un borrador" in status
    assert lines == [["AT-5044", 1, "LOCAL"]]  # form intact when nothing to save
