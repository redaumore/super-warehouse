"""Backoffice session trace helpers.

Exposes session file listing and structured event loading for the Gradio UI.
"""

from __future__ import annotations

import json
from datetime import datetime

from src.observability.session_logger import list_session_files, read_session_events
from src.tz import to_buenos_aires


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
            # Event timestamps are UTC ISO; the grid shows Buenos Aires local time.
            try:
                ts = to_buenos_aires(datetime.fromisoformat(ts)).strftime("%H:%M:%S")
            except ValueError:
                pass  # keep the raw string for unparseable timestamps
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
