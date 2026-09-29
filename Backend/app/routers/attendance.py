"""Attendance routes: shared CR session control, QR rotation, student flow."""
import logging

from fastapi import APIRouter, Depends, Request

from app.core.config import settings
from app.middleware.auth import CurrentUser, get_current_user, require_cr, require_student
from app.models.attendance import (
    AttendanceCheckRequest,
    AttendanceCheckResponse,
    Last3Request,
    StatsResponse,
    StudentConfirmResponse,
    StudentFoundResponse,
    StudentMeResponse,
    VerifyTokenRequest,
    VerifyTokenResponse,
)
from app.models.session import (
    ActiveSessionResponse,
    EndAttendanceRequest,
    EndAttendanceResponse,
    QRLifetimeRequest,
    QRLifetimeResponse,
    QRRefreshRequest,
    QRRefreshResponse,
    SessionInfo,
    SessionResponse,
    StartAttendanceRequest,
    StartAttendanceResponse,
    SummaryResponse,
)
from app.services import (
    attendance_service,
    qr_service,
    session_service,
    student_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["attendance"])


def _session_info(session: dict) -> SessionInfo:
    raw_lifetime = session.get("qr_lifetime_seconds")
    # None-safe: 0 is a valid "Permanent" lifetime, never coerce it away.
    lifetime = (settings.QR_TOKEN_LIFETIME_SECONDS if raw_lifetime is None
                else int(raw_lifetime))
    return SessionInfo(
        session_id=session["session_id"],
        session_name=session.get("session_name", ""),
        cr_uid=session.get("cr_uid", ""),
        latitude=float(session.get("latitude", 0)),
        longitude=float(session.get("longitude", 0)),
        radius_meters=float(session.get("radius_meters", settings.ATTENDANCE_RADIUS_METERS)),
        status=session.get("status", "active"),
        qr_lifetime_seconds=lifetime,
        started_at=str(session.get("started_at", "")),
        ended_at=session.get("ended_at"),
    )


def _client_info(request: Request) -> dict:
    """SERVER-observed request info for the attendance audit trail. Students
    never send or control these fields. Behind Render/Firebase proxies the
    real client IP is in the left-most X-Forwarded-For entry."""
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else ""
    )
    return {"ip": ip[:64], "user_agent": request.headers.get("user-agent", "")}


# ---- CR: shared session lifecycle ------------------------------------------

@router.get("/active-session", response_model=ActiveSessionResponse)
def get_active_session(cr: CurrentUser = Depends(require_cr)) -> ActiveSessionResponse:
    """CR-only rejoin endpoint: the single active session (or None).

    The backend is the source of truth — after /authorize-user the CR
    dashboard calls this to automatically continue an already-running session.
    """
    session = session_service.get_active_session()
    if session is None:
        return ActiveSessionResponse(success=True, session=None, qr=None, stats=None)
    qr = qr_service.get_current_qr(session["session_id"])
    stats = attendance_service.get_session_stats(session["session_id"])
    return ActiveSessionResponse(success=True, session=_session_info(session), qr=qr, stats=stats)


@router.post("/start-attendance", response_model=StartAttendanceResponse, status_code=200)
def start_attendance(
    payload: StartAttendanceRequest,
    cr: CurrentUser = Depends(require_cr),
) -> StartAttendanceResponse:
    """CR-only: start the session — or join the existing active session.

    create_or_get_session is transaction-safe: two CRs clicking simultaneously
    can never produce two active sessions.
    """
    session, created = session_service.create_or_get_session(
        cr.uid, payload.latitude, payload.longitude, payload.session_name
    )
    qr = qr_service.get_current_qr(session["session_id"])
    return StartAttendanceResponse(
        success=True,
        session_id=session["session_id"],
        session_name=session.get("session_name", ""),
        radius_meters=session["radius_meters"],
        is_new=created,
        qr=qr,
    )


@router.post("/qr/refresh", response_model=QRRefreshResponse)
def refresh_qr(
    payload: QRRefreshRequest,
    cr: CurrentUser = Depends(require_cr),
) -> QRRefreshResponse:
    """CR-only: return the session's current QR (rotates only when expired).

    Every CR receives the SAME token until rotation, so all CRs show one QR.
    """
    qr = qr_service.get_current_qr(payload.session_id)
    return QRRefreshResponse(success=True, qr=qr)


@router.post("/qr/rotate", response_model=QRRefreshResponse)
def rotate_qr(
    payload: QRRefreshRequest,
    cr: CurrentUser = Depends(require_cr),
) -> QRRefreshResponse:
    """CR-only: force a brand-new QR right now and invalidate the previous
    token. Powers the manual "Refresh QR" control (notably in Permanent mode,
    where the QR would otherwise never change on its own)."""
    qr = qr_service.rotate_qr_now(payload.session_id)
    return QRRefreshResponse(success=True, qr=qr)


