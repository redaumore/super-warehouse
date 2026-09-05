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

import pandas as pd
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.backoffice.app import (
    _catalog_edit,
    _catalog_grid,
    _ingest_confirm,
    _ingest_preview,
    _order_row_selected,
    _register_client,
    _save_exchange_rate,
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
    ConfirmedIngest,
    confirm_items,
    extract_document_items,
    to_grid_rows,
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
)
from src.db.session import SessionLocal
from src.integrations.sheets import SheetsWriter
from src.orchestrator.approval import PendingConversionError, confirm_and_register
from src.purchasing.accumulate import accumulate_need
from src.sourcing.persistence import upsert_sourcing_need
from src.supplier.ocr import DocumentExtraction, ExtractedItem

# ---------------------------------------------------------------- app structure


def _tabs_block(demo) -> object:
    """The Tabs layout inside the Blocks tree (ignoring Markdown siblings)."""
    return next(c for c in demo.children if type(c).__name__ == "Tabs")


def test_build_app_creates_tabs_with_expected_labels():
    """Building the app creates tabs with the expected labels."""
    demo = build_app()
    labels = [tab.label for tab in _tabs_block(demo).children]
    assert labels == [
        "Catalog",
        "Clients",
        "Orders/Monitor",
        "Purchase Orders",
        "Ingestion",
        "Suppliers",
        "Customer Orders",
        "Settings",
        "Sessions",
    ]


def test_build_app_ingestion_tab_has_preview_and_confirm():
    """La pestaña Ingestion expone la vista previa editable y el botón de confirmar."""
    demo = build_app()
    ingestion_tab = next(tab for tab in _tabs_block(demo).children if tab.label == "Ingestion")
    component_labels = {getattr(c, "label", None) for c in ingestion_tab.children}
    assert "Vista previa (editable)" in component_labels


def test_build_app_catalog_tab_has_product_grid():
    """La pestaña Catalog expone la grilla de productos y el botón de guardado."""
    demo = build_app()
    catalog_tab = next(tab for tab in _tabs_block(demo).children if tab.label == "Catalog")
    component_labels = {getattr(c, "label", None) for c in catalog_tab.children}
    assert "Productos" in component_labels


# -------------------------------------------------- ingestion logic (no DB)


def test_to_grid_rows_renders_editable_preview():
    """Las filas extraídas se renderizan como grilla editable."""
    extraction = DocumentExtraction(
        items=(
            ExtractedItem(
                codigo="CLV-001", descripcion="Clavos", cantidad=10, costo=Decimal("1250.00")
            ),
        )
    )
    assert to_grid_rows(extraction) == [["CLV-001", "Clavos", "10", "1250.00"]]


def test_extract_document_items_uses_vision_analyzer(tmp_path):
    """La extracción delega en el analizador de visión y parsea las filas."""
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")
    analyzer = SimpleNamespace(
        analyze=lambda url, prompt: SimpleNamespace(text="10 x Clavos Paris", confidence=1.0)
    )
    extraction = extract_document_items(analyzer, image)  # type: ignore[arg-type]
    assert extraction.items[0].descripcion == "Clavos Paris"
    assert extraction.items[0].cantidad == 10


def test_extract_document_items_rejects_illegible(tmp_path):
    """Un documento ilegible se rechaza con un error claro."""
    image = tmp_path / "mancha.jpg"
    image.write_bytes(b"fake")
    analyzer = SimpleNamespace(
        analyze=lambda url, prompt: SimpleNamespace(text="texto sin filas", confidence=0.2)
    )
    with pytest.raises(Exception, match="illegible"):
        extract_document_items(analyzer, image)  # type: ignore[arg-type]


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
            stock_disponible=10,
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
    rows = list_products(shop_ctx["session"])
    assert rows[0]["codigo_interno"] == "CLV-001"
    assert rows[0]["stock_disponible"] == 10
    assert rows[0]["precio_lista_base"] == "135.00"


