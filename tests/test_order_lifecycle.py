"""Order state machine tests (Phase 2: six-state transitions).

Unit tests (no DB): transition legality for all six states, the needs_requote
guard, cancel release/restore rules, modify reconciliation and draft line
edits, using a minimal fake session so no Postgres is required.

Integration tests (Postgres, skipped when down):
- happy path Draft → Confirmed → Picking → Ready for delivery → Closed with
  the delivery date stored;
- a stale (TTL-expired) reservation refuses confirm and flags a re-quote;
- cancel from Draft/Confirmed releases ACTIVE reservations;
- late cancel (Picking/Ready) restores deducted stock AND records the
  StockAdjustment audit row (reason order_cancelled, actor);
- modify restores the deducted stock without double-counting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.inventory import available_stock, reserve_stock, seed_inventory
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
    SourcingNeed,
    StockAdjustment,
    StockReservation,
    Supplier,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
)
from src.order_lifecycle.state import (
    InvalidTransitionError,
    RequiresRequoteError,
    add_draft_item,
    cancel_order,
    complete_picking,
    confirm_order,
    deliver_order,
    expire_reservations,
    modify_order,
    remove_draft_item,
    requires_requote,
    start_picking,
)
from src.purchasing.accumulate import accumulate_need
from src.purchasing.state import send_po
from src.sourcing.persistence import upsert_sourcing_need

# ---------------------------------------------------------------- unit tests


class _FakeSession:
    """Minimal stand-in for the SQLAlchemy Session used by the transitions."""

    def __init__(
        self,
        *,
        stale_reservation: bool = False,
        stale_rows: list | None = None,
        reservations: list | None = None,
        scalar_result: object = None,
    ):
        self.stale = stale_reservation
        self.stale_rows = stale_rows or []
        self.reservations = reservations or []
        self.scalar_result = scalar_result
        self.executed: list = []
        self.added: list = []
        self.flushed = 0

    def scalar(self, _statement):
        if self.scalar_result is not None:
            return self.scalar_result
        return 1 if self.stale else None

    def scalars(self, _statement):
        # Filter by the queried entity: cancel_order runs a second scalars
        # query (SourcingNeed) whose rows must not be the reservations.
        entity = None
        try:
            entity = _statement.column_descriptions[0]["entity"]
        except (AttributeError, IndexError, KeyError, TypeError):
            pass
        rows = list(self.stale_rows) + list(self.reservations) + list(self.added)
        if entity is not None:
            rows = [row for row in rows if isinstance(row, entity)]
        return _ScalarResult(rows)

    def execute(self, statement):
        self.executed.append(statement)

    def flush(self):
        self.flushed += 1

    def add(self, obj):
        self.added.append(obj)

    def delete(self, _obj):
        pass


class _ScalarResult:
    """Duck-typed stand-in for SQLAlchemy's ScalarResult."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


def _order(order_id: int = 1, *, estado: OrderEstado, needs_requote: bool = False) -> Order:
    return Order(order_id=order_id, customer_id=1, estado=estado, needs_requote=needs_requote)


def _reservation(*, estado: ReservationEstado, cantidad: int = 4) -> StockReservation:
    return StockReservation(
        reservation_id=1,
        sku="CLV-001",
        customer_id=1,
        order_id=1,
        cantidad=cantidad,
        ttl_minutes=30,
        estado=estado,
    )


def test_confirm_draft_moves_to_confirmed():
    """Confirmar un pedido Draft lo mueve a Confirmado."""
    session = _FakeSession()
    order = _order(estado=OrderEstado.DRAFT)
    confirm_order(session, order)
    assert order.estado is OrderEstado.CONFIRMED
    assert order.approved_at is not None
    assert order.needs_requote is False


def test_confirm_flagged_order_raises_requote():
    """El flag needs_requote bloquea la confirmación silenciosa."""
    session = _FakeSession()
    order = _order(estado=OrderEstado.DRAFT, needs_requote=True)
    with pytest.raises(RequiresRequoteError):
        confirm_order(session, order)
    assert order.estado is OrderEstado.DRAFT


