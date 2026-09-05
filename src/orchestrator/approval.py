"""Confirm orchestration: the DRAFT → CONFIRMED ceremony (design AD5).

Composes the lifecycle confirm transition with the registration side effects
that complete an order (design data flow):

    RAG lines auto-source (supplier by definition: need + OPEN PO, no
      availability check, no selection prompt)
      → classify A/B/C from latest availability (LOCAL lines + unresolved
        RAG leftovers only)
      → Case C: cancel_order + sourcing CANCELLED (no registration); the
        order's contribution is released from the OPEN POs its auto-sourced
        lines had accumulated (shared POs keep other orders' items; a PO left
        empty is cancelled)
      → Case B: persist SourcingNeed rows, hand the selection prompt back
      → Case A: confirm (TTL guard) → reserve→convert→deduct → append Sheets row

``confirm_and_register`` is the full flow for a clean confirmation. It raises
``RequiresRequoteError`` (from the lifecycle) when the order's reservations
have expired — the caller re-quotes first, never confirming silently.

Sheets is append-only and its failure is TOLERATED: a quarantined write keeps
the order CONFIRMED and the status rides the ``ConfirmResult`` — there is no
rollback (spec order-lifecycle: "the order MUST remain Confirmed"). The old
``notifier`` / ``owner_phone`` push is gone; the confirmation/error text rides
the ``ConfirmResult`` / the caller's reply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.agents.inventory import available_stock, reserve_stock
from src.db.models import (
    Inventory,
    Order,
    ReservationEstado,
    SourcingState,
    StockReservation,
    Supplier,
    SupplierStatus,
)
from src.integrations.sheets import SheetsWriter, SheetsWriteStatus
from src.observability.session_logger import log_session_event
from src.orchestrator.session import ResolvedItem
from src.order_lifecycle.state import (
    cancel_order,
    confirm_order,
)
from src.pricing.order_pricing import line_subtotal
from src.sourcing.classify import MissingItem, SourcingCase, classify_case
from src.supplier.searcher import SupplierCatalogSearcher

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")


class PendingConversionError(Exception):
    """An order cannot be confirmed while one or more prices lack conversion."""


@dataclass(frozen=True)
class ConfirmResult:
    """Outcome of the confirm ceremony.

    ``cancelled_case`` marks a Case C outcome (the order was cancelled instead
    of confirmed, so no stock was converted and no Sheets row was appended);
    ``missing`` carries the Case B items when the ceremony hands the supplier
    selection back to the conversation.
    """

    order: Order
    converted: int
    sheets_status: SheetsWriteStatus
    total: Decimal
    confirmation_text: str
    cancelled_case: bool = False
    missing: tuple[MissingItem, ...] = ()


def order_total(order: Order) -> Decimal:
    """Sum of every line's final price × quantity, HALF_UP to the cent."""
    return sum(
        (line_subtotal(item.final_price, item.cantidad) for item in order.items),
        Decimal(0),
    ).quantize(_CENT, rounding=ROUND_HALF_UP)


def build_items_summary(order: Order) -> str:
    """Compact per-line summary for the Sheets row, e.g. '10 × CLV-001'."""
    return "; ".join(f"{item.cantidad} × {item.sku}" for item in order.items)


def _active_reservations(session: Session, order: Order) -> list[StockReservation]:
    return list(
        session.scalars(
            select(StockReservation).where(
                StockReservation.order_id == order.order_id,
                StockReservation.estado == ReservationEstado.ACTIVE,
            )
        ).all()
    )


def _local_quantities(order: Order) -> dict[str, int]:
    """Per-SKU requested quantities of LOCAL lines (RAG lines never reserve)."""
    quantities: dict[str, int] = {}
    for item in order.items:
        if (item.source or "LOCAL").upper() != "LOCAL":
            continue
        quantities[item.sku] = quantities.get(item.sku, 0) + item.cantidad
    return quantities


def _reconcile_reservations(session: Session, order: Order) -> None:
    """Align ACTIVE reservations with the current LOCAL line quantities.

    Releases reservations whose quantity no longer matches the line (an item
    was removed or re-quantified on the draft) and creates a fresh ACTIVE
    reservation for LOCAL lines that have none (e.g. after ``modify_order``
    released the converted locks — design AD6's fresh re-reserve). Matching
    reservations keep their original TTL, so the stale-quote guard stays
    meaningful for unmodified quotes.
    """
    wanted = _local_quantities(order)
    for reservation in _active_reservations(session, order):
        if wanted.get(reservation.sku) != reservation.cantidad:
            reservation.estado = ReservationEstado.RELEASED
    session.flush()  # autoflush-off sessions must see the released rows below
    for sku, cantidad in wanted.items():
        matching = session.scalar(
            select(StockReservation.reservation_id).where(
                StockReservation.order_id == order.order_id,
                StockReservation.sku == sku,
                StockReservation.estado == ReservationEstado.ACTIVE,
            )
        )
        if matching is None:
            reserve_stock(
                session,
                sku,
                order.customer_id,
                cantidad,
                order_id=order.order_id,
            )


