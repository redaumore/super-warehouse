"""Tests for Buenos Aires timezone display helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from src.tz import BUENOS_AIRES, now_buenos_aires, to_buenos_aires


def test_to_buenos_aires_assumes_utc_for_naive_datetime():
    """Naive input is treated as UTC and shifted to UTC-3 for display."""
    naive = datetime(2026, 1, 1, 0, 30, 0)  # noqa: DTZ001 — naive input is the case under test
    result = to_buenos_aires(naive)
    assert result == datetime(2025, 12, 31, 21, 30, 0, tzinfo=BUENOS_AIRES)
    assert result.utcoffset().total_seconds() == -3 * 3600


def test_to_buenos_aires_converts_aware_datetime():
    """Aware UTC input converts preserving the same instant."""
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    result = to_buenos_aires(aware)
    assert result == datetime(2026, 1, 1, 9, 0, 0, tzinfo=BUENOS_AIRES)
    assert result.utcoffset().total_seconds() == -3 * 3600


def test_to_buenos_aires_offset_label_is_minus_three():
    """The rendered ISO offset is -03:00 (no DST in Argentina)."""
    result = to_buenos_aires(datetime(2026, 7, 15, 18, 0, 0, tzinfo=UTC))
    assert result.isoformat().endswith("-03:00")


def test_now_buenos_aires_returns_aware_art_datetime():
    """now_buenos_aires always returns a timezone-aware Buenos Aires datetime."""
    result = now_buenos_aires()
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == -3 * 3600
