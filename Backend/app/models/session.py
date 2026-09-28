"""Pydantic models for attendance sessions and QR tokens."""
from pydantic import BaseModel, Field, field_validator

from app.models.attendance import AttendanceSummary


class StartAttendanceRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    # Human-readable name entered by the CR. Display/sheet labeling only —
    # it never replaces or influences the backend-generated session_id.
    session_name: str = Field(min_length=1, max_length=80)

    @field_validator("session_name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        cleaned = " ".join(v.split())  # trim + collapse inner whitespace
        if not cleaned:
            raise ValueError("session_name must not be empty")
        return cleaned


class QRPayload(BaseModel):
    """A freshly generated QR token (raw token is intentionally exposed here —
    it is a random, short-lived, single-purpose value, not a credential)."""
    session_id: str
    qr_token: str
    qr_url: str
    expires_in_seconds: int
    countdown_seconds: int
    # True when the session runs on the Permanent lifetime (never auto-expires;
    # only the CR's manual refresh replaces it). expires_in_seconds is 0 then.
    is_permanent: bool = False


class StartAttendanceResponse(BaseModel):
    success: bool = True
    session_id: str
    session_name: str = ""
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


class QRLifetimeRequest(BaseModel):
    """CR request to change the lifetime of FUTURE QR tokens."""
    session_id: str
    lifetime_seconds: int


class QRLifetimeResponse(BaseModel):
    success: bool = True
    qr_lifetime_seconds: int
    message: str = "QR lifetime updated. Applies to the next rotation."


class QRRefreshResponse(BaseModel):
    success: bool = True
    qr: QRPayload


class SessionInfo(BaseModel):
    session_id: str
    session_name: str = ""
    cr_uid: str
    latitude: float
    longitude: float
    radius_meters: float
    status: str
    qr_lifetime_seconds: int = 10
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
