"""Student identity: Google Sheet roster verification + Firebase UID linkage.

- The roster lives in the Google Sheet (`Sheet1`: Enrollment-ID | Name) and is
  only ever read by the backend.
- A student is first identified by the LAST 3 DIGITS of their enrollment,
  which must match exactly one roster row.
- After the student confirms, the verified enrollment is saved on the
  Firestore `users/{uid}` document and reused on future attendance attempts.
"""
import logging
import re
import time

from app.core.firebase import COLLECTION_USERS, get_db
from app.services import sheets_service
from app.services.errors import BadRequestError, ConflictError, ServiceUnavailableError

logger = logging.getLogger(__name__)

# Short-lived roster cache so a busy scan session does not hammer the Sheets API.
_ROSTER_CACHE_TTL_SECONDS = 60
_roster_cache: tuple[float, list[tuple[str, str]]] | None = None

_LAST3_RE = re.compile(r"^\d{3}$")


def normalize_name(name: str) -> str:
    """Case-insensitive, whitespace-collapsed comparison key."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def normalize_enrollment(enrollment_no: str) -> str:
    """Trim whitespace (leading/trailing + inner) — value must otherwise match."""
    return re.sub(r"\s+", "", str(enrollment_no or "").strip())


def normalize_last3(value: str) -> str:
    """Validate the 'last 3 digits' input (exactly three digits)."""
    cleaned = str(value or "").strip()
    if not _LAST3_RE.match(cleaned):
        raise BadRequestError(
            "Enter exactly the last 3 digits of your enrollment number.",
            code="INVALID_LAST3",
        )
    return cleaned


def _get_roster() -> list[tuple[str, str]]:
    global _roster_cache
    now = time.monotonic()
    if _roster_cache and now - _roster_cache[0] < _ROSTER_CACHE_TTL_SECONDS:
        return _roster_cache[1]
    try:
        roster = sheets_service.fetch_student_roster()
    except Exception:
        # A transient Sheets read failure (e.g. ReadTimeout) must never 500 the
        # live dashboard: if a roster has loaded at least once, serve it stale
        # (the roster changes rarely). Only a cold cache is a hard failure, and
        # even that surfaces as a clean 503 rather than an unhandled 500.
        if _roster_cache:
            logger.warning("Google Sheets roster read failed; serving stale roster")
            return _roster_cache[1]
        logger.exception("Google Sheets roster read failed with no cached roster")
        raise ServiceUnavailableError(
            "Could not read the student roster from Google Sheets."
        )
    _roster_cache = (now, roster)
    return roster


def get_roster() -> list[tuple[str, str]]:
    """Public accessor for the process-cached Sheet1 roster (60s TTL).

    Session start uses this instead of calling the Sheets API again, so one
    session start downloads the roster at most once per cache window.
    """
    return _get_roster()


def roster_size() -> int:
    return len(_get_roster())


def mask_enrollment(enrollment_no: str) -> str:
    """2025001001 -> ********001 (never expose the full number to the UI)."""
    normalized = normalize_enrollment(enrollment_no)
    if len(normalized) <= 3:
        return normalized
    return "*" * (len(normalized) - 3) + normalized[-3:]


def verify_student_details(name: str, enrollment_no: str) -> tuple[str, str]:
    """Match submitted name + enrollment number against the roster.

    Returns the authoritative (name, enrollment_no) pair from the sheet.
    Raises 400 STUDENT_NOT_FOUND when the pair does not match.
    """
    roster = _get_roster_safe()
    target_name = normalize_name(name)
    target_enrollment = normalize_enrollment(enrollment_no)
    if not target_name or not target_enrollment:
        raise BadRequestError("Student information is required.", code="MISSING_STUDENT_INFO")

    for sheet_enrollment, sheet_name in roster:
        if (
            normalize_enrollment(sheet_enrollment) == target_enrollment
            and normalize_name(sheet_name) == target_name
        ):
            return sheet_name.strip(), sheet_enrollment.strip()

    raise BadRequestError(
        "Student information does not match our records.", code="STUDENT_NOT_FOUND"
    )


def _get_roster_safe():
    try:
        return _get_roster()
    except ServiceUnavailableError:
        raise


def find_by_last3(last3: str) -> list[tuple[str, str]]:
    """All roster rows whose enrollment ends with these 3 digits."""
    return [
        (enrollment, name)
        for enrollment, name in _get_roster_safe()
        if normalize_enrollment(enrollment).endswith(last3)
    ]


def resolve_last3(last3: str) -> tuple[str, str]:
    """Resolve last-3 digits to a single (enrollment_no, name) roster row.

    Ambiguity and no-match are errors — the backend never guesses a student.
    """
    last3 = normalize_last3(last3)
    matches = find_by_last3(last3)
    if not matches:
        logger.info("No roster match for last3 digits")
        raise BadRequestError(
            "Student information does not match our records.", code="STUDENT_NOT_FOUND"
        )
    if len(matches) > 1:
        logger.info("Ambiguous last3 digits matched %s students", len(matches))
        raise BadRequestError(
            "Multiple students found with these digits. Please contact your CR.",
            code="MULTIPLE_STUDENTS_MATCH",
        )
    enrollment, name = matches[0]
    return normalize_enrollment(enrollment), name.strip()


# ---- Firebase UID <-> verified enrollment ---------------------------------

def get_saved_enrollment(uid: str) -> dict | None:
    """Return {name, enrollment_no, photo_url, auth_time} when backend-verified.

    `photo_url` is the Google avatar captured during authorization; it lets
    the attendance record carry the picture the CR popup shows.

    `auth_time` (epoch seconds, 0 when unknown) was written by the backend from
    the signature-verified Firebase ID token. Detection reads it from here, so
    the value never travels through — or is trusted from — the student's client.
    """
    if not uid:
        return None
    snapshot = get_db().collection(COLLECTION_USERS).document(uid).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    if data.get("enrollment_no") and data.get("enrollment_verified"):
        return {
            "name": data.get("student_name") or data.get("name") or "",
            "enrollment_no": str(data["enrollment_no"]),
            "photo_url": str(data.get("photo_url") or ""),
            "auth_time": _as_epoch(data.get("auth_time")),
        }
    return None


def _as_epoch(value: object) -> int:
    """Tolerant epoch read: anything unusable becomes 0 (= unknown, not 0 =
    signed in at the epoch), so a missing value can never look like evidence."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def save_verified_enrollment(uid: str, name: str, enrollment_no: str) -> dict:
    """Persist a Sheet1-verified enrollment on the user's Firestore document.

    The enrollment is written ONLY after backend verification. A saved
    enrollment is never overwritten from the frontend afterwards.
    """
    ref = get_db().collection(COLLECTION_USERS).document(uid)
    existing = ref.get()
    data = existing.to_dict() if existing.exists else {}
    if data.get("enrollment_no") and data.get("enrollment_verified"):
        saved_enrollment = str(data["enrollment_no"])
        if normalize_enrollment(saved_enrollment) != normalize_enrollment(enrollment_no):
            raise ConflictError(
                "An enrollment is already linked to your account. "
                "Please contact your CR to change it.",
                code="ENROLLMENT_ALREADY_SET",
            )
        return {
            "name": data.get("student_name") or data.get("name") or name,
            "enrollment_no": saved_enrollment,
            "already_saved": True,
        }
    ref.set(
        {
            "student_name": name,
            "enrollment_no": normalize_enrollment(enrollment_no),
            "enrollment_verified": True,
        },
        merge=True,
    )
    logger.info("Enrollment verified and saved against Firebase UID")
    return {"name": name, "enrollment_no": normalize_enrollment(enrollment_no),
            "already_saved": False}
