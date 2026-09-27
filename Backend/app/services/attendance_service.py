"""Attendance marking pipeline — all validation lives on the server.

Order of checks performed for every /attendance/check call:
1. JWT (FastAPI dependency, before this service runs)
2. Backend-verified enrollment linked to the Firebase UID (users/{uid})
3. QR token exists / valid / not expired (server timestamps)
4. Session exists and is active
5. One-minute completion window (server-authoritative)
6. Enrollment still matches the authoritative Google Sheet roster
7. Distance between CR location and student location <= radius (Haversine)
8. Duplicate prevention (per student per session, server-side)
Then: write Firestore attendance document + Google Sheet PRESENT row.

Also provides the session statistics (jar fill / popups) and the final
absentee summary — both computed strictly from server-side data.
"""
import logging
from datetime import datetime, timezone

from app.core.config import settings
from app.core.firebase import COLLECTION_ATTENDANCE, COLLECTION_SESSIONS, get_db
from app.services import qr_service, sheets_service, student_service
from app.services.errors import BadRequestError, ConflictError
from app.utils.location import haversine_distance_meters

logger = logging.getLogger(__name__)


def _attendance_doc_id(session_id: str, enrollment_no: str) -> str:
    """Deterministic id: one attendance record per student per session."""
    normalized = student_service.normalize_enrollment(enrollment_no).upper()
    return f"{session_id}__{normalized}"


def mark_attendance(student_uid: str, payload) -> dict:
    """Validate everything and mark the student PRESENT.

    `payload` is an AttendanceCheckRequest (session_token + coordinates).
    The student's identity is taken from the backend-verified enrollment
    saved against their Firebase UID — never from the request body.
    """
    logger.info("Attendance validation attempted session_token=present")

    # Check 2 — the student must have a backend-verified enrollment saved.
    saved = student_service.get_saved_enrollment(student_uid)
    if saved is None:
        raise BadRequestError(
            "Please verify your enrollment details first.",
            code="ENROLLMENT_NOT_VERIFIED",
        )

    # Checks 3 + 4 + 5 — QR token, session status, one-minute window.
    token_record, session = qr_service.validate_token_for_check(payload.session_token)
    session_id = session["session_id"]

    # Check 6 — enrollment must still exist in the authoritative roster.
    roster_enrollment, roster_name = _match_roster_enrollment(saved["enrollment_no"])

    # Check 7 — location, calculated only from server-side session data.
    distance = haversine_distance_meters(
        float(session["latitude"]), float(session["longitude"]),
        payload.latitude, payload.longitude,
    )
    radius = float(session.get("radius_meters") or settings.ATTENDANCE_RADIUS_METERS)
    if distance > radius:
        logger.info("Attendance rejected (outside radius) distance=%.1fm radius=%.1fm",
                    distance, radius)
        raise BadRequestError("You are outside the attendance location.",
                              code="OUTSIDE_LOCATION")

    # Check 8 — duplicate prevention (deterministic id + UID scan).
    db = get_db()
    doc_id = _attendance_doc_id(session_id, roster_enrollment)
    ref = db.collection(COLLECTION_ATTENDANCE).document(doc_id)
    if ref.get().exists or _uid_already_marked(session_id, student_uid):
        logger.info("Attendance rejected (duplicate) session_id=%s", session_id)
        raise ConflictError("Attendance already marked for this session.",
                            code="ALREADY_MARKED")

    marked_at_iso = datetime.now(timezone.utc).isoformat()

    # Firestore record first (acts as the idempotency lock).
    ref.set({
        "session_id": session_id,
        "student_uid": student_uid,
        "name": roster_name,
        "enrollment_no": roster_enrollment,
        "status": "PRESENT",
        "marked_at": marked_at_iso,
        "distance_meters": round(distance, 2),
    })

    # Google Sheet row. If the sheet write fails, roll back the Firestore
    # record so the student can retry cleanly.
    try:
        sheets_service.append_attendance_row(roster_enrollment, roster_name, session_id)
    except Exception:
        logger.exception("Sheet write failed; removing Firestore attendance record")
        ref.delete()
        raise

    logger.info("Attendance successful session_id=%s distance=%.1fm",
                session_id, distance)
    return {
        "status": "PRESENT",
        "marked_at": marked_at_iso,
        "session_id": session_id,
    }


