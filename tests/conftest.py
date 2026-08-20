"""Pytest fixtures for the super-warehouse test suite.

Provides an integration fixture backed by the real Postgres + pgvector running
in docker-compose. Tests that require the DB are tagged `@pytest.mark.integration`;
the fixture creates the schema fresh (via the ORM metadata) and drops it after.

RED tests for Phase 1 prove the data model and the Alembic migration produce the
design's tables and the `vector(1536)` column.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from src.config import get_settings
from src.db.models import Base

pytest_plugins: list[str] = []


@pytest.fixture(scope="session")
def db_engine():
    """Create a fresh schema on the shared Postgres for the whole test session."""
    engine = create_engine(get_settings().sqlalchemy_database_url, pool_pre_ping=True)
    # Drop + recreate so each test run starts clean.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
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
