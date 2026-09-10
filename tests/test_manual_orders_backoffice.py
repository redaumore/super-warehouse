"""Manual customer-order creation ("alta manual") and the backoffice confirm.

``create_manual_order`` (src/sourcing/draft_order.py) is the backoffice
counterpart of the chat draft: it resolves the customer, prices catalog lines
with ``compute_order``, and persists the DRAFT via ``persist_draft_order`` with
the same snapshot contract — no reservations, no Sheets. The one-DRAFT-per-
customer rule is enforced app-side and the DB partial index translates its
race ``IntegrityError`` into the same domain error. The backoffice confirm
action reuses the conversation ceremony (``confirm_and_register``) unchanged.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from gradio import skip
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError

from src.backoffice.app import (
    _client_dropdown_choices,
    _create_manual_order,
    _customer_orders_grid,
    _manual_line_selected,
    _manual_order_add_line,
    _manual_order_remove_line,
    _order_edit_request,
    _order_edit_selected,
)
from src.backoffice.customer_orders import (
    confirm_order_action,
    create_manual_order_action,
    is_editable_order,
    legal_actions,
    open_draft_for_customer,
)
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    Inventory,
    ListaPrecios,
    Order,
    OrderEstado,
    OrderItem,
    ReservationEstado,
    StockReservation,
    Supplier,
)
from src.db.session import SessionLocal
from src.orchestrator.approval import PendingConversionError, SheetsWriteStatus
from src.pricing.order_pricing import MissingRateError
from src.sourcing.draft_order import ManualOrderError, create_manual_order


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


@pytest.fixture
def shop_ctx(db_session):
    """Seed two price lists, two customers, a supplier, a product and inventory."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        ListaPrecios(lista_id=2, nombre="Gremio A", descuento_lista_pct=Decimal("10.00"))
    )
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
        Cliente(
            customer_id=2,
            nombre_comercial="Corralón Gremio",
            telefono_norm="+5491155555678",
            lista_precios_id=2,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.add(
        Supplier(id=1, code="SUP", business_name="Supplier", default_margin_pct=Decimal(0))
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="LOCAL-1",
            supplier_id=1,
            nombre_oficial="Local item",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            sinonimos=[],
        )
    )
    db_session.add(Inventory(sku_id="LOCAL-1", quantity_on_hand=10))
    db_session.flush()
    return db_session


class _FakeSheets:
    """SheetsPort stand-in that records the appended order row."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def append_order_row(
        self,
        order_id: int,
        *,
        customer_name: str | None = None,
        total: str | None = None,
        items_summary: str = "",
    ) -> SheetsWriteStatus:
        self.calls.append(
            {
                "order_id": order_id,
                "customer_name": customer_name,
                "total": total,
                "items_summary": items_summary,
            }
        )
        return SheetsWriteStatus.APPENDED


# ------------------------------------------------- create_manual_order (domain)


def test_create_manual_order_persists_priced_draft_with_snapshots(shop_ctx):
    """The manual draft is priced from the catalog and snapshots every line."""
    session = shop_ctx
    order = create_manual_order(session, 1, [("LOCAL-1", 2)])

    assert order.estado is OrderEstado.DRAFT
    assert order.conversion_pending is False
    assert order.subtotal == Decimal("270.00")
    assert order.total == Decimal("270.00")
    item = session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.sku == "LOCAL-1"
    assert item.cantidad == 2
    assert item.source == "LOCAL"
    assert item.name == "Local item"
    assert item.moneda == "ARS"
    assert item.supplier == "SUP"
    assert item.precio_original == Decimal("100.0000")
    assert item.base_price == Decimal("135.00")  # 100 × 1.35
    assert item.final_price == Decimal("135.00")  # Base list: no discount
    # Persist never reserves: stock is soft-locked at the confirm ceremony.
    assert session.scalars(select(StockReservation)).all() == []


def test_create_manual_order_applies_customer_list_discount(shop_ctx):
    """The customer's price-list discount lands on final_price and the total."""
    session = shop_ctx
    order = create_manual_order(session, 2, [("LOCAL-1", 2)])

    item = session.scalar(select(OrderItem).where(OrderItem.order_id == order.order_id))
    assert item.base_price == Decimal("135.00")
    assert item.final_price == Decimal("121.50")  # 135 × 0.90
    assert order.subtotal == Decimal("270.00")
    assert order.total == Decimal("243.00")


@pytest.mark.parametrize("lines", [[], [("LOCAL-1", 0)], [("LOCAL-1", -1)]])
def test_create_manual_order_rejects_invalid_input_without_writing(shop_ctx, lines):
    """Empty line lists and non-positive quantities are refused with no writes."""
    session = shop_ctx
    with pytest.raises(ManualOrderError):
        create_manual_order(session, 1, lines)
    assert session.scalar(select(func.count(Order.order_id))) == 0


