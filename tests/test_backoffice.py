"""Backoffice tests (tasks 3.5, 3.6, 3.7 and customer orders).

App structure (no server): building the Blocks tree yields seven tabs with the
expected labels and key components. Module logic covers catalog edits, client
registration, customer-order maintenance, the live order monitor, and the
ingestion preview→confirm flow. DB-backed cases run on Postgres and skip when
it is down.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from src.backoffice.adoption import (
    EmbeddingUnavailableError,
    MissingProvenanceError,
    OwnerContext,
)
from src.backoffice.app import (
    _SUPPLIER_PLACEHOLDER,
    _active_supplier_choices,
    _adoption_confirm,
    _adoption_row_selected,
    _adoption_search,
    _catalog_edit,
    _catalog_grid,
    _catalog_ingest,
    _catalog_job_status,
    _ingest_assign,
    _ingest_confirm,
    _ingest_manual_search,
    _ingest_mark_new,
    _ingest_parse,
    _ingest_resolve,
    _ingesta_supplier_choices,
    _load_provider_documents,
    _order_row_selected,
    _pending_row_selected,
    _register_client,
    _resolved_grid,
    _save_exchange_rate,
    _selected_supplier_id,
    build_app,
)
from src.backoffice.catalog import list_products, update_margin, update_price, update_stock
from src.backoffice.clients import (
    InvalidClientDataError,
    create_client,
    list_clients,
    update_client,
)
from src.backoffice.customer_orders import (
    cancel_order_action,
    complete_picking_action,
    deliver_order_action,
    get_default_margin,
    legal_actions,
    list_customer_orders,
    list_exchange_rates,
    order_detail,
    order_state_diagram,
    recompute_pending_conversion,
    set_default_margin,
    set_exchange_rate,
    start_picking_action,
)
from src.backoffice.ingestion import (
    IngestResult,
    PendingReason,
    ReceiptLine,
    ResolvedLine,
    UnresolvedLineError,
    ingest_receipt_lines,
    resolve_lines,
)
from src.backoffice.monitor import list_orders
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
    ReservationEstado,
    SourcingNeed,
    StockAdjustment,
    StockReservation,
    Supplier,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
    SupplierStatus,
)
from src.db.session import SessionLocal
from src.integrations.rag import (
    RagDocumentSummary,
    RagJobStatus,
    RagProduct,
    RagProductError,
    RagProviderDocuments,
)
from src.integrations.sheets import SheetsWriter
from src.orchestrator.approval import PendingConversionError, confirm_and_register
from src.purchasing.accumulate import accumulate_need
from src.sourcing.persistence import upsert_sourcing_need
from src.supplier.guards import SupplierInactiveError

# ---------------------------------------------------------------- app structure


def _tabs_block(demo) -> object:
    """The Tabs layout inside the Blocks tree (ignoring Markdown siblings)."""
    return next(c for c in demo.children if type(c).__name__ == "Tabs")


def _component_labels(block) -> set:
    """Collect every component label in a tab, descending Gradio Form/Row wrappers."""
    labels: set = set()
    stack = list(getattr(block, "children", []) or [])
    while stack:
        child = stack.pop()
        label = getattr(child, "label", None)
        if label:
            labels.add(label)
        stack.extend(getattr(child, "children", []) or [])
    return labels


def test_build_app_creates_tabs_with_expected_labels():
    """Building the app creates tabs with the expected labels."""
    demo = build_app()
    labels = [tab.label for tab in _tabs_block(demo).children]
    assert labels == [
        "Productos",
        "Clientes",
        "Proveedores",
        "Pedidos de clientes",
        "Monitor de pedidos",
        "Órdenes de compra",
        "Ingesta de remitos",
        "Ingesta de catálogo",
        "Adopción desde RAG",
        "Configuración",
        "Sesiones de Telegram",
    ]


def test_build_app_ingestion_tab_has_dropdown_and_no_numeric_id():
    """La pestaña Ingestion expone el dropdown de proveedor y no un ID numérico."""
    demo = build_app()
    ingestion_tab = next(tab for tab in _tabs_block(demo).children if tab.label == "Ingesta de remitos")
    labels = _component_labels(ingestion_tab)
    assert "Proveedor (activo)" in labels
    assert "Supplier ID" not in labels  # no free numeric ID (spec R1)


def test_build_app_catalogo_tab_has_warning_and_flow_components():
    """El tab Catálogo warn del reemplazo total y expone el flujo completo."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Ingesta de catálogo")
    labels = _all_labels(tab)
    assert "Proveedor (activo)" in labels
    assert "Catálogo PDF" in labels
    assert "Ingestar catálogo" in labels
    assert "Consultar estado" in labels
    assert "Estado del job" in labels
    assert "Documento / lista" in labels
    assert "Ingesta incremental (reemplaza solo este documento)" in labels
    # The destructive-replacement warning is the tab's Markdown copy.
    markdown_values = [c.value for c in tab.children if type(c).__name__ == "Markdown"]
    assert any("reemplaza TODAS las filas indexadas" in (value or "") for value in markdown_values)


def test_build_app_catalog_tab_has_product_grid():
    """La pestaña Catalog expone la grilla de productos y el botón de guardado."""
    demo = build_app()
    catalog_tab = next(tab for tab in _tabs_block(demo).children if tab.label == "Productos")
    component_labels = {getattr(c, "label", None) for c in catalog_tab.children}
    assert "Productos" in component_labels


# -------------------------------------------------- ingestion logic (no DB)


class FakeRag:
    """RagProductClient stand-in: canned parse/exact/hybrid responses.

    Records the exact-lookup and hybrid-query calls so tests can assert the
    two-pass resolution order (exact first, hybrid only on miss, scoping).
    """

    def __init__(self, parse_lines=(), exact=(), hybrid=()) -> None:
        self.parse_lines = parse_lines
        self.exact = exact
        self.hybrid = hybrid
        self.parse_codes: list[str] = []  # codigo_proveedor passed to each parse
        self.exact_calls: list[tuple[str, str]] = []
        self.query_calls: list[str] = []

    def parse_document(self, *, filename: str, content: bytes, codigo_proveedor: str):
        return self.parse_lines

    def exact_lookup(self, codigo_orig: str, codigo_proveedor: str):
        self.exact_calls.append((codigo_orig, codigo_proveedor))
        return self.exact

    def query(self, text: str):
        self.query_calls.append(text)
        return self.hybrid


def _product(*, sku: str = "CLV-001", name: str = "Clavos Paris 2 Pulgadas", node_id: str = "node-1"):
    return RagProduct(
        sku=sku,
        name=name,
        codigo_proveedor="MSA",
        price=135.5,
        currency="ARS",
        node_id=node_id,
    )


def _receipt(
    codigo_orig: str | None = "CLV-001",
    descripcion: str = "Clavos Paris 2 Pulgadas",
    cantidad: int = 5,
) -> ReceiptLine:
    return ReceiptLine(
        codigo_orig=codigo_orig,
        descripcion=descripcion,
        cantidad=cantidad,
        costo=Decimal("95.00"),
        pagina=1,
    )


# -------------------------------------------------- DB-backed module logic


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
def _clean_schema(db_engine):
    yield
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE supplier_purchase_order_items, supplier_purchase_orders, "
                "sourcing_needs, order_items, orders, stock_reservations, stock_adjustments, "
                "inventory, catalogo, suppliers, clientes, lista_precios, "
                "supplier_sku_mappings, exchange_rates, app_settings RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def shop_ctx(db_session):
    """Seed supplier, catalog product, price list and a customer."""
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
        Supplier(
            id=1,
            code="MSA",
            business_name="Mayorista SA",
            default_margin_pct=Decimal("0.10"),
        )
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="CLV-001",
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            sinonimos=["clavos"],
        )
    )
    db_session.flush()
    # The fixture inserts explicit ids, which does not advance the sequences;
    # bump them so subsequent auto-id inserts do not collide.
    db_session.execute(text("SELECT setval(pg_get_serial_sequence('catalogo', 'id'), 1, true)"))
    db_session.execute(
        text("SELECT setval(pg_get_serial_sequence('clientes', 'customer_id'), 1, true)")
    )
    return {"session": db_session}


def test_catalog_list_products_returns_expected_fields(shop_ctx):
    """La grilla de catálogo devuelve todos los campos por producto."""
    shop_ctx["session"].add(Inventory(sku_id="CLV-001", quantity_on_hand=10))
    shop_ctx["session"].flush()
    rows = list_products(shop_ctx["session"])
    assert rows[0]["codigo_interno"] == "CLV-001"
    assert rows[0]["on_hand"] == 10
    assert rows[0]["precio_lista_base"] == "135.00"
    # Catalog metadata columns (nullable → rendered as empty string).
    assert rows[0]["marca"] == ""
    assert rows[0]["categoria"] == ""
    assert rows[0]["subcategoria"] == ""
    assert rows[0]["moneda"] == ""
    # Display-time AR$ list price: cost 100 × margin 0.35, ARS rate 1.
    assert rows[0]["precio_lista_ars"] == "135.00"
    # The stored base price column is never mutated by the display computation.
    assert rows[0]["precio_lista_base"] == "135.00"


def test_catalog_list_products_usd_cost_converts_with_supplier_rate(shop_ctx):
    """A USD catalog product multiplies its AR$ list price by the USD rate."""
    from src.db.models import ExchangeRate

    session = shop_ctx["session"]
    product = session.get(Catalogo, 1)
    product.moneda = "USD"
    product.marca = "Fischer"
    product.categoria = "Anclajes"
    product.subcategoria = "Tarugos"
    session.add(ExchangeRate(currency="USD", rate_to_ars=Decimal("1530.0000")))
    session.flush()

    row = list_products(session)[0]
    # base = 100 × 1.35 = 135.00 USD → 135 × 1530 = 206,550.00 AR$
    assert row["precio_lista_ars"] == "206550.00"
    assert row["moneda"] == "USD"
    assert row["marca"] == "Fischer"
    assert row["categoria"] == "Anclajes"
    assert row["subcategoria"] == "Tarugos"
    # The stored column is untouched by the display computation.
    assert product.precio_lista_base == Decimal("135.00")


def test_catalog_list_products_usd_missing_rate_falls_back_to_one(shop_ctx):
    """Sin cotización USD cargada, el precio AR$ cae a la tasa 1 sin crashear."""
    session = shop_ctx["session"]
    product = session.get(Catalogo, 1)
    product.moneda = "USD"
    session.flush()

    row = list_products(session)[0]
    assert row["precio_lista_ars"] == "135.00"
    assert row["moneda"] == "USD"


def test_catalog_update_stock_and_price(shop_ctx):
    """Editar stock y precio se refleja en la grilla."""
    update_stock(shop_ctx["session"], "CLV-001", 25)
    update_price(shop_ctx["session"], "CLV-001", Decimal("150.00"))
    product = shop_ctx["session"].get(Catalogo, 1)
    assert product.precio_lista_base == Decimal("150.00")
    inventory = shop_ctx["session"].scalar(select(Inventory).where(Inventory.sku_id == "CLV-001"))
    assert inventory.quantity_on_hand == 25


def test_catalog_update_margin_recomputes_base_price(shop_ctx):
    """Cambiar el margen recalcula el precio de lista con el motor de precios."""
    update_margin(shop_ctx["session"], "CLV-001", Decimal("0.50"))
    product = shop_ctx["session"].get(Catalogo, 1)
    assert product.margen_aplicado_pct == Decimal("0.50")
    assert product.precio_lista_base == Decimal("150.00")  # 100 × 1.50


