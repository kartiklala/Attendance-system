"""Google Sheets access via a service account (backend-only, never frontend).

Worksheets inside the configured spreadsheet:
- settings.GOOGLE_STUDENTS_SHEET_NAME: authoritative student roster
  (columns: Enrollment No | Name) — never modified here
- settings.GOOGLE_SHEET_NAME: attendance log
  (columns: Timestamp | Enrollment No | Student Name | Status | Session-ID)
- one worksheet PER SESSION (created on session start): a copy of the roster
  with a Status column (ABSENT/red initially, PRESENT/green once the backend
  records valid attendance) and a Remarks column that is REWRITTEN from the
  session's current shared-browser state on every sync — "Possible shared
  browser" while a browser served two or more students, blank otherwise (and
  therefore cleared when a flag is overridden or stops applying).
"""
import hashlib
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
AMBER = _rgba(0.98, 0.75, 0.14)   # Remarks background for a shared-browser flag

# Prefix of the remark written next to a student whose attendance looks
# proxied. Written by the whole-column sync below, which derives it from a
# shared BROWSER id (one profile marking for two or more students). A shared IP
# alone is never a remark: a whole class on one Wi-Fi router is legitimate, and
# the address itself was historically client-supplied.
REMARK_PROXY = "Possible shared browser"
# How many tabs remember the remarks already written to them. Purely a size
# guard for this process-local cache.
_PROXY_STATE_MAX = 25
# sheet_title -> signature of the remarks currently believed to be on the tab.
# An optimization only: it lets an unchanged conclusion skip a Sheets write, so
# a full class does not rewrite the column sixty times. Holds no identity data.
_proxy_state: dict[str, str] = {}


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
            range_name="A1:E1",
            values=[["Timestamp", "Enrollment No", "Student Name", "Status", "Session-ID"]],
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
# One tab per attendance session:
# "Enrollment ID | Student Name | Status | Remarks". Sheet1 stays the untouched
# master roster. Remarks is blank normally and carries a "Possible shared
# browser" flag while one browser id marks two or more students — never for a
# shared IP alone. The flag is recalculated from the session's current records
# on every sync, so it cannot pile up or outlive the evidence behind it. These
# calls are best-effort from the session/attendance services — Firestore is the
# source.

_SESSION_SHEET_PREFIX = "ATT_"
_UNSAFE_TAB_CHARS = re.compile(r"[^A-Za-z0-9_ -]")

# sheet_title -> {normalized enrollment: 1-based worksheet row}, remembered when
# a tab is seeded. The tab IS the roster order, so this map is exact: every
# student marked present in the same process avoids a full-column read against
# the Sheets API. It is an optimization only — an unknown title (e.g. after a
# server restart) simply falls back to a column-A value search.
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

    # Idempotent: an existing tab (retry / duplicate submit) is reused, and a
    # tab created before the Remarks column existed is upgraded so the column
    # is always present for this session.
    try:
        existing = spreadsheet.worksheet(title)
        logger.info("Session sheet already exists title=%s", title)
        _ensure_remarks_column(existing)
        return title
    except gspread.WorksheetNotFound:
        pass

    try:
        sheet = spreadsheet.add_worksheet(
            title=title, rows=max(len(roster) + 1, 2), cols=4
        )
        body = [["Enrollment ID", "Student Name", "Status", "Remarks"]]
        body += [[enr, name, "ABSENT", ""] for enr, name in roster]
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


def _ensure_remarks_column(sheet) -> None:
    """Backfill the 4th 'Remarks' column on a session tab that predates this
    feature (3-column layout). Idempotent and best-effort — a failure here
    must never block a session from starting or joining."""
    try:
        if sheet.col_count < 4:
            sheet.add_cols(4 - sheet.col_count)
        header = (sheet.get_values("A1:D1") or [[]])[0]
        current = str(header[3]).strip().lower() if len(header) >= 4 else ""
        if current == "remarks":
            return
        sheet.update(range_name="D1", values=[["Remarks"]])
        try:
            sheet.format("D1", {"textFormat": {"bold": True}})
        except (GSpreadException, APIError):
            pass
        logger.info("Backfilled Remarks column title=%s", sheet.title)
    except (gspread.WorksheetNotFound, gspread.SpreadsheetNotFound):
        logger.warning("Session sheet missing while ensuring Remarks column")
    except (GSpreadException, APIError):
        logger.exception("Could not ensure Remarks column title=%s", sheet.title)


def _resolve_row(sheet, sheet_title: str, enrollment_no: str,
                 row: int | None = None) -> int | None:
    """1-based worksheet row for an enrollment: caller-supplied, else the
    seeding cache, else a scan of column A. Returns None if not found."""
    target = row or _session_rows.get(sheet_title, {}).get(
        _norm_enrollment(enrollment_no)
    )
    if target is not None:
        return int(target)
    key = _norm_enrollment(enrollment_no)
    if not key:
        return None
    # Cache miss (e.g. after a server restart). gspread's Worksheet has no
    # formula() method, so a MATCH formula cannot be evaluated client-side —
    # read column A and locate the enrollment value directly instead.
    try:
        column_a = sheet.col_values(1)
    except (GSpreadException, APIError):
        logger.exception("Could not read column A title=%s", sheet_title)
        return None
    for index, value in enumerate(column_a, start=1):
        if _norm_enrollment(value) == key:
            return index
    return None


