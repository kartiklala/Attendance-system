"""Pydantic models for the student attendance flow (last-3 enrollment + check)."""
from pydantic import BaseModel, Field, field_validator

from app.services.student_service import normalize_last3


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


class Last3Request(BaseModel):
    """Student submits ONLY the last 3 digits of their enrollment number."""
    last3: str = Field(min_length=3, max_length=3)

    @field_validator("last3")
    @classmethod
    def _digits(cls, v: str) -> str:
        return normalize_last3(v)


class StudentFoundResponse(BaseModel):
    success: bool = True
    name: str
    masked_enrollment: str


class StudentConfirmResponse(BaseModel):
    success: bool = True
    name: str
    masked_enrollment: str
    already_saved: bool = False


class StudentMeResponse(BaseModel):
    success: bool = True
    verified: bool
    name: str = ""
    masked_enrollment: str = ""


class AttendanceCheckRequest(BaseModel):
    """Name/enrollment are NOT accepted here — the backend uses the verified
    enrollment saved against the student's Firebase UID."""
    session_token: str = Field(min_length=8, max_length=128)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @field_validator("session_token")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class AttendanceCheckResponse(BaseModel):
    success: bool = True
    status: str = "PRESENT"
    message: str = "Attendance marked successfully."
    marked_at: str = ""


class PresentEvent(BaseModel):
    name: str
    marked_at: str


class SessionStats(BaseModel):
    session_id: str
    session_status: str
    total_students: int
    present_count: int
    percentage: float
    recent: list[PresentEvent] = []


class StatsResponse(BaseModel):
    success: bool = True
    stats: SessionStats


class Absentee(BaseModel):
    name: str
    enrollment_no: str


class AttendanceSummary(BaseModel):
    session_id: str
    status: str
    total_students: int
    present_count: int
    absent_count: int
    percentage: float
    absentees: list[Absentee] = []