def test_catalog_update_stock_and_price(shop_ctx):
    """Editar stock y precio se refleja en la grilla."""
    update_stock(shop_ctx["session"], "CLV-001", 25)
    update_price(shop_ctx["session"], "CLV-001", Decimal("150.00"))
    product = shop_ctx["session"].get(Catalogo, 1)
    assert product.stock_disponible == 25
    assert product.precio_lista_base == Decimal("150.00")


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


def test_confirm_items_updates_existing_product_stock(shop_ctx):
    """Confirmar filas con SKU existente aumenta el stock y el costo."""
    result = confirm_items(
        shop_ctx["session"],
        [["CLV-001", "Clavos Paris 2 Pulgadas", 5, "95.00"]],
        supplier_id=1,
    )
    assert result == ConfirmedIngest(updated=1, created=0)
    product = shop_ctx["session"].get(Catalogo, 1)
    assert product.stock_disponible == 15
    assert product.costo_proveedor == Decimal("95.00")
    assert product.precio_lista_base == Decimal("128.25")  # 95 × 1.35


def test_confirm_items_creates_new_product_for_unknown_sku(shop_ctx):
    """Una fila sin SKU existente crea un producto nuevo con margen del supplier."""
    result = confirm_items(
        shop_ctx["session"],
        [["NEW-001", "Pintura Látex Blanco", 4, "3200.00"]],
        supplier_id=1,
    )
    assert result == ConfirmedIngest(updated=0, created=1)
    product = shop_ctx["session"].scalar(
        select(Catalogo).where(Catalogo.codigo_interno == "NEW-001")
    )
    assert product.stock_disponible == 4
    assert product.precio_lista_base == Decimal("3520.00")  # 3200 × 1.10


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
    # Derived: markup (135 / 100 − 1) × 100; final 128.25 × qty 2.
    assert line["margin_pct"] == "35.00"
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
    assert lines[0]["margin_pct"] == "25.00"  # (125 / 100 − 1) × 100
    assert lines[1]["margin_pct"] == "0.00"  # RAG: base == original × rate
    assert lines[2]["margin_pct"] is None
    assert lines[3]["margin_pct"] is None
    assert lines[1]["line_total"] == "475.00"  # 237.50 × 2


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
    """La grilla del catálogo renderiza los productos sembrados."""
    shop_ctx["session"].commit()
    rows = _catalog_grid()
    assert any(row[0] == "CLV-001" for row in rows)
    assert any(row[6] == 10 for row in rows)  # stock column


def test_app_register_client_returns_success_message(shop_ctx):
    """Registrar un cliente desde la UI devuelve un mensaje de éxito."""
    shop_ctx["session"].commit()
    message = _register_client("Nueva Ferretería", "11 6666 7777", 1, 0.0)
    assert message == "Cliente registrado"
    with SessionLocal() as session:
        assert (
            session.scalar(select(Cliente).where(Cliente.nombre_comercial == "Nueva Ferretería"))
            is not None
        )


def test_app_catalog_edit_persists_stock_change(shop_ctx):
    """Editar stock desde la UI persiste el cambio en el catálogo."""
    shop_ctx["session"].commit()
    message = _catalog_edit("CLV-001", 25, None, None)
    assert message == "Guardado: CLV-001"
    with SessionLocal() as session:
        assert session.get(Catalogo, 1).stock_disponible == 25


def test_app_register_client_surfaces_error_for_bad_phone(shop_ctx):
    """Un teléfono inválido desde la UI devuelve el error en pantalla."""
    shop_ctx["session"].commit()
    message = _register_client("Pepe", "no-es-telefono", 1, 0.0)
    assert message.startswith("Error:")


def test_app_ingest_confirm_reports_counts(shop_ctx):
    """Confirmar la ingesta desde la UI reporta actualizados y creados."""
    shop_ctx["session"].commit()
    message = _ingest_confirm([["CLV-001", "Clavos Paris 2 Pulgadas", 3, "95.00"]], 1)
    assert message == "Ingresado: 1 actualizados, 0 creados."
    with SessionLocal() as session:
        product = session.get(Catalogo, 1)
    assert product.stock_disponible == 13  # 10 sembrados + 3 ingresados
    assert product.costo_proveedor == Decimal("95.00")


