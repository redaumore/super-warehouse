"""Backoffice operations for customer orders and pricing maintenance.

The module keeps the Customer Orders tab independent from Gradio. Order rows
and line details are returned as plain dictionaries, while exchange-rate and
default-margin actions mutate only the supplied SQLAlchemy session. The
fulfillment actions (start picking, complete picking, deliver, cancel) wrap
the lifecycle transitions and COMMIT inside — the po.py pattern — so the tab
persists the owner's execution even though the Gradio handler closes its
short-lived ``SessionLocal`` at the end of the with-block. The UI owns the
transaction boundary for every other action.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import AppSetting, ExchangeRate, Order, Supplier
from src.order_lifecycle.state import (
    cancel_order,
    complete_picking,
    deliver_order,
    start_picking,
)
from src.pricing.order_pricing import MissingRateError, PricingLine, compute_order

_DEFAULT_MARGIN_KEY = "default_margin_pct"
_DEFAULT_MARGIN = Decimal(20)
_CENT = Decimal("0.01")
_RATE_PRECISION = Decimal("0.0001")


class ExchangeRateError(ValueError):
    """The requested exchange-rate operation is invalid."""


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _money_text(value: Decimal | None) -> str | None:
    """Render a money value as display text quantized to 2 decimals (HALF_UP).

    Purely cosmetic: stored ``Numeric`` values are never mutated, only the
    text handed to the Order lines grid (e.g. ``100.0000`` → ``"100.00"``).
    """
    return None if value is None else str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _margin_pct_text(base_price: Decimal | None, precio_original: Decimal | None) -> str | None:
    """Derive the Markup % between the original price and the ARS base price.

    ``((base_price / precio_original) - 1) × 100``, quantized HALF_UP to two
    decimals. LOCAL lines snapshot the cost as ``precio_original`` and price
    as ``base = cost × (1 + margin/100)``, so the markup is the applied
    margin; RAG lines carry no margin (``base = original × rate``) and
    therefore naturally derive ``0.00``. List/particular discounts live
    between base and final price and are NOT part of this percentage.
    Returns ``None`` (rendered "—") when the original price is missing or
    zero, or when the base price is missing (pending conversion), guarding
    the division by zero.
    """
    if base_price is None or precio_original is None or precio_original == 0:
        return None
    markup = (base_price / precio_original - 1) * 100
    return str(markup.quantize(_CENT, rounding=ROUND_HALF_UP))


def _line_total_text(final_price: Decimal | None, cantidad: int) -> str | None:
    """Render the line total as ``final_price × cantidad`` quantized to 2 decimals."""
    if final_price is None:
        return None
    return str((final_price * cantidad).quantize(_CENT, rounding=ROUND_HALF_UP))


def _order_row(order: Order) -> dict[str, object]:
    """Map one order into the Customer Orders grid contract."""
    return {
        "order_id": order.order_id,
        "customer": order.customer.nombre_comercial if order.customer else "—",
        "estado": order.estado.value,
        "subtotal": _decimal_text(order.subtotal),
        "total": _decimal_text(order.total),
        "conversion_pending": order.conversion_pending,
        "created_at": order.created_at,
    }


def list_customer_orders(session: Session) -> list[dict[str, object]]:
    """List persisted customer orders with state, customer, and ARS totals."""
    return [
        _order_row(order)
        for order in session.scalars(select(Order).order_by(Order.order_id.desc()))
    ]


def _order_or_raise(session: Session, order_id: int) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise KeyError(f"unknown customer order: {order_id}")
    return order


def order_detail(session: Session, order_id: int) -> dict[str, object]:
    """Return one customer order and its frozen line snapshots.

    Every money field is display text quantized to 2 decimals (HALF_UP) —
    stored values are never mutated. ``margin_pct`` is a derived field, not
    persisted: the markup between ``precio_original`` and the ARS base price
    per unit (LOCAL lines snapshot the cost and price ``base = cost ×
    (1 + margin/100)``, so the markup equals the applied margin; RAG lines
    carry no margin and derive ``0.00``). List/particular discounts live
    between base and final price and are not part of ``margin_pct``.
    ``line_total`` is ``final_price × cantidad``, also derived for display.
    """
    order = _order_or_raise(session, order_id)
    lines = [
        {
            "sku": item.sku,
            "name": item.name,
            "cantidad": item.cantidad,
            "base_price": _money_text(item.base_price),
            "final_price": _money_text(item.final_price),
            "source": item.source,
            "supplier": item.supplier,
            "moneda": item.moneda,
            "precio_original": _money_text(item.precio_original),
            "margin_pct": _margin_pct_text(item.base_price, item.precio_original),
            "line_total": _line_total_text(item.final_price, item.cantidad),
        }
        for item in order.items
    ]
    row = _order_row(order)
    row["lines"] = lines
    return row


def list_exchange_rates(session: Session) -> list[dict[str, object]]:
    """List manual currency-to-ARS rates, including the read-only ARS row."""
    return [
        {
            "currency": rate.currency,
            "rate_to_ars": str(rate.rate_to_ars),
            "updated_at": rate.updated_at,
            "editable": rate.currency != "ARS",
        }
        for rate in session.scalars(select(ExchangeRate).order_by(ExchangeRate.currency))
    ]


def _currency_code(currency: str) -> str:
    code = str(currency or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ExchangeRateError("currency must be a three-letter code")
    return code


def set_exchange_rate(
    session: Session, currency: str, rate_to_ars: Decimal | float
) -> ExchangeRate:
    """Create or update a non-ARS exchange rate without committing the session."""
    code = _currency_code(currency)
    if code == "ARS":
        raise ExchangeRateError("ARS exchange rate is read-only")
    try:
        rate = Decimal(str(rate_to_ars)).quantize(_RATE_PRECISION, rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError) as exc:
        raise ExchangeRateError("exchange rate must be numeric") from exc
    if rate <= 0:
        raise ExchangeRateError("exchange rate must be positive")

    row = session.get(ExchangeRate, code)
    if row is None:
        row = ExchangeRate(currency=code, rate_to_ars=rate, updated_at=datetime.now(UTC))
        session.add(row)
    else:
        row.rate_to_ars = rate
        row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def get_default_margin(session: Session) -> Decimal:
    """Read the default RAG margin in the setting's percentage representation."""
    setting = session.get(AppSetting, _DEFAULT_MARGIN_KEY)
    return _DEFAULT_MARGIN if setting is None else Decimal(setting.value)


