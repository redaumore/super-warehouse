"""Backoffice clients & price-lists editor (task 3.7).

Pure DB operations behind the Gradio clients tab: browse clients, register a
new client (the spec's "flagged unknown phone → owner registers it") and edit
their commercial condition — WhatsApp phone, commercial name, assigned price
list and particular discount. No credit/payment fields (out of MVP scope).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.customer import normalize_phone
from src.db.models import Cliente, ListaPrecios

_CENT = Decimal("0.01")


class InvalidClientDataError(Exception):
    """The client record cannot be created/updated as given."""


def list_price_lists(session: Session) -> list[dict[str, object]]:
    """Every price list for the dropdown (Base = 0%, Gremio A = 10%, Gremio B = 20%)."""
    return [
        {
            "lista_id": lista.lista_id,
            "nombre": lista.nombre,
            "descuento_lista_pct": str(lista.descuento_lista_pct),
        }
        for lista in session.scalars(select(ListaPrecios).order_by(ListaPrecios.lista_id))
    ]


def default_price_list_id(session: Session) -> int:
    """The price list a client created in chat is assigned to (locked input: Base).

    Returns the ``Base`` list when present; falls back to the lowest-id list so
    a store that renamed the lists still works. Raises ``InvalidClientDataError``
    when no price list exists at all (the chat create path has nothing to
    assign).
    """
    base = session.scalar(select(ListaPrecios).where(ListaPrecios.nombre.ilike("base")))
    if base is not None:
        return base.lista_id
    first = session.scalar(select(ListaPrecios).order_by(ListaPrecios.lista_id).limit(1))
    if first is None:
        raise InvalidClientDataError("no price list exists to assign a new client")
    return first.lista_id


def list_clients(session: Session) -> list[dict[str, object]]:
    """Every client row for the grid: name, phone, list, particular discount."""
    rows = []
    for client in session.scalars(select(Cliente).order_by(Cliente.nombre_comercial)):
        rows.append(
            {
                "customer_id": client.customer_id,
                "nombre_comercial": client.nombre_comercial,
                "telefono_norm": client.telefono_norm,
                "lista_precios_id": client.lista_precios_id,
                "descuento_particular_pct": str(client.descuento_particular_pct),
            }
        )
    return rows


def create_client(
    session: Session,
    *,
    nombre_comercial: str,
    telefono_raw: str,
    lista_precios_id: int,
    descuento_particular_pct: Decimal | float = 0,
) -> Cliente:
    """Register a new client, normalizing the phone to the canonical E.164 form."""
    nombre = nombre_comercial.strip()
    if not nombre:
        raise InvalidClientDataError("commercial name is required")
    normalized = normalize_phone(telefono_raw)
    if normalized is None:
        raise InvalidClientDataError(f"invalid phone: {telefono_raw}")
    if session.scalar(select(Cliente).where(Cliente.telefono_norm == normalized)) is not None:
        raise InvalidClientDataError(f"phone already registered: {normalized}")
    client = Cliente(
        nombre_comercial=nombre,
        telefono_norm=normalized,
        lista_precios_id=lista_precios_id,
        descuento_particular_pct=Decimal(str(descuento_particular_pct)).quantize(_CENT),
    )
    session.add(client)
    session.flush()
    return client


def update_client(
    session: Session,
    customer_id: int,
    *,
    nombre_comercial: str | None = None,
    telefono_raw: str | None = None,
    lista_precios_id: int | None = None,
    descuento_particular_pct: Decimal | float | None = None,
) -> Cliente:
    """Edit a client's commercial condition; phone re-normalization when given."""
    client = session.get(Cliente, customer_id)
    if client is None:
        raise KeyError(f"unknown customer: {customer_id}")
    if nombre_comercial is not None:
        client.nombre_comercial = nombre_comercial.strip()
    if telefono_raw is not None:
        normalized = normalize_phone(telefono_raw)
        if normalized is None:
            raise InvalidClientDataError(f"invalid phone: {telefono_raw}")
        client.telefono_norm = normalized
    if lista_precios_id is not None:
        client.lista_precios_id = lista_precios_id
    if descuento_particular_pct is not None:
        client.descuento_particular_pct = Decimal(str(descuento_particular_pct)).quantize(_CENT)
    session.flush()
    return client
