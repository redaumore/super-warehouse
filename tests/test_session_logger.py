"""Tests for session logging and context management."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.observability.session_logger import (
    generate_session_id,
    get_current_session_id,
    list_session_files,
    log_session_event,
    read_session_events,
    set_current_session_id,
)


def test_generate_session_id() -> None:
    """Genera identificadores de sesión únicos y formateados."""
    sid1 = generate_session_id()
    sid2 = generate_session_id()
    assert sid1.startswith("ses_")
    assert sid2.startswith("ses_")
    assert sid1 != sid2

    sid_sender = generate_session_id("chat_12345")
    assert "chat_12345" in sid_sender


def test_contextvar_propagation() -> None:
    """Propaga el session_id a través de contextvars en el contexto síncrono."""
    assert get_current_session_id() is None
    set_current_session_id("ses_test_123")
    try:
        assert get_current_session_id() == "ses_test_123"
    finally:
        set_current_session_id(None)
    assert get_current_session_id() is None


@pytest.mark.asyncio
async def test_contextvar_async_isolation() -> None:
    """Aísla el session_id entre diferentes corutinas asincrónicas concurrentes."""

    async def worker(sid: str) -> str | None:
        set_current_session_id(sid)
        await asyncio.sleep(0.01)
        return get_current_session_id()

    r1, r2 = await asyncio.gather(worker("ses_a"), worker("ses_b"))
    assert r1 == "ses_a"
    assert r2 == "ses_b"


def test_log_session_event_and_read(tmp_path: Path) -> None:
    """Registra eventos estructurados en el archivo de log individual de la sesión."""
    sid = "ses_custom_999"
    event = log_session_event(
        "test_service",
        "test_action",
        {"foo": "bar", "count": 42},
        session_id=sid,
        log_dir=tmp_path,
    )

    assert event["session_id"] == sid
    assert event["service"] == "test_service"
    assert event["action"] == "test_action"
    assert event["details"]["count"] == 42

    log_file = tmp_path / f"{sid}.log"
    assert log_file.exists()

    events = read_session_events(sid, log_dir=tmp_path)
    assert len(events) == 1
    assert events[0]["action"] == "test_action"
    assert events[0]["details"]["foo"] == "bar"

    files = list_session_files(log_dir=tmp_path)
    assert files == [sid]


def test_backoffice_sessions_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El backoffice puede listar archivos de sesión y renderizar eventos tabulares."""
    from src.backoffice.sessions import list_sessions, session_events_grid

    monkeypatch.setattr("src.observability.session_logger.DEFAULT_SESSIONS_DIR", tmp_path)
    assert list_sessions() == []
    assert session_events_grid(None) == []

    sid = "ses_bo_test"
    log_session_event("rag", "query", {"q": "clavos"}, session_id=sid, log_dir=tmp_path)

    assert list_sessions() == [sid]
    grid = session_events_grid(sid)
    assert len(grid) == 1
    # grid row: [ts, service, action, level, details_str]
    assert grid[0][1] == "rag"
    assert grid[0][2] == "query"
    assert grid[0][3] == "INFO"
    assert "clavos" in grid[0][4]