def test_confirm_order_with_stale_reservation_raises_requote():
    """Un pedido con reserva vencida no se confirma en silencio: exige recotizar."""
    session = _FakeSession(stale_reservation=True)
    order = _order(estado=OrderEstado.DRAFT)
    with pytest.raises(RequiresRequoteError):
        confirm_order(session, order)
    assert order.estado is OrderEstado.DRAFT
    assert order.needs_requote is True  # flagged for the re-quote


def test_confirm_non_draft_order_is_invalid():
    """Confirmar un pedido que no está en Draft es una transición inválida."""
    for estado in (OrderEstado.CONFIRMED, OrderEstado.CANCELED, OrderEstado.CLOSED):
        with pytest.raises(InvalidTransitionError, match="cannot confirm"):
            confirm_order(_FakeSession(), _order(estado=estado))


def test_start_picking_only_from_confirmed():
    """Start picking solo es válido desde Confirmado."""
    session = _FakeSession()
    confirmed = _order(estado=OrderEstado.CONFIRMED)
    start_picking(session, confirmed)
    assert confirmed.estado is OrderEstado.PICKING

    for estado in (
        OrderEstado.DRAFT,
        OrderEstado.PICKING,
        OrderEstado.READY_FOR_DELIVERY,
        OrderEstado.CANCELED,
        OrderEstado.CLOSED,
    ):
        with pytest.raises(InvalidTransitionError, match="cannot start picking"):
            start_picking(session, _order(estado=estado))


def test_complete_picking_only_from_picking():
    """Complete picking solo es válido desde Picking."""
    session = _FakeSession()
    picking = _order(estado=OrderEstado.PICKING)
    complete_picking(session, picking)
    assert picking.estado is OrderEstado.READY_FOR_DELIVERY

    for estado in (
        OrderEstado.DRAFT,
        OrderEstado.CONFIRMED,
        OrderEstado.READY_FOR_DELIVERY,
        OrderEstado.CANCELED,
        OrderEstado.CLOSED,
    ):
        with pytest.raises(InvalidTransitionError, match="cannot complete picking"):
            complete_picking(session, _order(estado=estado))


def test_deliver_only_from_ready_for_delivery():
    """Deliver solo es válido desde Ready for delivery."""
    session = _FakeSession()
    ready = _order(estado=OrderEstado.READY_FOR_DELIVERY)
    deliver_order(session, ready)
    assert ready.estado is OrderEstado.CLOSED

    for estado in (
        OrderEstado.DRAFT,
        OrderEstado.CONFIRMED,
        OrderEstado.PICKING,
        OrderEstado.CANCELED,
        OrderEstado.CLOSED,
    ):
        with pytest.raises(InvalidTransitionError, match="cannot deliver"):
            deliver_order(session, _order(estado=estado))


def test_cancel_releases_active_reservations_from_draft():
    """Cancelar un Draft libera las reservas activas de inmediato."""
    session = _FakeSession(reservations=[_reservation(estado=ReservationEstado.ACTIVE)])
    order = _order(estado=OrderEstado.DRAFT)
    cancel_order(session, order, actor="owner")
    assert order.estado is OrderEstado.CANCELED
    assert order.rejected_at is not None
    assert session.reservations[0].estado is ReservationEstado.RELEASED


def test_cancel_releases_active_reservations_from_confirmed():
    """Cancelar un Confirmado libera las reservas activas de inmediato."""
    session = _FakeSession(reservations=[_reservation(estado=ReservationEstado.ACTIVE)])
    order = _order(estado=OrderEstado.CONFIRMED)
    cancel_order(session, order, actor="owner")
    assert order.estado is OrderEstado.CANCELED
    assert session.reservations[0].estado is ReservationEstado.RELEASED


