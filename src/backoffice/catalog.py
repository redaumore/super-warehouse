"""Backoffice catalog & stock editor (task 3.7).

Pure DB operations behind the Gradio catalog tab: browse products, edit stock,
price or margin. Margin edits recompute the list base price through the pure
pricing engine (base = cost × (1 + margin)) so the backoffice never diverges
from the pricing rules. RAG catalog consultation moved to the unified search
use case in ``src.sourcing.product_search`` (the Productos tab queries both
sources through it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.backoffice.sku_mappings import find_product_by_any_supplier_code, primary_supplier_codes
from src.db.models import Catalogo, ExchangeRate, Inventory, Supplier
from src.pricing.engine import compute_base

_CENT = Decimal("0.01")


def _list_price_ars(
    product: Catalogo, usd_rate: Decimal | None
) -> Decimal | None:
    """Display-time AR$ list price: cost × (1 + margin) × currency rate.

    ``precio_lista_base`` is stored unconverted, so when the product's cost is
    in a non-ARS currency (``Catalogo.moneda == "USD"``) the supplier rate is
    applied here — the stored column is never mutated. A missing USD rate falls
    back to rate 1 (display degrades gracefully instead of crashing). Returns
    ``None`` only when the cost itself is missing.
    """
    if product.costo_proveedor is None:
        return None
    currency = (product.moneda or "ARS").strip().upper()
    rate = Decimal(1)
    if currency == "USD" and usd_rate is not None:
        rate = usd_rate
    # ``margen_aplicado_pct`` is stored as percentage POINTS (15.00 = 15%),
    # matching the fraction coercion the order-pricing engine applies
    # (``_as_fraction``): values > 1 are divided by 100 before the markup.
    margin = product.margen_aplicado_pct
    if margin is not None and margin.copy_abs() > 1:
        margin = margin / Decimal(100)
    base = compute_base(product.costo_proveedor, margin)
    return (base * rate).quantize(_CENT, rounding=ROUND_HALF_UP)


def list_products(session: Session) -> list[dict[str, object]]:
    """Every product row for the catalog grid.

    ``codigo_interno`` is an OPAQUE internal identifier (user-approved design):
    the grid leads with the SUPPLIER code + the product's primary mapped
    supplier code instead. Stock comes from the canonical
    ``Inventory.quantity_on_hand`` (ADR 0002); a product with no Inventory row
    displays zero. The primary supplier code is the mapping with the highest
    confidence (earliest id on ties), fetched in one extra query — empty
    string when the product is unmapped.

    ``precio_lista_ars`` is a display-time computation (cost × margin ×
    supplier-currency rate); the stored ``precio_lista_base`` is never touched.
    """
    results = session.execute(
        select(Catalogo, Inventory.quantity_on_hand, Supplier.code)
        .join(Catalogo.supplier)  # every catalog row has a supplier (non-null FK)
        .outerjoin(Inventory, Inventory.sku_id == Catalogo.codigo_interno)
        .order_by(Catalogo.codigo_interno)
    )
    products = list(results)
    primary_codes = primary_supplier_codes(session, [p.codigo_interno for p, _, _ in products])
    usd_rate = session.scalar(
        select(ExchangeRate.rate_to_ars).where(ExchangeRate.currency == "USD")
    )
    rows = []
    for product, on_hand, supplier_code in products:
        precio_ars = _list_price_ars(product, usd_rate)
        rows.append(
            {
                "codigo_interno": product.codigo_interno,
                "supplier_code": supplier_code,
                "supplier_sku_code": primary_codes.get(product.codigo_interno, ""),
                "nombre_oficial": product.nombre_oficial,
                "costo_proveedor": str(product.costo_proveedor),
                "moneda": product.moneda or "",
                "marca": product.marca or "",
                "categoria": product.categoria or "",
                "subcategoria": product.subcategoria or "",
                "precio_lista_ars": str(precio_ars) if precio_ars is not None else "",
                "margen_aplicado_pct": str(product.margen_aplicado_pct),
                "precio_lista_base": str(product.precio_lista_base),
                "on_hand": int(on_hand or 0),
            }
        )
    return rows


def resolve_product_code(session: Session, code: str) -> Catalogo:
    """Resolve a typed code to a catalog product for the Productos edit box.

    Resolution order: (a) exact ``codigo_interno`` match first (cheap, keeps
    the internal identifier usable as a fallback), else (b) the code as a
    mapped supplier code across ALL suppliers. Raises ``KeyError`` when
    neither matches, with a message that mentions both options (surfaced in
    the UI status box).
    """
    text = (code or "").strip()
    product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == text)) if text else None
    if product is None:
        product = find_product_by_any_supplier_code(session, text)
    if product is None:
        raise KeyError(
            "Producto no encontrado: ingresá el SKU interno o el código de proveedor del producto."
        )
    return product


def _product_by_sku(session: Session, sku: str) -> Catalogo:
    product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == sku))
    if product is None:
        raise KeyError(f"unknown sku: {sku}")
    return product


def update_stock(session: Session, sku: str, stock: int) -> Catalogo:
    """Set the on-hand stock of a product (audited adjustments live in Phase 3+).

    ``Inventory.quantity_on_hand`` is the single on-hand source (ADR 0002);
    this edit writes it directly.
    """
    if stock < 0:
        raise ValueError("stock cannot be negative")
    product = _product_by_sku(session, sku)
    inventory_row = session.scalar(select(Inventory).where(Inventory.sku_id == sku))
    if inventory_row is not None:
        inventory_row.quantity_on_hand = stock
        inventory_row.updated_at = datetime.now(UTC)
    else:
        session.add(Inventory(sku_id=sku, quantity_on_hand=stock))
    session.flush()
    return product


def update_price(session: Session, sku: str, price: Decimal | float) -> Catalogo:
    """Set the list base price directly."""
    if Decimal(str(price)) < 0:
        raise ValueError("price cannot be negative")
    product = _product_by_sku(session, sku)
    product.precio_lista_base = Decimal(str(price)).quantize(_CENT)
    session.flush()
    return product


def update_margin(session: Session, sku: str, margin: Decimal | float) -> Catalogo:
    """Set the applied margin and recompute the list base price."""
    if Decimal(str(margin)) < 0:
        raise ValueError("price cannot be negative")
    product = _product_by_sku(session, sku)
    product.margen_aplicado_pct = Decimal(str(margin)).quantize(_CENT)
    product.precio_lista_base = compute_base(product.costo_proveedor, margin)
    session.flush()
    return product
