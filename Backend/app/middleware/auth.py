"""Reusable FastAPI auth dependencies built on the HttpOnly application JWT.

get_current_user() -> require_cr() / require_student() / require_admin()
No endpoint repeats JWT verification code.
"""
from dataclasses import dataclass

from fastapi import Depends, Request

from app.core.config import settings
from app.core.security import JWTError, decode_application_jwt
from app.services.errors import ForbiddenError, UnauthorizedError

VALID_ROLES = ("admin", "cr", "student")


@dataclass(frozen=True)
class CurrentUser:
    uid: str
    role: str
    email: str = ""


def _application_token(request: Request) -> str | None:
    """Read the application JWT from the HttpOnly cookie, falling back to the
    'Authorization: Bearer' header.

    The header transport is what makes protected calls work cross-site in
    production (React on Firebase Hosting vs. FastAPI on Render), where the
    browser refuses to store/send the third-party cookie. The Bearer value
    here is the application JWT — NOT the Firebase ID token, which is only
    ever sent to /authorize-user and handled by a separate dependency.
    """
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if token:
        return token
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def get_current_user(request: Request) -> CurrentUser:
    """Verify the application JWT from the HttpOnly cookie or Bearer header."""
    token = _application_token(request)
    if not token:
        raise UnauthorizedError("You are not signed in. Please sign in again.")
    try:
        payload = decode_application_jwt(token)
    except JWTError:
        # Covers missing secret / bad signature / expired tokens.
        raise UnauthorizedError("Your session expired. Please sign in again.")

    uid = payload.get("sub")
    role = payload.get("role")
    if not uid or role not in VALID_ROLES:
        raise UnauthorizedError("Invalid authentication token.")
    return CurrentUser(uid=uid, role=role, email=payload.get("email", "") or "")


def require_cr(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    # Admins inherit every CR capability (they can open and control the CR
    # dashboard). Students are still rejected.
    if user.role not in ("cr", "admin"):
        raise ForbiddenError("Only a Class Representative can perform this action.",
                             code="CR_ONLY")
    return user


def require_student(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "student":
        raise ForbiddenError("Only students can perform this action.", code="STUDENT_ONLY")
    return user


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Admin-only APIs. A student/CR calling these manually gets 403 — the
    role comes from the backend-signed JWT, never from the request body."""
    if user.role != "admin":
        raise ForbiddenError("Only an administrator can perform this action.",
                             code="ADMIN_ONLY")
    return user