@pytest.fixture
def client_ctx(db_session):
    """Price list only — for client registration/edit tests."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.flush()
    return {"session": db_session}


def test_clients_create_normalizes_phone(client_ctx):
    """Registrar un cliente normaliza el teléfono al formato canónico."""
    client = create_client(
        client_ctx["session"],
        nombre_comercial="Ferretería Don Juan",
        telefono_raw="11 5555 1234",
        lista_precios_id=1,
    )
    assert client.telefono_norm == "+5491155551234"
    assert list_clients(client_ctx["session"])[0]["nombre_comercial"] == "Ferretería Don Juan"


def test_clients_create_rejects_invalid_phone(client_ctx):
    """Un teléfono inválido impide registrar el cliente."""
    with pytest.raises(InvalidClientDataError):
        create_client(
            client_ctx["session"],
            nombre_comercial="Pepe",
            telefono_raw="no-es-telefono",
            lista_precios_id=1,
        )


def test_clients_update_changes_discount(client_ctx):
    """Editar un cliente cambia su descuento particular."""
    client = create_client(
        client_ctx["session"],
        nombre_comercial="Don Juan",
        telefono_raw="11 5555 1234",
        lista_precios_id=1,
    )
    update_client(
        client_ctx["session"], client.customer_id, descuento_particular_pct=Decimal("0.05")
    )
    assert client_ctx["session"].get(
        Cliente, client.customer_id
    ).descuento_particular_pct == Decimal("0.05")


def _seed_receipt_product(session, *, codigo_interno="MSA-CLV-001", origen=None) -> Catalogo:
    """Seed a catalog row whose SKU follows the build_sku convention (adoption-style)."""
    product = Catalogo(
        codigo_interno=codigo_interno,
        supplier_id=1,
        nombre_oficial="Clavos Paris 2 Pulgadas",
        costo_proveedor=Decimal("100.00"),
        margen_aplicado_pct=Decimal("0.35"),
        precio_lista_base=Decimal("135.00"),
        sinonimos=["clavos"],
        origen=origen,
    )
    session.add(product)
    session.flush()
    return product


def _embedder(*, fail: bool = False):
    """Fake 1536-dim embedder; ``fail=True`` raises like an unavailable service."""

    class _FakeEmbedder:
        def embed(self, texts):
            if fail:
                raise RuntimeError("embedding service down")
            return [[0.0] * 1536 for _ in texts]

    return _FakeEmbedder()


def test_resolve_lines_exact_hit_resolves_without_hybrid(shop_ctx):
    """[rag-doc R3] Un hit exacto resuelve la línea sin correr búsqueda híbrida."""
    rag = FakeRag(exact=(_product(),))
    resolved = resolve_lines(shop_ctx["session"], rag, [_receipt()], supplier_id=1)
    assert len(resolved) == 1
    assert not resolved[0].pending
    assert resolved[0].product.node_id == "node-1"
    assert rag.query_calls == []  # exact first, hybrid only on miss


def test_resolve_lines_exact_miss_falls_back_to_hybrid_scoped(shop_ctx):
    """[rag-doc R3] Un miss exacto cae al híbrido, scoped al proveedor."""
    rag = FakeRag(exact=(), hybrid=(_product(sku="AT-5044", name="Tarugo", node_id="n-hyb"),))
    resolved = resolve_lines(
        shop_ctx["session"],
        rag,
        [_receipt(codigo_orig="AT-5044", descripcion="Tarugo 8mm")],
        supplier_id=1,
    )
    assert len(resolved) == 1
    assert not resolved[0].pending
    assert resolved[0].product.node_id == "n-hyb"
    assert rag.query_calls == ["AT-5044 Tarugo 8mm"]


def test_resolve_lines_hybrid_ignores_other_supplier_products(shop_ctx):
    """[rag-doc R3] El híbrido se filtra al proveedor: filas de otro proveedor no resuelven."""
    other = _product(sku="X-1", name="Otro proveedor")
    other = RagProduct(
        sku="X-1", name="Otro proveedor", codigo_proveedor="ZZZ", node_id="n-zzz"
    )
    rag = FakeRag(exact=(), hybrid=(other,))
    resolved = resolve_lines(shop_ctx["session"], rag, [_receipt()], supplier_id=1)
    assert resolved[0].pending  # supplier-scoped filter → no candidate


def test_resolve_lines_duplicate_exact_stays_pending(shop_ctx):
    """[rag-doc R3/R5] >1 hit exacto → ambigua: nunca se elige silenciosamente."""
    rag = FakeRag(exact=(_product(node_id="n-1"), _product(node_id="n-2")))
    resolved = resolve_lines(shop_ctx["session"], rag, [_receipt()], supplier_id=1)
    assert resolved[0].pending
    assert resolved[0].pending_reason is PendingReason.AMBIGUOUS
    assert resolved[0].candidates == (_product(node_id="n-1"), _product(node_id="n-2"))
    assert rag.query_calls == []  # ambiguous exact is pending, no hybrid either


def test_resolve_lines_ambiguous_hybrid_caches_candidates(shop_ctx):
    """[manual R1] >1 candidatos híbridos → ambigua con los candidatos cacheados."""
    rag = FakeRag(
        exact=(),
        hybrid=(
            _product(sku="A-1", name="Ducha flexible A", node_id="n-a"),
            _product(sku="A-2", name="Ducha flexible B", node_id="n-b"),
        ),
    )
    resolved = resolve_lines(shop_ctx["session"], rag, [_receipt()], supplier_id=1)
    assert resolved[0].pending
    assert resolved[0].pending_reason is PendingReason.AMBIGUOUS
    assert [p.node_id for p in resolved[0].candidates] == ["n-a", "n-b"]


def test_resolve_lines_no_match_stays_pending(shop_ctx):
    """[manual R2] Sin match exacto ni híbrido → pendiente sin candidatos (ADR 0003)."""
    rag = FakeRag(exact=(), hybrid=())
    resolved = resolve_lines(shop_ctx["session"], rag, [_receipt()], supplier_id=1)
    assert resolved[0].pending
    assert resolved[0].pending_reason is PendingReason.NO_CANDIDATES
    assert resolved[0].candidates == ()


def test_resolve_lines_normalizes_codigo_orig_uppercase_trim(shop_ctx):
    """[rag-doc R3] El código se normaliza UPPER(TRIM) antes del lookup exacto."""
    rag = FakeRag(exact=(_product(),))
    resolve_lines(shop_ctx["session"], rag, [_receipt(codigo_orig="  clv-001  ")], supplier_id=1)
    assert rag.exact_calls == [("CLV-001", "MSA")]


def test_resolve_lines_zero_quantity_does_not_gate(shop_ctx):
    """Las líneas sin cantidad positiva no se resuelven ni bloquean el ingreso."""
    rag = FakeRag(exact=(), hybrid=())
    resolved = resolve_lines(shop_ctx["session"], rag, [_receipt(cantidad=0)], supplier_id=1)
    assert len(resolved) == 1
    assert rag.exact_calls == []  # never queried


def test_ingest_ambiguous_line_blocks_confirmation(shop_ctx):
    """[rag-doc R5] Línea ambigua (>1 hits) sin asignar impide el ingreso (ADR 0003)."""
    lines = [ResolvedLine(receipt=_receipt(), product=None, pending_reason=PendingReason.AMBIGUOUS)]
    with pytest.raises(UnresolvedLineError, match="ambiguous lines require manual assignment"):
        ingest_receipt_lines(shop_ctx["session"], 1, lines, OwnerContext(owner_id="t"), _embedder())
    assert shop_ctx["session"].scalar(select(Inventory)) is None  # nothing written


def test_ingest_updates_existing_stock_keeps_origen_and_audits(shop_ctx):
    """[rag-doc R5] SKU existente: bump + Inventory + StockAdjustment, origen intacto."""
    session = shop_ctx["session"]
    _seed_receipt_product(session, origen={"rag": {"node_id": "node-1"}})
    line = ResolvedLine(receipt=_receipt(cantidad=5), product=_product())
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=1, created=0)
    product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-CLV-001"))
    assert product is not None
    assert product.origen == {"rag": {"node_id": "node-1"}}  # write-once: untouched
    inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-CLV-001"))
    assert inventory.quantity_on_hand == 5
    adjustment = session.scalar(
        select(StockAdjustment).where(StockAdjustment.reason == "receipt_ingestion")
    )
    assert adjustment.delta == 5
    assert adjustment.actor == "owner:t"


def test_ingest_adopts_new_product_with_rag_origen_dict(shop_ctx):
    """[rag-doc R5] Solo-en-RAG: se adopta con origen {"rag": {node_id, ...}}."""
    session = shop_ctx["session"]
    product = RagProduct(
        sku="AT-5044",
        name="Tarugo Fischer 8mm",
        codigo_proveedor="MSA",
        brand="Fischer",
        price=135.5,
        currency="ARS",
        source_file="catalogo-2024.pdf",
        page=12,
        node_id="node-prod-AT-5044",
    )
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044", descripcion="Tarugo Fischer 8mm", cantidad=4, costo=None, pagina=1
        ),
        product=product,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=0, created=1)
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
    assert created is not None
    assert created.origen == {
        "rag": {
            "node_id": "node-prod-AT-5044",
            "archivo_origen": "catalogo-2024.pdf",
            "pagina_origen": 12,
        }
    }
    assert session.scalar(
        select(StockAdjustment).where(StockAdjustment.sku == "MSA-AT-5044")
    ).delta == 4
    assert session.scalar(
        select(Inventory).where(Inventory.sku_id == "MSA-AT-5044")
    ).quantity_on_hand == 4


def test_ingest_no_candidate_adopts_definitive_product_with_remito_origen(shop_ctx):
    """[ADR 0003] Línea sin match en el índice → producto DEFINITIVO con origen remito."""
    session = shop_ctx["session"]
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044",
            descripcion="Tarugo Fischer 8mm",
            cantidad=4,
            costo=Decimal("95.00"),
            pagina=3,
            source_file="remito-2026-09-10.jpg",
        ),
        product=None,
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=0, created=1)
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
    assert created is not None
    assert created.nombre_oficial == "Tarugo Fischer 8mm"
    assert created.costo_proveedor == Decimal("95.00")
    assert created.precio_lista_base == Decimal("104.50")  # 95.00 × 1.10
    assert created.embedding is not None
    assert len(created.embedding) == 1536
    remito = dict(created.origen["remito"])
    remito.pop("fecha_ingesta")  # ingest timestamp: audited but nondeterministic
    assert remito == {
        "archivo_origen": "remito-2026-09-10.jpg",
        "codigo_proveedor": "MSA",
        "pagina_origen": 3,
        "linea": {
            "codigo_orig": "AT-5044",
            "descripcion": "Tarugo Fischer 8mm",
            "cantidad": 4,
            "costo": "95.00",
        },
    }
    assert "fecha_ingesta" in created.origen["remito"]
    assert (
        session.scalar(select(StockAdjustment).where(StockAdjustment.sku == "MSA-AT-5044")).delta
        == 4
    )
    assert (
        session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-AT-5044")).quantity_on_hand
        == 4
    )


def test_ingest_no_candidate_adopts_product_with_supplier_moneda(shop_ctx):
    """[moneda] Proveedor que factura en USD → el producto adoptado hereda USD."""
    session = shop_ctx["session"]
    session.get(Supplier, 1).moneda = "USD"
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044",
            descripcion="Tarugo Fischer 8mm",
            cantidad=4,
            costo=Decimal("95.00"),
            pagina=3,
            source_file="remito-2026-09-10.jpg",
        ),
        product=None,
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=0, created=1)
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
    assert created is not None
    assert created.moneda == "USD"


def test_ingest_no_candidate_keeps_moneda_null_without_supplier_currency(shop_ctx):
    """[moneda] Proveedor sin moneda declarada → el producto adoptado queda NULL."""
    session = shop_ctx["session"]
    assert session.get(Supplier, 1).moneda is None  # shop_ctx seeds no currency
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044",
            descripcion="Tarugo Fischer 8mm",
            cantidad=4,
            costo=Decimal("95.00"),
            pagina=3,
        ),
        product=None,
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=0, created=1)
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
    assert created is not None
    assert created.moneda is None


def test_ingest_rag_adoption_falls_back_to_supplier_moneda(shop_ctx):
    """[moneda] RAG sin moneda → la adopción RAG hereda la moneda del proveedor."""
    session = shop_ctx["session"]
    session.get(Supplier, 1).moneda = "USD"
    product = RagProduct(
        sku="AT-5044",
        name="Tarugo Fischer 8mm",
        codigo_proveedor="MSA",
        brand="Fischer",
        price=135.5,
        currency=None,
        source_file="catalogo-2024.pdf",
        page=12,
        node_id="node-prod-AT-5044",
    )
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044", descripcion="Tarugo Fischer 8mm", cantidad=4, costo=None, pagina=1
        ),
        product=product,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=0, created=1)
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
    assert created is not None
    assert created.moneda == "USD"


def test_ingest_rag_adoption_keeps_rag_currency_over_supplier(shop_ctx):
    """[moneda] La moneda del RAG es primaria: pisa la del proveedor cuando viene."""
    session = shop_ctx["session"]
    session.get(Supplier, 1).moneda = "USD"
    product = RagProduct(
        sku="AT-5044",
        name="Tarugo Fischer 8mm",
        codigo_proveedor="MSA",
        brand="Fischer",
        price=135.5,
        currency="ARS",
        source_file="catalogo-2024.pdf",
        page=12,
        node_id="node-prod-AT-5044",
    )
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044", descripcion="Tarugo Fischer 8mm", cantidad=4, costo=None, pagina=1
        ),
        product=product,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=0, created=1)
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
    assert created is not None
    assert created.moneda == "ARS"


def test_ingest_no_candidate_local_sku_collision_bumps_existing(shop_ctx):
    """[ADR 0003] El SKU calculado ya existe en el catálogo local → bump, no duplicado."""
    session = shop_ctx["session"]
    _seed_receipt_product(
        session, codigo_interno="MSA-AT-5044", origen={"rag": {"node_id": "node-tar"}}
    )
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044",
            descripcion="Tarugo Fischer 8mm",
            cantidad=4,
            costo=Decimal("95.00"),
            pagina=1,
        ),
        product=None,
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=1, created=0)
    product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
    assert product.origen == {"rag": {"node_id": "node-tar"}}  # write-once: untouched
    assert (
        session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-AT-5044")).quantity_on_hand
        == 4
    )
    assert (
        len(session.scalars(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044")).all())
        == 1  # no duplicate row
    )


def test_ingest_no_candidate_without_code_builds_sku_from_description(shop_ctx):
    """[ADR 0003] Sin codigo_orig el SKU sale de la descripción normalizada."""
    session = shop_ctx["session"]
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig=None,
            descripcion="Tarugo Fischer 8mm",
            cantidad=2,
            costo=None,
            pagina=1,
        ),
        product=None,
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result == IngestResult(updated=0, created=1)
    created = session.scalar(
        select(Catalogo).where(Catalogo.codigo_interno == "MSA-TARUGO-FISCHER-8MM")
    )
    assert created is not None
    assert created.costo_proveedor == Decimal("0.00")  # document showed no cost


def test_ingest_no_candidate_embedding_failure_adopts_without_vector(shop_ctx):
    """[ADR 0003] Fallo del embedder NO bloquea: se adopta el producto sin vector."""
    session = shop_ctx["session"]
    line = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044",
            descripcion="Tarugo Fischer 8mm",
            cantidad=4,
            costo=Decimal("95.00"),
            pagina=1,
        ),
        product=None,
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    result = ingest_receipt_lines(
        session, 1, [line], OwnerContext(owner_id="t"), _embedder(fail=True)
    )
    assert result == IngestResult(updated=0, created=1)
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
    assert created is not None
    assert created.embedding is None
    assert (
        session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-AT-5044")).quantity_on_hand
        == 4
    )


def test_ingest_embed_failure_rolls_back_whole_confirmation(shop_ctx):
    """[rag-doc R5] Fallo de embedding → la confirmación completa se revierte."""
    session = shop_ctx["session"]
    _seed_receipt_product(session)
    session.commit()  # persist the seed so rollback restores this exact state
    first = ResolvedLine(receipt=_receipt(cantidad=3), product=_product())
    failing = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044", descripcion="Tarugo", cantidad=4, costo=None, pagina=1
        ),
        product=RagProduct(sku="AT-5044", name="Tarugo", codigo_proveedor="MSA", node_id="n-tar"),
    )
    with pytest.raises(EmbeddingUnavailableError, match="embedding failed"):
        ingest_receipt_lines(
            session, 1, [first, failing], OwnerContext(owner_id="t"), _embedder(fail=True)
        )
    session.rollback()  # caller-commits: rollback undoes the whole batch
    assert session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-CLV-001")) is not None
    assert session.scalar(select(Inventory)) is None  # first line's bump rolled back too


def test_ingest_missing_node_id_fails_closed(shop_ctx):
    """[rag-doc R5] Resuelto sin node_id → no se persiste nada (provenance obligatoria)."""
    session = shop_ctx["session"]
    line = ResolvedLine(
        receipt=ReceiptLine(codigo_orig="AT-5044", descripcion="Tarugo", cantidad=2, costo=None, pagina=1),
        product=RagProduct(sku="AT-5044", name="Tarugo", codigo_proveedor="MSA", node_id=None),
    )
    with pytest.raises(MissingProvenanceError):
        ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert session.scalar(select(Inventory)) is None


def test_ingest_unknown_supplier_raises(shop_ctx):
    """[rag-doc R1] Proveedor desconocido → KeyError antes de escribir."""
    rag = FakeRag(exact=(_product(),))
    with pytest.raises(KeyError, match="unknown supplier"):
        resolve_lines(shop_ctx["session"], rag, [_receipt()], supplier_id=999)


def test_ingest_inactive_supplier_refused(shop_ctx):
    """[rag-doc R1] Proveedor INACTIVO → rechazado sin escrituras."""
    session = shop_ctx["session"]
    session.get(Supplier, 1).status = SupplierStatus.INACTIVO
    session.flush()
    rag = FakeRag(exact=(_product(),))
    with pytest.raises(SupplierInactiveError, match="INACTIVO"):
        resolve_lines(session, rag, [_receipt()], supplier_id=1)


def test_monitor_lists_orders_with_state_and_sheets_status(shop_ctx):
    """El monitor lista pedidos con estado y estado de sincronización Sheets."""
    db_session = shop_ctx["session"]
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, needs_requote=False)
    db_session.add(order)
    db_session.flush()
    sheets = SheetsWriter(gc=None, settings=get_settings())
    rows = list_orders(db_session, sheets=sheets)
    assert rows[0]["order_id"] == order.order_id
    assert rows[0]["estado"] == "DRAFT"
    assert rows[0]["sheets_synced"] is False
    assert rows[0]["active_reservations"] == 0


def test_customer_orders_list_and_detail_include_ars_totals_and_snapshots(shop_ctx):
    """Customer Orders returns persisted order totals and frozen line fields."""
    db_session = shop_ctx["session"]
    order = Order(
        customer_id=1,
        estado=OrderEstado.DRAFT,
        subtotal=Decimal("270.00"),
        total=Decimal("256.50"),
        conversion_pending=False,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="CLV-001",
            cantidad=2,
            base_price=Decimal("135.00"),
            final_price=Decimal("128.25"),
            adjustment=Decimal(0),
            name="Clavos Paris 2 Pulgadas",
            source="LOCAL",
            supplier="MSA",
            moneda="ARS",
            precio_original=Decimal("100.00"),
        )
    )
    db_session.flush()

    rows = list_customer_orders(db_session)
    assert rows[0]["total"] == "256.50"
    assert rows[0]["conversion_pending"] is False
    detail = order_detail(db_session, order.order_id)
    line = detail["lines"][0]
    assert line["sku"] == "CLV-001"
    assert line["name"] == "Clavos Paris 2 Pulgadas"
    assert line["cantidad"] == 2
    assert line["source"] == "LOCAL"
    assert line["base_price"] == "135.00"
    assert line["precio_original"] == "100.00"
    # LOCAL with a live catalog row: the applied margin (0.35), verbatim.
    assert line["margin_pct"] == "0.35"
    assert line["line_total"] == "256.50"


def test_order_line_margin_pct_derivation(shop_ctx):
    """Margin % derives from original vs base price: LOCAL markup, RAG 0.00.

    Missing (None) or zero original prices derive ``None`` (rendered "—");
    the app layer guards the division by zero the same way.
    """
    db_session = shop_ctx["session"]
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, conversion_pending=False)
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="CLV-LOCAL",
            cantidad=1,
            base_price=Decimal("125.00"),
            final_price=Decimal("118.75"),
            adjustment=Decimal(0),
            name="Local item",
            source="LOCAL",
            supplier="MSA",
            moneda="ARS",
            precio_original=Decimal("100.00"),
        )
    )
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="RAG-1",
            cantidad=2,
            base_price=Decimal("250.00"),
            final_price=Decimal("237.50"),
            adjustment=Decimal(0),
            name="RAG item",
            source="RAG",
            supplier="MSA",
            moneda="USD",
            precio_original=Decimal("250.0000"),
        )
    )
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="NO-ORIG",
            cantidad=1,
            base_price=Decimal("50.00"),
            final_price=Decimal("47.50"),
            adjustment=Decimal(0),
            name="No snapshot",
            source="LOCAL",
            supplier="MSA",
            moneda="ARS",
            precio_original=None,
        )
    )
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="ZERO-ORIG",
            cantidad=1,
            base_price=Decimal("50.00"),
            final_price=Decimal("47.50"),
            adjustment=Decimal(0),
            name="Zero snapshot",
            source="LOCAL",
            supplier="MSA",
            moneda="ARS",
            precio_original=Decimal(0),
        )
    )
    db_session.flush()

    lines = order_detail(db_session, order.order_id)["lines"]
    assert lines[0]["margin_pct"] == "25.00"  # LOCAL without catalog row: (125/100 − 1) × 100
    assert lines[1]["margin_pct"] == "0.00"  # RAG: base == original × rate
    assert lines[2]["margin_pct"] is None
    assert lines[3]["margin_pct"] is None
    assert lines[1]["line_total"] == "475.00"  # 237.50 × 2
    # Supplier codes: LOCAL resolves through the mappings (unmapped → ""),
    # RAG already carries the provider code in the snapshot.
    assert lines[0]["codigo_proveedor"] == ""
    assert lines[1]["codigo_proveedor"] == "MSA"


def test_order_line_local_margin_pct_shows_catalog_margin(shop_ctx):
    """LOCAL lines with a catalog row show the applied margin, not a derivation."""
    db_session = shop_ctx["session"]
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, conversion_pending=False)
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="CLV-001",
            cantidad=1,
            base_price=Decimal("135.00"),
            final_price=Decimal("128.25"),
            adjustment=Decimal(0),
            name="Clavos Paris 2 Pulgadas",
            source="LOCAL",
            supplier="MSA",
            moneda="ARS",
            precio_original=Decimal("100.00"),
        )
    )
    db_session.flush()

    line = order_detail(db_session, order.order_id)["lines"][0]
    assert line["margin_pct"] == "0.35"  # Catalogo.margen_aplicado_pct, verbatim
    assert line["moneda"] == "ARS"
    assert line["codigo_proveedor"] == ""  # no supplier mapping seeded


def test_order_line_local_margin_pct_falls_back_without_catalog_row(shop_ctx):
    """A delisted LOCAL product falls back to the derived markup."""
    db_session = shop_ctx["session"]
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, conversion_pending=False)
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="DELISTED-1",
            cantidad=1,
            base_price=Decimal("125.00"),
            final_price=Decimal("118.75"),
            adjustment=Decimal(0),
            name="Delisted item",
            source="LOCAL",
            supplier="MSA",
            moneda="ARS",
            precio_original=Decimal("100.00"),
        )
    )
    db_session.flush()

    line = order_detail(db_session, order.order_id)["lines"][0]
    assert line["margin_pct"] == "25.00"  # derived: (125/100 − 1) × 100


def test_order_line_rag_margin_pct_is_zero_only_with_both_prices(shop_ctx):
    """RAG lines render 0.00 when priced, and "—" when a snapshot price is gone."""
    db_session = shop_ctx["session"]
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, conversion_pending=False)
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="RAG-USD",
            cantidad=1,
            base_price=Decimal("15300.00"),
            final_price=Decimal("14535.00"),
            adjustment=Decimal(0),
            name="RAG USD item",
            source="RAG",
            supplier="SCO",
            moneda="USD",
            precio_original=Decimal("10.0000"),
        )
    )
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="RAG-PENDING",
            cantidad=1,
            base_price=Decimal(0),
            final_price=Decimal(0),
            adjustment=Decimal(0),
            name="RAG pending item",
            source="RAG",
            supplier="SCO",
            moneda="USD",
            precio_original=Decimal("10.0000"),
        )
    )
    db_session.flush()

    lines = order_detail(db_session, order.order_id)["lines"]
    # Before the fix this derived (15300/10 − 1) × 100 = 152900.00 across
    # currencies; the RAG semantics are "no margin".
    assert lines[0]["margin_pct"] == "0.00"
    assert lines[0]["codigo_proveedor"] == "SCO"
    assert lines[0]["moneda"] == "USD"
    # Pending conversion (base 0, no rate yet): no margin to show.
    assert lines[1]["margin_pct"] is None


def test_exchange_rate_rejects_ars_and_persists_usd(shop_ctx):
    """ARS cannot be edited while a USD rate is stored with a timestamp."""
    db_session = shop_ctx["session"]
    with pytest.raises(ValueError, match="ARS.*read-only"):
        set_exchange_rate(db_session, "ARS", Decimal("1.00"))
    usd = set_exchange_rate(db_session, "usd", Decimal("950.12345"))
    assert usd.currency == "USD"
    assert usd.rate_to_ars == Decimal("950.1235")
    assert list_exchange_rates(db_session)[-1]["currency"] == "USD"


def test_recompute_pending_conversion_clears_flag_and_fills_totals(shop_ctx):
    """Loading a rate recomputes a pending RAG order and clears its flag."""
    db_session = shop_ctx["session"]
    db_session.add(AppSetting(key="default_margin_pct", value="20"))
    db_session.add(ExchangeRate(currency="USD", rate_to_ars=Decimal("1000.0000")))
    order = Order(
        customer_id=1,
        estado=OrderEstado.DRAFT,
        conversion_pending=True,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="RAG-1",
            cantidad=2,
            base_price=Decimal(0),
            final_price=Decimal(0),
            adjustment=Decimal(0),
            name="RAG item",
            source="RAG",
            supplier="UNMAPPED",
            moneda="USD",
            precio_original=Decimal("10.00"),
        )
    )
    db_session.flush()

    assert recompute_pending_conversion(db_session) == 1
    assert order.conversion_pending is False
    assert order.subtotal == Decimal("20000.00")
    assert order.total == Decimal("20000.00")
    item = db_session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.base_price == Decimal("10000.00")


def test_default_margin_round_trips(client_ctx):
    """The default RAG margin setting can be read and updated."""
    session = client_ctx["session"]
    session.add(AppSetting(key="default_margin_pct", value="20"))
    session.flush()
    assert get_default_margin(session) == Decimal(20)
    assert set_default_margin(session, Decimal("27.50")) == Decimal("27.50")
    assert get_default_margin(session) == Decimal("27.50")


def test_pending_conversion_order_is_blocked_at_approval(shop_ctx):
    """Approval registration refuses an order until its prices are converted."""
    order = Order(
        customer_id=1,
        estado=OrderEstado.CONFIRMED,
        conversion_pending=True,
    )
    shop_ctx["session"].add(order)
    shop_ctx["session"].flush()

    with pytest.raises(PendingConversionError, match="pending currency conversion"):
        confirm_and_register(shop_ctx["session"], order, sheets=SimpleNamespace())


# ------------------------------------------------ app handler functions (DB)
# NOTE: the app handlers open their own SessionLocal, which only sees COMMITTED
# rows — so these tests commit the fixture seed before exercising the handler.


def test_app_catalog_grid_renders_seeded_products(shop_ctx):
    """La grilla del catálogo muestra proveedor + código mapeado, no el SKU interno."""
    shop_ctx["session"].add(Inventory(sku_id="CLV-001", quantity_on_hand=10))
    shop_ctx["session"].commit()
    rows = _catalog_grid()
    assert any(row[0] == "MSA" for row in rows)  # supplier code leads the row
    assert any(row[1] == "" for row in rows)  # unmapped product: blank supplier code
    assert any(row[2] == "Clavos Paris 2 Pulgadas" for row in rows)  # Nombre column
    assert any(row[4] == 10 for row in rows)  # Stock column (Inventory)
    assert any(row[6] == "100.00" for row in rows)  # Costo column
    assert any(row[7] == "135.00" for row in rows)  # Precio lista (AR$) column


def test_app_register_client_returns_success_message(shop_ctx):
    """Registrar un cliente recarga la grilla y limpia el formulario."""
    shop_ctx["session"].commit()
    message, rows, name, phone, lista, discount = _register_client(
        "Nueva Ferretería", "11 6666 7777", 1, 0.0
    )
    assert message == "Cliente registrado"
    assert any(row[1] == "Nueva Ferretería" for row in rows)
    assert (name, phone, lista, discount) == ("", "", None, 0)
    with SessionLocal() as session:
        assert (
            session.scalar(select(Cliente).where(Cliente.nombre_comercial == "Nueva Ferretería"))
            is not None
        )


def test_app_catalog_edit_persists_stock_change(shop_ctx):
    """Editar stock desde la UI persiste el cambio en Inventory (fuente única)."""
    shop_ctx["session"].commit()
    message = _catalog_edit("CLV-001", 25, None, None)
    assert message == "Guardado: CLV-001"
    with SessionLocal() as session:
        inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "CLV-001"))
        assert inventory is not None
        assert inventory.quantity_on_hand == 25


def test_app_catalog_edit_resolves_supplier_code(shop_ctx):
    """El campo de edición resuelve códigos de proveedor, no solo el SKU interno."""
    from src.backoffice.sku_mappings import record_supplier_sku

    session = shop_ctx["session"]
    record_supplier_sku(session, 1, "AX 302-8", "CLV-001")
    session.commit()

    message = _catalog_edit("  ax  302-8 ", 30, None, None)
    assert message == "Guardado:   ax  302-8 "  # echoes the typed code, not the SKU
    with SessionLocal() as fresh:
        inventory = fresh.scalar(select(Inventory).where(Inventory.sku_id == "CLV-001"))
        assert inventory is not None
        assert inventory.quantity_on_hand == 30


def test_app_catalog_edit_unknown_code_mentions_both_options(shop_ctx):
    """Código desconocido: el error menciona SKU interno y código de proveedor."""
    shop_ctx["session"].commit()
    message = _catalog_edit("ZZZ-404", 5, None, None)
    assert message.startswith("Error:")
    assert "SKU interno" in message and "código de proveedor" in message


def test_app_register_client_surfaces_error_for_bad_phone(shop_ctx):
    """Un teléfono inválido desde la UI devuelve el error y no toca el formulario."""
    shop_ctx["session"].commit()
    result = _register_client("Pepe", "no-es-telefono", 1, 0.0)
    assert result[0].startswith("Error:")
    # The other outputs are gr.update() keepers, not cleared values.
    assert all(not isinstance(v, str) for v in result[1:])


def test_active_supplier_choices_lists_activo_by_business_name(shop_ctx):
    """[rag-doc R1] El dropdown lista solo ACTIVO por business_name y retiene el ID."""
    shop_ctx["session"].add(
        Supplier(
            id=2,
            code="XYZ",
            business_name="Inactivo SA",
            default_margin_pct=Decimal("0.10"),
            status=SupplierStatus.INACTIVO,
        )
    )
    shop_ctx["session"].commit()
    choices = _active_supplier_choices()
    assert choices == [("Mayorista SA", 1)]  # business_name → id; ACTIVO only
    assert _SUPPLIER_PLACEHOLDER not in [label for label, _id in choices]


def test_active_supplier_choices_prepends_placeholder_when_requested(shop_ctx):
    """El placeholder "seleccionar proveedor" es la primera opción del tab Ingesta."""
    shop_ctx["session"].commit()
    choices = _ingesta_supplier_choices()
    assert choices[0] == (_SUPPLIER_PLACEHOLDER, _SUPPLIER_PLACEHOLDER)  # literal sentinel
    assert choices[1:] == [("Mayorista SA", 1)]  # active suppliers keep their order


def _clavos_document_rag(**kwargs: object) -> FakeRag:
    """FakeRag que parsea un remito de dos líneas (CLV-001 y AT-5044)."""
    from src.integrations.rag import DocumentLine

    class _ParsingRag(FakeRag):
        def parse_document(self, *, filename, content, codigo_proveedor):
            self.parse_codes.append(codigo_proveedor)
            return (
                DocumentLine(
                    codigo_orig="CLV-001",
                    codigo=None,
                    descripcion="Clavos Paris 2 Pulgadas",
                    cantidad=5,
                    costo=95.0,
                    pagina=1,
                ),
                DocumentLine(
                    codigo_orig="AT-5044",
                    codigo=None,
                    descripcion="Tarugo Fischer 8mm",
                    cantidad=4,
                    costo=None,
                    pagina=1,
                ),
            )

    return _ParsingRag(**kwargs)  # type: ignore[arg-type]


def test_app_ingest_parse_returns_receipt_lines_without_resolving(shop_ctx, tmp_path):
    """[backoffice R1][rag-doc R4] Parsear guarda ReceiptLines y NO resuelve ni toca el catálogo."""
    shop_ctx["session"].commit()
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")

    rag = _clavos_document_rag()
    parsed, message = _ingest_parse(rag, image, 1)
    assert len(parsed) == 2
    assert parsed[0].codigo_orig == "CLV-001"
    assert parsed[0].source_file == "remito.jpg"  # provenance carried for adoption
    assert message == "Documento parseado: 2 líneas. Evaluando con Mayorista SA..."
    # Parse is supplier-independent: no resolution queries hit the RAG client.
    assert rag.exact_calls == []
    assert rag.query_calls == []
    # No catalog/inventory writes: the seeded rows are the only ones.
    assert shop_ctx["session"].scalar(select(func.count()).select_from(Catalogo)) == 1
    assert shop_ctx["session"].scalar(select(func.count()).select_from(Inventory)) == 0


def test_app_ingest_parse_placeholder_supplier_still_parses(shop_ctx, tmp_path):
    """Con el placeholder seleccionado el parse corre igual (es agnóstico) y pide elegir proveedor."""
    shop_ctx["session"].commit()
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")

    rag = _clavos_document_rag(exact=(), hybrid=())
    parsed, message = _ingest_parse(rag, image, _SUPPLIER_PLACEHOLDER)
    assert len(parsed) == 2
    assert rag.parse_codes == [""]  # empty supplier code: parse is supplier-agnostic
    assert message == "Documento parseado: 2 líneas. Seleccioná un proveedor para evaluarlo."
    # Placeholder also comes through as the None-mapped dropdown value.
    parsed2, message2 = _ingest_parse(rag, image, None)
    assert len(parsed2) == 2
    assert "Seleccioná un proveedor" in message2
    assert rag.parse_codes == ["", ""]


def test_app_ingest_parse_rag_down_returns_error_tuple(shop_ctx, tmp_path):
    """RAG caído → (estado vacío, mensaje de error honesto)."""
    shop_ctx["session"].commit()
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")

    class _DownRag(FakeRag):
        def parse_document(self, *, filename, content, codigo_proveedor):
            raise RagProductError("connection refused")

    parsed, message = _ingest_parse(_DownRag(), image, 1)
    assert parsed == ()
    assert message == "Error: RAG no disponible (connection refused)"


def test_app_ingest_resolve_returns_grid_with_pending_lines(shop_ctx, tmp_path):
    """[backoffice R1][rag-doc R4] Resolver → grilla con líneas resueltas/pendientes."""
    shop_ctx["session"].commit()
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")

    parsed, _parse_message = _ingest_parse(_clavos_document_rag(), image, 1)
    grid, state, message = _ingest_resolve(_clavos_document_rag(exact=(), hybrid=()), parsed, 1, (), "")
    assert len(grid) == 2
    assert grid[0][0] == "CLV-001"
    assert grid[0][1] == "Clavos Paris 2 Pulgadas"
    assert grid[0][4] == "NUEVO (por confirmar)"  # no candidates → definitive on confirm
    assert grid[1][4] == "NUEVO (por confirmar)"
    assert len(state) == 2
    assert all(line.pending for line in state)
    assert "2 líneas; 2 sin match en el índice (se adoptan al confirmar)" in message


def test_app_ingest_resolve_re_scopes_when_supplier_changes(shop_ctx):
    """Cambiar el proveedor re-evalúa las mismas líneas parseadas con el nuevo scope."""
    shop_ctx["session"].add(
        Supplier(
            id=2,
            code="XYZ",
            business_name="XYZ Mayorista",
            default_margin_pct=Decimal("0.10"),
        )
    )
    shop_ctx["session"].commit()
    parsed = (_receipt(),)  # CLV-001, 5 unidades
    supplier_a_rag = FakeRag(exact=(_product(),), hybrid=())
    grid_a, state_a, message_a = _ingest_resolve(supplier_a_rag, parsed, 1, (), "")
    assert grid_a[0][4] == "CLV-001 — Clavos Paris 2 Pulgadas"
    assert not state_a[0].pending

    supplier_b_rag = FakeRag(
        exact=(),
        hybrid=(
            RagProduct(
                sku="AT-5044",
                name="Tarugo Fischer 8mm",
                codigo_proveedor="XYZ",
                node_id="n-tar",
            ),
        ),
    )
    grid_b, state_b, message_b = _ingest_resolve(supplier_b_rag, parsed, 2, state_a, message_a)
    assert grid_b[0][4] == "AT-5044 — Tarugo Fischer 8mm"
    assert not state_b[0].pending
    assert state_b[0].product.sku == "AT-5044"  # re-scoped to supplier B's catalog
    assert "1 líneas." in message_b


def test_app_ingest_resolve_placeholder_keeps_state_and_asks_supplier(shop_ctx):
    """Con el placeholder, la resolución no toca RAG ni DB y mantiene el estado previo."""
    shop_ctx["session"].commit()
    parsed = (_receipt(),)
    current = (ResolvedLine(receipt=_receipt(codigo_orig=None)),)
    rag = FakeRag(exact=(), hybrid=())
    for raw in (_SUPPLIER_PLACEHOLDER, None, ""):
        grid, state, message = _ingest_resolve(rag, parsed, raw, current, "")
        assert message == "Seleccioná un proveedor para evaluar el documento."
        assert state == current  # untouched: no wipe of previous resolution
        assert grid == _resolved_grid(current)
    assert rag.exact_calls == []  # placeholder guard runs BEFORE any RAG call


def test_app_ingest_resolve_without_parsed_document_asks_to_upload(shop_ctx):
    """Sin documento parseado → el estado se mantiene y se pide subir el remito."""
    shop_ctx["session"].commit()
    current = (ResolvedLine(receipt=_receipt(codigo_orig=None)),)
    grid, state, message = _ingest_resolve(FakeRag(), (), 1, current, "")
    assert message == "Primero subí un documento."
    assert state == current
    assert grid == _resolved_grid(current)


def test_app_ingest_resolve_keeps_chained_parse_error_message(shop_ctx):
    """Un error de parse encadenado no lo pisa 'Primero subí un documento'."""
    shop_ctx["session"].commit()
    error = "Error: RAG no disponible (connection refused)"
    _grid, state, message = _ingest_resolve(FakeRag(), (), 1, (), error)
    assert message == error
    assert state == ()


def test_selected_supplier_id_extracts_id_or_placeholder_none():
    """El helper devuelve el ID real, o None para el placeholder / basura / vacío."""
    assert _selected_supplier_id(_SUPPLIER_PLACEHOLDER) is None
    assert _selected_supplier_id(None) is None
    assert _selected_supplier_id("") is None
    assert _selected_supplier_id("  ") is None
    assert _selected_supplier_id(1) == 1
    assert _selected_supplier_id("1") == 1
    assert _selected_supplier_id(" 3 ") == 3
    assert _selected_supplier_id("seleccionar otra cosa") is None
    assert _selected_supplier_id("abc") is None
    assert _selected_supplier_id("1.5") is None


def test_app_ingest_manual_search_placeholder_blocks_without_db_or_rag(shop_ctx):
    """Con el placeholder, la búsqueda manual no toca DB ni RAG y pide elegir proveedor."""
    shop_ctx["session"].commit()
    rag = FakeRag(exact=(_product(),))
    for raw in (_SUPPLIER_PLACEHOLDER, None, ""):
        rows, candidates, message = _ingest_manual_search(rag, 1, "CLV-001", raw)
        assert rows == []
        assert candidates == ()
        assert message == "Seleccioná un proveedor para buscar candidatos."
    assert rag.exact_calls == []  # guard fires BEFORE any lookup
    assert rag.query_calls == []


def test_app_ingest_confirm_placeholder_blocks_with_zero_writes(shop_ctx):
    """Confirmar con el placeholder está bloqueado: mensaje claro y cero escrituras."""
    shop_ctx["session"].commit()
    resolved = ResolvedLine(receipt=_receipt(cantidad=5), product=_product())
    for raw in (_SUPPLIER_PLACEHOLDER, None, ""):
        message = _ingest_confirm((resolved,), raw, _embedder())
        assert message == "Seleccioná un proveedor antes de confirmar."
    with SessionLocal() as session:
        assert session.scalar(select(Inventory)) is None  # zero writes
        assert session.scalar(select(func.count()).select_from(Catalogo)) == 1


def test_app_ingest_manual_search_and_assign_fix_pending(shop_ctx):
    """[manual R1] La búsqueda manual devuelve candidatos y asignar resuelve la línea."""
    shop_ctx["session"].commit()
    pending = ResolvedLine(receipt=_receipt(codigo_orig="AT-5044", descripcion="Tarugo 8mm"))
    rag = FakeRag(
        hybrid=(RagProduct(sku="AT-5044", name="Tarugo Fischer 8mm", codigo_proveedor="MSA", node_id="n-tar"),)
    )
    candidates_grid, candidates, _status = _ingest_manual_search(rag, 1, "AT-5044", 1)
    assert len(candidates_grid) == 1
    assert candidates_grid[0][0] == "AT-5044"
    assert candidates_grid[0][4] == "n-tar"
    new_state, grid, assign_status = _ingest_assign((pending,), 1, 0, candidates)
    assert "asignada" in assign_status
    assert new_state[0].product.node_id == "n-tar"
    assert grid[0][4] == "AT-5044 — Tarugo Fischer 8mm"


def test_app_ingest_manual_search_exact_hit_skips_hybrid(shop_ctx):
    """[manual R1] El código exacto hace lookup exacto: la híbrida ni se consulta."""
    shop_ctx["session"].commit()
    exact_product = RagProduct(
        sku="SM-0048-84", name="Ducha Flexible Cromo 84cm", codigo_proveedor="MSA", node_id="n-ducha"
    )

    class _ExplodingHybridRag(FakeRag):
        def query(self, text: str):
            raise AssertionError("hybrid query must not run when exact lookup hits")

    rag = _ExplodingHybridRag(exact=(exact_product,))
    candidates_grid, candidates, status = _ingest_manual_search(rag, 1, "SM 0048-84", 1)
    assert len(candidates_grid) == 1
    assert candidates_grid[0][4] == "n-ducha"
    assert candidates == (exact_product,)
    assert "exacta" in status


def test_app_ingest_manual_search_exact_miss_falls_back_to_hybrid(shop_ctx):
    """[manual R1] Sin hit exacto cae a la híbrida scoped al proveedor."""
    shop_ctx["session"].commit()
    hybrid_product = RagProduct(
        sku="AT-5044", name="Tarugo Fischer 8mm", codigo_proveedor="MSA", node_id="n-tar"
    )
    rag = FakeRag(exact=(), hybrid=(hybrid_product,))
    candidates_grid, candidates, status = _ingest_manual_search(rag, 1, "tarugo 8mm", 1)
    assert len(candidates_grid) == 1
    assert candidates == (hybrid_product,)
    assert "exacta" not in status
    assert rag.exact_calls == [("TARUGO 8MM", "MSA")]
    assert rag.query_calls == ["tarugo 8mm"]


def test_app_ingest_manual_search_exact_multi_hit_returns_all(shop_ctx):
    """[manual R1] Varios hits exactos comparten el código: se devuelven todos."""
    shop_ctx["session"].commit()
    dup_a = RagProduct(sku="SM-1", name="Ducha A", codigo_proveedor="MSA", node_id="n-a")
    dup_b = RagProduct(sku="SM-2", name="Ducha B", codigo_proveedor="MSA", node_id="n-b")

    class _ExplodingHybridRag(FakeRag):
        def query(self, text: str):
            raise AssertionError("hybrid query must not run when exact lookup hits")

    rag = _ExplodingHybridRag(exact=(dup_a, dup_b))
    candidates_grid, candidates, status = _ingest_manual_search(rag, 1, "SM-1", 1)
    assert [row[4] for row in candidates_grid] == ["n-a", "n-b"]
    assert candidates == (dup_a, dup_b)
    assert "exacta" in status


def test_pending_row_selected_fills_grid_from_cached_candidates(shop_ctx):
    """[manual R1] Seleccionar la fila ambigua popula la grilla con los candidatos cacheados."""
    cached = (
        RagProduct(sku="SM-1", name="Ducha A", codigo_proveedor="MSA", node_id="n-a"),
        RagProduct(sku="SM-2", name="Ducha B", codigo_proveedor="MSA", node_id="n-b"),
    )
    state = (
        ResolvedLine(receipt=_receipt(cantidad=2), product=_product()),  # resolved row 1
        ResolvedLine(
            receipt=_receipt(codigo_orig="SM 0048-84", descripcion="Ducha flexible"),
            pending_reason=PendingReason.AMBIGUOUS,
            candidates=cached,
        ),
    )
    evt = SimpleNamespace(selected=True, index=[1])
    rows, candidates, message, line_number = _pending_row_selected(evt, state, 7)  # type: ignore[arg-type]
    assert [row[4] for row in rows] == ["n-a", "n-b"]
    assert candidates == cached
    assert "2 candidatos recuperados para la línea 2" in message
    assert line_number == 2  # syncs the clicked row's 1-based number, not the typed 7


def test_pending_row_selected_resolved_row_clears_grid(shop_ctx):
    """[manual R1] Fila resuelta → grilla limpia con mensaje de línea resuelta."""
    state = (ResolvedLine(receipt=_receipt(cantidad=2), product=_product()),)
    evt = SimpleNamespace(selected=True, index=[0])
    rows, candidates, message, line_number = _pending_row_selected(evt, state, 7)  # type: ignore[arg-type]
    assert rows == []
    assert candidates == ()
    assert "ya está resuelta" in message
    assert line_number == 7  # passthrough: a resolved row never clobbers the typed number


def test_pending_row_selected_ambiguous_without_candidates_clears_grid(shop_ctx):
    """[manual R1] Ambigua sin cache → grilla limpia sugiriendo búsqueda manual."""
    state = (
        ResolvedLine(
            receipt=_receipt(codigo_orig="SM 0048-84", descripcion="Ducha flexible"),
            pending_reason=PendingReason.AMBIGUOUS,
        ),
    )
    evt = SimpleNamespace(selected=True, index=[0])
    rows, candidates, message, line_number = _pending_row_selected(evt, state, 7)  # type: ignore[arg-type]
    assert rows == []
    assert candidates == ()
    assert "búsqueda manual" in message
    assert line_number == 7  # no cached candidates → the typed number is left untouched


def test_pending_row_selected_no_candidates_row_clears_grid(shop_ctx):
    """[ADR 0003] Fila sin candidatos en el índice → grilla limpia, se adopta al confirmar."""
    state = (
        ResolvedLine(
            receipt=_receipt(codigo_orig="XX-1", descripcion="Producto raro"),
            pending_reason=PendingReason.NO_CANDIDATES,
        ),
    )
    evt = SimpleNamespace(selected=True, index=[0])
    rows, candidates, message, line_number = _pending_row_selected(evt, state, 7)  # type: ignore[arg-type]
    assert rows == []
    assert candidates == ()
    assert "no tiene candidatos" in message
    assert line_number == 1  # actionable pending line → the click syncs its 1-based number


def test_pending_row_selected_deselection_is_a_noop(shop_ctx):
    """Deseleccionar (o un evento sin fila usable) no toca la grilla ni el estado."""
    evt = SimpleNamespace(selected=False, index=[0])
    rows, candidates, message, line_number = _pending_row_selected(evt, (), 7)  # type: ignore[arg-type]
    assert rows == []
    assert candidates == ()
    assert message == ""
    assert line_number == 7  # deselection passes the typed number through unchanged


def test_pending_row_selected_zero_quantity_pending_keeps_typed_number(shop_ctx):
    """Línea pendiente con cantidad 0 no se ingesta: el número tipeado no se pisa."""
    state = (
        ResolvedLine(
            receipt=_receipt(codigo_orig="XX-2", descripcion="Muestra sin cargo", cantidad=0),
            pending_reason=PendingReason.NONE,
        ),
    )
    evt = SimpleNamespace(selected=True, index=[0])
    rows, candidates, _message, line_number = _pending_row_selected(evt, state, 7)  # type: ignore[arg-type]
    assert rows == []
    assert candidates == ()
    assert line_number == 7  # zero-qty lines are never ingested → passthrough


def test_app_ingest_confirm_blocked_while_ambiguous(shop_ctx):
    """[rag-doc R4] Confirmación bloqueada solo por líneas AMBIGUAS; mensaje las lista."""
    shop_ctx["session"].commit()
    pending = ResolvedLine(
        receipt=_receipt(codigo_orig="AT-5044", descripcion="Tarugo 8mm"),
        pending_reason=PendingReason.AMBIGUOUS,
    )
    message = _ingest_confirm((pending,), 1, _embedder())
    assert "bloqueado" in message
    assert "asignación manual" in message
    assert "AT-5044" in message
    with SessionLocal() as session:
        assert session.scalar(select(Inventory)) is None  # zero writes while blocked


def test_app_ingest_confirm_adopts_no_candidate_line(shop_ctx):
    """[ADR 0003] Línea sin match NO bloquea: confirmar crea el producto definitivo."""
    shop_ctx["session"].commit()
    no_candidate = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="AT-5044",
            descripcion="Tarugo 8mm",
            cantidad=4,
            costo=Decimal("95.00"),
            pagina=1,
            source_file="remito.jpg",
        ),
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    message = _ingest_confirm((no_candidate,), 1, _embedder())
    assert message == "Ingresado: 0 actualizados, 1 nuevos."
    with SessionLocal() as session:
        created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
        assert created is not None
        assert created.origen is not None and "remito" in created.origen
        assert (
            session.scalar(
                select(Inventory).where(Inventory.sku_id == "MSA-AT-5044")
            ).quantity_on_hand
            == 4
        )


def test_app_ingest_confirm_unblocked_when_all_resolved(shop_ctx):
    """[rag-doc R4/R5] Todas resueltas → confirma y escribe stock con node_id."""
    session = shop_ctx["session"]
    _seed_receipt_product(session)
    session.commit()
    resolved = ResolvedLine(receipt=_receipt(cantidad=3), product=_product())
    message = _ingest_confirm((resolved,), 1, _embedder())
    assert message == "Ingresado: 1 actualizados, 0 nuevos."
    with SessionLocal() as session:
        inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-CLV-001"))
        assert inventory is not None
        assert inventory.quantity_on_hand == 3  # 0 + 3 (no pre-existing row)


def test_app_ingest_mark_new_reclassifies_ambiguous_and_confirm_adopts(shop_ctx):
    """[ADR 0003] Marcar una ambigua como nueva: sin candidatos, se adopta al confirmar."""
    shop_ctx["session"].commit()
    cached = (
        RagProduct(sku="SM-1", name="Arrancador", codigo_proveedor="MSA", node_id="n-a"),
        RagProduct(sku="SM-2", name="Grasa", codigo_proveedor="MSA", node_id="n-b"),
    )
    ambiguous = ResolvedLine(
        receipt=ReceiptLine(
            codigo_orig="SM 0048-84",
            descripcion="Ducha flexible",
            cantidad=2,
            costo=Decimal("95.00"),
            pagina=1,
            source_file="remito.jpg",
        ),
        pending_reason=PendingReason.AMBIGUOUS,
        candidates=cached,
    )
    new_state, grid, status = _ingest_mark_new((ambiguous,), 1)
    assert "marcada como producto nuevo" in status
    assert new_state[0].pending
    assert new_state[0].pending_reason is PendingReason.NO_CANDIDATES
    assert new_state[0].candidates == ()  # cached candidates dropped: misleading now
    assert grid[0][4] == "NUEVO (por confirmar)"
    message = _ingest_confirm(new_state, 1, _embedder())
    assert message == "Ingresado: 0 actualizados, 1 nuevos."
    with SessionLocal() as session:
        created = session.scalar(
            select(Catalogo).where(Catalogo.codigo_interno == "MSA-SM-0048-84")
        )
        assert created is not None
        assert created.origen is not None and "remito" in created.origen


def test_app_ingest_mark_new_rejects_resolved_line(shop_ctx):
    """[ADR 0003] Una línea ya resuelta no se puede marcar como nueva."""
    shop_ctx["session"].commit()
    resolved = ResolvedLine(receipt=_receipt(cantidad=2), product=_product())
    state, grid, status = _ingest_mark_new((resolved,), 1)
    assert status == "Esa línea ya está resuelta."
    assert state[0].product is not None
    assert grid[0][4] == "CLV-001 — Clavos Paris 2 Pulgadas"


def test_app_ingest_mark_new_rejects_invalid_index(shop_ctx):
    """[ADR 0003] Índice inválido → mensaje útil y estado intacto."""
    shop_ctx["session"].commit()
    ambiguous = ResolvedLine(
        receipt=_receipt(cantidad=2), pending_reason=PendingReason.AMBIGUOUS
    )
    original = (ambiguous,)
    for bad_index in (-1, 0, 5, None, ""):
        state, _grid, status = _ingest_mark_new(original, bad_index)
        assert status == "Seleccioná una línea pendiente válida."
        assert state == original
        assert state[0].pending_reason is PendingReason.AMBIGUOUS


def test_app_ingest_mark_new_is_idempotent_on_no_candidates_line(shop_ctx):
    """[ADR 0003] Marcar como nueva una línea ya NO_CANDIDATES es un éxito sin cambios."""
    shop_ctx["session"].commit()
    no_candidate = ResolvedLine(
        receipt=_receipt(cantidad=2), pending_reason=PendingReason.NO_CANDIDATES
    )
    original = (no_candidate,)
    state, grid, status = _ingest_mark_new(original, 1)
    assert "ya está marcada como producto nuevo" in status
    assert state == original
    assert grid[0][4] == "NUEVO (por confirmar)"


def test_app_ingest_mark_new_rejects_zero_quantity_line(shop_ctx):
    """[ADR 0003] Línea con cantidad 0 nunca se ingesta: marcarla como nueva no aplica."""
    shop_ctx["session"].commit()
    zero_qty = ResolvedLine(receipt=_receipt(cantidad=0))
    original = (zero_qty,)
    state, grid, status = _ingest_mark_new(original, 1)
    assert "nunca se ingesta" in status
    assert state == original
    assert grid[0][4] == "PENDIENTE"


def test_app_rate_save_updates_timestamp_and_recomputes_pending_order(shop_ctx):
    """The app-level rate save bumps updated_at and recomputes pending orders."""
    db_session = shop_ctx["session"]
    db_session.add(AppSetting(key="default_margin_pct", value="20"))
    order = Order(
        customer_id=1,
        estado=OrderEstado.DRAFT,
        conversion_pending=True,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="RAG-1",
            cantidad=2,
            base_price=Decimal(0),
            final_price=Decimal(0),
            adjustment=Decimal(0),
            name="RAG item",
            source="RAG",
            supplier="UNMAPPED",
            moneda="USD",
            precio_original=Decimal("10.00"),
        )
    )
    db_session.commit()

    with patch("src.backoffice.customer_orders.datetime") as fake_datetime:
        fake_datetime.now.side_effect = [
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 6, 1, tzinfo=UTC),
        ]
        first_message, first_rates, _ = _save_exchange_rate("USD", "1000.00")
        second_message, second_rates, _ = _save_exchange_rate("USD", "1100.00")

    assert "recomputed 1 pending order(s)" in first_message
    assert "recomputed 0 pending order(s)" in second_message
    assert first_rates[-1][0] == "USD"
    # Grid renders updated_at in Buenos Aires local time (UTC-3 display contract)
    assert first_rates[-1][2] == "2023-12-31 21:00:00"
    assert second_rates[-1][2] == "2024-05-31 21:00:00"
    with SessionLocal() as session:
        reloaded = session.get(Order, order.order_id)
        assert reloaded.conversion_pending is False
        assert reloaded.subtotal == Decimal("20000.00")  # 2 × 10 USD × 1000, no margin
        assert reloaded.total == Decimal("20000.00")


# ------------------------------------------------ fulfillment actions (Phase 6)


@pytest.mark.parametrize(
    ("estado", "expected"),
    [
        ("DRAFT", ("confirm_order", "cancel_order")),
        ("CONFIRMED", ("start_picking", "cancel_order")),
        ("PICKING", ("complete_picking", "cancel_order")),
        ("READY_FOR_DELIVERY", ("deliver_order", "cancel_order")),
        ("CANCELED", ()),
        ("CLOSED", ()),
    ],
)
def test_legal_actions_per_state(estado, expected):
    """Solo las acciones legales del estado se ofrecen en el tab (backoffice spec)."""
    assert legal_actions(estado) == expected


# ------------------------------------------------ state progress diagram (pure)


def _pill_style(html: str, state: str) -> str:
    """The inline style of one state pill in the diagram HTML."""
    match = re.search(rf'data-state="{state}" style="([^"]*)"', html)
    assert match is not None, f"missing pill for {state}"
    return match.group(1)


def _is_colored(style: str) -> bool:
    """A pill is colored when its background is not the gray/white outline."""
    return "background:#ffffff" not in style


@pytest.mark.parametrize(
    ("estado", "passed", "future"),
    [
        ("DRAFT", ("DRAFT",), ("CONFIRMED", "PICKING", "READY_FOR_DELIVERY", "CLOSED")),
        ("CONFIRMED", ("DRAFT", "CONFIRMED"), ("PICKING", "READY_FOR_DELIVERY", "CLOSED")),
        ("PICKING", ("DRAFT", "CONFIRMED", "PICKING"), ("READY_FOR_DELIVERY", "CLOSED")),
        (
            "READY_FOR_DELIVERY",
            ("DRAFT", "CONFIRMED", "PICKING", "READY_FOR_DELIVERY"),
            ("CLOSED",),
        ),
        (
            "CLOSED",
            ("DRAFT", "CONFIRMED", "PICKING", "READY_FOR_DELIVERY", "CLOSED"),
            (),
        ),
    ],
)
def test_order_state_diagram_colors_passed_and_future_states(estado, passed, future):
    """Passed and current main-path states are colored; future states stay gray."""
    html = order_state_diagram(estado)
    for state in passed:
        assert _is_colored(_pill_style(html, state))
    for state in future:
        assert not _is_colored(_pill_style(html, state))


def test_order_state_diagram_canceled_grays_path_and_highlights_badge():
    """A canceled order grays the whole main path and highlights the Canceled badge."""
    html = order_state_diagram("CANCELED")
    for state in ("DRAFT", "CONFIRMED", "PICKING", "READY_FOR_DELIVERY", "CLOSED"):
        assert not _is_colored(_pill_style(html, state))
    canceled_style = _pill_style(html, "CANCELED")
    assert _is_colored(canceled_style)
    assert "#dc2626" in canceled_style


@pytest.mark.parametrize("estado", ["", "WEIRD"])
def test_order_state_diagram_unknown_estado_renders_all_uncolored(estado):
    """Unknown or empty states render gray with no highlighted badge."""
    html = order_state_diagram(estado)
    for state in ("DRAFT", "CONFIRMED", "PICKING", "READY_FOR_DELIVERY", "CLOSED", "CANCELED"):
        assert not _is_colored(_pill_style(html, state))


def test_app_customer_orders_tab_has_state_progress_diagram():
    """The Customer Orders tab renders the order state progress diagram."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Pedidos de clientes")
    html_components = [c for c in _components_recursive(tab) if type(c).__name__ == "HTML"]
    assert len(html_components) == 1
    assert 'data-state="DRAFT"' in (html_components[0].value or "")
    markdown_values = [
        c.value for c in _components_recursive(tab) if type(c).__name__ == "Markdown"
    ]
    assert any("Progreso del estado del pedido" in (value or "") for value in markdown_values)