def test_create_manual_order_rejects_unknown_sku(shop_ctx):
    """An SKU missing from the catalog is a domain error, not a partial write."""
    session = shop_ctx
    with pytest.raises(ManualOrderError, match="unknown SKU: NOPE-9"):
        create_manual_order(session, 1, [("LOCAL-1", 1), ("NOPE-9", 2)])
    assert session.scalar(select(func.count(Order.order_id))) == 0


def test_create_manual_order_rejects_unknown_customer(shop_ctx):
    """An unknown customer id fails clearly before anything is written."""
    with pytest.raises(ManualOrderError, match="unknown customer: 999"):
        create_manual_order(shop_ctx, 999, [("LOCAL-1", 1)])


def test_create_manual_order_enforces_one_draft_per_customer(shop_ctx):
    """A second DRAFT for the same customer is refused; the first survives."""
    session = shop_ctx
    first = create_manual_order(session, 1, [("LOCAL-1", 2)])
    session.commit()

    with pytest.raises(ManualOrderError, match="already has an open draft"):
        create_manual_order(session, 1, [("LOCAL-1", 1)])
    session.rollback()

    orders = session.scalars(select(Order)).all()
    assert len(orders) == 1
    assert orders[0].order_id == first.order_id
    assert orders[0].estado is OrderEstado.DRAFT


def test_create_manual_order_translates_missing_rate_into_domain_error(shop_ctx):
    """A missing exchange rate surfaces as the friendly ManualOrderError."""
    with (
        patch(
            "src.sourcing.draft_order.compute_order",
            side_effect=MissingRateError("USD"),
        ),
        pytest.raises(ManualOrderError, match="exchange rate"),
    ):
        create_manual_order(shop_ctx, 1, [("LOCAL-1", 1)])
    assert shop_ctx.scalar(select(func.count(Order.order_id))) == 0


def test_create_manual_order_action_commits(shop_ctx):
    """The backoffice wrapper commits: a fresh session sees the draft."""
    session = shop_ctx
    order = create_manual_order_action(session, 1, [("LOCAL-1", 2)])

    with SessionLocal() as fresh:
        reloaded = fresh.get(Order, order.order_id)
        assert reloaded.estado is OrderEstado.DRAFT
        assert reloaded.total == Decimal("270.00")


# ------------------------------------------------ confirm_order_action (backoffice)


def test_confirm_order_action_reserves_confirms_and_deducts(shop_ctx):
    """The backoffice confirm runs the full ceremony and commits it."""
    session = shop_ctx
    order = Order(customer_id=1, estado=OrderEstado.DRAFT)
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            order_id=order.order_id,
            sku="LOCAL-1",
            cantidad=2,
            base_price=Decimal("135.00"),
            final_price=Decimal("135.00"),
            adjustment=Decimal(0),
            name="Local item",
            source="LOCAL",
            supplier="SUP",
            moneda="ARS",
            precio_original=Decimal("100.00"),
        )
    )
    session.commit()

    sheets = _FakeSheets()
    with SessionLocal() as action_session:
        message = confirm_order_action(action_session, order.order_id, sheets=sheets)

    assert "confirmado" in message
    assert "243.00" not in message  # total is 2 × 135
    assert len(sheets.calls) == 1
    call = sheets.calls[0]
    assert call["order_id"] == order.order_id
    assert call["customer_name"] == "Ferretería Don Juan"
    assert call["total"] == "270.00"
    assert call["items_summary"] == "2 × LOCAL-1"

    with SessionLocal() as fresh:
        reloaded = fresh.get(Order, order.order_id)
        assert reloaded.estado is OrderEstado.CONFIRMED
        reservation = fresh.scalar(
            select(StockReservation).where(StockReservation.order_id == order.order_id)
        )
        assert reservation is not None
        assert reservation.estado is ReservationEstado.CONVERTED
        assert (
            fresh.scalar(select(Inventory.quantity_on_hand).where(Inventory.sku_id == "LOCAL-1"))
            == 8
        )  # 10 − 2


def test_confirm_order_action_blocked_on_pending_conversion(shop_ctx):
    """A conversion-pending draft cannot be confirmed from the backoffice."""
    session = shop_ctx
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, conversion_pending=True)
    session.add(order)
    session.commit()

    with (
        SessionLocal() as action_session,
        pytest.raises(PendingConversionError, match="pending currency conversion"),
    ):
        confirm_order_action(action_session, order.order_id, sheets=_FakeSheets())


# ----------------------------------------------------- UI-level logic (no Gradio)


def test_legal_actions_draft_includes_confirm():
    """A DRAFT offers confirm before cancel in the legal-actions label."""
    assert legal_actions("DRAFT") == ("confirm_order", "cancel_order")


