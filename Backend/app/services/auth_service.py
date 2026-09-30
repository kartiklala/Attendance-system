"""Authentication service: Firebase ID token verification + role determination.

Roles are decided HERE (never by the frontend):
- `admins` collection email      -> "admin"   (manages the CR list)
- `admin_list` collection email  -> "cr"      (runs attendance sessions)
- everyone else                  -> "student"
An "admin" role string is used for both, and require_cr() explicitly lets
admins through, so an admin has every CR capability WITHOUT needing to also
appear in `admin_list`. Admins are still rejected from student-only actions.
"""
import logging
import re
import time

import firebase_admin.auth as firebase_auth
import jwt
from google.cloud import firestore

from app.core.firebase import (
    COLLECTION_ADMIN_LIST,
    COLLECTION_ADMINS,
    COLLECTION_USERS,
    get_db,
)
from app.services.errors import UnauthorizedError

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Role-collection document ids are the normalized email with Firestore's
# forbidden characters substituted. Mirrors routers/admin.py's DOC_ID_SAFE_RE;
# duplicated rather than imported so the service layer never depends on a
# router module (which would risk an import cycle).
DOC_ID_SAFE_RE = re.compile(r"[/.#\[\]$*]")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email or ""))


def _safe_token_summary(id_token: str) -> dict:
    """Describe a REJECTED bearer token for logging, without revealing anything.

    `verify_id_token()` collapses every rejection (wrong project, application
    JWT sent by mistake, truncated token, bad signature, future/past clock)
    into one InvalidIdTokenError, and the client message is deliberately
    generic, so a 401 alone can never say which it was. This re-reads the
    UNVERIFIED header/claims to make the log line decisive instead.

    Never contains the token itself, a `sub`/uid, an email or any other user
    identifier. `iss`/`aud` are project ids (already public in the client
    config) and are exactly what distinguishes a project mismatch from a
    malformed token, so they are kept.
    """
    raw = id_token or ""
    summary = {
        "length": len(raw),
        "segments": raw.count(".") + 1,
        "alg": None,
        "has_kid": False,
        "iss": None,
        "aud": None,
        # Positive = expires in the future; negative = already expired.
        "exp_in_seconds": None,
    }
    try:
        header = jwt.get_unverified_header(raw)
        claims = jwt.decode(raw, options={"verify_signature": False})
    except Exception:
        # Not a JWT at all (or unreadable): length + segment count alone already
        # distinguish a truncated token from a wrong-project one.
        summary["parse"] = "unreadable"
        return summary
    exp = claims.get("exp")
    summary.update({
        "alg": header.get("alg"),
        "has_kid": bool(header.get("kid")),
        "iss": claims.get("iss"),
        "aud": claims.get("aud"),
        "exp_in_seconds": (int(exp) - int(time.time())) if isinstance(exp, (int, float)) else None,
    })
    return summary


def _verify_failure_reason(exc: Exception) -> str:
    """SDK message with any echoed token material stripped out.

    Some firebase-admin messages embed a raw prefix of the token (e.g.
    "Wrong number of segments in token: b'eyJ...'"); that fragment must not
    reach the logs, so everything after such a marker is dropped.
    """
    message = str(exc).split(" in token:")[0]
    return message[:200]


def verify_firebase_id_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return {uid, email, name, photo, auth_time}."""
    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except firebase_auth.ExpiredIdTokenError as exc:
        logger.warning("Rejected Firebase ID token (expired): %s | %s",
                       _verify_failure_reason(exc), _safe_token_summary(id_token))
        raise UnauthorizedError("Firebase ID token expired. Please sign in again.") from exc
    except firebase_auth.InvalidIdTokenError as exc:
        logger.warning("Rejected Firebase ID token (invalid): %s | %s",
                       _verify_failure_reason(exc), _safe_token_summary(id_token))
        raise UnauthorizedError("Invalid Firebase ID token.") from exc
    except firebase_auth.RevokedIdTokenError as exc:
        logger.warning("Rejected Firebase ID token (revoked)")
        raise UnauthorizedError("Your session was revoked.") from exc
    except Exception as exc:  # cert fetch failures, etc.
        logger.exception("Firebase token verification failed")
        raise UnauthorizedError("Could not verify your sign-in.") from exc

    return {
        "uid": decoded.get("uid", ""),
        "email": normalize_email(decoded.get("email", "")),
        "name": decoded.get("name") or "",
        # Google profile avatar (lh3.googleusercontent.com): a public image URL
        # the signer's own account exposes, used for the CR present-popup.
        "photo": str(decoded.get("picture") or "")[:512],
        # When this Google session was created, in epoch seconds. It comes from
        # the SIGNATURE-VERIFIED token, so no client can set it — which is what
        # makes it usable as shared-browser detection evidence (0 = unknown).
        "auth_time": _as_epoch(decoded.get("auth_time")),
    }


def _as_epoch(value: object) -> int:
    """auth_time as epoch seconds, tolerating an absent or non-numeric claim."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _email_in_collection(collection: str, email: str) -> bool:
    """Membership check for a role collection.

    Every write path in this codebase (routers/admin.py) normalizes the email
    before storing it and uses that normalized value as the document id, so a
    direct document read is the common-case lookup: one round trip, no index
    required, and constant time regardless of collection size.

    The equality query remains only as a fallback for rows whose document id
    does not follow that scheme (e.g. an `admins` entry seeded by hand from
    the Firebase Console). It is still a limit(1) indexed read. This replaces
    the previous whole-collection stream fallback, which ran on every
    /authorize-user — twice, since determine_role() checks admins then
    admin_list — and therefore scaled the sign-in path with collection size.
    """
    db = get_db()
    target = normalize_email(email)
    if not target:
        return False

    doc_id = DOC_ID_SAFE_RE.sub("_", target)
    if db.collection(collection).document(doc_id).get().exists:
        return True

    docs = db.collection(collection).where("email", "==", target).limit(1).stream()
    for _ in docs:
        return True
    return False


def is_cr_email(email: str) -> bool:
    """Check the admin_list (CR) collection for this email."""
    return _email_in_collection(COLLECTION_ADMIN_LIST, email)


def is_admin_email(email: str) -> bool:
    """Check the dedicated admins collection for this email."""
    return _email_in_collection(COLLECTION_ADMINS, email)


def determine_role(email: str) -> str:
    if is_admin_email(email):
        return "admin"
    if is_cr_email(email):
        return "cr"
    return "student"


def upsert_user(uid: str, name: str, email: str, role: str,
                photo_url: str = "", auth_time: int = 0) -> None:
    """Keep the Firestore `users` document in sync on every authorization.

    The Google avatar is only ever written when the signer actually has one;
    an existing value is kept when a later token carries no picture.

    `auth_time` is purely additive: it changes no role, no decision, no token
    and no sign-in behaviour. It simply records how old the verified Google
    session was, which shared-browser detection later reads back from this same
    document so the value never has to be trusted from a request body.
    """
    db = get_db()
    ref = db.collection(COLLECTION_USERS).document(uid)
    payload = {"uid": uid, "name": name, "email": normalize_email(email), "role": role}
    if photo_url:
        payload["photo_url"] = photo_url
    if auth_time:
        payload["auth_time"] = int(auth_time)
    transaction = db.transaction()

    @firestore.transactional
    def _write(transaction):
        transaction.set(ref, payload, merge=True)

    try:
        _write(transaction)
    except Exception:
        # Non-fatal: authorization must still succeed if the user doc write fails.
        logger.exception("Failed to upsert user document")
