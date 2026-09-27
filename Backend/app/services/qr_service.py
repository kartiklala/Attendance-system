"""QR token generation, rotation and validation.

The backend is the authority for QR validity:
- the session has one current QR token at a time, shared by ALL connected CRs,
- a token lives for settings.QR_TOKEN_LIFETIME_SECONDS (rotation window);
  get_current_qr() rotates only when the stored token has expired, so every
  CR polling sees the SAME QR until the next rotation tick,
- once scanned, a student gets settings.STUDENT_SESSION_MINUTES to complete,
- tokens belonging to ended sessions are rejected.

The token itself is a random short-lived public value (shown openly as a QR);
its SHA-256 hash is used as the Firestore document id.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from google.cloud import firestore as gf

from app.core.config import settings
from app.core.firebase import COLLECTION_QR_TOKENS, COLLECTION_SESSIONS, get_db
from app.services.errors import BadRequestError, NotFoundError

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


def _payload(raw_token: str, session_id: str, expires_at, lifetime_seconds: int) -> dict:
    exp = _parse(expires_at) or _utcnow()
    remaining = max(0, int((exp - _utcnow()).total_seconds()))
    return {
        "session_id": session_id,
        "qr_token": raw_token,
        "qr_url": build_qr_url(raw_token),
        # The lifetime configured for THIS session (CR-adjustable via
        # /qr/lifetime) — never a value the frontend decides on its own.
        "expires_in_seconds": lifetime_seconds,
        # countdown_seconds is the REMAINING lifetime of the current token so
        # every CR's display stays in sync with the server rotation.
        "countdown_seconds": remaining,
    }


def get_current_qr(session_id: str) -> dict:
    """Return the session's current QR, rotating it only when expired.

    Runs in a Firestore transaction on the session document so two CRs
    refreshing at the same moment cannot both rotate (single shared QR).
    """
    db = get_db()
    session_ref = db.collection(COLLECTION_SESSIONS).document(session_id)
    tokens = db.collection(COLLECTION_QR_TOKENS)

    @gf.transactional
    def _txn(transaction):
        snapshot = session_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise NotFoundError("Attendance session not found.", code="SESSION_NOT_FOUND")
        data = snapshot.to_dict() or {}
        if data.get("status") != "active":
            raise BadRequestError("Attendance session is no longer active.",
                                  code="SESSION_NOT_ACTIVE")

        now = _utcnow()
        current_hash = data.get("current_qr_hash")
        current_exp = _parse(data.get("current_qr_expires_at"))
        # Lifetime the CR configured for this session (falls back to the
        # server default for sessions created before the setting existed).
        try:
            lifetime = int(data.get("qr_lifetime_seconds")
                           or settings.QR_TOKEN_LIFETIME_SECONDS)
        except (TypeError, ValueError):
            lifetime = settings.QR_TOKEN_LIFETIME_SECONDS

        # Firestore transactions require ALL reads before ANY write, so the
        # old token document is fetched up-front and reused below.
        old_snapshot = (
            tokens.document(current_hash).get(transaction=transaction)
            if current_hash
            else None
        )

        # Reuse the still-valid token so all CRs display the same QR.
        if current_exp and now < current_exp and old_snapshot is not None and old_snapshot.exists:
            doc_data = old_snapshot.to_dict() or {}
            if doc_data.get("active") and doc_data.get("token"):
                return _payload(doc_data["token"], session_id,
                                doc_data.get("expires_at"), lifetime)

        # Rotate: create the next token and point the session at it.
        raw = secrets.token_urlsafe(24)  # cryptographically random
        token_hash = _hash_token(raw)
        expires_at = now + timedelta(seconds=lifetime)
        transaction.set(tokens.document(token_hash), {
            "session_id": session_id,
            "token_hash": token_hash,
            "token": raw,
            "created_at": _iso(now),
            "expires_at": _iso(expires_at),
            "active": True,
            "first_scanned_at": None,
            "window_expires_at": None,
        })
        if old_snapshot is not None and old_snapshot.exists:
            transaction.update(old_snapshot.reference, {"active": False})
        transaction.update(session_ref, {
            "current_qr_hash": token_hash,
            "current_qr_expires_at": _iso(expires_at),
        })
        logger.info("QR generated session_id=%s lifetime=%ss", session_id, lifetime)
        return _payload(raw, session_id, _iso(expires_at), lifetime)

    return _txn(db.transaction())


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
    """Resolve a raw QR token to its record or raise 400 (QR_INVALID)."""
    record = _load_token_doc(_hash_token(raw_token))
    if record is None:
        raise BadRequestError(TOKEN_NOT_FOUND_MSG, code="QR_INVALID")
    return record


def scan_token(raw_token: str) -> dict:
    """Called when a student opens the QR link.

    Starts the one-minute completion window server-side. The token must
    still belong to an active session; only the CURRENT (unexpired,
    un-rotated) token may start a new scan.
    """
    record = get_token_record(raw_token)
    session_ref = get_db().collection(COLLECTION_SESSIONS).document(record["session_id"])
    session = session_ref.get()
    session_data = session.to_dict() or {} if session.exists else {}
    if session_data.get("status") != "active":
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
    session_snapshot = (
        get_db().collection(COLLECTION_SESSIONS).document(record["session_id"]).get()
    )
    if not session_snapshot.exists:
        raise NotFoundError("Attendance session not found.", code="SESSION_NOT_FOUND")
    session = session_snapshot.to_dict() or {}
    session["session_id"] = session_snapshot.id
    if session.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")

    now = _utcnow()
    window_expires_at = _parse(record.get("window_expires_at"))
    if window_expires_at is None:
        # Student submitted without a prior scan: allow only a freshly issued,
        # still-current token (never redeem an arbitrarily old/rotated token).
        # The grace uses the TOKEN's own expiry so CR-adjustable lifetimes
        # (5-60s) are honoured regardless of the server default.
        token_exp = _parse(record.get("expires_at")) or _utcnow()
        grace_end = token_exp + timedelta(minutes=settings.STUDENT_SESSION_MINUTES)
        if not record.get("active") or now > grace_end:
            raise BadRequestError("The QR code has expired.", code="QR_EXPIRED")
    elif now > window_expires_at:
        raise BadRequestError(
            "Attendance window expired. Please scan the current QR code again.",
            code="ATTENDANCE_WINDOW_EXPIRED",
        )
    return record, session
