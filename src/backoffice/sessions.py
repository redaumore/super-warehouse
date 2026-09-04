"""Backoffice session trace helpers.

Exposes session file listing and structured event loading for the Gradio UI.
"""

from __future__ import annotations

import json

from src.observability.session_logger import list_session_files, read_session_events


def list_sessions() -> list[str]:
    """Return available session IDs, most recent first."""
    return list_session_files()


def session_events_grid(session_id: str | None) -> list[list[object]]:
    """Return tabular representation of events for the given session ID."""
    if not session_id:
        return []
    events = read_session_events(session_id)
    rows: list[list[object]] = []
    for ev in events:
        ts = ev.get("timestamp", "")
        if "T" in ts:
            ts = ts.split("T")[1][:8]  # HH:MM:SS
        rows.append(
            [
                ts,
                ev.get("service", ""),
                ev.get("action", ""),
                ev.get("level", "INFO"),
                json.dumps(ev.get("details", {}), ensure_ascii=False),
            ]
        )
    return rows
