"""Property-based inventory invariants (matrix L6, hypothesis).

Every hypothesis example drives the REAL Postgres test DB with the production
`reserve_stock`/`available_stock` SQL path (no in-memory mirror), applying an
arbitrary sequence of reservation requests (per-SKU quantities including 0, 1,
exact available, available+1 and beyond) and asserting after each step:

1. `Inventory.quantity_on_hand` never changes from reservations alone
   (soft-locks do not deduct).
2. `available_stock` is never negative at any point.
3. A refused reservation (`InsufficientStockError` / `ValueError` for
   non-positive quantities) leaves the state untouched: no new
   `StockReservation` row, identical availability.

Approach: DB-backed only. The invariants read through the same SQL formula the
agent uses in production, so an in-memory mirror would risk diverging from the
real semantics (e.g. `make_interval` TTL filtering); at 25 examples × ≤8
requests against the local docker Postgres the test stays in the ~seconds
range (`deadline=None` absorbs first-connection latency). The
expired/non-ACTIVE exclusion invariant is already unit-covered in
test_inventory.py (`test_non_active_reservations_do_not_lock_stock`,
`test_expired_ttl_reservation_does_not_lock_stock`) and is deliberately not
duplicated here.

Note: the concurrent TOCTOU double-booking race (matrix L5) is covered and
fixed in test_inventory.py::test_two_session_reserve_race_at_most_one_succeeds
(`SELECT ... FOR UPDATE` on the Inventory row); these properties are
sequential, single-session, and stay orthogonal to that guarantee.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.exc import OperationalError

from src.agents.inventory import InsufficientStockError, available_stock, reserve_stock
from src.config import get_settings
from src.db.models import (
    Cliente,
    Inventory,
    ListaPrecios,
    StockReservation,
)


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

SKUS = ("CLV-001", "CLV-002", "CLV-003")
ON_HAND = 4  # per SKU; quantities explore 0..ON_HAND+2 (exact, exact+1, far over)

requests_strategy = st.lists(
    st.tuples(st.sampled_from(SKUS), st.integers(min_value=0, max_value=ON_HAND + 2)),
    min_size=1,
    max_size=8,
)


@pytest.fixture(autouse=True)
def _clean_schema(db_engine):
    yield
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE inventory, stock_reservations, clientes, lista_precios "
                "RESTART IDENTITY CASCADE"
            )
        )


def _reset_inventory(session) -> dict[str, int]:
    """Restore every SKU to a fresh ON_HAND with zero reservations; return on-hand."""
    session.execute(delete(StockReservation))
    session.execute(delete(Inventory))
    for sku in SKUS:
        session.add(Inventory(sku_id=sku, quantity_on_hand=ON_HAND))
    if session.get(Cliente, 1) is None:
        session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
        session.add(
            Cliente(
                customer_id=1,
                nombre_comercial="Property Owner",
                telefono_norm="+5491155550000",
                lista_precios_id=1,
                descuento_particular_pct=Decimal(0),
            )
        )
    session.commit()
    on_hand = {
        sku: session.scalar(select(Inventory.quantity_on_hand).where(Inventory.sku_id == sku))
        for sku in SKUS
    }
    return on_hand


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(requests=requests_strategy)
def test_arbitrary_reservation_sequences_preserve_stock_invariants(db_session, requests):
    """Bajo cualquier secuencia de reservas, el stock en mano no cambia y la disponibilidad nunca es negativa."""
    session = db_session
    on_hand = _reset_inventory(session)

    for sku, cantidad in requests:
        rows_before = session.scalar(select(func.count()).select_from(StockReservation))
        avail_before = available_stock(session, sku)
        try:
            reserve_stock(session, sku, customer_id=1, cantidad=cantidad)
        except (InsufficientStockError, ValueError):
            # Invariant 3: a refused reservation leaves the state untouched.
            assert (
                session.scalar(select(func.count()).select_from(StockReservation)) == rows_before
            ), f"refused {sku} x{cantidad} created a StockReservation row"
            assert available_stock(session, sku) == avail_before, (
                f"refused {sku} x{cantidad} changed availability"
            )
            continue

        # Invariant 2: availability never goes negative after an accepted lock.
        assert available_stock(session, sku) >= 0, f"negative availability for {sku}"
        # Invariant 1: reservations never deduct on-hand stock.
        assert (
            session.scalar(select(Inventory.quantity_on_hand).where(Inventory.sku_id == sku))
            == on_hand[sku]
        ), f"quantity_on_hand changed for {sku} after reserving {cantidad}"

    # Final sweep: every SKU invariant holds after the whole sequence.
    for sku in SKUS:
        assert available_stock(session, sku) >= 0
        assert (
            session.scalar(select(Inventory.quantity_on_hand).where(Inventory.sku_id == sku))
            == on_hand[sku]
        )
