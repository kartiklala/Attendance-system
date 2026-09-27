"""Application JWT creation/verification and cookie helpers."""
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings


class JWTError(Exception):
    """Raised when a token is missing, malformed or expired."""


def create_application_jwt(uid: str, role: str, email: str = "") -> str:
    """Create the short-lived (settings.JWT_EXPIRE_MINUTES) application JWT."""
    if not settings.JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY is not configured")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": uid,
        "role": role,
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_application_jwt(token: str) -> dict:
    """Decode and verify an application JWT. Raises JWTError on any problem."""
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:  # expired, bad signature, wrong claims...
        raise JWTError(str(exc)) from exc


def apply_auth_cookie(response, token: str) -> None:
    """Store the application JWT in an HttpOnly cookie.

    In production the cookie is Secure + SameSite=None (cross-site HTTPS).
    In local development Secure is disabled so it works on http://localhost,
    while remaining HttpOnly and same-site lax (localhost:5173 <-> :8000 are
    same-site because ports do not affect the cookie site).
    """
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token,
        max_age=settings.JWT_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.is_production,
        samesite="none" if settings.is_production else "lax",
        path="/",
        domain=None,
    )


def clear_auth_cookie(response) -> None:
    response.delete_cookie(key=settings.AUTH_COOKIE_NAME, path="/")