def test_app_customer_orders_tab_has_nested_consult_and_entry_subtabs():
    """The Customer Orders tab nests two sub-tabs: consult and manual entry."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Pedidos de clientes")
    nested_tabs = [
        c
        for c in _components_recursive(tab)
        if type(c).__name__ == "Tab" and getattr(c, "id", None) is not None
    ]
    assert [t.label for t in nested_tabs] == [
        "Consulta de pedidos",
        "Alta / Modificación de pedido",
    ]
    assert {t.id for t in nested_tabs} == {"orders-consult", "order-entry"}
    labels = _all_labels(tab)
    assert "Modificar la orden existente" in labels
    assert "Ignorar" in labels
    # The orders grid carries the DRAFT-only edit affordance column.
    orders_grid = next(
        c
        for c in _components_recursive(tab)
        if type(c).__name__ == "Dataframe" and c.label == "Pedidos de clientes"
    )
    assert orders_grid.headers[-1] == "Editar"


def _committed_order(session, *, estado: OrderEstado) -> Order:
    order = Order(customer_id=1, estado=estado)
    session.add(order)
    session.flush()
    session.commit()
    return order


def test_start_picking_action_commits_transition(shop_ctx):
    """La acción start picking transiciona y hace commit (patrón po.py)."""
    db_session = shop_ctx["session"]
    order = _committed_order(db_session, estado=OrderEstado.CONFIRMED)

    with SessionLocal() as session:
        message = start_picking_action(session, order.order_id)

    assert "→ Picking." in message
    with SessionLocal() as session:
        assert session.get(Order, order.order_id).estado is OrderEstado.PICKING


def test_fulfillment_chain_commits_to_closed_with_delivery_date(shop_ctx):
    """Confirmado → Picking → Ready → Closed; deliver guarda la fecha de entrega."""
    db_session = shop_ctx["session"]
    order = _committed_order(db_session, estado=OrderEstado.CONFIRMED)

    with SessionLocal() as session:
        assert "Picking" in start_picking_action(session, order.order_id)
    with SessionLocal() as session:
        assert "Ready" in complete_picking_action(session, order.order_id)
    with SessionLocal() as session:
        message = deliver_order_action(session, order.order_id)

    assert "→ Closed" in message
    with SessionLocal() as session:
        reloaded = session.get(Order, order.order_id)
        assert reloaded.estado is OrderEstado.CLOSED
        assert reloaded.delivery_date is not None  # the delivery date is stored


def test_cancel_action_releases_reservations_with_backoffice_actor(shop_ctx):
    """Cancelar desde Confirmado libera reservas; el actor del ajuste es backoffice."""
    db_session = shop_ctx["session"]
    order = _committed_order(db_session, estado=OrderEstado.CONFIRMED)
    db_session.add(
        StockReservation(
            sku="CLV-001",
            customer_id=1,
            order_id=order.order_id,
            cantidad=2,
            ttl_minutes=30,
            estado=ReservationEstado.ACTIVE,
        )
    )
    db_session.commit()

    with SessionLocal() as session:
        message = cancel_order_action(session, order.order_id)

    assert "cancelado" in message
    with SessionLocal() as session:
        reloaded = session.get(Order, order.order_id)
        assert reloaded.estado is OrderEstado.CANCELED
        reservation = session.scalar(
            select(StockReservation).where(StockReservation.order_id == order.order_id)
        )
        assert reservation.estado is ReservationEstado.RELEASED


def test_cancel_action_releases_auto_sourced_needs_and_cancels_the_empty_po(shop_ctx):
    """Cancelar desde backoffice libera la necesidad auto-sourced y cancela el PO vacío."""
    db_session = shop_ctx["session"]
    order = _committed_order(db_session, estado=OrderEstado.CONFIRMED)
    need = upsert_sourcing_need(db_session, order.order_id, "CLV-001", 3)
    po = accumulate_need(db_session, need, 1)
    db_session.commit()

    with SessionLocal() as session:
        message = cancel_order_action(session, order.order_id)

    assert "cancelado" in message
    with SessionLocal() as session:
        assert session.get(Order, order.order_id).estado is OrderEstado.CANCELED
        reloaded = session.get(SupplierPurchaseOrder, po.po_id)
        assert reloaded.estado is SupplierPurchaseOrderState.CANCELLED
        assert session.scalars(select(SupplierPurchaseOrderItem)).all() == []
        reloaded_need = session.get(SourcingNeed, need.need_id)
        assert reloaded_need.po_item_id is None  # detached: no phantom PO quantities


def test_cancel_action_restores_deducted_stock_with_audit(shop_ctx):
    """Cancelar desde Picking restaura stock y audita con actor backoffice."""
    db_session = shop_ctx["session"]
    order = _committed_order(db_session, estado=OrderEstado.PICKING)
    db_session.add(Inventory(sku_id="CLV-001", quantity_on_hand=8))
    db_session.add(
        StockReservation(
            sku="CLV-001",
            customer_id=1,
            order_id=order.order_id,
            cantidad=2,
            ttl_minutes=30,
            estado=ReservationEstado.CONVERTED,
        )
    )
    db_session.commit()

    with SessionLocal() as session:
        cancel_order_action(session, order.order_id)

    with SessionLocal() as session:
        assert (
            session.scalar(select(Inventory.quantity_on_hand).where(Inventory.sku_id == "CLV-001"))
            == 10
        )  # restored
        adjustment = session.scalar(select(StockAdjustment))
        assert adjustment is not None
        assert adjustment.reason == "order_cancelled"
        assert adjustment.actor == "backoffice"
        assert adjustment.delta == 2


def test_monitor_shows_all_six_states(shop_ctx):
    """El monitor muestra los seis estados del pedido."""
    db_session = shop_ctx["session"]
    for estado in OrderEstado:
        db_session.add(Order(customer_id=1, estado=estado))
    db_session.flush()
    db_session.commit()

    with SessionLocal() as session:
        rows = list_orders(session, sheets=SheetsWriter(gc=None, settings=get_settings()))

    assert {row["estado"] for row in rows} == {e.value for e in OrderEstado}


def _all_labels(block) -> set[object]:
    """Collect every descendant component label of a Blocks subtree.

    Gradio stores the visible text of a Button in ``value`` and the label of
    other components in ``label``; both are collected.
    """
    labels: set[object] = set()
    for child in getattr(block, "children", ()):
        if type(child).__name__ == "Button":
            labels.add(getattr(child, "value", None))
        else:
            labels.add(getattr(child, "label", None))
        labels |= _all_labels(child)
    return labels


def _components_recursive(block) -> list:
    """Collect every descendant component of a Blocks subtree (depth-first)."""
    found: list = []
    for child in getattr(block, "children", ()):
        found.append(child)
        found.extend(_components_recursive(child))
    return found


def test_app_customer_orders_tab_has_fulfillment_buttons():
    """El tab Customer Orders expone las cuatro acciones de cumplimiento."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Pedidos de clientes")
    labels = _all_labels(tab)
    assert "Iniciar preparación (Confirmed → Picking)" in labels
    assert "Completar preparación (Picking → Ready)" in labels
    assert "Entregar (Ready → Closed)" in labels
    assert "Cancelar pedido" in labels
    assert "Acciones disponibles para el pedido seleccionado" in labels