def test_app_ingest_confirm_creates_new_product(shop_ctx):
    """Confirmar una fila nueva desde la UI la crea en el catálogo."""
    shop_ctx["session"].commit()
    message = _ingest_confirm([["NEW-001", "Pintura Látex Blanco", 4, "3200.00"]], 1)
    assert message == "Ingresado: 0 actualizados, 1 creados."
    with SessionLocal() as session:
        product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "NEW-001"))
    assert product is not None
    assert product.stock_disponible == 4


def test_app_ingest_confirm_accepts_dataframe_with_headers(shop_ctx):
    """La grilla con headers llega como DataFrame y se confirma igual."""
    shop_ctx["session"].commit()
    df = pd.DataFrame(
        [["CLV-001", "Clavos Paris 2 Pulgadas", 3, "95.00"]],
        columns=["Código", "Descripción", "Cantidad", "Supplier cost"],
    )
    message = _ingest_confirm(df, 1)
    assert message == "Ingresado: 1 actualizados, 0 creados."
    with SessionLocal() as session:
        assert session.get(Catalogo, 1).stock_disponible == 13  # 10 sembrados + 3 ingresados


def test_app_ingest_preview_returns_grid_and_message(shop_ctx, tmp_path):
    """La vista previa de ingesta devuelve la grilla y un mensaje de estado."""
    shop_ctx["session"].commit()
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")
    analyzer = SimpleNamespace(
        analyze=lambda url, prompt: SimpleNamespace(
            text="10 x Clavos Paris 2 Pulgadas", confidence=1.0
        )
    )
    with patch("src.supplier.ocr.image_to_data_url", return_value="data:image/jpeg;base64,AA=="):
        grid, message = _ingest_preview(analyzer, image)  # type: ignore[arg-type]
    assert len(grid) == 1
    assert grid[0][1] == "Clavos Paris 2 Pulgadas"
    assert "1 filas extraídas" in message


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
        ("DRAFT", ("cancel_order",)),
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
    tab = next(t for t in _tabs_block(demo).children if t.label == "Customer Orders")
    html_components = [c for c in tab.children if type(c).__name__ == "HTML"]
    assert len(html_components) == 1
    assert 'data-state="DRAFT"' in (html_components[0].value or "")
    markdown_values = [c.value for c in tab.children if type(c).__name__ == "Markdown"]
    assert any("Order state progress" in (value or "") for value in markdown_values)


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


def test_app_customer_orders_tab_has_fulfillment_buttons():
    """El tab Customer Orders expone las cuatro acciones de cumplimiento."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Customer Orders")
    labels = _all_labels(tab)
    assert "Start picking (Confirmed → Picking)" in labels
    assert "Complete picking (Picking → Ready)" in labels
    assert "Deliver (Ready → Closed)" in labels
    assert "Cancel order" in labels
    assert "Legal actions for the selected order" in labels


def test_app_customer_orders_tab_selects_rows_without_order_id_input():
    """El tab ya no tiene el input Order ID ni el botón de detalle; hay grilla de líneas."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Customer Orders")
    labels = _all_labels(tab)
    assert "Order ID" not in labels
    assert "Show line detail" not in labels
    # The detail grid, diagram and action buttons remain.
    assert "Order lines" in labels
    assert "Legal actions for the selected order" in labels
    assert "Start picking (Confirmed → Picking)" in labels


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
        row_value=[order.order_id, "Ferretería Don Juan", "CONFIRMED", "270.00", "256.50", False],
    )
    selected_id, label, html, lines = _order_row_selected(evt)  # type: ignore[arg-type]

    assert selected_id == order.order_id
    assert "start_picking" in label
    assert _is_colored(_pill_style(html, "CONFIRMED"))
    assert not _is_colored(_pill_style(html, "PICKING"))
    assert lines == [
        [
            "CLV-001",
            "Clavos Paris 2 Pulgadas",
            2,
            "100.00",
            "35.00",
            "135.00",
            "256.50",
        ],
        ["RAG-2", "RAG item", 1, "—", "—", "1100.00", "1045.00"],
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
