"""QR token generation/rotation plus individual attendance attempts.

The backend is the authority for QR validity:
- the session has one current QR token at a time, shared by ALL connected CRs,
- a token lives for the session's `qr_lifetime_seconds` (CR-adjustable);
  get_current_qr() rotates only when the stored token has expired, so every
  CR polling sees the SAME QR until the next rotation tick,
- the QR answers ONE question: "did the student enter through a valid QR?";
- completion is then governed by an INDIVIDUAL attendance attempt
  (`attendance_attempts/{attempt_id}`) with its own server-side window, so a
  QR rotating moments later MUST NOT invalidate a student already inside;
- tokens belonging to ended sessions are rejected.

The token itself is a random short-lived public value (shown openly as a QR);
its SHA-256 hash is used as the Firestore document id.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from google.cloud import firestore as gf
from google.api_core import exceptions as gexc

from app.core.config import settings
from app.core.firebase import (
    COLLECTION_ATTENDANCE_ATTEMPTS,
    COLLECTION_QR_TOKENS,
    COLLECTION_SESSIONS,
    get_db,
)
from app.services.errors import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
)

logger = logging.getLogger(__name__)

TOKEN_NOT_FOUND_MSG = "Invalid or expired attendance QR."


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _permanent_expiry(now: datetime) -> datetime:
    # Effectively never: a Permanent QR only changes when the CR refreshes it.
    return now + timedelta(days=36500)


def _lifetime_from(data: dict) -> int:
    """Read the session's configured QR lifetime, PRESERVING 0 (Permanent).
    Never use `value or default` here — that would silently turn Permanent
    back into the default lifetime."""
    raw = data.get("qr_lifetime_seconds")
    if raw is None:
        return settings.QR_TOKEN_LIFETIME_SECONDS
    try:
        return int(raw)
    except (TypeError, ValueError):
        return settings.QR_TOKEN_LIFETIME_SECONDS


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
    try:
        lifetime = int(lifetime_seconds)
    except (TypeError, ValueError):
        lifetime = settings.QR_TOKEN_LIFETIME_SECONDS
    is_permanent = lifetime == settings.QR_PERMANENT_LIFETIME_SECONDS
    if is_permanent:
        expires_in = 0
        countdown = 0
    else:
        exp = _parse(expires_at) or _utcnow()
        expires_in = lifetime
        # countdown_seconds is the REMAINING lifetime of the current token so
        # every CR's display stays in sync with the server rotation.
        countdown = max(0, int((exp - _utcnow()).total_seconds()))
    return {
        "session_id": session_id,
        "qr_token": raw_token,
        "qr_url": build_qr_url(raw_token),
        # The lifetime configured for THIS session (CR-adjustable via
        # /qr/lifetime) — 0 means Permanent. Never a value the frontend
        # decides on its own.
        "expires_in_seconds": expires_in,
        "countdown_seconds": countdown,
        "is_permanent": is_permanent,
    }


def _read_session(db, session_id):
    """Non-transactional session read + shared validation. Returns
    (data, current_hash, current_exp, lifetime) or raises the domain error."""
    snapshot = db.collection(COLLECTION_SESSIONS).document(session_id).get()
    if not snapshot.exists:
        raise NotFoundError("Attendance session not found.", code="SESSION_NOT_FOUND")
    data = snapshot.to_dict() or {}
    if data.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")
    try:
        raw_lifetime = data.get("qr_lifetime_seconds")
        # 0 is a valid Permanent lifetime — only fall back when the key is
        # genuinely absent (None), never on a falsy 0.
        lifetime = (settings.QR_TOKEN_LIFETIME_SECONDS
                    if raw_lifetime is None else int(raw_lifetime))
    except (TypeError, ValueError):
        lifetime = settings.QR_TOKEN_LIFETIME_SECONDS
    return (
        data,
        data.get("current_qr_hash"),
        _parse(data.get("current_qr_expires_at")),
        lifetime,
    )


def _valid_payload(db, session_id):
    """Return the current token's payload WITHOUT a transaction, or None when a
    rotation is needed. This is the hot path: every CR poll that finds a
    still-valid token returns here and never opens a transaction, so concurrent
    refreshes cannot abort one another (the Aborted/500 contention)."""
    data, current_hash, current_exp, lifetime = _read_session(db, session_id)
    now = _utcnow()
    if not (current_hash and current_exp and now < current_exp):
        return None
    doc = db.collection(COLLECTION_QR_TOKENS).document(current_hash).get()
    if not doc.exists:
        return None
    dd = doc.to_dict() or {}
    if dd.get("active") and dd.get("token"):
        return _payload(dd["token"], session_id, dd.get("expires_at"), lifetime)
    return None


def get_current_qr(session_id: str) -> dict:
    """Return the session's current QR, rotating it only when expired.

    Reads are contention-free; ONLY an actual rotation needs a transaction (to
    keep the single shared QR across simultaneous CRs). If that transaction is
    aborted by contention, another CR almost certainly just rotated it — so we
    re-read and serve the token they committed instead of erroring.
    """
    db = get_db()

    existing = _valid_payload(db, session_id)
    if existing is not None:
        return existing

    last_err = None
    for _ in range(3):
        # A concurrent CR may have rotated between our attempts; serve it.
        existing = _valid_payload(db, session_id)
        if existing is not None:
            return existing
        try:
            return _rotate_qr(db, session_id)
        except gexc.Aborted as err:
            last_err = err

    existing = _valid_payload(db, session_id)
    if existing is not None:
        return existing
    raise last_err


def _rotate_qr(db, session_id: str, force: bool = False) -> dict:
    """Transactional rotation: create the next token and point the session at
    it. Serializes concurrent rotations so only one QR becomes current.

    `force=True` (CR manual refresh) mints a brand-new token even when the
    current one has not expired, invalidating the previous QR."""
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
        try:
            raw_lifetime = data.get("qr_lifetime_seconds")
            lifetime = (settings.QR_TOKEN_LIFETIME_SECONDS
                        if raw_lifetime is None else int(raw_lifetime))
        except (TypeError, ValueError):
            lifetime = settings.QR_TOKEN_LIFETIME_SECONDS
        # `_payload` also derives permanence from `lifetime`, but the branch
        # below and the log line need it as a local, so compute it once here.
        is_permanent = lifetime == settings.QR_PERMANENT_LIFETIME_SECONDS

        # Firestore transactions require ALL reads before ANY write, so the
        # old token document is fetched up-front and reused below.
        old_snapshot = (
            tokens.document(current_hash).get(transaction=transaction)
            if current_hash
            else None
        )

        # Another CR already rotated this instant: reuse the still-valid token
        # so all CRs display the same QR and no redundant token is created.
        # A manual refresh (force) must always mint a NEW token instead.
        if not force and current_exp and now < current_exp and old_snapshot is not None and old_snapshot.exists:
            doc_data = old_snapshot.to_dict() or {}
            if doc_data.get("active") and doc_data.get("token"):
                return _payload(doc_data["token"], session_id,
                                doc_data.get("expires_at"), lifetime)

        raw = secrets.token_urlsafe(24)  # cryptographically random
        token_hash = _hash_token(raw)
        if is_permanent:
            expires_at = _permanent_expiry(now)
        else:
            expires_at = now + timedelta(seconds=lifetime)
        transaction.set(tokens.document(token_hash), {
            "session_id": session_id,
            "token_hash": token_hash,
            "token": raw,
            "created_at": _iso(now),
            "expires_at": _iso(expires_at),
            "active": True,
        })
        if old_snapshot is not None and old_snapshot.exists:
            transaction.update(old_snapshot.reference, {"active": False})
        transaction.update(session_ref, {
            "current_qr_hash": token_hash,
            "current_qr_expires_at": _iso(expires_at),
        })
        logger.info("QR generated session_id=%s lifetime=%ss permanent=%s force=%s",
                    session_id, lifetime, is_permanent, force)
        return _payload(raw, session_id, _iso(expires_at), lifetime)

    return _txn(db.transaction())


def rotate_qr_now(session_id: str) -> dict:
    """CR manual refresh: force an immediate rotation regardless of expiry.

    Mints a brand-new token and invalidates the previous one, so the old QR
    stops working at once. Under a Permanent lifetime this is the ONLY way the
    QR changes."""
    return _rotate_qr(get_db(), session_id, force=True)


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
    """Validate ENTRY through a QR and open an individual attendance attempt.

    This is the pre-authentication gateway: the QR token itself is the proof
    that the student arrived at /attendance from a currently valid QR of an
    active session. Checks performed: token exists (QR_INVALID) -> session is
    active (SESSION_NOT_ACTIVE) -> token is still the displayed, unexpired one
    (QR_EXPIRED). On success a fresh attempt with its OWN server-side completion
    window is created; the QR may rotate/expire seconds later without affecting
    it.
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
    # Entry is allowed only while the QR is still the live one on screen.
    if not record.get("active") or (expires_at and now > expires_at):
        raise BadRequestError("The QR code has expired.", code="QR_EXPIRED")

    return record


