"""Google Sheets access via a service account (backend-only, never frontend).

Worksheets inside the configured spreadsheet:
- settings.GOOGLE_STUDENTS_SHEET_NAME: authoritative student roster
  (columns: Enrollment No | Name) — never modified here
- settings.GOOGLE_SHEET_NAME: attendance log
  (columns: Timestamp | Enrollment No | Student Name | Status | Session-ID)
- one worksheet PER SESSION (created on session start): a copy of the roster
  with a Status column, ABSENT/red initially and PRESENT/green once the
  backend records valid attendance.
"""
import logging
import re
from datetime import datetime, timezone

import gspread
from gspread.exceptions import APIError, GSpreadException

from app.core.config import settings
from app.services.errors import ServiceUnavailableError

logger = logging.getLogger(__name__)

_client: gspread.Client | None = None
_spreadsheet = None


def _rgba(red: float, green: float, blue: float) -> dict:
    """Google Sheets API Color. Only red/green/blue are valid fields here — an
    `alpha` key makes batchUpdate return HTTP 400, so it is never sent."""
    return {"red": red, "green": green, "blue": blue}


GREEN = _rgba(0.13, 0.55, 0.13)   # PRESENT background
RED = _rgba(0.80, 0.12, 0.12)     # ABSENT background
WHITE = _rgba(1.0, 1.0, 1.0)      # bold text on a coloured background


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
        # Bound every Sheets HTTP read (shared http_client) so a slow
        # sheets.googleapis.com call fails fast (ReadTimeout) instead of
        # hanging until the OS drops it; _get_roster then serves stale data.
        try:
            _client.set_timeout(settings.SHEETS_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("Could not set Google Sheets timeout; using default")
        return _client
    except Exception:
        logger.exception("Failed to open Google Sheets service account")
        raise ServiceUnavailableError("Could not connect to Google Sheets.")


def _open_sheet(sheet_name: str):
    sheet = _open_spreadsheet().worksheet(sheet_name)
    return sheet


def _open_spreadsheet():
    """Cached spreadsheet handle (the service account must have access)."""
    global _spreadsheet
    client = _get_client()
    if _spreadsheet is not None:
        return _spreadsheet
    if not settings.GOOGLE_SHEET_ID or settings.GOOGLE_SHEET_ID == "CHANGE_ME":
        raise ServiceUnavailableError("GOOGLE_SHEET_ID is not configured.")
    try:
        _spreadsheet = client.open_by_key(settings.GOOGLE_SHEET_ID)
        return _spreadsheet
    except gspread.SpreadsheetNotFound:
        logger.exception("Spreadsheet not found; is the sheet shared with the service account?")
        raise ServiceUnavailableError("Attendance spreadsheet not found.")
    except GSpreadException:
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


# ---- Per-session worksheets -----------------------------------------------
#
# One tab per attendance session: "Enrollment ID | Student Name | Status".
# Sheet1 stays the untouched master roster. These calls are best-effort from
# the session/attendance services — Firestore remains the source of truth.

_SESSION_SHEET_PREFIX = "ATT_"
_UNSAFE_TAB_CHARS = re.compile(r"[^A-Za-z0-9_ -]")

# sheet_title -> {normalized enrollment: 1-based worksheet row}, remembered when
# a tab is seeded. The tab IS the roster order, so this map is exact: every
# student marked present in the same process avoids a full-column MATCH against
# the Sheets API. It is an optimization only — an unknown title (e.g. after a
# server restart) simply falls back to the formula lookup.
_session_rows: dict[str, dict[str, int]] = {}
_SESSION_ROW_CACHE_MAX = 25


def _norm_enrollment(value: str) -> str:
    """Whitespace-free comparison key (mirrors student_service)."""
    return re.sub(r"\s+", "", str(value or ""))


def session_sheet_title(session_id: str, session_name: str) -> str:
    """Deterministic tab name so a duplicate start request never creates a
    second sheet: ATT_<short id>_<cleaned session name> (max 100 chars)."""
    suffix = _UNSAFE_TAB_CHARS.sub("", session_name).strip()[:70] or "session"
    short_id = session_id.replace("SESSION-", "")[-6:]
    return f"{_SESSION_SHEET_PREFIX}{short_id}_{suffix}"[:100]


def create_session_sheet(session_id: str, session_name: str,
                         roster: list[tuple[str, str]] | None = None) -> str:
    """Create (or reuse) the session worksheet seeded with everyone ABSENT.

    Returns the worksheet title. Enrollment numbers are written as TEXT so
    leading zeros survive; PRESENT/ABSENT colours are conditional formats so
    later status updates re-colour automatically.

    `roster` may be supplied by the caller (the process-cached Sheet1 read) to
    avoid downloading the roster a second time for the same session start.
    """
    spreadsheet = _open_spreadsheet()
    if roster is None:
        roster = fetch_student_roster()
    title = session_sheet_title(session_id, session_name)

    # Idempotent: an existing tab (retry / duplicate submit) is reused as-is.
    try:
        spreadsheet.worksheet(title)
        logger.info("Session sheet already exists title=%s", title)
        return title
    except gspread.WorksheetNotFound:
        pass

    try:
        sheet = spreadsheet.add_worksheet(
            title=title, rows=max(len(roster) + 1, 2), cols=3
        )
        body = [["Enrollment ID", "Student Name", "Status"]]
        body += [[enr, name, "ABSENT"] for enr, name in roster]
        requests = [
            {
                # Column A as plain text protects leading zeros.
                "repeatCell": {
                    "range": {"sheetId": sheet.id, "startColumnIndex": 0,
                              "endColumnIndex": 1},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
            {
                # Header + every roster row, seeded with status ABSENT.
                "updateCells": {
                    "range": {"sheetId": sheet.id},
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": str(v)}}
                                         for v in row]} for row in body],
                    "fields": "userEnteredValue",
                }
            },
            {
                # Header row bold.
                "repeatCell": {
                    "range": {"sheetId": sheet.id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat",
                }
            },
        ]
        # Everyone starts ABSENT: paint the Status column (C2:C…) with a solid
        # red background up-front. mark_student_present flips one cell to green
        # the instant that student is recorded, so the tab reads red -> green
        # exactly as required (no fragile conditional-format rules).
        if roster:
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet.id, "startRowIndex": 1,
                              "endRowIndex": 1 + len(roster),
                              "startColumnIndex": 2, "endColumnIndex": 3},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": RED,
                        "textFormat": {"bold": True, "foregroundColor": WHITE},
                    }},
                    "fields": "userEnteredFormat",
                }
            })
        # NOTE: Worksheet.batch_update() is gspread's *value-range* helper and
        # rewrites `data[i]["range"]`, so passing raw Sheets API request objects
        # (repeatCell/updateCells) raises KeyError: 'range'. Raw requests must go
        # through the spreadsheet-level batchUpdate endpoint instead.
        spreadsheet.batch_update({"requests": requests})
    except (GSpreadException, APIError):
        logger.exception("Could not create the session sheet title=%s", title)
        raise ServiceUnavailableError("Could not create the session Google Sheet.")
    logger.info("Session sheet created title=%s students=%s", title, len(roster))
    _remember_rows(title, roster)
    return title