def _match_roster_enrollment(enrollment_no: str) -> tuple[str, str]:
    """Return the authoritative (enrollment, name) row for a saved enrollment."""
    target = student_service.normalize_enrollment(enrollment_no)
    for sheet_enrollment, sheet_name in student_service._get_roster_safe():
        if student_service.normalize_enrollment(sheet_enrollment) == target:
            return target, sheet_name.strip()
    raise BadRequestError(
        "Your saved enrollment no longer matches our records. Please contact your CR.",
        code="STUDENT_NOT_FOUND",
    )


def _uid_already_marked(session_id: str, student_uid: str) -> bool:
    docs = (
        get_db()
        .collection(COLLECTION_ATTENDANCE)
        .where("session_id", "==", session_id)
        .where("student_uid", "==", student_uid)
        .limit(1)
        .stream()
    )
    return any(True for _ in docs)


# ---- Live statistics (jar fill + present popups) ---------------------------

def get_session_stats(session_id: str) -> dict:
    """Present/total counts + most recent arrivals, all server-side data."""
    snapshot = get_db().collection(COLLECTION_SESSIONS).document(session_id).get()
    session = snapshot.to_dict() or {} if snapshot.exists else {}
    total = student_service.roster_size()

    attendance = get_db().collection(COLLECTION_ATTENDANCE)
    # Single equality query — no Firestore orderBy: an equality + order_by
    # query requires a composite index. A class-size result set is trivial
    # to sort in memory, which keeps deployment index-free.
    present_docs = list(attendance.where("session_id", "==", session_id).stream())
    present_count = len(present_docs)

    entries = [doc.to_dict() or {} for doc in present_docs]
    entries.sort(key=lambda d: str(d.get("marked_at", "")), reverse=True)
    recent = [
        {"name": d.get("name", ""), "marked_at": str(d.get("marked_at", ""))}
        for d in entries[:10]
    ]

    percentage = round((present_count / total) * 100, 2) if total else 0.0
    return {
        "session_id": session_id,
        "session_status": session.get("status", "unknown"),
        "total_students": total,
        "present_count": present_count,
        "percentage": percentage,
        "recent": recent,
    }


# ---- Final summary (absentees + present count) -----------------------------

def get_session_summary(session_id: str) -> dict:
    """Absentee list computed by comparing the Sheet1 roster against the
    PRESENT records for this session (server-side only)."""
    session = get_db().collection(COLLECTION_SESSIONS).document(session_id).get()
    session_data = session.to_dict() or {} if session.exists else {}

    roster = student_service._get_roster_safe()
    present_docs = (
        get_db()
        .collection(COLLECTION_ATTENDANCE)
        .where("session_id", "==", session_id)
        .stream()
    )
    present_enrollments = {
        student_service.normalize_enrollment((doc.to_dict() or {}).get("enrollment_no", ""))
        for doc in present_docs
    }

    absentees = [
        {"name": name.strip(), "enrollment_no": student_service.normalize_enrollment(enrollment)}
        for enrollment, name in roster
        if student_service.normalize_enrollment(enrollment) not in present_enrollments
    ]

    total = len(roster)
    present_count = len(present_enrollments & {
        student_service.normalize_enrollment(e) for e, _ in roster
    })
    absent_count = total - present_count
    percentage = round((present_count / total) * 100, 2) if total else 0.0

    return {
        "session_id": session_id,
        "status": session_data.get("status", "unknown"),
        "total_students": total,
        "present_count": present_count,
        "absent_count": absent_count,
        "percentage": percentage,
        "absentees": sorted(absentees, key=lambda a: a["name"].lower()),
    }
