"""Authentication service: Firebase ID token verification + role determination.

CR identity is decided HERE (never by the frontend) by matching the user's
email against the Firestore `admin_list` collection.
"""
import logging

import firebase_admin.auth as firebase_auth
from google.cloud import firestore

from app.core.firebase import COLLECTION_ADMIN_LIST, COLLECTION_USERS, get_db
from app.services.errors import UnauthorizedError

logger = logging.getLogger(__name__)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def verify_firebase_id_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return {uid, email, name}."""
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
    }


def is_cr_email(email: str) -> bool:
    """Check the admin_list collection for this email."""
    db = get_db()
    target = normalize_email(email)
    if not target:
        return False
    docs = db.collection(COLLECTION_ADMIN_LIST).where("email", "==", target).limit(1).stream()
    for _ in docs:
        return True
    # Also tolerate mixed-case values already stored in admin_list.
    docs = db.collection(COLLECTION_ADMIN_LIST).stream()
    for doc in docs:
        stored = normalize_email((doc.to_dict() or {}).get("email", ""))
        if stored == target:
            return True
    return False


def determine_role(email: str) -> str:
    return "cr" if is_cr_email(email) else "student"


def upsert_user(uid: str, name: str, email: str, role: str) -> None:
    """Keep the Firestore `users` document in sync on every authorization."""
    db = get_db()
    ref = db.collection(COLLECTION_USERS).document(uid)
    payload = {"uid": uid, "name": name, "email": normalize_email(email), "role": role}
    transaction = db.transaction()

    @firestore.transactional
    def _write(transaction):
        transaction.set(ref, payload, merge=True)

    try:
        _write(transaction)
    except Exception:
        # Non-fatal: authorization must still succeed if the user doc write fails.
        logger.exception("Failed to upsert user document")
