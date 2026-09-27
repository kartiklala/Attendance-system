"""Attendance session lifecycle stored in Firestore `sessions`.

There is at most ONE active session across the class; all CRs share it.
The `session_state/current` pointer document is the source of truth for the
active session id, and creation runs inside a Firestore transaction so two
CRs clicking "Start" at nearly the same time can never create two sessions.
"""
import logging
import secrets
from datetime import datetime, timezone

from google.cloud import firestore as gf

from app.core.config import settings
from app.core.firebase import (
    COLLECTION_SESSIONS,
    COLLECTION_SESSION_STATE,
    get_db,
)
from app.services.errors import BadRequestError, NotFoundError

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_ref():
    return get_db().collection(COLLECTION_SESSION_STATE).document("current")


def get_session(session_id: str) -> dict:
    if not session_id:
        raise BadRequestError("session_id is required.")
    snapshot = get_db().collection(COLLECTION_SESSIONS).document(session_id).get()
    if not snapshot.exists:
        raise NotFoundError("Attendance session not found.", code="SESSION_NOT_FOUND")
    data = snapshot.to_dict() or {}
    data["session_id"] = snapshot.id
    return data


def get_active_session() -> dict | None:
    """Return the single active session (backend is the source of truth)."""
    db = get_db()
    pointer = _state_ref().get()
    if not pointer.exists:
        return None
    session_id = (pointer.to_dict() or {}).get("active_session_id")
    if not session_id:
        return None
    snapshot = db.collection(COLLECTION_SESSIONS).document(session_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    if data.get("status") != "active":
        return None
    data["session_id"] = snapshot.id
    return data


def create_or_get_session(cr_uid: str, latitude: float, longitude: float) -> tuple[dict, bool]:
    """Race-safe start-or-join: returns (session, created_new).

    If any active session already exists the CR joins it instead of creating
    a second one (the transaction serialises concurrent "Start" clicks).
    """
    db = get_db()
    new_id = f"SESSION-{secrets.token_hex(6).upper()}"
    ref = _state_ref()
    sessions = db.collection(COLLECTION_SESSIONS)

    @gf.transactional
    def _txn(transaction):
        pointer = ref.get(transaction=transaction)
        existing_id = (pointer.to_dict() or {}).get("active_session_id")
        if existing_id:
            existing = sessions.document(existing_id).get(transaction=transaction)
            if existing.exists and (existing.to_dict() or {}).get("status") == "active":
                return existing_id, False
        transaction.set(sessions.document(new_id), {
            "session_id": new_id,
            "cr_uid": cr_uid,
            "latitude": latitude,
            "longitude": longitude,
            "radius_meters": settings.ATTENDANCE_RADIUS_METERS,
            "status": "active",
            "started_at": _utcnow_iso(),
            "ended_at": None,
            "started_by": cr_uid,
        })
        transaction.set(ref, {"active_session_id": new_id, "updated_at": _utcnow_iso()})
        return new_id, True

    session_id, created = _txn(db.transaction())
    session = get_session(session_id)
    if created:
        logger.info("CR created shared session %s (radius=%sm)",
                    session_id, session.get("radius_meters"))
    else:
        logger.info("CR joined existing active session %s", session_id)
    return session, created


def end_session(session_id: str, cr_uid: str) -> dict:
    """End the shared session (any authenticated CR may end it, which also
    stops it for every other connected CR)."""
    session = get_session(session_id)
    if session.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")
    db = get_db()
    ended_at = _utcnow_iso()
    db.collection(COLLECTION_SESSIONS).document(session_id).update(
        {"status": "ended", "ended_at": ended_at, "ended_by": cr_uid}
    )

    # Clear the active pointer only if it still references this session.
    @gf.transactional
    def _clear(transaction):
        pointer = _state_ref().get(transaction=transaction)
        data = pointer.to_dict() or {}
        if data.get("active_session_id") == session_id:
            transaction.set(pointer.reference, {"active_session_id": None}, merge=True)

    _clear(db.transaction())

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
