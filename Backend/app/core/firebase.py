"""Firebase Admin SDK initialization and Firestore access helpers.

All Firestore traffic flows through this module — the React frontend never
talks to Firestore directly (except Firebase Authentication sign-in).
"""
import logging
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

from app.core.config import settings

logger = logging.getLogger(__name__)

_db: firestore.Client | None = None
_initialized: bool = False


def _credentials_path() -> Path | None:
    raw = settings.FIREBASE_CREDENTIALS_FILE or ""
    if not raw:
        return None
    path = settings.resolve_path(raw)
    return path if path.exists() else None


def init_firebase() -> bool:
    """Initialize Firebase Admin. Returns True when available.

    The backend still starts (and /health works) without credentials so the
    project can be developed incrementally; protected APIs return 503 until
    Firebase is configured.
    """
    global _db, _initialized
    if _initialized:
        return _db is not None

    _initialized = True
    path = _credentials_path()
    if path is None:
        logger.warning(
            "Firebase credentials not found (FIREBASE_CREDENTIALS_FILE). "
            "Firestore-dependent APIs will return 503."
        )
        return False

    try:
        if not firebase_admin._apps:  # noqa: SLF001 - guard against double init
            firebase_admin.initialize_app(credentials.Certificate(str(path)))
        _db = firestore.client()
        logger.info("Firebase Admin initialized (project=%s)",
                    getattr(firebase_admin.get_app().credential, "project_id", "unknown"))
        return True
    except Exception:
        logger.exception("Failed to initialize Firebase Admin")
        return False


def is_available() -> bool:
    return _db is not None


def get_db() -> firestore.Client:
    if _db is None:
        from app.services.errors import ServiceUnavailableError
        raise ServiceUnavailableError("Backend storage is not configured yet.")
    return _db


# ---- Collection helpers -------------------------------------------------

COLLECTION_ADMIN_LIST = "admin_list"
COLLECTION_USERS = "users"
COLLECTION_SESSIONS = "sessions"
COLLECTION_QR_TOKENS = "qr_tokens"
COLLECTION_ATTENDANCE = "attendance"
