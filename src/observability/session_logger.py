"""Session tracing and observability.

Provides context-bound session identification and human-readable, structured trace event logging
per session file under ``logs/sessions/{session_id}.log``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SESSIONS_DIR = Path(os.getenv("SESSION_LOGS_DIR", "logs/sessions"))

current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_session_id", default=None
)


def generate_session_id(sender_id: str | None = None) -> str:
    """Generate a unique timestamped session identifier.

    Format: ses_YYYYMMDD_HHMMSS_<sender>_<hex>
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
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


def format_event_for_human(event: dict[str, Any]) -> str:
    """Format a structured session event into clean, human-readable text for debugging."""
    ts = event.get("timestamp", "")
    if "T" in ts:
        # Format ISO timestamp to readable date time
        ts_part = ts.split(".")[0].replace("T", " ")
        time_str = f"{ts_part} UTC"
    else:
        time_str = ts

    service = str(event.get("service", "")).upper()
    action = str(event.get("action", ""))
    level = str(event.get("level", "INFO")).upper()
    details = event.get("details", {}) or {}

    lines: list[str] = [
        "--------------------------------------------------------------------------------",
        f"[{time_str}] [{level}] [{service} -> {action}]",
    ]

    # Service-specific human friendly formatting
    if action == "inbound_message":
        channel = details.get("channel", "chat")
        sender = details.get("sender_id", "unknown")
        text = details.get("text")
        media = details.get("media_type")
        lines.append(f"  Channel: {channel} | Sender: {sender}")
        if text is not None:
            lines.append(f'  Message: "{text}"')
        if media:
            lines.append(f"  Media: {media}")

    elif action == "outbound_reply":
        channel = details.get("channel", "chat")
        sender = details.get("sender_id", "unknown")
        reply = details.get("reply", "")
        lines.append(f"  Channel: {channel} | To: {sender}")
        lines.append("  Reply:")
        for r_line in str(reply).splitlines():
            lines.append(f"    {r_line}")

    elif action == "routing_decision":
        agent = details.get("agent", "")
        media_kind = details.get("media_kind")
        parsed = details.get("parsed", False)
        ctx = details.get("context_loaded", False)
        lines.append(f"  Routed Agent:    {agent}")
        lines.append(f"  Context Loaded:  {ctx} | Parsed: {parsed} | Media Kind: {media_kind}")

    elif action == "query_success":
        query = details.get("query", "")
        p_count = details.get("products_count", 0)
        lat = details.get("latency_sec", 0.0)
        lines.append(f'  RAG Query:       "{query}"')
        lines.append(f"  Results Found:   {p_count} product(s) (latency: {lat:.3f}s)")

    elif action == "query_refusal":
        query = details.get("query", "")
        lat = details.get("latency_sec", 0.0)
        lines.append(f'  RAG Query:       "{query}"')
        lines.append(f"  Status:          Refused / Out of Domain (latency: {lat:.3f}s)")

    elif action == "query_error":
        query = details.get("query", "")
        err = details.get("error") or details.get("status")
        lat = details.get("latency_sec", 0.0)
        lines.append(f'  RAG Query:       "{query}"')
        lines.append(f"  Error:           {err} (latency: {lat:.3f}s)")

    elif action == "order_draft_created":
        order_id = details.get("order_id")
        customer_id = details.get("customer_id")
        items_count = details.get("items_count", 0)
        lines.append(f"  Draft Order:     #{order_id} (Customer ID: {customer_id})")
        lines.append(f"  Items Count:     {items_count}")

    elif action == "item_confirmed":
        order_id = details.get("order_id")
        sku = details.get("sku")
        qty = details.get("quantity")
        lines.append(f"  Order #{order_id}: Confirmed SKU {sku} x {qty}")

    elif action == "rejection":
        channel = details.get("channel")
        sender = details.get("sender_id")
        reason = details.get("reason")
        lines.append(f"  Rejection:       Channel={channel} | Sender={sender} | Reason={reason}")

    else:
        for k, v in details.items():
            lines.append(f"  {k}: {v}")

    # Machine-readable JSON payload for automated parsing / backoffice
    json_line = json.dumps(event, ensure_ascii=False)
    lines.append(f"# JSON: {json_line}")

    return "\n".join(lines) + "\n"


def log_session_event(
    service: str,
    action: str,
    details: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    level: str = "INFO",
    log_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Record a human-readable and structured event in the session's log file.

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

    # Write human-readable block + machine-readable JSON to session log file
    target_dir = Path(log_dir) if log_dir is not None else DEFAULT_SESSIONS_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{sid}.log"
        is_new = not file_path.exists() or file_path.stat().st_size == 0
        with open(file_path, "a", encoding="utf-8") as f:
            if is_new:
                f.write(
                    "================================================================================\n"
                )
                f.write(f"SESSION LOG: {sid}\n")
                f.write(f"Created: {now_iso}\n")
                f.write(
                    "================================================================================\n\n"
                )
            f.write(format_event_for_human(event))
            f.write("\n")
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
    if not file_path.is_file():
        return []

    events: list[dict[str, Any]] = []
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if line_str.startswith("# JSON: "):
                    try:
                        events.append(json.loads(line_str[8:]))
                    except json.JSONDecodeError:
                        pass
                elif line_str.startswith("{") and line_str.endswith("}"):
                    try:
                        events.append(json.loads(line_str))
                    except json.JSONDecodeError:
                        pass
    except OSError as exc:
        logger.warning("Failed to read session events from %s: %s", file_path, exc)
    return events


def list_session_files(log_dir: Path | str | None = None) -> list[str]:
    """List available session IDs, ordered by most recently modified first."""
    target_dir = Path(log_dir) if log_dir is not None else DEFAULT_SESSIONS_DIR
    if not target_dir.is_dir():
        return []
    files = [f for f in target_dir.glob("*.log") if f.is_file() and f.stem != "unassigned"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [f.stem for f in files]
