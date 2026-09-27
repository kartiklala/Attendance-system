"""Google Sheets access via a service account (backend-only, never frontend).

Two worksheets are used from the configured spreadsheet:
- settings.GOOGLE_STUDENTS_SHEET_NAME: authoritative student roster
  (columns: Enrollment No | Name)
- settings.GOOGLE_SHEET_NAME: attendance log
  (columns: Timestamp | Enrollment No | Student Name | Status | Session-ID)
"""
import logging
from datetime import datetime, timezone

import gspread

from app.core.config import settings
from app.services.errors import ServiceUnavailableError

logger = logging.getLogger(__name__)

_client: gspread.Client | None = None


def _get_client() -> gspread.Client:
    global _client
    if _client is not None:
        return _client
    path = settings.GOOGLE_SERVICE_ACCOUNT_FILE
    if not path:
        raise ServiceUnavailableError("Google Sheets is not configured.")
    resolved = settings.resolve_path(path)
    if not resolved.exists():
        raise ServiceUnavailableError("Google Sheets credentials not found.")
    try:
        _client = gspread.service_account(filename=str(resolved))
        return _client
    except Exception:
        logger.exception("Failed to open Google Sheets service account")
        raise ServiceUnavailableError("Could not connect to Google Sheets.")


def _open_sheet(sheet_name: str):
    client = _get_client()
    if not settings.GOOGLE_SHEET_ID or settings.GOOGLE_SHEET_ID == "CHANGE_ME":
        raise ServiceUnavailableError("GOOGLE_SHEET_ID is not configured.")
    try:
        spreadsheet = client.open_by_key(settings.GOOGLE_SHEET_ID)
        return spreadsheet.worksheet(sheet_name)
    except gspread.SpreadsheetNotFound:
        logger.exception("Spreadsheet not found; is the sheet shared with the service account?")
        raise ServiceUnavailableError("Attendance spreadsheet not found.")
    except gspread.WorksheetNotFound:
        raise ServiceUnavailableError(f"Sheet '{sheet_name}' not found in spreadsheet.")
    except gspread.GspreadError:
        logger.exception("Google Sheets error")
        raise ServiceUnavailableError("Google Sheets request failed.")


def ensure_attendance_headers() -> None:
    """Create the header row on the attendance sheet when it is empty."""
    sheet = _open_sheet(settings.GOOGLE_SHEET_NAME)
    values = sheet.get_values("A1:E1")
    if not values or not values[0] or not values[0][0]:
        sheet.update(
            "A1:E1",
            [["Timestamp", "Enrollment No", "Student Name", "Status", "Session-ID"]],
        )


def append_attendance_row(enrollment_no: str, name: str, session_id: str) -> str:
    """Append a PRESENT row. Timestamp is server-side UTC (dd/mm/yyyy HH:MM:SS)."""
    sheet = _open_sheet(settings.GOOGLE_SHEET_NAME)
    marked_at = datetime.now(timezone.utc)
    formatted = marked_at.strftime("%d/%m/%Y %H:%M:%S")
    sheet.append_row(
        [formatted, enrollment_no, name, "PRESENT", session_id],
        value_input_option="USER_ENTERED",
    )
    logger.info("Attendance row written to sheet enrollment_no=%s session_id=%s",
                enrollment_no, session_id)
    return marked_at.isoformat()


def fetch_student_roster() -> list[tuple[str, str]]:
    """Return [(enrollment_no, name), ...] from the authoritative roster sheet."""
    sheet = _open_sheet(settings.GOOGLE_STUDENTS_SHEET_NAME)
    rows = sheet.get_values()
    roster: list[tuple[str, str]] = []
    for row in rows[1:]:  # skip header row
        if len(row) >= 2 and str(row[0]).strip():
            roster.append((str(row[0]).strip(), str(row[1]).strip()))
    return roster