# ---- Individual attendance attempts ---------------------------------------
#
# An attempt is the student's OWN timer, decoupled from QR rotation. Its id is
# a random opaque value (also the Firestore doc id) so nothing sensitive about
# the session or the token is exposed to the client.


def _attempts():
    return get_db().collection(COLLECTION_ATTENDANCE_ATTEMPTS)


def create_attempt(token_record: dict) -> dict:
    """Open an individual attendance attempt tied to the scanned QR token.

    The completion window starts NOW (server time), independent of the short
    QR lifetime and of any subsequent rotation.
    """
    now = _utcnow()
    window_seconds = settings.STUDENT_SESSION_MINUTES * 60
    expires_at = now + timedelta(seconds=window_seconds)
    attempt_id = secrets.token_urlsafe(24)
    doc = {
        "attempt_id": attempt_id,
        "session_id": token_record["session_id"],
        # Stored as a hash, never the raw QR token.
        "qr_token_hash": token_record.get("token_hash")
        or _hash_token(token_record.get("token", "")),
        "created_at": _iso(now),
        "expires_at": _iso(expires_at),
        "window_seconds": window_seconds,
        "status": "active",
        "firebase_uid": None,   # bound after authentication
        "bound_at": None,
    }
    _attempts().document(attempt_id).set(doc)
    logger.info("Attendance attempt created session_id=%s", token_record["session_id"])
    return doc


