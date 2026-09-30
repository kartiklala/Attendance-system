"""Attendance routes: shared CR session control, QR rotation, student flow."""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from app.core.config import settings
from app.middleware.auth import CurrentUser, get_current_user, require_cr, require_student
from app.models.attendance import (
    AttendanceCheckRequest,
    AttendanceCheckResponse,
    BindAttemptRequest,
    BindAttemptResponse,
    Last3Request,
    ProxyReviewRequest,
    ProxyReviewResponse,
    StartAttemptRequest,
    StartAttemptResponse,
    StatsResponse,
    StudentConfirmResponse,
    StudentFoundResponse,
    StudentMeResponse,
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
from app.utils.http import client_info as _http_client_info

logger = logging.getLogger(__name__)

router = APIRouter(tags=["attendance"])


def _session_info(session: dict) -> SessionInfo:
    raw_lifetime = session.get("qr_lifetime_seconds")
    # 0 is a valid Permanent lifetime; only fall back when the key is absent.
    try:
        lifetime = (settings.QR_TOKEN_LIFETIME_SECONDS
                    if raw_lifetime is None else int(raw_lifetime))
    except (TypeError, ValueError):
        lifetime = settings.QR_TOKEN_LIFETIME_SECONDS
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
    """SERVER-observed request info for the attendance audit trail.

    The IP is the address our own proxy appended, never the left-most
    X-Forwarded-For value the client chose to send (see app/utils/http.py): a
    student used to be able to dictate their own recorded IP by setting that
    header. Locally there is no proxy in front of uvicorn, so the header is not
    trustworthy at all and the socket peer is used instead.

    These fields are audit data only — shared-browser detection is driven by the
    hashed browser id and never reads them.
    """
    return _http_client_info(
        request.headers,
        request.client.host if request.client else "",
        settings.TRUSTED_PROXY_COUNT,
        # Only a real deployment has a proxy whose appended entry we can trust.
        trust_forwarded=settings.is_production,
    )


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
    """CR-only manual refresh: immediately mint a NEW QR and invalidate the
    previous one, regardless of its expiry. This is the control the Permanent
    lifetime relies on (a Permanent QR otherwise never changes on its own)."""
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
    """CR-only: live present/total counts + recent arrivals (jar animation),
    shared-browser warnings and the per-student review list."""
    return StatsResponse(success=True, stats=attendance_service.get_session_stats(session_id))


@router.post(
    "/sessions/{session_id}/attendance/{enrollment_no}/review",
    response_model=ProxyReviewResponse,
)
def review_attendance(
    session_id: str,
    enrollment_no: str,
    payload: ProxyReviewRequest,
    cr: CurrentUser = Depends(require_cr),
) -> ProxyReviewResponse:
    """CR-only: clear a shared-browser suspicion after reviewing it.

    Detection NEVER blocks a mark, so this is the whole remedy: the CR confirms
    those students were legitimately at one device (dead battery, shared family
    phone, lab machine). The decision is recorded on the attendance document with
    who made it and when, the Sheet's Remarks column is re-synced from the current
    state (so the flag visibly disappears), and fresh stats come back in the same
    response. It can only clear — a CR can never manufacture a suspicion.
    """
    stats = attendance_service.review_shared_browser_mark(
        session_id, enrollment_no, cr.uid, payload.note
    )
    return ProxyReviewResponse(
        success=True,
        enrollment_no=student_service.normalize_enrollment(enrollment_no).upper(),
        cleared=True,
        message="Mark cleared for this session.",
        stats=stats,
    )


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


# ---- Student: attendance attempt, enrollment lookup, attendance ------------

@router.post("/attendance/attempt/start", response_model=StartAttemptResponse)
def start_attempt(payload: StartAttemptRequest) -> StartAttemptResponse:
    """Public entry gateway: a student reached /attendance from a live QR.

    Deliberately NOT JWT-protected — Google sign-in has not happened yet, and
    the whole point is to start the student's individual completion window the
    instant the QR link is opened. The QR token is the only accepted credential
    and is validated entirely server-side (exists, session active, still the
    displayed/unexpired token). The response carries the opaque attempt id and
    the server-side window only: no roster, no student/CR/Firebase identity,
    no session coordinates, and nothing here marks attendance.

    `browser_id` is recorded here, at the moment of the scan, as this attempt's
    fixed anchor. Only its session-peppered hash is stored (never the id, never
    blended with the IP or user-agent), and a missing id is simply no anchor.
    """
    record = qr_service.scan_token(payload.token)
    attempt = qr_service.create_attempt(record, browser_id=payload.browser_id)
    return StartAttemptResponse(
        success=True,
        attempt_id=attempt["attempt_id"],
        window_seconds=int(attempt["window_seconds"]),
        expires_at=str(attempt["expires_at"]),
        remaining_seconds=qr_service.attempt_remaining_seconds(attempt),
    )


@router.post("/attendance/attempt/bind", response_model=BindAttemptResponse)
def bind_attempt(
    payload: BindAttemptRequest,
    student: CurrentUser = Depends(require_student),
) -> BindAttemptResponse:
    """Attach the authenticated Firebase UID to the student's attempt.

    The binding is server-side only: the client proves authentication, it can
    never claim an identity. An attempt already bound to a different account is
    rejected (ATTENDANCE_ATTEMPT_BOUND). The saved enrollment is returned with
    it so the returning-student screen needs a single round trip.

    `browser_id` is compared with the scan-time anchor inside the attempt's own
    transaction; a mismatch is recorded for the CR to review and is never a
    rejection reason.
    """
    attempt = qr_service.bind_attempt(payload.attempt_id, student.uid,
                                      browser_id=payload.browser_id)
    saved = student_service.get_saved_enrollment(student.uid)
    if saved is None:
        return BindAttemptResponse(
            success=True,
            verified=False,
            remaining_seconds=qr_service.attempt_remaining_seconds(attempt),
        )
    return BindAttemptResponse(
        success=True,
        verified=True,
        name=saved["name"],
        masked_enrollment=student_service.mask_enrollment(saved["enrollment_no"]),
        remaining_seconds=qr_service.attempt_remaining_seconds(attempt),
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
    background: BackgroundTasks,
    student: CurrentUser = Depends(require_student),
) -> AttendanceCheckResponse:
    """Student attendance submission. Identity comes from the backend-verified
    enrollment saved against the Firebase UID; the completion window is the
    student's own bound attendance attempt (never the QR token). Every other
    check is enforced server-side (see attendance_service.mark_attendance).
    IP and user-agent are captured from the actual HTTP request for the audit
    trail and are NOT used for detection; `browser_id` (validated + hashed
    server-side against this session) is. The cosmetic per-session Sheet tab
    work, including the shared-browser remark sync, runs after the response and
    can never change or block a successful mark."""
    result = attendance_service.mark_attendance(
        student.uid, payload, client=_client_info(request), background=background
    )
    return AttendanceCheckResponse(
        success=True,
        status=result["status"],
        message="Attendance marked successfully.",
        marked_at=result["marked_at"],
    )
