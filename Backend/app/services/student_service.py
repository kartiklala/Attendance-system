"""Student identity verification against the authoritative Google Sheet roster."""
import logging
import re
import time

from app.services import sheets_service
from app.services.errors import BadRequestError, ServiceUnavailableError

logger = logging.getLogger(__name__)

# Short-lived roster cache so a busy scan session does not hammer the Sheets API.
_ROSTER_CACHE_TTL_SECONDS = 60
_roster_cache: tuple[float, list[tuple[str, str]]] | None = None


def normalize_name(name: str) -> str:
    """Case-insensitive, whitespace-collapsed comparison key."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def normalize_enrollment(enrollment_no: str) -> str:
    """Trim whitespace (leading/trailing + inner) — value must otherwise match."""
    return re.sub(r"\s+", "", (enrollment_no or "").strip())


def _get_roster() -> list[tuple[str, str]]:
    global _roster_cache
    now = time.monotonic()
    if _roster_cache and now - _roster_cache[0] < _ROSTER_CACHE_TTL_SECONDS:
        return _roster_cache[1]
    roster = sheets_service.fetch_student_roster()
    _roster_cache = (now, roster)
    return roster


def verify_student_details(name: str, enrollment_no: str) -> tuple[str, str]:
    """Match submitted name + enrollment number against the roster.

    Returns the authoritative (name, enrollment_no) pair from the sheet.
    Raises 400 STUDENT_NOT_FOUND when the pair does not match.
    """
    try:
        roster = _get_roster()
    except ServiceUnavailableError:
        raise
    target_name = normalize_name(name)
    target_enrollment = normalize_enrollment(enrollment_no)
    if not target_name or not target_enrollment:
        raise BadRequestError("Student information is required.", code="MISSING_STUDENT_INFO")

    for sheet_enrollment, sheet_name in roster:
        if (
            normalize_enrollment(sheet_enrollment) == target_enrollment
            and normalize_name(sheet_name) == target_name
        ):
            logger.info("Student record matched enrollment_no=%s", sheet_enrollment)
            return sheet_name.strip(), sheet_enrollment.strip()

    logger.info("Student record NOT matched (enrollment_no=%s)", enrollment_no)
    raise BadRequestError(
        "Student information does not match our records.", code="STUDENT_NOT_FOUND"
    )
