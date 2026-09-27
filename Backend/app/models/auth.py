"""Pydantic request/response models for authentication."""
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["admin", "cr", "student"]


class AuthorizedUser(BaseModel):
    uid: str
    name: str = ""
    email: str = ""
    role: Role


class AuthorizeResponse(BaseModel):
    success: bool = True
    user: AuthorizedUser
    # The 3-minute application JWT is ALSO returned in the body so the client
    # can send it as an 'Authorization: Bearer' header. The frontend/backend
    # run on different sites in production, where browsers block third-party
    # cookies — the HttpOnly cookie alone never round-trips cross-site.
    application_token: str = ""


class MeResponse(BaseModel):
    success: bool = True
    user: AuthorizedUser


class LogoutResponse(BaseModel):
    success: bool = True


class MessageResponse(BaseModel):
    success: bool = True
    message: str = Field(default="")