def test_app_customer_orders_tab_selects_rows_without_order_id_input():
    """El tab ya no tiene el input Order ID ni el botón de detalle; hay grilla de líneas."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Pedidos de clientes")
    labels = _all_labels(tab)
    assert "Order ID" not in labels
    assert "Show line detail" not in labels
    # The detail grid, diagram and action buttons remain.
    assert "Líneas del pedido" in labels
    assert "Acciones disponibles para el pedido seleccionado" in labels
    assert "Iniciar preparación (Confirmed → Picking)" in labels


def test_order_row_selected_returns_state_label_diagram_and_lines(shop_ctx):
    """Clicking a row yields the selected id, legal actions, diagram and frozen lines."""
    db_session = shop_ctx["session"]
    order = Order(
        customer_id=1,
        estado=OrderEstado.CONFIRMED,
        subtotal=Decimal("270.00"),
        total=Decimal("256.50"),
        conversion_pending=False,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="CLV-001",
            cantidad=2,
            base_price=Decimal("135.00"),
            final_price=Decimal("128.25"),
            adjustment=Decimal(0),
            name="Clavos Paris 2 Pulgadas",
            source="LOCAL",
            supplier="MSA",
            moneda="ARS",
            precio_original=Decimal("100.00"),
        )
    )
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="RAG-2",
            cantidad=1,
            base_price=Decimal("1100.00"),
            final_price=Decimal("1045.00"),
            adjustment=Decimal(0),
            name="RAG item",
            source="RAG",
            supplier="RS",
            moneda="USD",
            precio_original=None,
        )
    )
    db_session.commit()

    evt = SimpleNamespace(
        selected=True,
        index=[0, 0],
        row_value=[
            order.order_id,
            "Ferretería Don Juan",
            "CONFIRMED",
            "270.00",
            "256.50",
            False,
            "",  # "Editar" column: empty for non-DRAFT rows
        ],
    )
    selected_id, label, html, lines = _order_row_selected(evt)  # type: ignore[arg-type]

    assert selected_id == order.order_id
    assert "start_picking" in label
    assert _is_colored(_pill_style(html, "CONFIRMED"))
    assert not _is_colored(_pill_style(html, "PICKING"))
    assert lines == [
        [
            "CLV-001",
            "",  # LOCAL without a supplier mapping → blank provider code
            "Clavos Paris 2 Pulgadas",
            2,
            "ARS",
            "100.00",
            "0.35",  # catalog applied margin (LOCAL)
            "135.00",  # base price = AR$ list price
            "128.25",  # final after customer list discount
            "256.50",  # 128.25 × 2
        ],
        ["RAG-2", "RS", "RAG item", 1, "USD", "—", "—", "1100.00", "1045.00", "1045.00"],
    ]


@pytest.mark.parametrize(
    "evt",
    [
        SimpleNamespace(selected=False, row_value=[1, "cust", "DRAFT"]),
        SimpleNamespace(selected=True, row_value=None),
        SimpleNamespace(selected=True, row_value=[]),
    ],
)
def test_order_row_selected_deselection_returns_cleared_state(evt):
    """Deselecting a row (or an event without a row) clears the whole panel."""
    assert _order_row_selected(evt) == (  # type: ignore[arg-type]
        None,
        "Seleccioná un pedido.",
        order_state_diagram(""),
        [],
    )


# ------------------------------------------------ adoption tab (RAG search + adopt)

_RAG_PRODUCT = RagProduct(
    sku="AT-5044",
    name="Tornillo Autoperforante 8x1",
    provider="Mercado Mayorista",
    brand="Tornimax",
    price=125.5,
    currency="ARS",
    source_file="catalogo_amx.pdf",
    page=12,
    codigo_proveedor="MSA",
    node_id="node-1",
    fragment_id=3,
    categoria="Fijaciones",
)


class _FakeEmbedder:
    """Fixed 1536-dim vectors — the adoption use case only checks the dims."""

    def embed(self, texts):
        return [[0.1] * 1536 for _ in texts]


def test_build_app_adoption_tab_has_search_and_adopt_flow():
    """El tab Adoption (RAG) expone la búsqueda, la grilla y el botón de adoptar."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Adopción desde RAG")
    labels = _all_labels(tab)
    assert "Buscar en RAG" in labels
    assert "Resultados RAG" in labels
    assert "Adoptar seleccionado" in labels
    assert "Stock inicial" in labels