def mark_student_present(sheet_title: str, enrollment_no: str,
                         row: int | None = None) -> None:
    """Flip one student's Status cell to PRESENT in the session worksheet.
    The enrollment comes from backend-verified data, never from user input.

    `row` is the 1-based worksheet row the caller already knows. When it is not
    supplied, the row remembered from seeding this tab is used; only when that
    is unknown too does the service fall back to a column-A value search.
    """
    try:
        spreadsheet = _open_spreadsheet()
        sheet = spreadsheet.worksheet(sheet_title)
        target = _resolve_row(sheet, sheet_title, enrollment_no, row)
        if target is None:
            logger.warning("Enrollment not found on session sheet title=%s",
                           sheet_title)
            return
        # Flip this student's Status cell to PRESENT AND paint its background
        # green (from the seeded red) in a single spreadsheet-level batchUpdate.
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


def sync_proxy_remarks(sheet_title: str, remarks: dict[str, str]) -> None:
    """Rewrite the WHOLE Remarks column (D) from the session's current state.

    `remarks` maps enrollment_no -> remark text for every student who is
    suspicious RIGHT NOW. Every other roster row is written blank with its
    formatting reset, which is what makes an overridden or stale flag visibly
    disappear. The previous behaviour only ever ADDED a remark and never removed
    one, so a suspicion from earlier in the session haunted the tab forever and
    each new flag re-stamped the whole group.

    One batchUpdate writes the entire column, so the tab is never left half
    updated, and a conclusion that has not changed skips the API call altogether
    (`_proxy_state`). Best-effort: failures are logged, never raised into a
    student's or a CR's request.
    """
    if not sheet_title:
        return
    try:
        spreadsheet = _open_spreadsheet()
        sheet = spreadsheet.worksheet(sheet_title)

        rows_of = dict(_session_rows.get(sheet_title) or {})
        if not rows_of:
            # Cache miss (e.g. after a restart): locate the roster in column A.
            # gspread's Worksheet has no formula() method, so a MATCH cannot be
            # evaluated client-side — read the values and locate them directly.
            try:
                column_a = sheet.col_values(1)
            except (GSpreadException, APIError):
                logger.exception("Could not read column A title=%s", sheet_title)
                return
            for index, value in enumerate(column_a, start=1):
                key = _norm_enrollment(value)
                if index > 1 and key and key not in rows_of:
                    rows_of[key] = index
        if not rows_of:
            logger.warning("No roster rows to sync remarks against title=%s",
                           sheet_title)
            return

        last_row = max(rows_of.values())
        # Resolve every remark to a worksheet ROW up front, so an enrollment that
        # is not on this tab is dropped rather than shifting other rows, and a
        # column-A scan fallback behaves identically to the seeded cache.
        flagged: dict[int, str] = {}
        for enrollment, text in (remarks or {}).items():
            row = rows_of.get(_norm_enrollment(enrollment))
            if row and text:
                flagged[row] = str(text)[:200]

        signature = hashlib.sha1(
            "|".join(f"{row}={flagged[row]}" for row in sorted(flagged)).encode("utf-8")
        ).hexdigest()
        if _proxy_state.get(sheet_title) == signature:
            return  # the tab already shows exactly this conclusion

        # One cell per roster row, in row order: a flagged row gets its remark
        # plus amber, every other row an empty value and a RESET format. Writing
        # the whole column is what makes a cleared flag visibly disappear instead
        # of lingering, so the tab always reads as the CURRENT conclusion.
        values = []
        for row_index in range(2, last_row + 1):
            text = flagged.get(row_index, "")
            cell: dict = {"userEnteredValue": {"stringValue": text}}
            if text:
                cell["userEnteredFormat"] = {
                    "backgroundColor": AMBER,
                    "textFormat": {"bold": True},
                }
            values.append({"values": [cell]})

        spreadsheet.batch_update({"requests": [{
            "updateCells": {
                "range": {"sheetId": sheet.id,
                          "startRowIndex": 1, "endRowIndex": last_row,
                          "startColumnIndex": 3, "endColumnIndex": 4},
                "rows": values,
                # Naming the format field for a BLANK cell is what clears a
                # stale amber background rather than leaving it behind.
                "fields": "userEnteredValue,userEnteredFormat",
            }
        }]})
        if len(_proxy_state) >= _PROXY_STATE_MAX:
            _proxy_state.pop(next(iter(_proxy_state)), None)  # trim oldest tab
        _proxy_state[sheet_title] = signature
        logger.info("Proxy remarks synced title=%s flagged=%s rows=%s",
                    sheet_title, len(flagged), len(values))
    except (gspread.WorksheetNotFound, gspread.SpreadsheetNotFound):
        logger.warning("Session sheet is missing title=%s", sheet_title)
    except (GSpreadException, APIError):
        logger.exception("Could not sync proxy remarks title=%s", sheet_title)
