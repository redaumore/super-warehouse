"""Database engine and session factory.

The engine URL is resolved from `src.config.Settings` so the whole app shares one
source of truth for connection config. `pool_pre_ping` avoids stale connections.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_settings

_settings = get_settings()

# tests/conftest.py sets SQLALCHEMY_DATABASE_URL before importing this module,
# so the app session factory targets the disposable ferreteria_test database
# during pytest runs instead of the dev database.
engine = create_engine(
    os.environ.get("SQLALCHEMY_DATABASE_URL", _settings.sqlalchemy_database_url),
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