def _remember_rows(title: str, roster: list[tuple[str, str]]) -> None:
    """Index the seeded rows (row 1 is the header) for later fast updates."""
    if len(_session_rows) >= _SESSION_ROW_CACHE_MAX:
        _session_rows.pop(next(iter(_session_rows)), None)  # trim oldest tab
    _session_rows[title] = {
        _norm_enrollment(enrollment): index + 2
        for index, (enrollment, _name) in enumerate(roster)
    }


def mark_student_present(sheet_title: str, enrollment_no: str,
                         row: int | None = None) -> None:
    """Flip one student's Status cell to PRESENT in the session worksheet.
    The enrollment comes from backend-verified data, never from user input.

    `row` is the 1-based worksheet row the caller already knows. When it is not
    supplied, the row remembered from seeding this tab is used; only when that
    is unknown too does the service fall back to the MATCH formula lookup.
    """
    try:
        spreadsheet = _open_spreadsheet()
        sheet = spreadsheet.worksheet(sheet_title)
        target = row or _session_rows.get(sheet_title, {}).get(
            _norm_enrollment(enrollment_no)
        )
        if target is None:
            # Exact match on the raw enrollment value; "" means not found.
            found = sheet.formula(f'=IFERROR(MATCH("{enrollment_no}",A:A,0),"")')
            raw = str(found[0][0]).strip() if found and found[0] else ""
            if not raw or not raw.replace(".0", "").isdigit():
                logger.warning("Enrollment not found on session sheet title=%s",
                               sheet_title)
                return
            target = int(float(raw))
        # Flip this student's Status cell to PRESENT AND paint its background
        # green (from the seeded red) in a single spreadsheet-level batchUpdate.
        target = int(target)
        requests = [{
            "updateCells": {
                "range": {"sheetId": sheet.id,
                          "startRowIndex": target - 1, "endRowIndex": target,
                          "startColumnIndex": 2, "endColumnIndex": 3},
                "rows": [{"values": [{
                    "userEnteredValue": {"stringValue": "PRESENT"},
                    "userEnteredFormat": {
                        "backgroundColor": GREEN,
                        "textFormat": {"bold": True, "foregroundColor": WHITE},
                    },
                }]}],
                "fields": "userEnteredValue,userEnteredFormat",
            }
        }]
        spreadsheet.batch_update({"requests": requests})
    except (gspread.WorksheetNotFound, gspread.SpreadsheetNotFound):
        logger.warning("Session sheet is missing title=%s", sheet_title)
    except (GSpreadException, APIError):
        logger.exception("Could not mark PRESENT on the session sheet title=%s", sheet_title)