def test_app_adoption_search_maps_rows_and_state():
    """La búsqueda RAG renderiza las filas y conserva los resultados crudos."""
    client = SimpleNamespace(query=lambda text: (_RAG_PRODUCT,))
    rows, results, message = _adoption_search(client, "tornillo")  # type: ignore[arg-type]
    assert rows == [
        ["AT-5044", "Tornillo Autoperforante 8x1", "Tornimax", "Fijaciones", 125.5, "ARS"]
    ]
    assert results == (_RAG_PRODUCT,)
    assert "1 resultado(s)" in message


def test_app_adoption_search_empty_results_returns_message():
    """Sin resultados del RAG se muestra un mensaje y no hay filas."""
    client = SimpleNamespace(query=lambda text: ())
    rows, results, message = _adoption_search(client, "inexistente")  # type: ignore[arg-type]
    assert rows == []
    assert results == ()
    assert "Sin resultados" in message


def test_app_adoption_search_surfaces_rag_unavailability():
    """Un fallo del RAG se muestra en el estado sin romper el handler."""

    def boom(text: str):
        raise RagProductError("rag down")

    client = SimpleNamespace(query=boom)
    rows, results, message = _adoption_search(client, "tornillo")  # type: ignore[arg-type]
    assert rows == []
    assert results == ()
    assert message.startswith("Error: RAG no disponible")