def test_cancel_from_picking_releases_converted_reservations():
    """Cancelar desde Picking libera las reservas convertidas (restore: integration)."""
    session = _FakeSession(reservations=[_reservation(estado=ReservationEstado.CONVERTED)])
    order = _order(estado=OrderEstado.PICKING)
    cancel_order(session, order, actor="owner")
    assert order.estado is OrderEstado.CANCELED
    assert session.reservations[0].estado is ReservationEstado.RELEASED


def test_cancel_from_closed_or_canceled_is_invalid():
    """Cancelar un pedido cerrado o ya cancelado es inválido."""
    for estado in (OrderEstado.CANCELED, OrderEstado.CLOSED):
        with pytest.raises(InvalidTransitionError, match="cannot cancel"):
            cancel_order(_FakeSession(), _order(estado=estado), actor="owner")


def test_modify_only_from_confirmed_and_releases_converted():
    """Modify solo es válido desde Confirmado y libera las reservas convertidas."""
    session = _FakeSession(reservations=[_reservation(estado=ReservationEstado.CONVERTED)])
    order = _order(estado=OrderEstado.CONFIRMED)
    modify_order(session, order)
    assert order.estado is OrderEstado.DRAFT
    assert session.reservations[0].estado is ReservationEstado.RELEASED

    for estado in (
        OrderEstado.DRAFT,
        OrderEstado.PICKING,
        OrderEstado.READY_FOR_DELIVERY,
        OrderEstado.CANCELED,
        OrderEstado.CLOSED,
    ):
        with pytest.raises(InvalidTransitionError, match="cannot modify"):
            modify_order(session, _order(estado=estado))


def test_add_draft_item_upserts_and_accumulates():
    """add_draft_item crea la línea y acumula cantidad si el SKU ya existe."""
    session = _FakeSession()
    order = _order(estado=OrderEstado.DRAFT)
    first = add_draft_item(session, order, "CLV-001", 3)
    assert first.cantidad == 3
    # A second add resolves the persisted line (reloaded session) and accumulates.
    resumed = _FakeSession(scalar_result=first)
    second = add_draft_item(resumed, order, "CLV-001", 2)
    assert second.cantidad == 5  # accumulated, not duplicated


def test_add_draft_item_refuses_non_positive_quantity():
    """Una cantidad no positiva no se puede agregar a un Draft."""
    with pytest.raises(ValueError, match="positive"):
        add_draft_item(_FakeSession(), _order(estado=OrderEstado.DRAFT), "CLV-001", 0)


def test_add_remove_draft_item_only_on_draft():
    """Solo los pedidos Draft aceptan edición de líneas."""
    for estado in (OrderEstado.CONFIRMED, OrderEstado.CANCELED, OrderEstado.CLOSED):
        with pytest.raises(InvalidTransitionError, match="cannot add"):
            add_draft_item(_FakeSession(), _order(estado=estado), "CLV-001", 1)
        with pytest.raises(InvalidTransitionError, match="cannot remove"):
            remove_draft_item(_FakeSession(), _order(estado=estado), "CLV-001")


def test_remove_draft_item_unknown_sku_is_a_noop():
    """Quitar un SKU que no está en el Draft no hace nada."""
    session = _FakeSession()
    remove_draft_item(session, _order(estado=OrderEstado.DRAFT), "ZZZ-999")
    assert session.flushed == 0  # nothing to delete, no flush


def test_requires_requote_true_when_flagged():
    """El flag needs_requote hace que requiera recotizar."""
    order = _order(estado=OrderEstado.DRAFT, needs_requote=True)
    assert requires_requote(_FakeSession(), order) is True


def test_requires_requote_true_when_stale_reservation():
    """Una reserva vencida hace que requiera recotizar."""
    order = _order(estado=OrderEstado.DRAFT)
    assert requires_requote(_FakeSession(stale_reservation=True), order) is True


def test_requires_requote_false_when_clean():
    """Sin flag ni reservas vencidas, no requiere recotizar."""
    order = _order(estado=OrderEstado.DRAFT)
    assert requires_requote(_FakeSession(), order) is False


