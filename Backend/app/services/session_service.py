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


def create_or_get_session(
    cr_uid: str, latitude: float, longitude: float, session_name: str
) -> tuple[dict, bool]:
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
            "session_name": session_name,
            "cr_uid": cr_uid,
            "latitude": latitude,
            "longitude": longitude,
            "radius_meters": settings.ATTENDANCE_RADIUS_METERS,
            "status": "active",
            "qr_lifetime_seconds": settings.QR_TOKEN_LIFETIME_SECONDS,
            "sheet_title": None,
            "started_at": _utcnow_iso(),
            "ended_at": None,
            "started_by": cr_uid,
        })
        transaction.set(ref, {"active_session_id": new_id, "updated_at": _utcnow_iso()})
        return new_id, True

    session_id, created = _txn(db.transaction())
    session = get_session(session_id)
    if created:
        logger.info("CR created shared session %s name=%r (radius=%sm)",
                    session_id, session_name, session.get("radius_meters"))
        # Per-session Google Sheet tab (ABSENT roster copy). Best-effort:
        # the session stays functional if the Sheets API is unavailable —
        # Firestore remains the source of truth.
        try:
            from app.services import sheets_service

            title = sheets_service.create_session_sheet(session_id, session_name)
            db.collection(COLLECTION_SESSIONS).document(session_id).update(
                {"sheet_title": title}
            )
            session["sheet_title"] = title
        except Exception:
            logger.exception("Could not create the per-session sheet tab for %s",
                             session_id)
    else:
        logger.info("CR joined existing active session %s", session_id)
    return session, created


def set_qr_lifetime(session_id: str, lifetime_seconds: int) -> int:
    """CR-adjustable QR lifetime. Applied to FUTURE token rotations; the
    current token keeps its own expiry until the next rotation. The backend
    validates against the allowed values — the frontend decides nothing.
    A lifetime of settings.QR_PERMANENT_LIFETIME_SECONDS (0) selects
    "Permanent": the QR never auto-expires and only changes on a manual
    refresh (POST /qr/rotate)."""
    allowed = (settings.QR_PERMANENT_LIFETIME_SECONDS, *settings.QR_ALLOWED_LIFETIME_SECONDS)
    if lifetime_seconds not in allowed:
        readable = [
            "Permanent" if s == settings.QR_PERMANENT_LIFETIME_SECONDS else f"{s}s"
            for s in allowed
        ]
        raise BadRequestError(
            "Invalid QR lifetime. Allowed values: " + ", ".join(readable) + ".",
            code="INVALID_QR_LIFETIME",
        )
    session = get_session(session_id)
    if session.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")
    get_db().collection(COLLECTION_SESSIONS).document(session_id).update(
        {"qr_lifetime_seconds": lifetime_seconds}
    )
    logger.info("QR lifetime set to %ss session_id=%s", lifetime_seconds, session_id)
    return lifetime_seconds


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
