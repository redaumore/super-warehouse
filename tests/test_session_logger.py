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


def test_human_readable_log_format(tmp_path: Path) -> None:
    """Verifica que el archivo de log contenga cabecera y bloques legibles para debugging."""
    sid = "ses_debug_123"
    log_session_event(
        "telegram",
        "inbound_message",
        {"channel": "telegram", "sender_id": "12345", "text": "amoladora recta"},
        session_id=sid,
        log_dir=tmp_path,
    )
    log_file = tmp_path / f"{sid}.log"
    content = log_file.read_text(encoding="utf-8")
    assert f"SESSION LOG: {sid}" in content
    assert "[TELEGRAM -> inbound_message]" in content
    assert 'Message: "amoladora recta"' in content
    assert "# JSON: " in content


def test_human_readable_dispatch_and_approval_logs(tmp_path: Path) -> None:
    """Verifica que los eventos de dispatch y approval se formateen con diagnóstico legible."""
    sid = "ses_approval_123"
    log_session_event(
        "dispatch",
        "decision_parsed",
        {"order_id": 8, "action": "APPROVE", "adjustments_count": 0},
        session_id=sid,
        log_dir=tmp_path,
    )
    log_session_event(
        "orders",
        "order_classified",
        {
            "order_id": 8,
            "case": "C",
            "missing_count": 1,
            "missing_items": [
                {
                    "sku": "AT-7033",
                    "missing_quantity": 3,
                    "candidates_count": 0,
                }
            ],
        },
        session_id=sid,
        log_dir=tmp_path,
    )
    log_session_event(
        "orders",
        "order_cancelled_case_c",
        {
            "order_id": 8,
            "actor": "owner",
            "reason": "missing_stock_no_suppliers",
            "missing_skus": ["AT-7033"],
        },
        session_id=sid,
        log_dir=tmp_path,
        level="WARNING",
    )
    log_session_event(
        "dispatch",
        "decision_approved",
        {
            "order_id": 8,
            "cancelled_case": True,
            "missing_count": 1,
            "sheets_status": "SKIPPED",
        },
        session_id=sid,
        log_dir=tmp_path,
    )

    log_file = tmp_path / f"{sid}.log"
    content = log_file.read_text(encoding="utf-8")
    assert "Dispatch Decision: APPROVE for Order #8" in content
    assert "Order #8 Classification: Case C" in content
    assert "- SKU AT-7033: missing 3 (candidates: 0)" in content
    assert "Order #8 Cancelled (Case C): missing_stock_no_suppliers" in content
    assert "Unavailable SKUs: AT-7033" in content
    assert "Dispatch Approved: Order #8 (Cancelled Case C: True | Sheets: SKIPPED)" in content


def test_format_event_for_human_renders_art_header_and_keeps_utc_json():
    """The human header shows Buenos Aires local time; the JSON payload stays UTC ISO."""
    from src.observability.session_logger import format_event_for_human

    event = {
        "timestamp": "2026-01-01T00:30:00+00:00",
        "session_id": "ses_tz_test",
        "level": "INFO",
        "service": "test",
        "action": "generic",
        "details": {"foo": "bar"},
    }
    rendered = format_event_for_human(event)
    assert "[2025-12-31 21:30:00 ART] [INFO] [TEST -> generic]" in rendered
    # Machine contract: the JSON payload still carries the original UTC ISO string.
    assert '"timestamp": "2026-01-01T00:30:00+00:00"' in rendered


def test_format_event_for_human_falls_back_to_raw_timestamp_on_parse_error():
    """Unparseable timestamps are rendered as-is instead of raising."""
    from src.observability.session_logger import format_event_for_human

    event = {
        "timestamp": "not-a-timestamp-T-but-has-T",
        "session_id": "ses_tz_test",
        "level": "INFO",
        "service": "test",
        "action": "generic",
        "details": {},
    }
    rendered = format_event_for_human(event)
    assert "[not-a-timestamp-T-but-has-T]" in rendered


def test_log_file_created_header_renders_buenos_aires_local_time(tmp_path: Path) -> None:
    """The 'Created:' header carries Buenos Aires local wall-clock time."""
    sid = "ses_created_header"
    log_session_event("test", "created_header", {}, session_id=sid, log_dir=tmp_path)
    content = (tmp_path / f"{sid}.log").read_text(encoding="utf-8")
    created_line = next(line for line in content.splitlines() if line.startswith("Created: "))
    assert created_line.endswith("-03:00")


def test_session_events_grid_renders_art_time_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backoffice grid shows the event time in Buenos Aires local time."""
    import json as jsonlib

    from src.backoffice.sessions import session_events_grid

    monkeypatch.setattr("src.observability.session_logger.DEFAULT_SESSIONS_DIR", tmp_path)
    sid = "ses_grid_art"
    event = {
        "timestamp": "2026-01-01T00:30:00+00:00",
        "session_id": sid,
        "level": "INFO",
        "service": "rag",
        "action": "query",
        "details": {"q": "clavos"},
    }
    with open(tmp_path / f"{sid}.log", "a", encoding="utf-8") as f:
        f.write(f"# JSON: {jsonlib.dumps(event)}\n")

    grid = session_events_grid(sid)
    assert len(grid) == 1
    # 00:30 UTC -> 21:30 of the previous day in Buenos Aires (UTC-3)
    assert grid[0][0] == "21:30:00"