def test_expire_reservations_flags_order_when_rows_expired():
    """Expirar reservas vencidas marca el pedido para recotizar."""
    stale = StockReservation(
        reservation_id=1,
        sku="CLV-001",
        customer_id=1,
        order_id=1,
        cantidad=3,
        ttl_minutes=30,
        estado=ReservationEstado.ACTIVE,
    )
    session = _FakeSession(stale_rows=[stale])
    order = _order(estado=OrderEstado.DRAFT)
    count = expire_reservations(session, order)
    assert count == 1
    assert stale.estado is ReservationEstado.EXPIRED
    assert order.needs_requote is True


def test_expire_reservations_noop_when_nothing_expired():
    """Sin reservas vencidas, expirar no hace nada."""
    session = _FakeSession(stale_rows=[])
    order = _order(estado=OrderEstado.DRAFT)
    assert expire_reservations(session, order) == 0
    assert order.needs_requote is False


# ------------------------------------------------------- integration (DB)


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
                "sourcing_needs, inventory, order_items, orders, stock_reservations, "
                "stock_adjustments, catalogo, suppliers, clientes, lista_precios "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def order_ctx(db_session):
    """Seed a product (10 units), a customer, and a DRAFT order."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Supplier(
            id=1,
            code="TES",
            business_name="Test Supplier",
            default_margin_pct=Decimal(0),
        )
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
        Catalogo(
            id=1,
            codigo_interno="CLV-001",
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=["clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    seed_inventory(db_session)
    order = Order(customer_id=1, estado=OrderEstado.DRAFT, needs_requote=False)
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.order_id,
            sku="CLV-001",
            cantidad=4,
            base_price=Decimal("135.00"),
            final_price=Decimal("135.00"),
            adjustment=Decimal(0),
            source="LOCAL",
        )
    )
    db_session.flush()
    return {"session": db_session, "order": order, "sku": "CLV-001"}


def _reserve_ctx(ctx, cantidad, *, minutes_ago: int | None = None):
    reservation = reserve_stock(
        ctx["session"], ctx["sku"], customer_id=1, cantidad=cantidad, order_id=ctx["order"].order_id
    )
    if minutes_ago is not None:
        reservation.timestamp = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        ctx["session"].flush()
    return reservation


def _on_hand(session, sku: str) -> int:
    row = session.scalar(select(Inventory).where(Inventory.sku_id == sku))
    assert row is not None
    return row.quantity_on_hand


def test_happy_path_draft_to_closed_sets_delivery_date(order_ctx):
    """Draft → Confirmed → Picking → Ready for delivery → Closed con fecha."""
    ctx = order_ctx
    confirm_order(ctx["session"], ctx["order"])
    assert ctx["order"].estado is OrderEstado.CONFIRMED
    start_picking(ctx["session"], ctx["order"])
    assert ctx["order"].estado is OrderEstado.PICKING
    complete_picking(ctx["session"], ctx["order"])
    assert ctx["order"].estado is OrderEstado.READY_FOR_DELIVERY
    deliver_order(ctx["session"], ctx["order"])
    assert ctx["order"].estado is OrderEstado.CLOSED
    assert ctx["order"].delivery_date is not None  # the delivery date is stored


def test_stale_quote_refused_with_requote_requirement(order_ctx):
    """Un pedido con reserva vencida no se confirma: exige recotizar."""
    _reserve_ctx(order_ctx, 4, minutes_ago=31)
    with pytest.raises(RequiresRequoteError):
        confirm_order(order_ctx["session"], order_ctx["order"])
    assert order_ctx["order"].estado is OrderEstado.DRAFT
    assert order_ctx["order"].needs_requote is True
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 10  # expired lock freed


def test_fresh_reservation_can_be_confirmed(order_ctx):
    """Una reserva vigente no bloquea la confirmación."""
    _reserve_ctx(order_ctx, 4, minutes_ago=5)
    confirm_order(order_ctx["session"], order_ctx["order"])
    assert order_ctx["order"].estado is OrderEstado.CONFIRMED
    assert order_ctx["order"].needs_requote is False


def test_cancel_draft_releases_reservations_and_stock_is_available(order_ctx):
    """Cancelar un Draft libera las reservas: el stock vuelve a estar disponible."""
    _reserve_ctx(order_ctx, 4)
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 6
    cancel_order(order_ctx["session"], order_ctx["order"], actor="owner")
    assert order_ctx["order"].estado is OrderEstado.CANCELED
    reservations = (
        order_ctx["session"]
        .scalars(
            select(StockReservation).where(StockReservation.order_id == order_ctx["order"].order_id)
        )
        .all()
    )
    assert all(r.estado is ReservationEstado.RELEASED for r in reservations)
    assert available_stock(order_ctx["session"], order_ctx["sku"]) == 10


def test_late_cancel_restores_deducted_stock_with_audit(order_ctx):
    """Cancelar desde Picking restaura el stock descontado y audita el ajuste."""
    ctx = order_ctx
    confirm_order(ctx["session"], ctx["order"])
    start_picking(ctx["session"], ctx["order"])
    # The confirm ceremony deducted the stock; simulate its converted state.
    reservation = _reserve_ctx(ctx, 4)
    reservation.estado = ReservationEstado.CONVERTED
    ctx["session"].flush()
    on_hand_row = ctx["session"].scalar(select(Inventory).where(Inventory.sku_id == ctx["sku"]))
    on_hand_row.quantity_on_hand = 6  # 10 − 4 deducted
    ctx["session"].flush()

    cancel_order(ctx["session"], ctx["order"], actor="backoffice")

    assert ctx["order"].estado is OrderEstado.CANCELED
    assert _on_hand(ctx["session"], ctx["sku"]) == 10  # restored
    adjustments = ctx["session"].scalars(select(StockAdjustment)).all()
    assert len(adjustments) == 1
    assert adjustments[0].reason == "order_cancelled"
    assert adjustments[0].actor == "backoffice"
    assert adjustments[0].delta == 4
    reservation = ctx["session"].get(StockReservation, reservation.reservation_id)
    assert reservation.estado is ReservationEstado.RELEASED  # never restores twice


# ------------------------- cancel releases auto-sourced needs from OPEN POs


def _autosourced_need(ctx, sku: str = "CLV-001", missing: int = 3):
    """Accumulate one sourcing need of the ctx order into supplier 1's OPEN PO."""
    need = upsert_sourcing_need(ctx["session"], ctx["order"].order_id, sku, missing)
    po = accumulate_need(ctx["session"], need, 1)
    return need, po


