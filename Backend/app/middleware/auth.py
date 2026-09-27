"""Reusable FastAPI auth dependencies built on the HttpOnly application JWT.

get_current_user() -> require_cr() / require_student()
No endpoint repeats JWT verification code.
"""
from dataclasses import dataclass

from fastapi import Depends, Request

from app.core.config import settings
from app.core.security import JWTError, decode_application_jwt
from app.services.errors import ForbiddenError, UnauthorizedError


@dataclass(frozen=True)
class CurrentUser:
    uid: str
    role: str
    email: str = ""


def get_current_user(request: Request) -> CurrentUser:
    """Verify the application JWT from the HttpOnly cookie."""
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if not token:
        raise UnauthorizedError("You are not signed in. Please sign in again.")
    try:
        payload = decode_application_jwt(token)
    except JWTError:
        # Covers missing secret / bad signature / expired tokens.
        raise UnauthorizedError("Your session expired. Please sign in again.")

    uid = payload.get("sub")
    role = payload.get("role")
    if not uid or role not in ("cr", "student"):
        raise UnauthorizedError("Invalid authentication token.")
    return CurrentUser(uid=uid, role=role, email=payload.get("email", "") or "")


def require_cr(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "cr":
        raise ForbiddenError("Only a Class Representative can perform this action.",
                             code="CR_ONLY")
    return user


def require_student(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "student":
        raise ForbiddenError("Only students can perform this action.", code="STUDENT_ONLY")
    return user