def _convert_reservations(session: Session, order: Order) -> list[StockReservation]:
    """Mark every ACTIVE reservation CONVERTED and return them."""
    reservations = _active_reservations(session, order)
    for reservation in reservations:
        reservation.estado = ReservationEstado.CONVERTED
    return reservations


def _deduct_stock(session: Session, reservations: list[StockReservation]) -> None:
    """Subtract each converted reservation's quantity from the canonical Inventory.

    ``Inventory.quantity_on_hand`` is the single on-hand source; the legacy
    ``catalogo.stock_disponible`` counter is deliberately left untouched.
    """
    for reservation in reservations:
        row = session.scalar(select(Inventory).where(Inventory.sku_id == reservation.sku))
        if row is None:
            logger.warning(
                "stock deduction skipped: unknown sku %s (reservation %s)",
                reservation.sku,
                reservation.reservation_id,
            )
            continue
        row.quantity_on_hand -= reservation.cantidad
        row.updated_at = datetime.now(UTC)


def _availability_for_order(session: Session, order: Order, sku: str) -> int:
    """Stock available to THIS order: on-hand minus other orders' ACTIVE locks.

    ``available_stock`` counts every ACTIVE reservation — including the order's
    own soft-lock, which would make a fully-quoted order look missing at
    confirm. The order's own reservation is its own stock, so it is added back:
    classification at confirm sees the availability the order actually has.
    """
    own = session.scalar(
        select(func.coalesce(func.sum(StockReservation.cantidad), 0)).where(
            StockReservation.order_id == order.order_id,
            StockReservation.sku == sku,
            StockReservation.estado == ReservationEstado.ACTIVE,
        )
    )
    return available_stock(session, sku) + int(own or 0)


def _classify_from_latest_availability(
    session: Session,
    order: Order,
    searcher: SupplierCatalogSearcher | None,
    unresolved_rag: tuple[ResolvedItem, ...] = (),
) -> tuple[SourcingCase, tuple[MissingItem, ...]]:
    """Classify the order from the LATEST availability (spec: at confirm).

    Only LOCAL lines (plus unresolved RAG leftovers) are stock-classified: a
    RAG line is supplier-sourced by definition — stock checks and supplier
    selection NEVER apply to it (owner rule). Unresolved RAG items (no supplier
    master match) fall back to the same classify so they surface as missing
    with candidates (Case B) or without (Case C).
    """
    items = (
        tuple(
            ResolvedItem(sku=item.sku, cantidad=item.cantidad, description=item.name)
            for item in order.items
            if (item.source or "LOCAL").upper() == "LOCAL"
        )
        + unresolved_rag
    )
    decision = classify_case(
        items,
        lambda sku: _availability_for_order(session, order, sku),
        searcher,
    )
    return decision.case, decision.missing


def _resolve_rag_supplier(session: Session, value: str | None) -> Supplier | None:
    """Resolve an OrderItem's supplier string to ONE ACTIVO supplier row.

    Exact first: the string is expected to be the 3-char ``codigo_proveedor``
    (what ``_draft_pricing_lines`` stores). Fallback: a unique case-insensitive
    ``business_name`` match. Zero matches or an ambiguous name leave the line
    unresolved (the owner decides via the selection prompt) — never guessed.
    """
    if not value or not value.strip():
        return None
    needle = value.strip().upper()
    supplier = session.scalar(
        select(Supplier).where(
            Supplier.code == needle,
            Supplier.status == SupplierStatus.ACTIVO,
        )
    )
    if supplier is not None:
        return supplier
    named = list(
        session.scalars(
            select(Supplier).where(
                func.lower(Supplier.business_name) == value.strip().lower(),
                Supplier.status == SupplierStatus.ACTIVO,
            )
        ).all()
    )
    return named[0] if len(named) == 1 else None


@dataclass(frozen=True)
class RagAutoSource:
    """One auto-sourced RAG line, for the session log and the confirmation reply."""

    sku: str
    supplier_name: str


