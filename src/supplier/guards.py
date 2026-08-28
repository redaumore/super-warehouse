"""Supplier ACTIVO guard — single source of truth for the soft-delete rule.

Purchasing (``open_or_create_po`` / ``accumulate_need``) and document
ingestion (``confirm_items``) share this guard: an INACTIVO supplier is
excluded from new sourcing, purchase orders and inventory writes, while its
history (existing POs, catalog rows, mappings) stays intact.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.models import Supplier, SupplierStatus


class SupplierInactiveError(Exception):
    """The supplier is INACTIVO — the operation must be refused."""


def ensure_active_supplier(session: Session, supplier_id: int) -> Supplier:
    """Return the supplier, refusing INACTIVO rows before any write happens.

    Raises ``KeyError`` for an unknown supplier and ``SupplierInactiveError``
    when the supplier is soft-deleted (INACTIVO).
    """
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise KeyError(f"unknown supplier: {supplier_id}")
    if supplier.status is not SupplierStatus.ACTIVO:
        raise SupplierInactiveError(f"supplier {supplier_id} is INACTIVO")
    return supplier