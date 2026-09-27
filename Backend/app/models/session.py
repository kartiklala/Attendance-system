"""Pydantic models for attendance sessions and QR tokens."""
from pydantic import BaseModel, Field

from app.models.attendance import AttendanceSummary


class StartAttendanceRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class QRPayload(BaseModel):
    """A freshly generated QR token (raw token is intentionally exposed here —
    it is a random, short-lived, single-purpose value, not a credential)."""
    session_id: str
    qr_token: str
    qr_url: str
    expires_in_seconds: int
    countdown_seconds: int


class StartAttendanceResponse(BaseModel):
    success: bool = True
    session_id: str
    radius_meters: float
    is_new: bool = True  # False when the CR joined the existing active session
    qr: QRPayload


class EndAttendanceRequest(BaseModel):
    session_id: str


class EndAttendanceResponse(BaseModel):
    success: bool = True
    message: str = "Attendance session ended successfully."
    summary: AttendanceSummary | None = None


class QRRefreshRequest(BaseModel):
    session_id: str


class QRRefreshResponse(BaseModel):
    success: bool = True
    qr: QRPayload


class SessionInfo(BaseModel):
    session_id: str
    cr_uid: str
    latitude: float
    longitude: float
    radius_meters: float
    status: str
    started_at: str
    ended_at: str | None = None


class SessionResponse(BaseModel):
    success: bool = True
    session: SessionInfo


class ActiveSessionResponse(BaseModel):
    """GET /active-session — the single shared active session (or None)."""
    success: bool = True
    session: SessionInfo | None = None
    qr: QRPayload | None = None
    stats: dict | None = None


class SummaryResponse(BaseModel):
    success: bool = True
    summary: AttendanceSummary
