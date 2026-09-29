"""Pydantic models for the student attendance flow (last-3 enrollment + check)."""
from pydantic import BaseModel, Field, field_validator

from app.services.student_service import normalize_last3


class StartAttemptRequest(BaseModel):
    """The ONLY input for opening an attendance attempt is the QR token.

    No name, no enrollment number, no status, no session id: the backend
    resolves the QR -> session itself (see qr_service.scan_token).
    """
    token: str = Field(min_length=8, max_length=128)

    @field_validator("token")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class StartAttemptResponse(BaseModel):
    """Minimum information the student page needs — nothing sensitive."""
    success: bool = True
    attempt_id: str
    window_seconds: int
    # Server-clock timestamps: the frontend countdown is a display only.
    expires_at: str
    remaining_seconds: int


class BindAttemptRequest(BaseModel):
    attempt_id: str = Field(min_length=8, max_length=128)

    @field_validator("attempt_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class BindAttemptResponse(BaseModel):
    """Attempt + saved-identity in one round trip (replaces /student/me).

    `verified` is decided by the backend users/{uid} document — the client
    never supplies an identity.
    """
    success: bool = True
    verified: bool = False
    name: str = ""
    masked_enrollment: str = ""
    remaining_seconds: int = 0


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
    enrollment saved against the student's Firebase UID. The QR token is not
    accepted either: the individual attendance attempt (created on entry from a
    live QR) is what authorizes completion, so a QR rotating meanwhile cannot
    invalidate the student."""
    attempt_id: str = Field(min_length=8, max_length=128)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @field_validator("attempt_id")
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
    # Google profile picture for the CR popup ("" -> the UI shows its fallback
    # avatar). Public avatar URL from the signer's own Google profile; nothing
    # else about the student (enrollment, IP, device, coordinates) is exposed.
    photo_url: str = ""


class AttendeeEntry(BaseModel):
    """One row in the CR's live 'Students Present' list.

    Carries the enrollment number (the `recent` popup deliberately does not),
    so the CR can confirm WHO has marked — not just how many. Only the three
    fields the CR needs are exposed; no IP/device/location data rides along.
    """
    name: str
    enrollment_no: str
    marked_at: str


class SessionStats(BaseModel):
    session_id: str
    session_name: str = ""
    session_status: str
    total_students: int
    present_count: int
    percentage: float
    recent: list[PresentEvent] = []
    # Full present list for THIS session (most recent first) for the CR's live
    # 'Students Present' panel. Reuses the same poll as `recent` — no extra
    # request. Bounded by class size, so the payload stays small.
    attendees: list[AttendeeEntry] = []
    # Human-readable proxy-attendance flags ("⚠ Multiple students…").
    # Raw IP/device data is never exposed here.
    warnings: list[str] = []


class StatsResponse(BaseModel):
    success: bool = True
    stats: SessionStats


class Absentee(BaseModel):
    name: str
    enrollment_no: str


class AttendanceSummary(BaseModel):
    session_id: str
    session_name: str = ""
    status: str
    total_students: int
    present_count: int
    absent_count: int
    percentage: float
    absentees: list[Absentee] = []