def _autosource_rag_lines(
    session: Session, order: Order
) -> tuple[tuple[RagAutoSource, ...], tuple[ResolvedItem, ...]]:
    """Auto-source the order's RAG lines (owner rule); return the leftovers.

    RAG lines carry their supplier from the RAG catalog snapshot, so they are
    NEVER availability-checked and NEVER prompt a supplier selection: each
    line's ``supplier`` string is resolved to an ACTIVO supplier row and the
    line goes straight into its sourcing need and the supplier's OPEN purchase
    order (missing quantity is the full ``cantidad`` — RAG lines never
    reserve). Unresolved lines are returned as ``ResolvedItem`` s so they
    classify together with the LOCAL gaps.
    """
    from src.purchasing.accumulate import accumulate_need
    from src.sourcing.persistence import upsert_sourcing_need

    quantities: dict[str, int] = {}
    suppliers: dict[str, str | None] = {}
    names: dict[str, str | None] = {}
    for item in order.items:
        if (item.source or "LOCAL").upper() == "LOCAL":
            continue
        quantities[item.sku] = quantities.get(item.sku, 0) + item.cantidad
        suppliers.setdefault(item.sku, item.supplier)
        names.setdefault(item.sku, item.name)

    autosourced: list[RagAutoSource] = []
    unresolved: list[ResolvedItem] = []
    for sku, cantidad in quantities.items():
        supplier = _resolve_rag_supplier(session, suppliers[sku])
        if supplier is None:
            unresolved.append(ResolvedItem(sku=sku, cantidad=cantidad, description=names[sku]))
            continue
        need = upsert_sourcing_need(session, order.order_id, sku, cantidad)
        accumulate_need(session, need, supplier.id)
        autosourced.append(RagAutoSource(sku=sku, supplier_name=supplier.business_name))
    return tuple(autosourced), tuple(unresolved)


def _confirmation_text(
    order: Order,
    total: Decimal,
    sheets_status: SheetsWriteStatus,
    po_suppliers: tuple[str, ...] = (),
) -> str:
    if sheets_status is SheetsWriteStatus.QUARANTINED:
        text = (
            f"Pedido #{order.order_id} confirmado — total {total:.2f} ARS. "
            "Stock descontado. NO se pudo registrar en Google Sheets: la fila "
            "quedó en cuarentena. Revisá la configuración."
        )
    else:
        text = (
            f"Pedido #{order.order_id} confirmado — total {total:.2f} ARS. "
            "Stock descontado. Registrado en Google Sheets."
        )
    if po_suppliers:
        text += f" Orden de compra abierta a: {', '.join(po_suppliers)}."
    return text


