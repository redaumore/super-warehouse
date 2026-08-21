"""Phone normalization tests (tasks 2.3 / Phase 4.2).

Pure normalization variants are unit tests; the KNOWN / UNKNOWN / INVALID
lookup outcomes exercise the `clientes` table against the real Postgres
fixture (skipped cleanly when Postgres is not running).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from src.agents.customer import PhoneStatus, lookup_phone, normalize_phone
from src.config import get_settings
from src.db.models import Cliente, ListaPrecios

_CANONICAL = "+5491155551234"

# The same Argentine mobile number in every plausible inbound format.
PHONE_VARIANTS = [
    "+54 9 11 5555-1234",
    "+5491155551234",
    "5491155551234",
    "11 5555 1234",
    "011-5555-1234",
    "(011) 5555 1234",
]


def _postgres_up() -> bool:
    try:
        engine = create_engine(
            get_settings().sqlalchemy_database_url, connect_args={"connect_timeout": 2}
        )
        with engine.connect():
            pass
        engine.dispose()
        return True
    except (OperationalError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _postgres_up(), reason="Postgres not running (make db-up)")


@pytest.mark.parametrize("variant", PHONE_VARIANTS)
def test_phone_format_variants_normalize_to_same_number(variant):
    """All spacing / country-code variants reconcile to one canonical E.164."""
    assert normalize_phone(variant) == _CANONICAL


@pytest.mark.parametrize("raw", ["", "abc", "5555", "12"])
def test_unparseable_phone_normalizes_to_none(raw):
    assert normalize_phone(raw) is None


@pytest.fixture(autouse=True)
def _clean_schema(db_engine):
    yield
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE order_items, orders, stock_reservations, catalogo, proveedores, "
                "clientes, lista_precios RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def registered_client(db_session):
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(
        Cliente(
            customer_id=1,
            nombre_comercial="Ferretería Don Juan",
            contacto="Juan",
            telefono_norm=_CANONICAL,
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.flush()
    return db_session


def test_known_phone_matches_registered_customer(registered_client):
    """A registered number — even re-typed in another format — resolves KNOWN."""
    result = lookup_phone(registered_client, "11 5555 1234")
    assert result.status is PhoneStatus.KNOWN
    assert result.normalized == _CANONICAL
    assert result.customer is not None
    assert result.customer.customer_id == 1


def test_unknown_phone_is_flagged_for_onboarding(registered_client):
    """A parseable but unregistered number is flagged UNKNOWN, never guessed."""
    result = lookup_phone(registered_client, "+54 9 11 3333-4444")
    assert result.status is PhoneStatus.UNKNOWN
    assert result.normalized == "+5491133334444"
    assert result.customer is None


def test_invalid_phone_is_flagged_not_guessed(registered_client):
    """An unparseable number is flagged INVALID with no normalized form."""
    result = lookup_phone(registered_client, "no-es-un-telefono")
    assert result.status is PhoneStatus.INVALID
    assert result.normalized is None
    assert result.customer is None
