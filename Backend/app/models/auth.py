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


class MeResponse(BaseModel):
    success: bool = True
    user: AuthorizedUser


class LogoutResponse(BaseModel):
    success: bool = True


class MessageResponse(BaseModel):
    success: bool = True
    message: str = Field(default="")
