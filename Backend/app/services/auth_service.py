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

import firebase_admin.auth as firebase_auth
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


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email or ""))


def verify_firebase_id_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return {uid, email, name, photo}."""
    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except firebase_auth.ExpiredIdTokenError as exc:
        raise UnauthorizedError("Firebase ID token expired. Please sign in again.") from exc
    except firebase_auth.InvalidIdTokenError as exc:
        raise UnauthorizedError("Invalid Firebase ID token.") from exc
    except firebase_auth.RevokedIdTokenError as exc:
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
    }


def _email_in_collection(collection: str, email: str) -> bool:
    """Membership check tolerant of mixed-case values already stored."""
    db = get_db()
    target = normalize_email(email)
    if not target:
        return False
    docs = db.collection(collection).where("email", "==", target).limit(1).stream()
    for _ in docs:
        return True
    # Also tolerate mixed-case values already stored in the collection.
    docs = db.collection(collection).stream()
    for doc in docs:
        stored = normalize_email((doc.to_dict() or {}).get("email", ""))
        if stored == target:
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
                photo_url: str = "") -> None:
    """Keep the Firestore `users` document in sync on every authorization.

    The Google avatar is only ever written when the signer actually has one;
    an existing value is kept when a later token carries no picture.
    """
    db = get_db()
    ref = db.collection(COLLECTION_USERS).document(uid)
    payload = {"uid": uid, "name": name, "email": normalize_email(email), "role": role}
    if photo_url:
        payload["photo_url"] = photo_url
    transaction = db.transaction()

    @firestore.transactional
    def _write(transaction):
        transaction.set(ref, payload, merge=True)

    try:
        _write(transaction)
    except Exception:
        # Non-fatal: authorization must still succeed if the user doc write fails.
        logger.exception("Failed to upsert user document")
