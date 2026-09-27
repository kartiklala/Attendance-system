"""Pydantic models for the student attendance check flow."""
from pydantic import BaseModel, Field, field_validator


class VerifyTokenRequest(BaseModel):
    session_token: str = Field(min_length=8, max_length=128)

    @field_validator("session_token")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class VerifyTokenResponse(BaseModel):
    success: bool = True
    session_id: str
    window_seconds: int
    message: str = "Attendance session found."


class AttendanceCheckRequest(BaseModel):
    session_token: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    enrollment_no: str = Field(min_length=1, max_length=40)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @field_validator("name", "enrollment_no", "session_token")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class AttendanceCheckResponse(BaseModel):
    success: bool = True
    status: str = "PRESENT"
    message: str = "Attendance marked successfully."
    marked_at: str = ""