def set_default_margin(session: Session, margin: Decimal | float) -> Decimal:
    """Store the default RAG margin without committing the session."""
    try:
        value = Decimal(str(margin)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError) as exc:
        raise ValueError("default margin must be numeric") from exc
    if value < 0 or value > 100:
        raise ValueError("default margin must be between 0 and 100 percent")
    setting = session.get(AppSetting, _DEFAULT_MARGIN_KEY)
    if setting is None:
        setting = AppSetting(key=_DEFAULT_MARGIN_KEY, value=str(value))
        session.add(setting)
    else:
        setting.value = str(value)
    session.flush()
    return value


def _supplier_margin_source(session: Session) -> Callable[[str | None], Decimal | None]:
    """Resolve a stored supplier code or provider name for a pending line."""

    def resolve(supplier: str | None) -> Decimal | None:
        if not supplier:
            return None
        value = supplier.strip()
        margin = session.scalar(
            select(Supplier.default_margin_pct).where(func.upper(Supplier.code) == value.upper())
        )
        if margin is not None:
            return margin
        return session.scalar(
            select(Supplier.default_margin_pct).where(
                func.lower(Supplier.business_name) == value.lower()
            )
        )

    return resolve


def _pricing_lines(order: Order) -> tuple[PricingLine, ...]:
    """Rebuild pure pricing inputs from persisted order-line snapshots."""
    lines: list[PricingLine] = []
    for item in order.items:
        source = (item.source or "LOCAL").upper()
        if source == "RAG":
            if item.precio_original is None:
                raise ValueError(f"RAG line {item.sku} has no original price snapshot")
            lines.append(
                PricingLine(
                    sku=item.sku,
                    cantidad=item.cantidad,
                    source=source,
                    name=item.name,
                    price=item.precio_original,
                    currency=item.moneda or "ARS",
                    supplier=item.supplier,
                    codigo_proveedor=item.supplier,
                )
            )
        elif source == "LOCAL":
            lines.append(
                PricingLine(
                    sku=item.sku,
                    cantidad=item.cantidad,
                    source=source,
                    name=item.name,
                    base_ars=item.base_price,
                    currency="ARS",
                    supplier=item.supplier,
                )
            )
        else:
            raise ValueError(f"unsupported persisted order line source: {source}")
    return tuple(lines)


# --------------------------------------------------- fulfillment actions (6.x)

# The fulfillment actions legal for each order state (backoffice spec: only
# legal next-state actions are shown on the Customer Orders tab).
_LEGAL_ACTIONS: dict[str, tuple[str, ...]] = {
    "DRAFT": ("cancel_order",),
    "CONFIRMED": ("start_picking", "cancel_order"),
    "PICKING": ("complete_picking", "cancel_order"),
    "READY_FOR_DELIVERY": ("deliver_order", "cancel_order"),
    "CANCELED": (),
    "CLOSED": (),
}


def legal_actions(estado: str) -> tuple[str, ...]:
    """The fulfillment actions legal for an order state, in display order."""
    return _LEGAL_ACTIONS.get(str(estado).upper(), ())


# --------------------------------------------------- state progress diagram

# Main fulfillment path in display order; CANCELED renders as a separate
# terminal badge outside this sequence.
_MAIN_PATH: tuple[tuple[str, str], ...] = (
    ("DRAFT", "Draft"),
    ("CONFIRMED", "Confirmed"),
    ("PICKING", "Picking"),
    ("READY_FOR_DELIVERY", "Ready for delivery"),
    ("CLOSED", "Closed"),
)
_MAIN_PATH_INDEX: dict[str, int] = {value: i for i, (value, _) in enumerate(_MAIN_PATH)}

