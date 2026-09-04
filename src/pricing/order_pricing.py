"""Source-aware pricing for orders assembled from the chat draft.

The module is deliberately free of database and network access. Callers provide
exchange-rate and supplier-margin sources, which keeps the pricing rules easy to
test and prevents the chat agent from re-deriving them ad hoc.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from src.pricing.engine import compute_base, compute_final

_CENT = Decimal("0.01")

type PriceInput = Decimal | float | int
type RateSource = Callable[[str], PriceInput | None]
type MarginSource = Callable[[str | None], PriceInput | None]


@dataclass(frozen=True)
class PricingLine:
    """One source-labelled draft line waiting for pure price computation."""

    sku: str
    cantidad: int
    source: str | object
    name: str | None = None
    cost: PriceInput | None = None
    margin: PriceInput | None = None
    price: PriceInput | None = None
    currency: str | None = None
    supplier: str | None = None
    codigo_proveedor: str | None = None
    base_ars: PriceInput | None = None


# These aliases make the input contract discoverable without forcing callers to
# depend on an implementation-specific name.
OrderLine = PricingLine
PriceableLine = PricingLine


@dataclass(frozen=True)
class PricedLine:
    """One order line after source-aware ARS conversion and discounts."""

    sku: str
    cantidad: int
    base_ars: Decimal
    final_ars: Decimal
    moneda: str
    source: str | object
    name: str | None = None
    supplier: str | None = None
    precio_original: Decimal | None = None
    codigo_proveedor: str | None = None

    @property
    def subtotal_ars(self) -> Decimal:
        """Base subtotal for the line, rounded to cents."""
        return (self.base_ars * self.cantidad).quantize(_CENT, rounding=ROUND_HALF_UP)

    @property
    def total_ars(self) -> Decimal:
        """Final total for the line, rounded to cents."""
        return (self.final_ars * self.cantidad).quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PricedOrder:
    """Priced order lines and persisted ARS totals."""

    lines: tuple[PricedLine, ...]
    subtotal: Decimal | None = None
    total: Decimal | None = None
    conversion_pending: bool = False

    @property
    def subtotal_ars(self) -> Decimal | None:
        """Alias for consumers that make the currency explicit in their UI."""
        return self.subtotal

    @property
    def total_ars(self) -> Decimal | None:
        """Alias for consumers that make the currency explicit in their UI."""
        return self.total


class MissingRateError(ValueError):
    """Raised when a non-ARS line has no exchange rate."""

    def __init__(self, currency: str) -> None:
        self.currency = currency
        super().__init__(f"missing exchange rate for currency {currency}")


def _as_decimal(value: PriceInput | None) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


def _as_fraction(value: PriceInput | None) -> Decimal:
    """Coerce either a fraction (``0.20``) or percentage points (``20``)."""
    decimal = _as_decimal(value)
    return decimal / Decimal(100) if decimal.copy_abs() > 1 else decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _unpack_line(raw: Any) -> dict[str, Any]:
    """Accept ``PricingLine``, mapping/object records, or ``(entry, qty)``."""
    quantity_override = None
    value = raw
    if isinstance(raw, tuple) and len(raw) == 2 and not isinstance(raw[0], str):
        value, quantity_override = raw

    source = _field(value, "source", "origen", default="LOCAL")
    return {
        "sku": _field(value, "sku", "codigo", "codigo_interno"),
        "cantidad": quantity_override
        if quantity_override is not None
        else _field(value, "cantidad", "quantity", "qty", default=1),
        "source": source,
        "name": _field(value, "name", "nombre", "description", "descripcion", "nombre_oficial"),
        "cost": _field(value, "cost", "costo", "costo_proveedor"),
        "margin": _field(value, "margin", "margen", "margen_aplicado_pct"),
        "price": _field(value, "price", "precio", "offer_price", "precio_original"),
        "currency": _field(value, "currency", "moneda"),
        "supplier": _field(value, "supplier", "provider", "proveedor", "nombre_proveedor"),
        "codigo_proveedor": _field(value, "codigo_proveedor", "supplier_code"),
        "base_ars": _field(value, "base_ars"),
    }


def _source_value(source: object) -> str:
    value = getattr(source, "value", source)
    return str(value).upper()


def _currency_value(currency: object) -> str:
    value = str(currency or "ARS").strip().upper()
    return value or "ARS"


def _resolve_rate(rate: RateSource | Mapping[str, PriceInput] | PriceInput | None, currency: str) -> Decimal | None:
    if currency == "ARS":
        return Decimal(1)
    if rate is None:
        return None
    if callable(rate):
        value = rate(currency)
    elif isinstance(rate, Mapping):
        value = rate.get(currency)
    else:
        value = rate
    return None if value is None else _as_decimal(value)


def _resolve_margin(
    supplier_margin: MarginSource | Mapping[str, PriceInput] | PriceInput | None,
    code: str | None,
    default_margin: PriceInput | None,
) -> Decimal:
    value: PriceInput | None
    if supplier_margin is None:
        value = None
    elif callable(supplier_margin):
        value = supplier_margin(code)
    elif isinstance(supplier_margin, Mapping):
        value = supplier_margin.get(code) if code is not None else None
    else:
        value = supplier_margin
    return _as_fraction(value if value is not None else default_margin)


def _price_line(
    fields: dict[str, Any],
    *,
    rate: RateSource | Mapping[str, PriceInput] | PriceInput | None,
    supplier_margin: MarginSource | Mapping[str, PriceInput] | PriceInput | None,
    default_margin: PriceInput | None,
    list_discount: PriceInput | None,
    particular_discount: PriceInput | None,
    allow_missing_rate: bool = False,
) -> PricedLine:
    sku = str(fields["sku"] or "").strip()
    if not sku:
        raise ValueError("order line SKU is required")
    quantity = int(fields["cantidad"])
    if quantity <= 0:
        raise ValueError(f"quantity must be positive for sku {sku}")
    source = _source_value(fields["source"])
    name = fields["name"]
    supplier = fields["supplier"]
    code = fields["codigo_proveedor"] or (supplier if source == "RAG" else None)

    if source == "LOCAL":
        cost = fields["cost"]
        if cost is None and fields["base_ars"] is None:
            raise ValueError(f"local line {sku} requires costo_proveedor")
        base = (
            _as_decimal(fields["base_ars"])
            if cost is None
            else compute_base(cost, _as_fraction(fields["margin"]))
        )
        original = _as_decimal(cost) if cost is not None else None
        currency = "ARS"
    elif source == "RAG":
        original_value = fields["price"]
        if original_value is None:
            raise ValueError(f"RAG line {sku} requires offer price")
        original = _as_decimal(original_value)
        currency = _currency_value(fields["currency"])
        conversion_rate = _resolve_rate(rate, currency)
        if conversion_rate is None:
            if not allow_missing_rate:
                raise MissingRateError(currency)
            base = Decimal(0)
        else:
            base = compute_base(
                _quantize(original * conversion_rate),
                _resolve_margin(supplier_margin, str(code) if code is not None else None, default_margin),
            )
    else:
        raise ValueError(f"unsupported order line source: {source}")

    final = compute_final(base, _as_fraction(list_discount), _as_fraction(particular_discount))
    raw_source = fields["source"]
    return PricedLine(
        sku=sku,
        cantidad=quantity,
        base_ars=_quantize(base),
        final_ars=_quantize(final),
        moneda=currency,
        source=raw_source if hasattr(raw_source, "value") else source,
        name=str(name) if name is not None else None,
        supplier=str(supplier) if supplier is not None else (str(code) if code else None),
        precio_original=original,
        codigo_proveedor=str(code) if code is not None else None,
    )


def compute_order(
    lines: Sequence[Any],
    *,
    rate: RateSource | Mapping[str, PriceInput] | PriceInput | None = None,
    supplier_margin: MarginSource | Mapping[str, PriceInput] | PriceInput | None = None,
    default_margin: PriceInput | None = Decimal(0),
    list_discount: PriceInput | None = Decimal(0),
    particular_discount: PriceInput | None = Decimal(0),
) -> PricedOrder:
    """Compute source-aware line prices and ARS subtotal/total.

    LOCAL lines use their catalog cost and applied margin. RAG lines convert the
    offer price to ARS first, then apply the mapped supplier margin or fallback
    default. Missing non-ARS rates raise :class:`MissingRateError`.
    """
    priced = tuple(
        _price_line(
            _unpack_line(line),
            rate=rate,
            supplier_margin=supplier_margin,
            default_margin=default_margin,
            list_discount=list_discount,
            particular_discount=particular_discount,
        )
        for line in lines
    )
    subtotal = _quantize(sum((line.subtotal_ars for line in priced), Decimal(0)))
    total = _quantize(sum((line.total_ars for line in priced), Decimal(0)))
    return PricedOrder(lines=priced, subtotal=subtotal, total=total)


def pending_order(
    lines: Sequence[Any],
    *,
    rate: RateSource | Mapping[str, PriceInput] | PriceInput | None = None,
    supplier_margin: MarginSource | Mapping[str, PriceInput] | PriceInput | None = None,
    default_margin: PriceInput | None = Decimal(0),
    list_discount: PriceInput | None = Decimal(0),
    particular_discount: PriceInput | None = Decimal(0),
) -> PricedOrder:
    """Build snapshots while leaving lines with missing rates at zero ARS.

    This is the persistence representation for a pending-conversion order. The
    original denomination price remains on each line and backoffice recomputes
    ARS prices once the missing rate is available.
    """
    priced = tuple(
        _price_line(
            _unpack_line(line),
            rate=rate,
            supplier_margin=supplier_margin,
            default_margin=default_margin,
            list_discount=list_discount,
            particular_discount=particular_discount,
            allow_missing_rate=True,
        )
        for line in lines
    )
    return PricedOrder(lines=priced, conversion_pending=True)