def test_cancel_confirmed_order_cancels_the_open_po_it_emptied(order_ctx):
    """Cancelar un Confirmado con necesidades auto-sourced cancela el PO vaciado."""
    ctx = order_ctx
    confirm_order(ctx["session"], ctx["order"])
    need, po = _autosourced_need(ctx)

    result = cancel_order(ctx["session"], ctx["order"], actor="owner")

    assert ctx["order"].estado is OrderEstado.CANCELED
    assert result.cancelled_po_ids == (po.po_id,)
    reloaded = ctx["session"].get(SupplierPurchaseOrder, po.po_id)
    assert reloaded.estado is SupplierPurchaseOrderState.CANCELLED
    assert ctx["session"].scalars(select(SupplierPurchaseOrderItem)).all() == []
    reloaded_need = ctx["session"].get(SourcingNeed, need.need_id)
    assert reloaded_need.po_item_id is None  # detached: a re-release is a no-op
    assert reloaded_need.supplier_id == 1  # the selection itself is kept


def test_cancel_leaves_a_shared_open_po_with_the_other_orders_items(order_ctx):
    """OC compartida: el PO sigue OPEN con la cantidad del otro pedido."""
    ctx = order_ctx
    confirm_order(ctx["session"], ctx["order"])
    need, shared_po = _autosourced_need(ctx)
    other = Order(customer_id=1, estado=OrderEstado.CONFIRMED, needs_requote=False)
    ctx["session"].add(other)
    ctx["session"].flush()
    other_need = upsert_sourcing_need(ctx["session"], other.order_id, "CLV-001", 2)
    accumulate_need(ctx["session"], other_need, 1)  # merges into the shared PO

    result = cancel_order(ctx["session"], ctx["order"], actor="owner")

    assert result.cancelled_po_ids == ()  # the PO still holds the other order
    reloaded = ctx["session"].get(SupplierPurchaseOrder, shared_po.po_id)
    assert reloaded.estado is SupplierPurchaseOrderState.OPEN
    item = ctx["session"].scalar(
        select(SupplierPurchaseOrderItem).where(SupplierPurchaseOrderItem.po_id == shared_po.po_id)
    )
    assert item.quantity == 2  # only the other order's share remains
    reloaded_need = ctx["session"].get(SourcingNeed, need.need_id)
    assert reloaded_need.po_item_id is None