def test_manual_order_add_line_appends_accumulates_and_validates():
    """Add appends lines, accumulates repeated (SKU, origen), and rejects bad input."""
    rows, grid, sku, qty, status = _manual_order_add_line([], "LOCAL-1", 2, "LOCAL")
    assert rows == [["LOCAL-1", 2, "LOCAL"]]
    assert grid == [["LOCAL-1", 2, "LOCAL"]]
    assert (sku, qty) == ("", 1.0)
    assert "agregada" in status

    rows, _grid, _sku, _qty, status = _manual_order_add_line(rows, "LOCAL-1", 1, "LOCAL")
    assert rows == [["LOCAL-1", 3, "LOCAL"]]  # same (SKU, origen) accumulates

    rows, _grid, _sku, _qty, status = _manual_order_add_line(rows, "LOCAL-1", 4, "RAG")
    assert rows == [["LOCAL-1", 3, "LOCAL"], ["LOCAL-1", 4, "RAG"]]  # per-source lines

    for bad_sku, bad_qty, expected in (
        ("", 2, "SKU"),
        ("LOCAL-1", 0, "mayor que cero"),
        ("LOCAL-1", "x", "cantidad válida"),
    ):
        rows, _grid, _sku, _qty, status = _manual_order_add_line(rows, bad_sku, bad_qty, "LOCAL")
        assert expected in status
        assert len(rows) == 2  # invalid input never mutates the lines


def test_manual_order_remove_line_uses_stored_selection():
    """The stored grid selection drives the removal; no selection is a no-op."""
    rows = [["LOCAL-1", 2, "LOCAL"], ["LOCAL-2", 1, "LOCAL"]]
    assert _manual_line_selected(SimpleNamespace(selected=True, index=[0])) == 0  # type: ignore[arg-type]
    assert _manual_line_selected(SimpleNamespace(selected=False, index=[0])) is None  # type: ignore[arg-type]

    state, grid, status = _manual_order_remove_line(0, rows)
    assert state == [["LOCAL-2", 1, "LOCAL"]]
    assert grid == [["LOCAL-2", 1, "LOCAL"]]
    assert "quitada" in status

    state, _grid, status = _manual_order_remove_line(None, rows)
    assert state == rows
    assert "Seleccioná una línea" in status

    state, _grid, _status = _manual_order_remove_line(99, rows)
    assert state == rows  # out-of-range selection never mutates


def test_app_create_manual_order_creates_committed_draft(shop_ctx):
    """The create handler commits the draft and clears the lines form."""
    shop_ctx.commit()
    message, orders_grid, lines, banner_visible, banner_text = _create_manual_order(
        1, [["LOCAL-1", 2]]
    )

    assert "creado (borrador)" in message
    assert "270.00" in message
    assert any("Ferretería Don Juan" in str(row[1]) for row in orders_grid)
    assert lines == []  # form cleared
    assert banner_visible is False  # no draft warning: the draft was just created
    assert banner_text == ""
    with SessionLocal() as session:
        order = session.scalar(select(Order))
        assert order is not None
        assert order.estado is OrderEstado.DRAFT
        assert order.total == Decimal("270.00")


def test_app_create_manual_order_intercepts_existing_draft(shop_ctx):
    """A second draft attempt is intercepted: warning banner, no creation."""
    shop_ctx.commit()
    first_message, _grid, _lines, _visible, _text = _create_manual_order(
        1, [["LOCAL-1", 2, "LOCAL"]]
    )
    assert "creado (borrador)" in first_message

    message, _grid, lines, banner_visible, banner_text = _create_manual_order(
        1, [["LOCAL-1", 1, "LOCAL"]]
    )

    assert "no se creó uno nuevo" in message
    assert lines == [["LOCAL-1", 1, "LOCAL"]]  # form intact for the fix
    assert banner_visible is True
    assert "borrador #" in banner_text
    with SessionLocal() as session:
        assert session.scalar(select(func.count(Order.order_id))) == 1  # still one draft


def test_app_create_manual_order_requires_client(shop_ctx):
    """Without a client selected nothing is created."""
    shop_ctx.commit()
    message, _grid, lines, banner_visible, _text = _create_manual_order(
        None, [["LOCAL-1", 2, "LOCAL"]]
    )
    assert message == "Seleccioná un cliente."
    assert lines == [["LOCAL-1", 2, "LOCAL"]]
    assert banner_visible is False
    assert shop_ctx.scalar(select(func.count(Order.order_id))) == 0


def test_client_dropdown_choices_lists_clients(shop_ctx):
    """The client dropdown lists every client by name with the id as value."""
    shop_ctx.commit()
    assert _client_dropdown_choices() == [
        ("Corralón Gremio", 2),
        ("Ferretería Don Juan", 1),
    ]


