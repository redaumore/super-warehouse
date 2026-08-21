"""SQLAlchemy 2.0 ORM models for the ferretería MVP.

Encodes the design's data model exactly. The pricing formula itself is a pure
function (Phase 2), but these tables carry every field that formula needs:
cost, margin, list/particular discounts, and the base price.

`catalogo.embedding` is a `vector(1536)` column (pgvector) — the fixed embedding
dimension from the config, ready for hybrid search in Phase 2.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class ReservationEstado(str, enum.Enum):
    """Soft-lock reservation lifecycle states."""

    ACTIVE = "ACTIVE"
    CONVERTED = "CONVERTED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class OrderEstado(str, enum.Enum):
    """Order state machine (fixed by spec — four states)."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    IN_DISPATCH = "IN_DISPATCH"
    REJECTED = "REJECTED"


class ListaPrecios(Base):
    """Commercial price list (Base = 0%, Gremio A = 10%, Gremio B = 20%)."""

    __tablename__ = "lista_precios"

    lista_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # Percent discount applied to list prices. Base=0, Gremio A=10, Gremio B=20.
    descuento_lista_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal(0)
    )


class Cliente(Base):
    """Customer resolved by normalized phone. No credit/payment fields (out of scope)."""

    __tablename__ = "clientes"

    customer_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre_comercial: Mapped[str] = mapped_column(String(200), nullable=False)
    contacto: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telefono_norm: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    lista_precios_id: Mapped[int] = mapped_column(
        ForeignKey("lista_precios.lista_id"), nullable=False
    )
    descuento_particular_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal(0)
    )

    lista_precios: Mapped[ListaPrecios] = relationship()


class Proveedor(Base):
    """Supplier."""

    __tablename__ = "proveedores"

    proveedor_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    razon_social: Mapped[str] = mapped_column(String(200), nullable=False)
    contacto: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telefono: Mapped[str | None] = mapped_column(String(32), nullable=True)
    margen_predeterminado: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal(0)
    )
    condiciones: Mapped[str | None] = mapped_column(Text, nullable=True)


class Catalogo(Base):
    """Catalog product with cost, margin, base price, stock, synonyms and vector.

    `embedding` is a pgvector `vector(1536)` used by hybrid search (Phase 2).
    """

    __tablename__ = "catalogo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo_interno: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    codigo_barras: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proveedor_id: Mapped[int] = mapped_column(
        ForeignKey("proveedores.proveedor_id"), nullable=False
    )
    nombre_oficial: Mapped[str] = mapped_column(String(300), nullable=False)
    costo_proveedor: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    margen_aplicado_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal(0)
    )
    precio_lista_base: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stock_disponible: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sinonimos: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=True)

    proveedor: Mapped[Proveedor] = relationship()


class ProveedorSkuMapping(Base):
    """Map a supplier's raw code/description to an internal SKU with confidence."""

    __tablename__ = "proveedor_sku_mapping"

    mapping_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proveedor_id: Mapped[int] = mapped_column(
        ForeignKey("proveedores.proveedor_id"), nullable=False
    )
    codigo_proveedor: Mapped[str] = mapped_column(String(64), nullable=False)
    descripcion_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    sku_interno: Mapped[str] = mapped_column(String(64), nullable=False)
    confianza: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal(0))


class StockReservation(Base):
    """Soft-lock reservation with a TTL. `estado` tracks ACTIVE→CONVERTED|RELEASED|EXPIRED."""

    __tablename__ = "stock_reservations"

    reservation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("clientes.customer_id"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.order_id"), nullable=True)
    cantidad: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    estado: Mapped[ReservationEstado] = mapped_column(
        Enum(
            ReservationEstado,
            name="reservation_estado",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ReservationEstado.ACTIVE,
    )


class Order(Base):
    """Order with the fixed four-state machine plus a needs_requote flag."""

    __tablename__ = "orders"

    order_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("clientes.customer_id"), nullable=False)
    estado: Mapped[OrderEstado] = mapped_column(
        Enum(OrderEstado, name="order_estado", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=OrderEstado.PENDING_APPROVAL,
    )
    needs_requote: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationship (not just FK) so SQLAlchemy orders inserts: orders depend on
    # clientes and must be inserted after them in the same flush.
    customer: Mapped[Cliente] = relationship()

    items: Mapped[list[OrderItem]] = relationship(back_populates="order")


class OrderItem(Base):
    """Line item of an order with per-line pricing."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.order_id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    cantidad: Mapped[int] = mapped_column(Integer, nullable=False)
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    final_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    adjustment: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal(0))

    order: Mapped[Order] = relationship(back_populates="items")