def get_attempt(attempt_id: str) -> dict:
    snapshot = _attempts().document(attempt_id).get()
    if not snapshot.exists:
        raise BadRequestError("Attendance attempt not found. Please scan the QR again.",
                              code="ATTENDANCE_ATTEMPT_NOT_FOUND")
    data = snapshot.to_dict() or {}
    data["_doc_id"] = snapshot.id
    return data


def bind_attempt(attempt_id: str, uid: str) -> dict:
    """Associate an authenticated Firebase UID with the attempt (server-side).

    Runs in a Firestore transaction so first bind wins even if two students
    hit the endpoint together: a read-then-write race could otherwise let the
    second account rebind an attempt that was already claimed. A bound attempt
    can never move to a different UID, and an attempt that is used up or whose
    window has run out is rejected here (immediately, rather than only at
    submission time).
    """
    db = get_db()
    ref = _attempts().document(attempt_id)

    @gf.transactional
    def _txn(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            raise BadRequestError(
                "Attendance attempt not found. Please scan the QR again.",
                code="ATTENDANCE_ATTEMPT_NOT_FOUND",
            )
        data = snapshot.to_dict() or {}
        if data.get("status") != "active":
            raise BadRequestError(
                "This attendance attempt has already been used. Please scan a fresh QR.",
                code="ATTENDANCE_ATTEMPT_USED",
            )
        expires_at = _parse(data.get("expires_at"))
        if expires_at is None or _utcnow() > expires_at:
            raise BadRequestError(
                "Attendance window expired. Please scan the current QR code again.",
                code="ATTENDANCE_WINDOW_EXPIRED",
            )
        bound_uid = data.get("firebase_uid")
        if bound_uid and bound_uid != uid:
            raise ForbiddenError(
                "This attendance attempt belongs to a different signed-in account. "
                "Please scan the QR code again.",
                code="ATTENDANCE_ATTEMPT_BOUND",
            )
        if not bound_uid:
            now_iso = _iso(_utcnow())
            transaction.update(ref, {"firebase_uid": uid, "bound_at": now_iso})
            data["firebase_uid"] = uid
            data["bound_at"] = now_iso
        data["_doc_id"] = snapshot.id
        return data

    return _txn(db.transaction())


def attempt_remaining_seconds(attempt: dict) -> int:
    """Server-authoritative seconds left in this attempt's completion window.

    The student page only ever displays this number; it never decides expiry.
    """
    expires_at = _parse(attempt.get("expires_at"))
    if expires_at is None:
        return 0
    return max(0, int((expires_at - _utcnow()).total_seconds()))


def close_attempt(attempt_id: str, status: str) -> None:
    """Finalize an attempt (used/redeemed) so it can never be replayed."""
    _attempts().document(attempt_id).update(
        {"status": status, "closed_at": _iso(_utcnow())}
    )


def validate_attempt_for_check(attempt: dict, session: dict, uid: str) -> None:
    """Enforce attempt expiry + binding before attendance is recorded.

    Deliberately does NOT re-check the QR token's own lifetime: the QR governs
    ENTRY only. The attempt governs COMPLETION.
    """
    if session.get("status") != "active":
        raise BadRequestError("Attendance session is no longer active.",
                              code="SESSION_NOT_ACTIVE")
    if attempt.get("status") != "active":
        raise BadRequestError(
            "This attendance attempt has already been used. Please scan a fresh QR.",
            code="ATTENDANCE_ATTEMPT_USED",
        )
    if attempt.get("firebase_uid") != uid:
        raise ForbiddenError(
            "This attendance attempt is bound to a different account.",
            code="ATTENDANCE_ATTEMPT_BOUND",
        )
    now = _utcnow()
    window_expires_at = _parse(attempt.get("expires_at"))
    if window_expires_at is None or now > window_expires_at:
        raise BadRequestError(
            "Attendance window expired. Please scan the current QR code again.",
            code="ATTENDANCE_WINDOW_EXPIRED",
        )