# ------------------------------------------------- grid edit affordance (DRAFT only)


def test_customer_orders_grid_marks_only_drafts_editable(shop_ctx):
    """The "Editar" column carries ✏️ for DRAFT rows and stays empty otherwise."""
    session = shop_ctx
    draft = create_manual_order(session, 1, [("LOCAL-1", 2)])
    session.add(Order(customer_id=2, estado=OrderEstado.CONFIRMED))
    session.commit()

    grid = _customer_orders_grid()
    draft_row = next(row for row in grid if row[1] == "Ferretería Don Juan")
    confirmed_row = next(row for row in grid if row[1] == "Corralón Gremio")

    assert draft_row[0] == draft.order_id
    assert draft_row[2] == "DRAFT"
    assert draft_row[6] == "✏️"
    assert confirmed_row[2] == "CONFIRMED"
    assert confirmed_row[6] == ""


@pytest.mark.parametrize(
    ("estado", "editable"),
    [("DRAFT", True), ("draft", True), ("CONFIRMED", False), ("", False)],
)
def test_is_editable_order_matches_draft_only(estado, editable):
    """The editability rule is exactly DRAFT (case-insensitive)."""
    assert is_editable_order(estado) is editable


# ------------------------------------------------ edit-select decision (pure)


def test_order_edit_request_edit_column_and_draft_loads():
    """A click on "Editar" of a DRAFT row resolves to the order id to load."""
    row = [7, "Ferretería Don Juan", "DRAFT", "270.00", "270.00", False, "✏️"]
    assert _order_edit_request(6, row) == 7


@pytest.mark.parametrize(
    ("col_index", "row_value"),
    [
        (0, [7, "cust", "DRAFT", "270.00", "270.00", False, "✏️"]),  # data column: no-op
        (2, [7, "cust", "DRAFT", "270.00", "270.00", False, "✏️"]),  # estado column
        (6, [8, "cust", "CONFIRMED", "270.00", "270.00", False, ""]),  # non-DRAFT row
        (6, [8, "cust", "PICKING", "270.00", "270.00", False, ""]),  # non-DRAFT row
        (6, None),  # deselection / no row payload
        (6, []),  # empty row payload
        (None, [7, "cust", "DRAFT", "270.00", "270.00", False, "✏️"]),  # malformed index
    ],
)
def test_order_edit_request_other_cases_are_no_op(col_index, row_value):
    """Every click outside "Editar"-of-a-DRAFT resolves to None (no-op)."""
    assert _order_edit_request(col_index, row_value) is None


def test_order_edit_selected_no_op_for_other_clicks():
    """Non-edit selections return gr.skip() for every output (nothing changes)."""
    evt = SimpleNamespace(
        selected=True,
        index=[0, 0],
        row_value=[7, "cust", "DRAFT", "270.00", "270.00", False, "✏️"],
    )
    assert all(value == skip() for value in _order_edit_selected(evt))  # type: ignore[arg-type]

    deselect = SimpleNamespace(selected=False, index=[0, 6], row_value=[])
    assert all(value == skip() for value in _order_edit_selected(deselect))  # type: ignore[arg-type]


def test_order_edit_selected_loads_draft_and_switches_subtab(shop_ctx):
    """Clicking "Editar" of a real DRAFT loads the form and targets the sub-tab."""
    shop_ctx.commit()
    order = create_manual_order(shop_ctx, 1, [("LOCAL-1", 2)])
    shop_ctx.commit()
    evt = SimpleNamespace(
        selected=True,
        index=[0, 6],
        row_value=[order.order_id, "Ferretería Don Juan", "DRAFT", "270.00", "270.00", False, "✏️"],
    )

    tabs, lines, _grid, loaded_id, customer_id, status = _order_edit_selected(evt)  # type: ignore[arg-type]

    assert tabs.selected == "order-entry"
    assert loaded_id == order.order_id
    assert customer_id == 1
    assert lines == [["LOCAL-1", 2, "LOCAL"]]
    assert "cargado" in status


# ------------------------------------------------ open_draft_for_customer


def test_open_draft_for_customer_returns_the_only_draft(shop_ctx):
    """The lookup returns the client's single DRAFT and None for others/absent."""
    session = shop_ctx
    assert open_draft_for_customer(session, 1) is None
    order = create_manual_order(session, 1, [("LOCAL-1", 2)])
    session.commit()

    with SessionLocal() as fresh:
        draft = open_draft_for_customer(fresh, 1)
        assert draft is not None
        assert draft.order_id == order.order_id
        assert draft.estado is OrderEstado.DRAFT
        assert open_draft_for_customer(fresh, 2) is None  # the other client: no draft
