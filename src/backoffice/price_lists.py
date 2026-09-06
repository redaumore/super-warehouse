"""Backoffice price lists CRUD (Settings tab block).

suppliers.py-pattern module behind the Gradio "Settings → Listas de precios"
block: list (with per-list client counts), create, edit and hard delete.

Storage convention (pricing engine contract): ``descuento_lista_pct`` holds a
FRACTION — ``0.10`` means 10% off, ``-0.05`` means a 5% surcharge (recargo).
The Gradio handlers convert percentage points ↔ fractions; this service stores
the value it is given, validated as a ``Numeric(5,2)``-compatible Decimal.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import Cliente, ListaPrecios

_CENT = Decimal("0.01")
# Numeric(5,2) bounds: the widest fraction the column can store.
_MAX_DISCOUNT = Decimal("999.99")


class InvalidPriceListDataError(Exception):
    """The price list record cannot be created/updated/deleted as given."""


def list_price_lists(session: Session) -> list[dict[str, object]]:
    """Every price list for the Settings grid, oldest first, with client counts.

    ``descuento_lista_pct`` is the stored FRACTION (0.10 = 10%); the UI layer
    converts to percentage points for display.
    """
    counts = (
        select(
            Cliente.lista_precios_id.label("lista_id"),
            func.count(Cliente.customer_id).label("clientes"),
        )
        .group_by(Cliente.lista_precios_id)
        .subquery()
    )
    stmt = (
        select(ListaPrecios, func.coalesce(counts.c.clientes, 0))
        .outerjoin(counts, counts.c.lista_id == ListaPrecios.lista_id)
        .order_by(ListaPrecios.lista_id)
    )
    return [
        {
            "lista_id": lista.lista_id,
            "nombre": lista.nombre,
            "descuento_lista_pct": str(lista.descuento_lista_pct),
            "clientes": int(clientes),
        }
        for lista, clientes in session.execute(stmt)
    ]


def create_price_list(
    session: Session,
    *,
    nombre: str,
    descuento_pct: Decimal | float | str | None,
) -> ListaPrecios:
    """Create a price list; the discount is stored AS GIVEN (fraction).

    The name must be non-empty and unique (case-insensitive check — friendlier
    and strictly stricter than the exact-match DB unique index, which stays as
    the backstop).
    """
    lista = ListaPrecios(
        nombre=_validated_name(session, nombre),
        descuento_lista_pct=_coerce_discount(descuento_pct),
    )
    session.add(lista)
    session.flush()
    return lista


def update_price_list(
    session: Session,
    lista_id: int,
    *,
    nombre: str,
    descuento_pct: Decimal | float | str | None,
) -> ListaPrecios:
    """Edit a price list's name and/or discount, excluding itself from the
    unique-name check so resubmitting the current name (as the UI does) keeps it."""
    lista = session.get(ListaPrecios, lista_id)
    if lista is None:
        raise KeyError(f"unknown price list: {lista_id}")
    lista.nombre = _validated_name(session, nombre, exclude_id=lista_id)
    lista.descuento_lista_pct = _coerce_discount(descuento_pct)
    session.flush()
    return lista


def delete_price_list(session: Session, lista_id: int) -> str:
    """Hard-delete a price list and return its name.

    Refused with a clear error while any ``Cliente`` still references the list —
    the FK is NOT NULL, so deleting an assigned list would orphan clients; they
    must be reassigned first.
    """
    lista = session.get(ListaPrecios, lista_id)
    if lista is None:
        raise KeyError(f"unknown price list: {lista_id}")
    linked = (
        session.scalar(
            select(func.count(Cliente.customer_id)).where(Cliente.lista_precios_id == lista_id)
        )
        or 0
    )
    if linked:
        raise InvalidPriceListDataError(
            f"price list '{lista.nombre}' is assigned to {linked} client(s); "
            "reassign them before deleting"
        )
    session.delete(lista)
    session.flush()
    return lista.nombre


def _validated_name(session: Session, nombre: object, *, exclude_id: int | None = None) -> str:
    name = str(nombre).strip() if nombre is not None else ""
    if not name:
        raise InvalidPriceListDataError("price list name is required")
    stmt = select(ListaPrecios).where(ListaPrecios.nombre.ilike(name))
    if exclude_id is not None:
        stmt = stmt.where(ListaPrecios.lista_id != exclude_id)
    if session.scalar(stmt) is not None:
        raise InvalidPriceListDataError(f"price list name already exists: {name}")
    return name


def _coerce_discount(value: Decimal | float | str | None) -> Decimal:
    """Coerce the discount to a 2-decimal Decimal within Numeric(5,2) bounds."""
    if value is None:
        raise InvalidPriceListDataError("discount is required")
    try:
        discount = Decimal(str(value)).quantize(_CENT)
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidPriceListDataError(f"invalid discount: {value!r}") from None
    if abs(discount) > _MAX_DISCOUNT:
        raise InvalidPriceListDataError(f"discount out of range: {discount}")
    return discount