def test_app_adoption_row_selected_maps_index():
    """El click en una fila mapea al índice del resultado crudo."""
    evt = SimpleNamespace(selected=True, index=[2])
    assert _adoption_row_selected(evt) == 2  # type: ignore[arg-type]
    deselect = SimpleNamespace(selected=False, index=[0])
    assert _adoption_row_selected(deselect) is None  # type: ignore[arg-type]


def test_app_adoption_confirm_adopts_selected_product(shop_ctx):
    """Adoptar el producto seleccionado crea el SKU con stock y provenance."""
    shop_ctx["session"].commit()
    message = _adoption_confirm((_RAG_PRODUCT,), 0, 1, _FakeEmbedder())
    assert message == "Adoptado: MSA-AT-5044"
    with SessionLocal() as session:
        product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-AT-5044"))
        assert product is not None
        inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-AT-5044"))
        assert inventory is not None
        assert inventory.quantity_on_hand == 1
    assert product.origen["rag"]["node_id"] == "node-1"


def test_app_adoption_confirm_surfaces_sku_collision(shop_ctx):
    """Adoptar el mismo producto dos veces avisa que el SKU ya existe."""
    shop_ctx["session"].commit()
    assert _adoption_confirm((_RAG_PRODUCT,), 0, 1, _FakeEmbedder()).startswith("Adoptado")
    message = _adoption_confirm((_RAG_PRODUCT,), 0, 1, _FakeEmbedder())
    assert "ya existe un producto con ese código" in message


