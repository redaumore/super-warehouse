"""SQLAlchemy 2.0 ORM models for the ferretería MVP.

Encodes the design's data model exactly. The pricing formula itself is a pure
function (Phase 2), but these tables carry every field that formula needs:
cost, margin, list/particular discounts, and the base price.

`catalogo.embedding` is a `vector(1536)` column (pgvector) — the fixed embedding
dimension from the config, ready for hybrid search in Phase 2.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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


class SourcingState(str, enum.Enum):
    """Sourcing/fulfillment axis, independent of the four approval states.

    Case A orders await assembly (PENDING_ASSEMBLY); Case B orders are being
    prepared from supplier purchase orders (IN_PREPARATION); Case C orders are
    cancelled because the missing items cannot be sourced (CANCELLED).
    """

    PENDING_ASSEMBLY = "PENDING_ASSEMBLY"
    IN_PREPARATION = "IN_PREPARATION"
    CANCELLED = "CANCELLED"


class SupplierPurchaseOrderState(str, enum.Enum):
    """Supplier purchase order lifecycle states (own state machine)."""

    OPEN = "OPEN"
    SENT = "SENT"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    FULLY_RECEIVED = "FULLY_RECEIVED"
    CANCELLED = "CANCELLED"


class SupplierStatus(str, enum.Enum):
    """Supplier master-data lifecycle: ACTIVO usable, INACTIVO soft-deleted.

    INACTIVO suppliers are excluded from sourcing, purchase-order creation and
    document ingestion (guards live in ``src/supplier/guards.py``).
    """

    ACTIVO = "ACTIVO"
    INACTIVO = "INACTIVO"


class IvaCondition(str, enum.Enum):
    """Argentine IVA condition of a supplier (master data, informational)."""

    RESPONSABLE_INSCRIPTO = "RESPONSABLE_INSCRIPTO"
    MONOTRIBUTO = "MONOTRIBUTO"
    EXENTO = "EXENTO"
    CONSUMIDOR_FINAL = "CONSUMIDOR_FINAL"
    NO_RESPONSABLE = "NO_RESPONSABLE"


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


class Supplier(Base):
    """Supplier master data (English domain naming).

    ``code`` is the 3-char uppercase code generated from ``business_name``,
    user-editable before save and immutable once linked (guarded in
    ``src/backoffice/suppliers.py``). ``cuit`` is nullable (legacy rows may lack
    it) and backed by a partial unique index; ``status`` is the soft-delete
    lifecycle (default ACTIVO).
    """

    __tablename__ = "suppliers"

    __table_args__ = (
        Index("uq_suppliers_code", "code", unique=True),
        Index(
            "uq_suppliers_cuit",
            "cuit",
            unique=True,
            postgresql_where=text("cuit IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_margin_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal(0)
    )
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    cuit: Mapped[str | None] = mapped_column(String(13), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(32), nullable=True)
    code: Mapped[str] = mapped_column(String(3), nullable=False)
    iva_condition: Mapped[IvaCondition | None] = mapped_column(
        Enum(IvaCondition, name="iva_condition", values_callable=lambda e: [m.value for m in e]),
        nullable=True,
    )
    status: Mapped[SupplierStatus] = mapped_column(
        Enum(
            SupplierStatus,
            name="supplier_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SupplierStatus.ACTIVO,
    )


class Catalogo(Base):
    """Catalog product with cost, margin, base price, stock, synonyms and vector.

    `embedding` is a pgvector `vector(1536)` used by hybrid search (Phase 2).
    """

    __tablename__ = "catalogo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo_interno: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    codigo_barras: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    nombre_oficial: Mapped[str] = mapped_column(String(300), nullable=False)
    costo_proveedor: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    margen_aplicado_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal(0)
    )
    precio_lista_base: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stock_disponible: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sinonimos: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=True)

    supplier: Mapped[Supplier] = relationship()


class SupplierSkuMapping(Base):
    """Map a supplier's raw code/description to an internal SKU with confidence."""

    __tablename__ = "supplier_sku_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    supplier_sku_code: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_sku: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal(0))


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


class StockAdjustment(Base):
    """Audited stock change by barcode with a reason and actor.

    `delta` is positive for increases and negative for decreases; every row is
    the audit trail the barcode-stock-ops spec requires.
    """

    __tablename__ = "stock_adjustments"

    adjustment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Order(Base):
    """Order with the fixed four-state machine plus a needs_requote flag.

    ``sourcing_state`` is the separate sourcing/fulfillment axis (spec:
    PENDING_ASSEMBLY / IN_PREPARATION / CANCELLED) and MUST NOT change or
    replace the four approval states. ``delivery_date`` is informational.
    """

    __tablename__ = "orders"

    order_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("clientes.customer_id"), nullable=False)
    estado: Mapped[OrderEstado] = mapped_column(
        Enum(OrderEstado, name="order_estado", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=OrderEstado.PENDING_APPROVAL,
    )
    sourcing_state: Mapped[SourcingState] = mapped_column(
        Enum(SourcingState, name="sourcing_state", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SourcingState.PENDING_ASSEMBLY,
    )
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
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

    sourcing_needs: Mapped[list[SourcingNeed]] = relationship(back_populates="order")


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


class Inventory(Base):
    """Canonical on-hand stock per SKU (the single availability source).

    Backfilled from ``catalogo.stock_disponible``; later stock changes update
    ``quantity_on_hand`` and touch ``updated_at``.
    """

    __tablename__ = "inventory"

    sku_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quantity_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SupplierPurchaseOrder(Base):
    """Owner's purchase order to a supplier, accumulating missing items.

    Own state machine (OPEN → SENT → PARTIALLY_RECEIVED → FULLY_RECEIVED,
    CANCELLED) mirroring ``src/order_lifecycle/state.py``; transitions live in
    ``src/purchasing/state.py``.
    """

    __tablename__ = "supplier_purchase_orders"

    po_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False, index=True)
    estado: Mapped[SupplierPurchaseOrderState] = mapped_column(
        Enum(
            SupplierPurchaseOrderState,
            name="supplier_purchase_order_state",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SupplierPurchaseOrderState.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    supplier: Mapped[Supplier] = relationship()

    items: Mapped[list[SupplierPurchaseOrderItem]] = relationship(back_populates="po")


class SupplierPurchaseOrderItem(Base):
    """One SKU line of a purchase order, aggregated across customer orders.

    ``quantity`` accumulates; ``received_quantity`` tracks partial receipts.
    """

    __tablename__ = "supplier_purchase_order_items"

    po_item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("supplier_purchase_orders.po_id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    received_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("po_id", "sku", name="uq_supplier_po_item_sku"),)

    po: Mapped[SupplierPurchaseOrder] = relationship(back_populates="items")


class SourcingNeed(Base):
    """Missing item of an order awaiting (or holding) a supplier selection.

    DB source of truth for the Case B multi-turn flow: rows persist the missing
    items and the owner's ``supplier_id`` selection so it survives the in-memory
    30-minute conversation TTL. ``po_item_id`` links the need to the purchase
    order item it was accumulated into.
    """

    __tablename__ = "sourcing_needs"

    need_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.order_id"), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    missing_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("suppliers.id"), nullable=True, index=True
    )
    po_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_purchase_order_items.po_item_id"), nullable=True
    )

    order: Mapped[Order] = relationship(back_populates="sourcing_needs")
