"""Attendance session lifecycle stored in Firestore `sessions`."""
import logging
import secrets
from datetime import datetime, timezone

from app.core.config import settings
from app.core.firebase import COLLECTION_SESSIONS, get_db
from app.services.errors import BadRequestError, ForbiddenError, NotFoundError

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(cr_uid: str, latitude: float, longitude: float) -> dict:
    """Create an active attendance session at the CR's current location."""
    session_id = f"SESSION-{secrets.token_hex(6).upper()}"
    doc = {
        "session_id": session_id,
        "cr_uid": cr_uid,
        "latitude": latitude,
        "longitude": longitude,
        "radius_meters": settings.ATTENDANCE_RADIUS_METERS,
        "status": "active",
        "started_at": _utcnow_iso(),
        "ended_at": None,
    }
    get_db().collection(COLLECTION_SESSIONS).document(session_id).set(doc)
    logger.info("CR started session %s (radius=%sm)", session_id, doc["radius_meters"])
    return doc


def get_session(session_id: str) -> dict:
    if not session_id:
        raise BadRequestError("session_id is required.")
    snapshot = get_db().collection(COLLECTION_SESSIONS).document(session_id).get()
    if not snapshot.exists:
        raise NotFoundError("Attendance session not found.", code="SESSION_NOT_FOUND")
    data = snapshot.to_dict() or {}
    data["session_id"] = snapshot.id
    return data


def get_active_session_for_cr(cr_uid: str) -> dict | None:
    docs = (
        get_db()
        .collection(COLLECTION_SESSIONS)
        .where("cr_uid", "==", cr_uid)
        .where("status", "==", "active")
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict() or {}
        data["session_id"] = doc.id
        return data
    return None


def end_session(session_id: str, cr_uid: str) -> dict:
    """Validate ownership + active status, then mark the session ended."""
    session = get_session(session_id)
    if session.get("cr_uid") != cr_uid:
        raise ForbiddenError("You can only end your own sessions.", code="NOT_SESSION_OWNER")
    if session.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")
    ended_at = _utcnow_iso()
    get_db().collection(COLLECTION_SESSIONS).document(session_id).update(
        {"status": "ended", "ended_at": ended_at}
    )
    session.update({"status": "ended", "ended_at": ended_at})
    logger.info("Session ended session_id=%s by cr_uid=%s", session_id, cr_uid)
    return session


def require_active_session(session_id: str) -> dict:
    """Fetch a session and ensure it is still active (used by QR flows)."""
    session = get_session(session_id)
    if session.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")
    return session