def test_cancel_keeps_an_executed_po_and_its_need_link(order_ctx):
    """PO ya enviado (SENT): la cancelación no lo toca ni desvincula la necesidad."""
    ctx = order_ctx
    confirm_order(ctx["session"], ctx["order"])
    need, po = _autosourced_need(ctx)
    send_po(ctx["session"], po)

    result = cancel_order(ctx["session"], ctx["order"], actor="owner")

    assert result.cancelled_po_ids == ()
    reloaded = ctx["session"].get(SupplierPurchaseOrder, po.po_id)
    assert reloaded.estado is SupplierPurchaseOrderState.SENT  # untouched
    reloaded_need = ctx["session"].get(SourcingNeed, need.need_id)
    assert reloaded_need.po_item_id is not None  # keeps its executed link


def test_plain_cancel_without_needs_releases_nothing(order_ctx):
    """Cancelar sin necesidades: sin POs tocados y sin ids en el resultado."""
    ctx = order_ctx
    confirm_order(ctx["session"], ctx["order"])

    result = cancel_order(ctx["session"], ctx["order"], actor="owner")

    assert ctx["order"].estado is OrderEstado.CANCELED
    assert result.cancelled_po_ids == ()
    assert ctx["session"].scalar(select(SupplierPurchaseOrder)) is None


def test_modify_restores_deducted_stock_without_double_count(order_ctx):
    """Modify restaura el stock descontado y libera las reservas convertidas."""
    ctx = order_ctx
    confirm_order(ctx["session"], ctx["order"])
    reservation = _reserve_ctx(ctx, 4)
    reservation.estado = ReservationEstado.CONVERTED
    ctx["session"].flush()
    on_hand_row = ctx["session"].scalar(select(Inventory).where(Inventory.sku_id == ctx["sku"]))
    on_hand_row.quantity_on_hand = 6
    ctx["session"].flush()

    modify_order(ctx["session"], ctx["order"])

    assert ctx["order"].estado is OrderEstado.DRAFT
    assert _on_hand(ctx["session"], ctx["sku"]) == 10  # exactly restored, no double-count
    reservation = ctx["session"].get(StockReservation, reservation.reservation_id)
    assert reservation.estado is ReservationEstado.RELEASED
    assert ctx["session"].scalars(select(StockAdjustment)).all() == []  # modify is not audited


def test_add_remove_draft_item_on_persisted_draft(order_ctx):
    """add/remove mutan OrderItem rows; el Draft vacío sigue Draft."""
    ctx = order_ctx
    add_draft_item(ctx["session"], ctx["order"], "TRN-002", 2)
    items = (
        ctx["session"]
        .scalars(select(OrderItem).where(OrderItem.order_id == ctx["order"].order_id))
        .all()
    )
    assert {i.sku for i in items} == {"CLV-001", "TRN-002"}

    remove_draft_item(ctx["session"], ctx["order"], "CLV-001")
    remove_draft_item(ctx["session"], ctx["order"], "TRN-002")
    remaining = (
        ctx["session"]
        .scalars(select(OrderItem).where(OrderItem.order_id == ctx["order"].order_id))
        .all()
    )
    assert remaining == []  # remove is real
    assert ctx["order"].estado is OrderEstado.DRAFT  # empty draft stays DRAFT
