"""Google Sheets writer tests (task 3.3).

The append-only registration boundary is exercised with a mocked gspread
client: happy path appends the row and marks the order synced; every failure
path quarantines the row instead of raising into the order flow.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.config import Settings
from src.integrations.sheets import SheetsWriter, SheetsWriteStatus

CONFIGURED = Settings(
    google_sheets_credentials_file="creds.json", google_sheets_spreadsheet_key="sheet-123"
)


def _sheet_mock() -> MagicMock:
    return MagicMock()


def _writer(*, gc: MagicMock | None = None, **kwargs) -> SheetsWriter:
    return SheetsWriter(gc=gc, settings=CONFIGURED, **kwargs)


def test_append_success_registers_row_and_marks_synced():
    """Una fila válida se agrega a la hoja y el pedido queda sincronizado."""
    sheet = _sheet_mock()
    gc = MagicMock()
    gc.open_by_key.return_value.worksheet.return_value = sheet
    writer = _writer(gc=gc)
    status = writer.append_order_row(42, customer_name="Don Juan", total="720.00")
    assert status is SheetsWriteStatus.APPENDED
    assert writer.sheets_synced(42) is True
    sheet.append_row.assert_called_once()
    row = sheet.append_row.call_args.args[0]
    assert row[0] == "42"
    assert row[1] == "Don Juan"
    assert row[2] == "720.00"
    assert row[4]  # timestamp present
    assert sheet.append_row.call_args.kwargs["value_input_option"] == "USER_ENTERED"


def test_append_failure_quarantines_row_and_never_raises():
    """Si la hoja falla, la fila se aísla en cuarentena sin lanzar excepción."""
    sheet = _sheet_mock()
    sheet.append_row.side_effect = RuntimeError("network down")
    quarantine = _sheet_mock()
    gc = MagicMock()
    gc.open_by_key.return_value.worksheet.side_effect = [sheet, quarantine]
    writer = _writer(gc=gc)
    status = writer.append_order_row(7, customer_name="Ana", total="100.00")
    assert status is SheetsWriteStatus.QUARANTINED
    assert writer.sheets_synced(7) is False
    quarantine.append_row.assert_called_once()
    quarantined = quarantine.append_row.call_args.args[0]
    assert quarantined[0] == "7"
    assert quarantined[-1] == "network down"  # error appended for diagnosis


def test_append_failure_with_unreachable_quarantine_keeps_memory_log():
    """Si la cuarentena también falla, la fila queda registrada en memoria."""
    failing = _sheet_mock()
    failing.append_row.side_effect = RuntimeError("boom")
    gc = MagicMock()
    gc.open_by_key.return_value.worksheet.return_value = failing
    writer = _writer(gc=gc)
    status = writer.append_order_row(3, customer_name="Pepe")
    assert status is SheetsWriteStatus.QUARANTINED
    assert writer.quarantine_log
    assert writer.quarantine_log[0]["order_id"] == 3


def test_missing_credentials_quarantines_instead_of_raising():
    """Sin credenciales configuradas, la fila se cuarentena y no se lanza nada."""
    writer = SheetsWriter(gc=None, settings=Settings(google_sheets_credentials_file=""))
    status = writer.append_order_row(9, customer_name="Nadie")
    assert status is SheetsWriteStatus.QUARANTINED
    assert writer.sheets_synced(9) is False


def test_sheets_synced_reflects_only_successful_appends():
    """El registro sincronizado refleja solo los appends exitosos."""
    sheet = _sheet_mock()
    gc = MagicMock()
    gc.open_by_key.return_value.worksheet.return_value = sheet
    writer = _writer(gc=gc)
    writer.append_order_row(1)
    assert writer.sheets_synced(1) is True
    assert writer.sheets_synced(2) is False