def confirm_and_register(
    session: Session,
    order: Order,
    *,
    sheets: SheetsWriter,
    searcher: SupplierCatalogSearcher | None = None,
    customer_name: str | None = None,
    actor: str = "owner",
    now: datetime | None = None,
) -> ConfirmResult:
    """Run the DRAFT → CONFIRMED ceremony atomically (design AD5).

    One transaction: TTL guard + transition (``confirm_order``) → re-classify
    from the latest availability:

    - Case C (missing items, no supplier): the order is cancelled via the
      cancel path with sourcing CANCELLED and ``cancelled_case=True`` — no
      stock is converted and no Sheets row is appended. Any quantities the
      auto-sourced RAG lines had accumulated on suppliers' OPEN POs are
      released first (``release_order_needs``): shared POs keep the other
      orders' items, a PO left with no items is cancelled and the cancelled
      ids are surfaced in the confirmation text.
    - Case B (missing items with suppliers): the order stays CONFIRMED (per
      the spec the order state is independent of PO progress), the SourcingNeed
      rows are persisted and the supplier-selection prompt is returned.
    - Case A (full stock): reservations reconciled → CONVERTED, stock
      deducted, and the Sheets row appended. A Sheets quarantine is TOLERATED:
      the order stays CONFIRMED and the failure is surfaced in the result.

    Owner rule (RAG auto-sourcing): a RAG line is supplier-sourced by
    definition — it is NEVER availability-checked and NEVER prompts a supplier
    selection. Each RAG line's ``supplier`` string is resolved against the
    supplier master (exact 3-char code, then unique ACTIVO business_name); a
    resolved line goes straight into its sourcing need and the supplier's OPEN
    purchase order. Only LOCAL lines (plus unresolved RAG leftovers) are
    classified by availability.
    """
    if order.conversion_pending:
        log_session_event(
            "orders",
            "approval_blocked_conversion_pending",
            {"order_id": order.order_id},
            level="WARNING",
        )
        raise PendingConversionError(f"order {order.order_id} is pending currency conversion")
    confirm_order(session, order, now=now)
    autosourced, unresolved_rag = _autosource_rag_lines(session, order)
    if autosourced:
        log_session_event(
            "orders",
            "order_rag_autosourced",
            {
                "order_id": order.order_id,
                "lines": [
                    {"sku": line.sku, "supplier": line.supplier_name} for line in autosourced
                ],
            },
        )
    case, missing = _classify_from_latest_availability(session, order, searcher, unresolved_rag)

    log_session_event(
        "orders",
        "order_classified",
        {
            "order_id": order.order_id,
            "case": case.value,
            "items_count": len(order.items),
            "missing_count": len(missing),
            "missing_items": [
                {
                    "sku": m.sku,
                    "description": m.description,
                    "requested": m.requested,
                    "missing_quantity": m.missing_quantity,
                    "candidates_count": len(m.candidates),
                }
                for m in missing
            ],
        },
    )

    if case is SourcingCase.C:
        from src.agents.customer import format_case_c_reply, unmapped_supplier_note
        from src.purchasing.accumulate import release_order_needs

        cancel_order(session, order, actor=actor, now=now)
        order.sourcing_state = SourcingState.CANCELLED
        # The auto-sourced RAG lines already accumulated quantities on the
        # suppliers' OPEN POs: this cancelled order must release its share
        # (only OPEN POs; shared POs keep the other orders' items).
        cancelled_pos = release_order_needs(session, order.order_id)
        session.flush()
        unmapped = tuple(dict.fromkeys(getattr(searcher, "last_unmapped_codes", ()) or ()))
        if unmapped:
            log_session_event(
                "orders",
                "case_c_unmapped_suppliers",
                {"order_id": order.order_id, "unmapped_codes": list(unmapped)},
                level="WARNING",
            )
        if cancelled_pos:
            log_session_event(
                "orders",
                "order_case_c_released_pos",
                {
                    "order_id": order.order_id,
                    "cancelled_po_ids": [po.po_id for po in cancelled_pos],
                    "released_skus": [line.sku for line in autosourced],
                },
                level="WARNING",
            )
        log_session_event(
            "orders",
            "order_cancelled_case_c",
            {
                "order_id": order.order_id,
                "actor": actor,
                "reason": "missing_stock_no_suppliers",
                "missing_skus": [m.sku for m in missing],
            },
            level="WARNING",
        )
        po_note = ""
        if cancelled_pos:
            po_ids = ", ".join(f"#{po.po_id}" for po in cancelled_pos)
            po_note = f" Se cancelaron las órdenes de compra abiertas por este pedido ({po_ids})."
        return ConfirmResult(
            order=order,
            converted=0,
            sheets_status=SheetsWriteStatus.SKIPPED,
            total=order_total(order),
            confirmation_text=(
                format_case_c_reply(order, missing) + po_note + unmapped_supplier_note(searcher)
            ),
            cancelled_case=True,
            missing=missing,
        )

    if case is SourcingCase.B:
        from src.sourcing.persistence import upsert_sourcing_need

        for item in missing:
            upsert_sourcing_need(session, order.order_id, item.sku, item.missing_quantity)
        from src.agents.customer import format_case_b_reply

        reply = format_case_b_reply(order, missing)
        session.flush()
        log_session_event(
            "orders",
            "order_sourcing_pending_case_b",
            {
                "order_id": order.order_id,
                "missing_items": [
                    {
                        "sku": m.sku,
                        "missing_quantity": m.missing_quantity,
                        "candidates": [c.business_name for c in m.candidates],
                    }
                    for m in missing
                ],
            },
        )
        return ConfirmResult(
            order=order,
            converted=0,
            sheets_status=SheetsWriteStatus.SKIPPED,
            total=order_total(order),
            confirmation_text=reply,
            missing=missing,
        )

    _reconcile_reservations(session, order)
    converted = _convert_reservations(session, order)
    total = order_total(order)
    sheets_status = sheets.append_order_row(
        order.order_id,
        customer_name=customer_name
        or (order.customer.nombre_comercial if order.customer else None),
        total=str(total),
        items_summary=build_items_summary(order),
    )
    _deduct_stock(session, converted)
    # Auto-sourced RAG lines accumulated OPEN POs: the order is complete but
    # its sourcing is IN_PREPARATION until those POs are executed.
    po_suppliers = tuple(dict.fromkeys(line.supplier_name for line in autosourced))
    if po_suppliers:
        order.sourcing_state = SourcingState.IN_PREPARATION
    session.flush()
    log_session_event(
        "orders",
        "order_confirmed_case_a",
        {
            "order_id": order.order_id,
            "total_ars": str(total),
            "converted_reservations": len(converted),
            "sheets_status": sheets_status.value,
            "open_po_suppliers": list(po_suppliers),
        },
    )
    return ConfirmResult(
        order=order,
        converted=len(converted),
        sheets_status=sheets_status,
        total=total,
        confirmation_text=_confirmation_text(order, total, sheets_status, po_suppliers),
    )
