"""Attendance routes: CR session control, QR rotation, student check."""
import logging

from fastapi import APIRouter, Depends

from app.core.config import settings
from app.middleware.auth import CurrentUser, get_current_user, require_cr, require_student
from app.models.attendance import (
    AttendanceCheckRequest,
    AttendanceCheckResponse,
    VerifyTokenRequest,
    VerifyTokenResponse,
)
from app.models.session import (
    EndAttendanceRequest,
    EndAttendanceResponse,
    QRRefreshRequest,
    QRRefreshResponse,
    SessionInfo,
    SessionResponse,
    StartAttendanceRequest,
    StartAttendanceResponse,
)
from app.services import attendance_service, qr_service, session_service
from app.services.errors import ForbiddenError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["attendance"])


@router.post("/start-attendance", response_model=StartAttendanceResponse, status_code=201)
def start_attendance(
    payload: StartAttendanceRequest,
    cr: CurrentUser = Depends(require_cr),
) -> StartAttendanceResponse:
    """CR-only: create an active session at the CR's current location."""
    session = session_service.create_session(cr.uid, payload.latitude, payload.longitude)
    qr = qr_service.generate_token(session["session_id"], rotate=False)
    return StartAttendanceResponse(
        success=True,
        session_id=session["session_id"],
        radius_meters=session["radius_meters"],
        qr=qr,
    )


@router.post("/qr/refresh", response_model=QRRefreshResponse)
def refresh_qr(
    payload: QRRefreshRequest,
    cr: CurrentUser = Depends(require_cr),
) -> QRRefreshResponse:
    """CR-only: rotate the QR token of an active session (invalidates previous)."""
    session = session_service.get_session(payload.session_id)
    if session.get("cr_uid") != cr.uid:
        raise ForbiddenError("You can only refresh your own sessions.", code="NOT_SESSION_OWNER")
    qr = qr_service.generate_token(payload.session_id, rotate=True)
    return QRRefreshResponse(success=True, qr=qr)


@router.post("/end-attendance", response_model=EndAttendanceResponse)
def end_attendance(
    payload: EndAttendanceRequest,
    cr: CurrentUser = Depends(require_cr),
) -> EndAttendanceResponse:
    """CR-only: end an active session and invalidate remaining QR tokens."""
    session_service.end_session(payload.session_id, cr.uid)
    qr_service.invalidate_session_tokens(payload.session_id)
    return EndAttendanceResponse(success=True)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> SessionResponse:
    """Authenticated: view session details (full data only for the owning CR)."""
    session = session_service.get_session(session_id)
    if user.role != "cr" and session.get("cr_uid") != user.uid:
        # Students only need to know the session exists/is active.
        session = {
            **session,
            "cr_uid": "",
            "latitude": 0.0,
            "longitude": 0.0,
        }
    return SessionResponse(
        success=True,
        session=SessionInfo(
            session_id=session["session_id"],
            cr_uid=session.get("cr_uid", ""),
            latitude=float(session.get("latitude", 0)),
            longitude=float(session.get("longitude", 0)),
            radius_meters=float(session.get("radius_meters", settings.ATTENDANCE_RADIUS_METERS)),
            status=session.get("status", "active"),
            started_at=str(session.get("started_at", "")),
            ended_at=session.get("ended_at"),
        ),
    )


@router.post("/attendance/verify-token", response_model=VerifyTokenResponse)
def verify_token(
    payload: VerifyTokenRequest,
    user: CurrentUser = Depends(get_current_user),
) -> VerifyTokenResponse:
    """Student scanned a QR: validate it and start the server-side
    one-minute completion window."""
    record = qr_service.scan_token(payload.session_token)
    return VerifyTokenResponse(
        success=True,
        session_id=record["session_id"],
        window_seconds=settings.STUDENT_SESSION_MINUTES * 60,
    )


@router.post("/attendance/check", response_model=AttendanceCheckResponse)
def attendance_check(
    payload: AttendanceCheckRequest,
    student: CurrentUser = Depends(require_student),
) -> AttendanceCheckResponse:
    """Student attendance submission. Every security check is enforced
    server-side (see attendance_service.mark_attendance)."""
    result = attendance_service.mark_attendance(student.uid, payload)
    return AttendanceCheckResponse(
        success=True,
        status=result["status"],
        message="Attendance marked successfully.",
        marked_at=result["marked_at"],
    )
