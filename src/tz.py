"""Buenos Aires timezone helpers.

Persisted data stays UTC; presentation layers (session logs, backoffice,
Sheets) convert to America/Argentina/Buenos_Aires (UTC-3, no DST) on display.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")


def to_buenos_aires(dt: datetime) -> datetime:
    """Convert a datetime to Buenos Aires local time; naive input is assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BUENOS_AIRES)


def now_buenos_aires() -> datetime:
    """Return the current time in Buenos Aires local time."""
    return datetime.now(BUENOS_AIRES)
