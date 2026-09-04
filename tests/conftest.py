"""Pytest fixtures for the super-warehouse test suite.

Provides an integration fixture backed by the real Postgres + pgvector running
in docker-compose, isolated on a disposable `ferreteria_test` database. Tests
that require the DB are tagged `@pytest.mark.integration`; the schema is built
from the committed Alembic migrations on every test session and dropped at
teardown — only the test database is ever touched, never the dev database.

RED tests for Phase 1 prove the data model and the Alembic migration produce the
design's tables and the `vector(1536)` column.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from src.config import get_settings
from src.db.models import Base

pytest_plugins: list[str] = []

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

# Alembic must migrate the disposable test database, not the dev database
# (alembic/env.py honors ALEMBIC_DATABASE_URL over the app's DATABASE_URL).
os.environ["ALEMBIC_DATABASE_URL"] = get_settings().sqlalchemy_test_database_url

# Point the app session factory at the test database too: tests that go through
# src.db.session.SessionLocal (pipeline, backoffice) would otherwise hit the dev
# database. This must run before any test module imports src.db.session.
os.environ["SQLALCHEMY_DATABASE_URL"] = get_settings().sqlalchemy_test_database_url

# Every table the integration fixtures must reset between tests (sourcing axis
# tables first — they carry FKs into orders/catalogo/suppliers). Pricing settings
# are included so one test cannot leak a rate or default margin into another.
TRUNCATE_TABLES = (
    "supplier_purchase_order_items, supplier_purchase_orders, sourcing_needs, "
    "inventory, order_items, orders, stock_reservations, catalogo, suppliers, "
    "clientes, lista_precios, supplier_sku_mappings, stock_adjustments, "
    "exchange_rates, app_settings"
)


def _ensure_test_database() -> None:
    """Create the disposable test database if it does not exist yet."""
    test_url = make_url(get_settings().sqlalchemy_test_database_url)
    admin_url = test_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": test_url.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def db_engine():
    """Rebuild the test schema from the Alembic migrations for the session."""
    _ensure_test_database()
    engine = create_engine(get_settings().sqlalchemy_test_database_url, pool_pre_ping=True)
    # Clear leftovers from a previous run before rebuilding from migrations.
    Base.metadata.drop_all(engine)
    cfg = AlembicConfig(str(_ALEMBIC_INI))
    command.stamp(cfg, "base")
    command.upgrade(cfg, "head")
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Yield a transactional session against the created schema."""
    Session = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def db_inspector(db_engine):
    """Provide a SQLAlchemy inspector for the live schema."""
    return inspect(db_engine)


@pytest.fixture
def clean_schema(db_engine):
    """Truncate every table after the test (shared by the sourcing suites)."""
    yield
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TRUNCATE_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def _isolate_session_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect session traces in all tests to tmp_path to prevent polluting logs/sessions."""
    session_dir = tmp_path / "test_sessions"
    monkeypatch.setattr("src.observability.session_logger.DEFAULT_SESSIONS_DIR", session_dir)