_PILL_BASE = "border-radius:9999px;padding:2px 12px;font-size:13px;white-space:nowrap;"
_PILL_PASSED = _PILL_BASE + "background:#2563eb;color:#ffffff;border:1px solid #2563eb;"
_PILL_CURRENT = _PILL_PASSED + "font-weight:700;box-shadow:0 0 0 3px #93c5fd;"
_PILL_FUTURE = _PILL_BASE + "background:#ffffff;color:#6b7280;border:1px solid #d1d5db;"
_PILL_CANCELED = (
    _PILL_BASE + "background:#dc2626;color:#ffffff;border:1px solid #dc2626;font-weight:700;"
)
_ARROW = '<span style="color:#9ca3af;font-size:13px;margin:0 2px;">→</span>'


def order_state_diagram(estado: str) -> str:
    """Render the six order states as a horizontal progress diagram (HTML).

    There is no order-state history table, so the states the order has passed
    are inferred from the main-path order: the current state and every state
    before it on DRAFT → CONFIRMED → PICKING → READY_FOR_DELIVERY → CLOSED are
    colored, later states stay gray. CANCELED renders as a separate terminal
    badge outside the path (red when canceled); unknown or empty states render
    the whole path uncolored with no highlighted badge.
    """
    current = str(estado or "").strip().upper()
    current_index = _MAIN_PATH_INDEX.get(current)

    pills: list[str] = []
    for index, (value, label) in enumerate(_MAIN_PATH):
        if current_index is None or index > current_index:
            style = _PILL_FUTURE
        elif index == current_index:
            style = _PILL_CURRENT
        else:
            style = _PILL_PASSED
        pills.append(f'<span data-state="{value}" style="{style}">{label}</span>')
    canceled_style = _PILL_CANCELED if current == "CANCELED" else _PILL_FUTURE
    canceled_pill = f'<span data-state="CANCELED" style="{canceled_style}">Canceled</span>'
    return (
        '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:2px;'
        'font-family:system-ui,sans-serif;">'
        f"{_ARROW.join(pills)}"
        '<span style="color:#9ca3af;margin:0 6px;">|</span>'
        f"{canceled_pill}"
        "</div>"
    )


def start_picking_action(session: Session, order_id: int) -> str:
    """Execute Confirmed → Picking and commit (po.py pattern)."""
    start_picking(session, _order_or_raise(session, order_id))
    session.commit()
    return f"Pedido #{order_id} → Picking."


def complete_picking_action(session: Session, order_id: int) -> str:
    """Execute Picking → Ready for delivery and commit."""
    complete_picking(session, _order_or_raise(session, order_id))
    session.commit()
    return f"Pedido #{order_id} → Ready for delivery."


def deliver_order_action(session: Session, order_id: int) -> str:
    """Execute Ready for delivery → Closed (stores delivery_date) and commit."""
    order = _order_or_raise(session, order_id)
    deliver_order(session, order)
    session.commit()
    date_part = f" (entrega {order.delivery_date.isoformat()})" if order.delivery_date else ""
    return f"Pedido #{order_id} → Closed{date_part}."


def cancel_order_action(session: Session, order_id: int) -> str:
    """Execute the cancel transition with the backoffice as actor and commit.

    Draft/Confirmed release ACTIVE reservations; Picking/Ready for delivery
    restore the deducted stock with the audit trail (actor ``backoffice``).
    """
    cancel_order(session, _order_or_raise(session, order_id), actor="backoffice")
    session.commit()
    return f"Pedido #{order_id} cancelado."


def recompute_pending_conversion(session: Session) -> int:
    """Recompute every pending order whose non-ARS rates are now available.

    Orders that still lack a rate remain pending and are not counted. Existing
    line snapshots are retained; only ARS base/final prices and order totals
    change after a successful conversion.
    """
    rate_source = lambda currency: session.scalar(
        select(ExchangeRate.rate_to_ars).where(ExchangeRate.currency == currency.upper())
    )
    supplier_margin = _supplier_margin_source(session)
    default_margin = get_default_margin(session)
    updated = 0
    pending_orders = session.scalars(
        select(Order).where(Order.conversion_pending.is_(True)).order_by(Order.order_id)
    )
    for order in pending_orders:
        customer = order.customer
        list_discount = customer.lista_precios.descuento_lista_pct if customer else Decimal(0)
        try:
            priced = compute_order(
                _pricing_lines(order),
                rate=rate_source,
                supplier_margin=supplier_margin,
                default_margin=default_margin,
                list_discount=list_discount,
                particular_discount=Decimal(0),
            )
        except MissingRateError:
            continue
        for item, line in zip(order.items, priced.lines, strict=True):
            item.base_price = line.base_ars
            item.final_price = line.final_ars
            item.moneda = line.moneda
        order.subtotal = priced.subtotal
        order.total = priced.total
        order.conversion_pending = False
        updated += 1
    session.flush()
    return updated
