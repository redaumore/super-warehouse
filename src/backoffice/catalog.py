"""Backoffice catalog & stock editor (task 3.7).

Pure DB operations behind the Gradio catalog tab: browse products, edit stock,
price or margin. Margin edits recompute the list base price through the pure
pricing engine (base = cost × (1 + margin)) so the backoffice never diverges
from the pricing rules.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Catalogo
from src.pricing.engine import compute_base

_CENT = Decimal("0.01")


def list_products(session: Session) -> list[dict[str, object]]:
    """Every product row for the catalog grid: SKU, barcode, name, prices, stock."""
    rows = []
    for product in session.scalars(
        select(Catalogo).order_by(Catalogo.codigo_interno)
    ):
        rows.append(
            {
                "codigo_interno": product.codigo_interno,
                "codigo_barras": product.codigo_barras or "",
                "nombre_oficial": product.nombre_oficial,
                "costo_proveedor": str(product.costo_proveedor),
                "margen_aplicado_pct": str(product.margen_aplicado_pct),
                "precio_lista_base": str(product.precio_lista_base),
                "stock_disponible": product.stock_disponible,
            }
        )
    return rows


def _product_by_sku(session: Session, sku: str) -> Catalogo:
    product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == sku))
    if product is None:
        raise KeyError(f"unknown sku: {sku}")
    return product


def update_stock(session: Session, sku: str, stock: int) -> Catalogo:
    """Set the available stock of a product (audited adjustments live in Phase 3+)."""
    if stock < 0:
        raise ValueError("stock cannot be negative")
    product = _product_by_sku(session, sku)
    product.stock_disponible = stock
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
        raise ValueError("margin cannot be negative")
    product = _product_by_sku(session, sku)
    product.margen_aplicado_pct = Decimal(str(margin)).quantize(_CENT)
    product.precio_lista_base = compute_base(product.costo_proveedor, margin)
    session.flush()
    return product