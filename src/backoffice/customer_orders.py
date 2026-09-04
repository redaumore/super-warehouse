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
    """Return one customer order and its frozen line snapshots."""
    order = _order_or_raise(session, order_id)
    lines = [
        {
            "sku": item.sku,
            "name": item.name,
            "cantidad": item.cantidad,
            "base_price": _decimal_text(item.base_price),
            "final_price": _decimal_text(item.final_price),
            "source": item.source,
            "supplier": item.supplier,
            "moneda": item.moneda,
            "precio_original": _decimal_text(item.precio_original),
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
