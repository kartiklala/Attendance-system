"""QR token generation, rotation and validation.

The backend is the authority for QR validity:
- each token lives for settings.QR_TOKEN_LIFETIME_SECONDS (rotation window),
- once scanned, a student gets settings.STUDENT_SESSION_MINUTES to complete,
- tokens for ended sessions are rejected.

Only a SHA-256 hash of the random token is stored — never the raw token.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.firebase import COLLECTION_QR_TOKENS, get_db
from app.services.errors import BadRequestError
from app.services import session_service

logger = logging.getLogger(__name__)

TOKEN_NOT_FOUND_MSG = "Invalid or expired attendance QR."


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(dt_value) -> datetime | None:
    if not dt_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(dt_value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_qr_url(token: str) -> str:
    base = settings.PUBLIC_APP_URL.rstrip("/")
    return f"{base}/attendance?token={token}"


def generate_token(session_id: str, rotate: bool = True) -> dict:
    """Create a fresh QR token for an active session.

    When `rotate` is True the previous active token for the session is
    invalidated first (single-valid-QR behaviour).
    """
    session_service.require_active_session(session_id)
    db = get_db()
    if rotate:
        _deactivate_active_tokens(db, session_id)

    raw_token = secrets.token_urlsafe(24)  # cryptographically random
    token_hash = _hash_token(raw_token)
    now = _utcnow()
    expires_at = now + timedelta(seconds=settings.QR_TOKEN_LIFETIME_SECONDS)
    doc = {
        "session_id": session_id,
        "token_hash": token_hash,
        "created_at": _iso(now),
        "expires_at": _iso(expires_at),
        "active": True,
        "first_scanned_at": None,
        "window_expires_at": None,
    }
    db.collection(COLLECTION_QR_TOKENS).document(token_hash).set(doc)
    logger.info("QR generated session_id=%s", session_id)
    return {
        "session_id": session_id,
        "qr_token": raw_token,
        "qr_url": build_qr_url(raw_token),
        "expires_in_seconds": settings.QR_TOKEN_LIFETIME_SECONDS,
        "countdown_seconds": settings.QR_TOKEN_LIFETIME_SECONDS,
    }


def _deactivate_active_tokens(db, session_id: str) -> None:
    docs = (
        db.collection(COLLECTION_QR_TOKENS)
        .where("session_id", "==", session_id)
        .where("active", "==", True)
        .stream()
    )
    batch = db.batch()
    count = 0
    for doc in docs:
        batch.update(doc.reference, {"active": False})
        count += 1
    if count:
        batch.commit()


def invalidate_session_tokens(session_id: str) -> int:
    """Mark every token of a (now ended) session inactive."""
    db = get_db()
    docs = (
        db.collection(COLLECTION_QR_TOKENS)
        .where("session_id", "==", session_id)
        .where("active", "==", True)
        .stream()
    )
    batch = db.batch()
    count = 0
    for doc in docs:
        batch.update(doc.reference, {"active": False})
        count += 1
    if count:
        batch.commit()
    logger.info("Invalidated %s QR tokens for ended session_id=%s", count, session_id)
    return count


def _load_token_doc(token_hash: str) -> dict | None:
    snapshot = get_db().collection(COLLECTION_QR_TOKENS).document(token_hash).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    data["_doc_id"] = snapshot.id
    return data


def get_token_record(raw_token: str) -> dict:
    """Resolve a raw QR token to its record or raise 400 (QR_EXPIRED)."""
    record = _load_token_doc(_hash_token(raw_token))
    if record is None:
        raise BadRequestError(TOKEN_NOT_FOUND_MSG, code="QR_INVALID")
    return record


def scan_token(raw_token: str) -> dict:
    """Called when a student opens the QR link.

    Starts the one-minute completion window server-side. The token must
    still belong to an active session; the 10s rotation expiry only limits
    when a *new* scan may start (a scan within the rotation window).
    """
    record = get_token_record(raw_token)
    session = session_service.get_session(record["session_id"])
    if session.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")

    now = _utcnow()
    expires_at = _parse(record.get("expires_at"))

    # First scan ever: must happen while the QR is still displayed/valid.
    if not record.get("first_scanned_at"):
        if not record.get("active") or (expires_at and now > expires_at):
            raise BadRequestError("The QR code has expired.", code="QR_EXPIRED")
        window_expires_at = now + timedelta(minutes=settings.STUDENT_SESSION_MINUTES)
        update = {
            "first_scanned_at": _iso(now),
            "window_expires_at": _iso(window_expires_at),
        }
        get_db().collection(COLLECTION_QR_TOKENS).document(record["_doc_id"]).update(update)
        record.update(update)

    return record


def validate_token_for_check(raw_token: str) -> tuple[dict, dict]:
    """Return (token_record, session) if the token may still be redeemed.

    Enforces: token known -> session active -> one-minute completion window
    (server-authoritative timestamps only).
    """
    record = get_token_record(raw_token)
    session = session_service.get_session(record["session_id"])
    if session.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")

    now = _utcnow()
    window_expires_at = _parse(record.get("window_expires_at"))
    if window_expires_at is None:
        # Student submitted without a prior scan: treat the rotation window
        # plus the completion window as starting now, but only if the QR was
        # generated recently enough (never redeem an arbitrarily old token).
        created_at = _parse(record.get("created_at"))
        grace = timedelta(
            seconds=settings.QR_TOKEN_LIFETIME_SECONDS
            + settings.STUDENT_SESSION_MINUTES * 60
        )
        if not record.get("active") or (created_at and now - created_at > grace):
            raise BadRequestError("The QR code has expired.", code="QR_EXPIRED")
    elif now > window_expires_at:
        raise BadRequestError(
            "Attendance window expired. Please scan the current QR code again.",
            code="ATTENDANCE_WINDOW_EXPIRED",
        )
    return record, session