@router.post("/qr/lifetime", response_model=QRLifetimeResponse)
def set_qr_lifetime(
    payload: QRLifetimeRequest,
    cr: CurrentUser = Depends(require_cr),
) -> QRLifetimeResponse:
    """CR-only: change how long FUTURE QR tokens of the shared session stay
    valid. The requested value is validated against the backend allow-list;
    the current token keeps its own expiry until the next rotation."""
    lifetime = session_service.set_qr_lifetime(payload.session_id, payload.lifetime_seconds)
    return QRLifetimeResponse(success=True, qr_lifetime_seconds=lifetime)


@router.post("/end-attendance", response_model=EndAttendanceResponse)
def end_attendance(
    payload: EndAttendanceRequest,
    cr: CurrentUser = Depends(require_cr),
) -> EndAttendanceResponse:
    """CR-only: end the shared session for ALL CRs, invalidate its tokens,
    and return the server-computed attendance summary (absentees + counts)."""
    session_service.end_session(payload.session_id, cr.uid)
    qr_service.invalidate_session_tokens(payload.session_id)
    summary = attendance_service.get_session_summary(payload.session_id)
    return EndAttendanceResponse(success=True, summary=summary)


@router.get("/sessions/{session_id}/summary", response_model=SummaryResponse)
def get_summary(
    session_id: str,
    cr: CurrentUser = Depends(require_cr),
) -> SummaryResponse:
    """CR-only: final attendance summary for a session (also after it ended)."""
    return SummaryResponse(success=True, summary=attendance_service.get_session_summary(session_id))


@router.get("/sessions/{session_id}/stats", response_model=StatsResponse)
def get_stats(
    session_id: str,
    cr: CurrentUser = Depends(require_cr),
) -> StatsResponse:
    """CR-only: live present/total counts + recent arrivals (jar animation)."""
    return StatsResponse(success=True, stats=attendance_service.get_session_stats(session_id))


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> SessionResponse:
    """Authenticated: view session details (location redacted for students)."""
    session = session_service.get_session(session_id)
    if user.role not in ("cr", "admin"):
        session = {**session, "cr_uid": "", "latitude": 0.0, "longitude": 0.0}
    return SessionResponse(success=True, session=_session_info(session))


# ---- Student: token, enrollment lookup, attendance --------------------------

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


@router.get("/student/me", response_model=StudentMeResponse)
def student_me(student: CurrentUser = Depends(require_student)) -> StudentMeResponse:
    """Check whether this Google account already has a verified enrollment."""
    saved = student_service.get_saved_enrollment(student.uid)
    if saved is None:
        return StudentMeResponse(success=True, verified=False)
    return StudentMeResponse(
        success=True,
        verified=True,
        name=saved["name"],
        masked_enrollment=student_service.mask_enrollment(saved["enrollment_no"]),
    )


@router.post("/student/lookup", response_model=StudentFoundResponse)
def student_lookup(
    payload: Last3Request,
    student: CurrentUser = Depends(require_student),
) -> StudentFoundResponse:
    """Resolve the last 3 enrollment digits against Sheet1 (backend-side).

    The full roster never reaches the frontend; only the matched name and a
    masked enrollment number are returned.
    """
    enrollment, name = student_service.resolve_last3(payload.last3)
    return StudentFoundResponse(
        success=True, name=name, masked_enrollment=student_service.mask_enrollment(enrollment)
    )


@router.post("/student/confirm", response_model=StudentConfirmResponse)
def student_confirm(
    payload: Last3Request,
    student: CurrentUser = Depends(require_student),
) -> StudentConfirmResponse:
    """Confirm the lookup: backend re-verifies against Sheet1 and links the
    verified enrollment to the Firebase UID (users/{uid})."""
    saved = student_service.get_saved_enrollment(student.uid)
    if saved is not None:
        # Already associated: keep the authoritative saved value.
        enrollment, name = saved["enrollment_no"], saved["name"]
        student_service.save_verified_enrollment(student.uid, name, enrollment)
    else:
        enrollment, name = student_service.resolve_last3(payload.last3)
        result = student_service.save_verified_enrollment(student.uid, name, enrollment)
        enrollment, name = result["enrollment_no"], result["name"]
    return StudentConfirmResponse(
        success=True, name=name, masked_enrollment=student_service.mask_enrollment(enrollment)
    )


@router.post("/attendance/check", response_model=AttendanceCheckResponse)
def attendance_check(
    payload: AttendanceCheckRequest,
    request: Request,
    student: CurrentUser = Depends(require_student),
) -> AttendanceCheckResponse:
    """Student attendance submission. Identity comes from the backend-verified
    enrollment saved against the Firebase UID; every other check is enforced
    server-side (see attendance_service.mark_attendance). IP and user-agent
    are captured from the actual HTTP request for the audit trail."""
    result = attendance_service.mark_attendance(
        student.uid, payload, client=_client_info(request)
    )
    return AttendanceCheckResponse(
        success=True,
        status=result["status"],
        message="Attendance marked successfully.",
        marked_at=result["marked_at"],
    )
