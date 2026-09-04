"""Session tracing and observability.

Provides context-bound session identification and structured trace event logging
per session file under ``logs/sessions/{session_id}.log``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SESSIONS_DIR = Path("logs/sessions")

current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_session_id", default=None
)


def generate_session_id(sender_id: str | None = None) -> str:
    """Generate a unique timestamped session identifier.

    Format: ses_YYYYMMDD_HHMMSS_<hex>
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(4)
    if sender_id:
        # Sanitize sender_id for filesystem safety
        safe_sender = "".join(c if c.isalnum() else "_" for c in str(sender_id))[:16]
        return f"ses_{ts}_{safe_sender}_{rand}"
    return f"ses_{ts}_{rand}"


def get_current_session_id() -> str | None:
    """Return the active session ID from the current context."""
    return current_session_id.get()


def set_current_session_id(session_id: str | None) -> contextvars.Token[str | None]:
    """Set the active session ID for the current context."""
    return current_session_id.set(session_id)


def log_session_event(
    service: str,
    action: str,
    details: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    level: str = "INFO",
    log_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Record a structured event in the session's log file and the standard logger.

    If session_id is not explicitly provided, it is retrieved from the context.
    If no session ID is found, 'unassigned' is used.
    """
    sid = session_id or get_current_session_id() or "unassigned"
    now_iso = datetime.now(UTC).isoformat()

    event: dict[str, Any] = {
        "timestamp": now_iso,
        "session_id": sid,
        "level": level.upper(),
        "service": service,
        "action": action,
        "details": details or {},
    }

    # Emit to standard logger
    service_logger = logging.getLogger(f"session.{service}")
    log_method = getattr(service_logger, level.lower(), service_logger.info)
    log_method("[%s] %s - %s: %s", sid, service, action, json.dumps(event["details"]))

    # Write to session log file
    target_dir = Path(log_dir) if log_dir is not None else DEFAULT_SESSIONS_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{sid}.log"
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Failed to write session trace to %s: %s", target_dir / f"{sid}.log", exc)

    return event


def read_session_events(
    session_id: str,
    log_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Read all structured events from a session's log file."""
    target_dir = Path(log_dir) if log_dir is not None else DEFAULT_SESSIONS_DIR
    file_path = target_dir / f"{session_id}.log"
    if not file_path.exists():
        return []

    events: list[dict[str, Any]] = []
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("Failed to read session log from %s: %s", file_path, exc)
    return events


def list_session_files(log_dir: Path | str | None = None) -> list[str]:
    """List available session IDs sorted by modification time (most recent first)."""
    target_dir = Path(log_dir) if log_dir is not None else DEFAULT_SESSIONS_DIR
    if not target_dir.exists():
        return []

    files = [f for f in target_dir.glob("*.log") if f.is_file()]
    # Sort by mtime descending
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [f.stem for f in files]
