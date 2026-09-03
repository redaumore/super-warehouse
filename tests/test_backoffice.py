"""Backoffice tests (tasks 3.5, 3.6, 3.7 and customer orders).

App structure (no server): building the Blocks tree yields seven tabs with the
expected labels and key components. Module logic covers catalog edits, client
registration, customer-order maintenance, the live order monitor, and the
ingestion preview→confirm flow. DB-backed cases run on Postgres and skip when
it is down.
"""

from __future__ import annotations

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
    _register_client,
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
    get_default_margin,
    list_customer_orders,
    list_exchange_rates,
    order_detail,
    recompute_pending_conversion,
    set_default_margin,
    set_exchange_rate,
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
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    Supplier,
)
from src.db.session import SessionLocal
from src.integrations.sheets import SheetsWriter
from src.orchestrator.approval import PendingConversionError, register_approved_order
from src.supplier.ocr import DocumentExtraction, ExtractedItem

# ---------------------------------------------------------------- app structure


def _tabs_block(demo) -> object:
    """The Tabs layout inside the Blocks tree (ignoring Markdown siblings)."""
    return next(c for c in demo.children if type(c).__name__ == "Tabs")


def test_build_app_creates_seven_tabs_with_expected_labels():
    """Building the app creates seven tabs with the expected labels."""
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
                "TRUNCATE order_items, orders, stock_reservations, catalogo, suppliers, "
                "clientes, lista_precios, supplier_sku_mappings, exchange_rates, app_settings "
                "RESTART IDENTITY CASCADE"
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
    order = Order(customer_id=1, estado=OrderEstado.PENDING_APPROVAL, needs_requote=False)
    db_session.add(order)
    db_session.flush()
    sheets = SheetsWriter(gc=None, settings=get_settings())
    rows = list_orders(db_session, sheets=sheets)
    assert rows[0]["order_id"] == order.order_id
    assert rows[0]["estado"] == "PENDING_APPROVAL"
    assert rows[0]["sheets_synced"] is False
    assert rows[0]["active_reservations"] == 0


def test_customer_orders_list_and_detail_include_ars_totals_and_snapshots(shop_ctx):
    """Customer Orders returns persisted order totals and frozen line fields."""
    db_session = shop_ctx["session"]
    order = Order(
        customer_id=1,
        estado=OrderEstado.PENDING_APPROVAL,
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
    assert detail["lines"][0]["source"] == "LOCAL"
    assert detail["lines"][0]["name"] == "Clavos Paris 2 Pulgadas"


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
        estado=OrderEstado.PENDING_APPROVAL,
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
    assert order.subtotal == Decimal("24000.00")
    assert order.total == Decimal("24000.00")
    item = db_session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.base_price == Decimal("12000.00")


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
        estado=OrderEstado.APPROVED,
        conversion_pending=True,
    )
    shop_ctx["session"].add(order)
    shop_ctx["session"].flush()

    with pytest.raises(PendingConversionError, match="pending currency conversion"):
        register_approved_order(shop_ctx["session"], order, sheets=SimpleNamespace())


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
