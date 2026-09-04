"""Google Sheets integration (task 3.3): append-only order registration.

Approved orders are appended to a spreadsheet as rows. The writer is
append-only by design (no updates, no deletes) and NEVER raises into the order
flow: any failure — credentials missing, sheet missing, network error — logs
the error and quarantines the row (appended to a quarantine sheet when
possible, otherwise held in an in-memory log) so registration is observable
and recoverable without blocking the owner confirmation.

The gspread client is constructor-injected for tests; when omitted it is built
from the service-account credentials file in settings.
"""

from __future__ import annotations

import enum
import logging
from datetime import UTC, datetime
from typing import Any

import gspread
from gspread.utils import ValueInputOption

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


class SheetsWriteStatus(str, enum.Enum):
    """Outcome of registering one order row in the spreadsheet."""

    APPENDED = "APPENDED"
    QUARANTINED = "QUARANTINED"
    SKIPPED = "SKIPPED"  # no write attempted (e.g. a Case C confirm cancelled the order)


class SheetsWriter:
    """Append-only writer that quarantines failed rows instead of raising."""

    def __init__(
        self,
        gc: gspread.Client | None = None,
        *,
        spreadsheet_key: str | None = None,
        sheet_name: str = "Pedidos",
        quarantine_sheet_name: str = "Cuarentena",
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._gc = gc
        self._spreadsheet_key = spreadsheet_key or self.settings.google_sheets_spreadsheet_key
        self.sheet_name = sheet_name
        self.quarantine_sheet_name = quarantine_sheet_name
        # In-memory registry of successfully registered order ids (monitor view)
        # and of rows that could not even reach the quarantine sheet.
        self._synced: set[int] = set()
        self._quarantine_log: list[dict[str, Any]] = []

    def _open_sheet(self, name: str) -> gspread.Worksheet:
        if self._gc is None:
            if not self.settings.google_sheets_credentials_file:
                raise RuntimeError("google sheets credentials not configured")
            self._gc = gspread.service_account(
                filename=self.settings.google_sheets_credentials_file
            )
        if not self._spreadsheet_key:
            raise RuntimeError("google sheets spreadsheet key not configured")
        return self._gc.open_by_key(self._spreadsheet_key).worksheet(name)

    def append_order_row(
        self,
        order_id: int,
        *,
        customer_name: str | None = None,
        total: str | None = None,
        items_summary: str = "",
        registered_at: datetime | None = None,
    ) -> SheetsWriteStatus:
        """Append one order row; on any failure quarantine it (never raise)."""
        timestamp = (registered_at or datetime.now(UTC)).isoformat()
        row: list[str] = [
            str(order_id),
            customer_name or "",
            total or "",
            items_summary,
            timestamp,
        ]
        try:
            sheet = self._open_sheet(self.sheet_name)
            sheet.append_row(row, value_input_option=ValueInputOption.user_entered)
        except Exception as exc:  # noqa: BLE001 — the boundary must never raise
            logger.error("sheets append failed for order %s: %s", order_id, exc)
            return self._quarantine(order_id, row, exc)
        self._synced.add(order_id)
        return SheetsWriteStatus.APPENDED

    def _quarantine(self, order_id: int, row: list[str], error: Exception) -> SheetsWriteStatus:
        """Isolate a failed row: quarantine sheet when reachable, else in-memory."""
        try:
            sheet = self._open_sheet(self.quarantine_sheet_name)
            sheet.append_row([*row, str(error)], value_input_option=ValueInputOption.user_entered)
        except Exception as qexc:  # noqa: BLE001
            logger.error("quarantine append failed for order %s: %s", order_id, qexc)
            self._quarantine_log.append(
                {
                    "order_id": order_id,
                    "row": row,
                    "error": str(error),
                    "quarantine_error": str(qexc),
                }
            )
        return SheetsWriteStatus.QUARANTINED

    def sheets_synced(self, order_id: int) -> bool:
        """True when the order was successfully registered (monitor view)."""
        return order_id in self._synced

    @property
    def quarantine_log(self) -> list[dict[str, Any]]:
        """Rows held in memory because even the quarantine sheet failed."""
        return list(self._quarantine_log)