def test_app_adoption_confirm_rejects_non_positive_stock(shop_ctx):
    """Un stock inicial no positivo se rechaza con mensaje de validación."""
    shop_ctx["session"].commit()
    for stock in (0, -2):
        message = _adoption_confirm((_RAG_PRODUCT,), 0, stock, _FakeEmbedder())
        assert "el stock inicial debe ser mayor que cero" in message
    with SessionLocal() as session:
        assert (
            session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "RAG-MSA-AT-5044"))
            is None
        )


def test_app_adoption_confirm_requires_selection(shop_ctx):
    """Sin búsqueda previa o sin fila seleccionada no se adopta nada."""
    shop_ctx["session"].commit()
    assert _adoption_confirm((), 0, 1, _FakeEmbedder()) == "Buscá productos en el RAG primero."
    assert (
        _adoption_confirm((_RAG_PRODUCT,), None, 1, _FakeEmbedder())
        == "Seleccioná un producto de la grilla."
    )


# ------------------------------------------------ catalog ingest tab (RAG PDF)


class _FakeCatalogRag:
    """RagProductClient stand-in for the catalog ingest/status flow."""

    def __init__(
        self,
        *,
        job_id: str = "job-123",
        job: RagJobStatus | None = None,
        ingest_error: str | None = None,
        job_error: str | None = None,
        documents: RagProviderDocuments | None = None,
        documents_error: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.job = job
        self.ingest_error = ingest_error
        self.job_error = job_error
        self.documents = documents
        self.documents_error = documents_error
        self.ingest_calls: list[dict] = []
        self.documents_calls: list[str] = []

    def ingest_catalog(
        self,
        *,
        filename,
        content,
        codigo_proveedor,
        nombre_proveedor,
        proveedor_id=None,
        documento_id=None,
        delete_scope="proveedor",
        start_page=1,
        max_pages=None,
        skip_pages=None,
        no_vision=False,
        marca=None,
    ):
        self.ingest_calls.append(
            {
                "filename": filename,
                "content": content,
                "codigo_proveedor": codigo_proveedor,
                "nombre_proveedor": nombre_proveedor,
                "proveedor_id": proveedor_id,
                "documento_id": documento_id,
                "delete_scope": delete_scope,
                "start_page": start_page,
                "max_pages": max_pages,
                "skip_pages": skip_pages,
                "no_vision": no_vision,
                "marca": marca,
            }
        )
        if self.ingest_error:
            raise RagProductError(self.ingest_error)
        return self.job_id

    def get_job(self, job_id: str) -> RagJobStatus:
        if self.job_error:
            raise RagProductError(self.job_error)
        assert self.job is not None
        return self.job

    def list_documents(self, codigo_proveedor: str) -> RagProviderDocuments:
        self.documents_calls.append(codigo_proveedor)
        if self.documents_error:
            raise RagProductError(self.documents_error)
        return self.documents or RagProviderDocuments(
            codigo_proveedor=codigo_proveedor, documents=()
        )


def test_app_catalog_ingest_requires_file():
    """Sin PDF subido no se lanza ninguna ingesta."""
    job_id, message = _catalog_ingest(_FakeCatalogRag(), None, 1, None, False)
    assert job_id is None
    assert "Subí el PDF" in message


def test_app_catalog_ingest_launches_job_and_returns_id(shop_ctx, tmp_path):
    """El upload lanza el job con code/business_name/id y retorna el job_id."""
    shop_ctx["session"].commit()
    pdf = tmp_path / "catalogo-mayorista.pdf"
    pdf.write_bytes(b"%PDF-fake")
    rag = _FakeCatalogRag(job_id="job-abc")
    job_id, message = _catalog_ingest(rag, SimpleNamespace(path=str(pdf)), 1, None, False)

    assert job_id == "job-abc"
    assert "Ingesta lanzada en modo reemplazo total (job job-abc)" in message
    call = rag.ingest_calls[0]
    assert call["filename"] == "catalogo-mayorista.pdf"
    assert call["content"] == b"%PDF-fake"
    assert call["codigo_proveedor"] == "MSA"
    assert call["nombre_proveedor"] == "Mayorista SA"
    assert call["proveedor_id"] == "1"
    assert call["documento_id"] is None
    assert call["delete_scope"] == "proveedor"


def test_app_catalog_ingest_incremental_requires_documento_id(tmp_path):
    """La ingesta incremental sin Documento / lista declarado no llama al RAG."""
    pdf = tmp_path / "catalogo.pdf"
    pdf.write_bytes(b"%PDF-fake")
    rag = _FakeCatalogRag(job_id="job-abc")
    job_id, message = _catalog_ingest(rag, SimpleNamespace(path=str(pdf)), 1, "", True)
    assert job_id is None
    assert "Documento / lista" in message
    assert not rag.ingest_calls  # the API was never called


def test_app_catalog_ingest_incremental_passes_documento_scope(shop_ctx, tmp_path):
    """La ingesta incremental envía documento_id y delete_scope='documento'."""
    shop_ctx["session"].commit()
    pdf = tmp_path / "catalogo.pdf"
    pdf.write_bytes(b"%PDF-fake")
    rag = _FakeCatalogRag(job_id="job-abc")
    job_id, message = _catalog_ingest(
        rag, SimpleNamespace(path=str(pdf)), 1, "  Lista General  ", True
    )

    assert job_id == "job-abc"
    assert "modo incremental" in message
    call = rag.ingest_calls[0]
    assert call["documento_id"] == "Lista General"  # normalized (strip) by the client
    assert call["delete_scope"] == "documento"


def test_app_catalog_ingest_surfaces_rag_unavailability(shop_ctx, tmp_path):
    """Un fallo del RAG muestra el error y no lanza ningún job."""
    shop_ctx["session"].commit()
    pdf = tmp_path / "catalogo.pdf"
    pdf.write_bytes(b"%PDF-fake")
    rag = _FakeCatalogRag(ingest_error="connection refused")
    job_id, message = _catalog_ingest(rag, SimpleNamespace(path=str(pdf)), 1, None, False)
    assert job_id is None
    assert message.startswith("Error: RAG no disponible")
    assert rag.ingest_calls  # the attempt happened; the launch failed


def test_app_catalog_ingest_defaults_are_neutral(shop_ctx, tmp_path):
    """Sin opciones avanzadas, el handler pasa los defaults neutros al cliente."""
    shop_ctx["session"].commit()
    pdf = tmp_path / "catalogo.pdf"
    pdf.write_bytes(b"%PDF-fake")
    rag = _FakeCatalogRag(job_id="job-abc")
    _catalog_ingest(
        rag,
        SimpleNamespace(path=str(pdf)),
        1,
        None,
        False,
        None,  # blank Gradio Number for start_page
        None,
        None,
        False,
        None,
    )
    assert rag.ingest_calls
    call = rag.ingest_calls[0]
    assert call["start_page"] == 1
    assert call["max_pages"] is None
    assert call["skip_pages"] is None
    assert call["no_vision"] is False
    assert call["marca"] is None


def test_app_catalog_ingest_rejects_invalid_skip_pages(tmp_path):
    """Un formato inválido de Páginas a saltar se bloquea antes de llamar a la API."""
    pdf = tmp_path / "catalogo.pdf"
    pdf.write_bytes(b"%PDF-fake")
    for bad in ("1,,x", "abc", "1-2,", "-3"):
        rag = _FakeCatalogRag(job_id="job-abc")
        job_id, message = _catalog_ingest(
            rag, SimpleNamespace(path=str(pdf)), 1, None, False, None, None, bad, False, None
        )
        assert job_id is None, bad
        assert "Páginas a saltar" in message, bad
        assert not rag.ingest_calls  # the API was never called


def test_app_catalog_ingest_rejects_reversed_skip_range(tmp_path):
    """Un rango invertido ('4-2') se rechaza antes de llamar a la API."""
    pdf = tmp_path / "catalogo.pdf"
    pdf.write_bytes(b"%PDF-fake")
    rag = _FakeCatalogRag(job_id="job-abc")
    job_id, message = _catalog_ingest(
        rag, SimpleNamespace(path=str(pdf)), 1, None, False, None, None, "1,4-2", False, None
    )
    assert job_id is None
    assert "no puede ser mayor" in message
    assert not rag.ingest_calls  # the API was never called


def test_app_catalog_ingest_passes_advanced_options_through(shop_ctx, tmp_path):
    """Las opciones avanzadas válidas viajan al cliente (marca normalizada)."""
    shop_ctx["session"].commit()
    pdf = tmp_path / "catalogo.pdf"
    pdf.write_bytes(b"%PDF-fake")
    rag = _FakeCatalogRag(job_id="job-abc")
    job_id, _message = _catalog_ingest(
        rag,
        SimpleNamespace(path=str(pdf)),
        1,
        None,
        False,
        3,  # start_page
        5,  # max_pages
        " 1-2,4 ",  # skip_pages (trimmed by the handler)
        True,  # no_vision
        "  BULON  ",  # marca (stripped by the handler)
    )
    assert job_id == "job-abc"
    call = rag.ingest_calls[0]
    assert call["start_page"] == 3
    assert call["max_pages"] == 5
    assert call["skip_pages"] == "1-2,4"
    assert call["no_vision"] is True
    assert call["marca"] == "BULON"


def test_app_catalog_ingest_rejects_invalid_start_page(tmp_path):
    """Una Página inicial menor a 1 se rechaza antes de llamar a la API."""
    pdf = tmp_path / "catalogo.pdf"
    pdf.write_bytes(b"%PDF-fake")
    rag = _FakeCatalogRag(job_id="job-abc")
    job_id, message = _catalog_ingest(
        rag, SimpleNamespace(path=str(pdf)), 1, None, False, 0, None, None, False, None
    )
    assert job_id is None
    assert "Página inicial" in message
    assert not rag.ingest_calls  # the API was never called


def test_app_catalog_job_status_without_job_prompts_first_launch():
    """Sin job lanzado se lo indica en lugar de consultar al RAG."""
    assert (
        _catalog_job_status(_FakeCatalogRag(), None)
        == "Todavía no se lanzó ninguna ingesta en esta sesión."
    )


def test_app_catalog_job_status_running_shows_progress():
    """PENDING/RUNNING se muestran como en proceso con el mensaje del servicio."""
    rag = _FakeCatalogRag(
        job=RagJobStatus(
            job_id="job-123", status="RUNNING", progress_message="Procesando Fases 0 a 3..."
        )
    )
    message = _catalog_job_status(rag, "job-123")
    assert "RUNNING" in message
    assert "Procesando Fases 0 a 3..." in message


def test_app_catalog_job_status_completed_shows_result_summary():
    """COMPLETED muestra el resumen que trae el payload del job."""
    rag = _FakeCatalogRag(
        job=RagJobStatus(
            job_id="job-123",
            status="COMPLETED",
            progress_message="Ingesta finalizada con éxito",
            result={"total_productos": 42},
        )
    )
    message = _catalog_job_status(rag, "job-123")
    assert "COMPLETED" in message
    assert "42" in message


def test_app_catalog_job_status_failed_shows_error_detail():
    """FAILED expone el detalle de error del servicio."""
    rag = _FakeCatalogRag(job=RagJobStatus(job_id="job-123", status="FAILED", error="OCR explode"))
    message = _catalog_job_status(rag, "job-123")
    assert "FAILED" in message
    assert "OCR explode" in message


def test_app_catalog_job_status_surfaces_rag_unavailability():
    """Un fallo al consultar el job se muestra como error honesto."""
    message = _catalog_job_status(_FakeCatalogRag(job_error="HTTP 404"), "job-123")
    assert message.startswith("Error: RAG no disponible")


# ------------------------------------------------ provider documents dropdown


def test_build_app_catalog_tab_documento_dropdown_defaults_to_lista_general():
    """[dropdown default] El dropdown Documento / lista nace con 'LISTA GENERAL'."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Ingesta de catálogo")
    stack = list(tab.children)
    dropdowns = []
    while stack:
        child = stack.pop()
        if type(child).__name__ == "Dropdown" and child.label == "Documento / lista":
            dropdowns.append(child)
        stack.extend(getattr(child, "children", []) or [])
    assert len(dropdowns) == 1
    assert dropdowns[0].value == "LISTA GENERAL"


def test_app_load_provider_documents_populates_choices(shop_ctx):
    """[dropdown R1] Proveedor conocido: el dropdown se llena con sus documento_id."""
    shop_ctx["session"].commit()
    rag = _FakeCatalogRag(
        documents=RagProviderDocuments(
            codigo_proveedor="MSA",
            documents=(
                RagDocumentSummary(documento_id="LISTA GENERAL", total_productos=42),
                RagDocumentSummary(documento_id="OFERTAS", total_productos=7),
            ),
        )
    )
    update = _load_provider_documents(rag, 1)
    assert rag.documents_calls == ["MSA"]  # resolved supplier id → code, like _catalog_ingest
    assert update["choices"] == ["LISTA GENERAL", "OFERTAS"]
    # Reset to the default instead of clearing: "LISTA GENERAL" is among the
    # fetched choices, so the dropdown selects that item.
    assert update["value"] == "LISTA GENERAL"


def test_app_load_provider_documents_surfaces_graceful_empty_update_on_rag_failure(shop_ctx):
    """[dropdown R1] Un fallo del RAG degrada a un update vacío; nunca crashea la UI."""
    shop_ctx["session"].commit()
    rag = _FakeCatalogRag(documents_error="connection refused")
    update = _load_provider_documents(rag, 1)
    assert rag.documents_calls == ["MSA"]  # the attempt happened
    assert update["choices"] == []
    # Even on failure the value resets to the default (allow_custom_value keeps
    # it typeable, so the ingest stays tagged).
    assert update["value"] == "LISTA GENERAL"


def test_app_load_provider_documents_empty_provider_returns_empty_update(shop_ctx):
    """[dropdown R1] Proveedor sin documentos: choices vacíos, sin error."""
    shop_ctx["session"].commit()
    rag = _FakeCatalogRag(documents=None)  # fake defaults to an empty documents tuple
    update = _load_provider_documents(rag, 1)
    assert update["choices"] == []
    # Default not among the (empty) choices, but allow_custom_value keeps it selected.
    assert update["value"] == "LISTA GENERAL"


def test_app_load_provider_documents_unknown_supplier_skips_rag_call(shop_ctx):
    """[dropdown R1] Proveedor desconocido no consulta el RAG y devuelve choices vacíos."""
    shop_ctx["session"].commit()
    rag = _FakeCatalogRag()
    update = _load_provider_documents(rag, 999)
    assert rag.documents_calls == []
    assert update["choices"] == []


def test_app_load_provider_documents_without_supplier_returns_empty_update():
    """[dropdown R1] Sin proveedor seleccionado (None) se devuelve un update vacío."""
    rag = _FakeCatalogRag()
    update = _load_provider_documents(rag, None)
    assert rag.documents_calls == []
    assert update["choices"] == []
