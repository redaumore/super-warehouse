"""Backoffice suppliers CRUD (task 3.1).

clients.py-pattern module behind the Gradio "Suppliers" tab: list with quick
search (business_name / cuit / code) and status filter, create, edit and
status toggle. ``create_supplier`` / ``update_supplier`` re-validate CUIT
(mod-11), email (RFC 5322) and the two phone forms (strict E.164 + WhatsApp);
``code`` is generated from ``business_name`` (reactive suggestion), kept
editable until save and immutable once the supplier is linked to a Catalogo,
SupplierPurchaseOrder, SourcingNeed or SupplierSkuMapping row.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.db.models import (
    Catalogo,
    IvaCondition,
    SourcingNeed,
    Supplier,
    SupplierPurchaseOrder,
    SupplierSkuMapping,
    SupplierStatus,
)
from src.supplier.validation import (
    normalize_e164_phone,
    normalize_whatsapp,
    resolve_code,
    suggest_code,
    validate_cuit,
    validate_email,
)

_CENT = Decimal("0.01")

# Sentinel distinguishing "field not provided" (no change) from an explicit
# ``None``/empty value (clear the field). The UI always sends the full form.
_UNSET = object()


class InvalidSupplierDataError(Exception):
    """The supplier record cannot be created/updated as given."""


def list_suppliers(
    session: Session,
    *,
    query: str | None = None,
    status: SupplierStatus | None = None,
) -> list[dict[str, object]]:
    """Suppliers for the grid, filtered by quick search and/or status.

    ``query`` ILIKE-matches ``business_name``, ``cuit`` or ``code``; ``status``
    filters the soft-delete lifecycle (default: every status).
    """
    stmt = select(Supplier)
    if status is not None:
        stmt = stmt.where(Supplier.status == status)
    if query and query.strip():
        needle = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                Supplier.business_name.ilike(needle),
                Supplier.cuit.ilike(needle),
                Supplier.code.ilike(needle),
            )
        )
    return [_row(supplier) for supplier in session.scalars(stmt.order_by(Supplier.business_name))]


def create_supplier(
    session: Session,
    *,
    business_name: str,
    code: str | None = None,
    cuit: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    whatsapp: str | None = None,
    contact_name: str | None = None,
    address: str | None = None,
    default_margin_pct: Decimal | float = 0,
    iva_condition: str | None = None,
    terms: str | None = None,
) -> Supplier:
    """Create a supplier, validating contact fields and resolving the code.

    ``code`` is suggested from ``business_name`` when omitted and resolved to a
    free variant via ``resolve_code`` (collision rotates over A-Z0-9; the DB
    unique index is the backstop). Status defaults to ACTIVO.
    """
    name = business_name.strip()
    if not name:
        raise InvalidSupplierDataError("business name is required")
    resolved_code = resolve_code(session, code or suggest_code(name))
    cuit_clean = _validate_optional_cuit(cuit)
    email_clean = _validate_optional_email(email)
    phone_clean = _validate_optional_phone(phone, whatsapp)
    supplier = Supplier(
        business_name=name,
        code=resolved_code,
        cuit=cuit_clean,
        email=email_clean,
        phone=phone_clean["phone"],
        whatsapp=phone_clean["whatsapp"],
        contact_name=contact_name.strip() if contact_name else None,
        address=address.strip() if address else None,
        default_margin_pct=_coerce_margin(default_margin_pct),
        iva_condition=_coerce_iva_condition(iva_condition),
        terms=terms.strip() if terms else None,
    )
    session.add(supplier)
    session.flush()
    return supplier


def update_supplier(
    session: Session,
    supplier_id: int,
    *,
    business_name: object = _UNSET,
    code: object = _UNSET,
    cuit: object = _UNSET,
    email: object = _UNSET,
    phone: object = _UNSET,
    whatsapp: object = _UNSET,
    contact_name: object = _UNSET,
    address: object = _UNSET,
    default_margin_pct: object = _UNSET,
    iva_condition: object = _UNSET,
    terms: object = _UNSET,
) -> Supplier:
    """Edit a supplier, re-validating contact fields on every change.

    Omitting a field (default) leaves it untouched; an explicit ``None`` or
    empty string clears nullable fields. Changing ``code`` is refused while the
    supplier is linked to any catalog/PO/need/mapping row (immutability guard).
    Editing the margin never re-prices existing catalog rows (future
    ingestions only).
    """
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise KeyError(f"unknown supplier: {supplier_id}")
    if code is not _UNSET:
        resolved = resolve_code(session, _as_text(code))
        _assert_code_not_linked(session, supplier, resolved)
        supplier.code = resolved
    if business_name is not _UNSET:
        supplier.business_name = _as_text(business_name)
    if cuit is not _UNSET:
        supplier.cuit = _validate_optional_cuit(_as_optional_text(cuit))
    if email is not _UNSET:
        supplier.email = _validate_optional_email(_as_optional_text(email))
    if phone is not _UNSET or whatsapp is not _UNSET:
        clean = _validate_optional_phone(
            _as_optional_text(phone) if phone is not _UNSET else supplier.phone or None,
            _as_optional_text(whatsapp) if whatsapp is not _UNSET else supplier.whatsapp or None,
        )
        if phone is not _UNSET:
            supplier.phone = clean["phone"]
        if whatsapp is not _UNSET:
            supplier.whatsapp = clean["whatsapp"]
    if contact_name is not _UNSET:
        supplier.contact_name = _as_optional_text(contact_name)
    if address is not _UNSET:
        supplier.address = _as_optional_text(address)
    if default_margin_pct is not _UNSET:
        supplier.default_margin_pct = _coerce_margin(default_margin_pct)
    if iva_condition is not _UNSET:
        supplier.iva_condition = _coerce_iva_condition(_as_optional_text(iva_condition))
    if terms is not _UNSET:
        supplier.terms = _as_optional_text(terms)
    session.flush()
    return supplier


def toggle_status(session: Session, supplier_id: int) -> Supplier:
    """Soft-delete / restore: flip the supplier's status (ACTIVO ↔ INACTIVO)."""
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise KeyError(f"unknown supplier: {supplier_id}")
    supplier.status = (
        SupplierStatus.INACTIVO
        if supplier.status is SupplierStatus.ACTIVO
        else SupplierStatus.ACTIVO
    )
    session.flush()
    return supplier


def _row(supplier: Supplier) -> dict[str, object]:
    return {
        "id": supplier.id,
        "code": supplier.code,
        "business_name": supplier.business_name,
        "cuit": supplier.cuit,
        "contact_name": supplier.contact_name,
        "phone": supplier.phone,
        "whatsapp": supplier.whatsapp,
        "email": supplier.email,
        "address": supplier.address,
        "default_margin_pct": str(supplier.default_margin_pct),
        "iva_condition": supplier.iva_condition.value if supplier.iva_condition else None,
        "terms": supplier.terms,
        "status": supplier.status.value,
    }


def _as_text(value: object) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise InvalidSupplierDataError("business name is required")
    return text


def _as_optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _validate_optional_cuit(cuit: str | None) -> str | None:
    if cuit is None or not cuit.strip():
        return None
    cleaned = cuit.strip()
    if not validate_cuit(cleaned):
        raise InvalidSupplierDataError(f"invalid CUIT: {cleaned}")
    return cleaned


def _validate_optional_email(email: str | None) -> str | None:
    if email is None or not email.strip():
        return None
    cleaned = email.strip()
    if not validate_email(cleaned):
        raise InvalidSupplierDataError(f"invalid email: {cleaned}")
    return cleaned


def _validate_optional_phone(
    phone: str | None, whatsapp: str | None
) -> dict[str, str | None]:
    phone_clean = normalize_e164_phone(phone) if phone and phone.strip() else None
    if phone and phone.strip() and phone_clean is None:
        raise InvalidSupplierDataError(f"invalid phone: {phone}")
    whatsapp_clean = normalize_whatsapp(whatsapp) if whatsapp and whatsapp.strip() else None
    if whatsapp and whatsapp.strip() and whatsapp_clean is None:
        raise InvalidSupplierDataError(f"invalid whatsapp: {whatsapp}")
    return {"phone": phone_clean, "whatsapp": whatsapp_clean}


def _coerce_margin(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(_CENT)


def _coerce_iva_condition(value: str | None) -> IvaCondition | None:
    if value is None or not str(value).strip():
        return None
    try:
        return IvaCondition(str(value).strip())
    except ValueError:
        raise InvalidSupplierDataError(f"invalid IVA condition: {value}") from None


def _assert_code_not_linked(session: Session, supplier: Supplier, new_code: str) -> None:
    """Refuse a ``code`` change while the supplier is referenced anywhere.

    Counts over the four linked models (Catalogo, SupplierPurchaseOrder,
    SourcingNeed, SupplierSkuMapping) by ``supplier_id``; any row means the code
    is part of the supplier's history and must stay immutable.
    """
    if supplier.code == new_code:
        return
    linked = (
        session.scalar(
            select(func.count(Catalogo.id)).where(Catalogo.supplier_id == supplier.id)
        )
        + session.scalar(
            select(func.count(SupplierPurchaseOrder.po_id)).where(
                SupplierPurchaseOrder.supplier_id == supplier.id
            )
        )
        + session.scalar(
            select(func.count(SourcingNeed.need_id)).where(
                SourcingNeed.supplier_id == supplier.id
            )
        )
        + session.scalar(
            select(func.count(SupplierSkuMapping.id)).where(
                SupplierSkuMapping.supplier_id == supplier.id
            )
        )
    )
    if linked > 0:
        raise InvalidSupplierDataError(
            f"code {new_code} is immutable: supplier {supplier.id} is linked to "
            f"{linked} catalog/PO/need/mapping rows"
        )