"""Attendance marking pipeline — all validation lives on the server.

Order of checks performed for every /attendance/check call:
1. JWT (FastAPI dependency, before this service runs)
2. QR token exists / valid / not expired (server timestamps)
3. Session exists and is active
4. One-minute completion window (server-authoritative)
5. Student name + enrollment match the Google Sheet roster (case-insensitive)
6. Distance between CR location and student location <= radius (Haversine)
7. Duplicate prevention (deterministic Firestore document id)
Then: write Firestore attendance document + Google Sheet PRESENT row.
"""
import logging
from datetime import datetime, timezone

from app.core.config import settings
from app.core.firebase import COLLECTION_ATTENDANCE, get_db
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

    `payload` is an AttendanceCheckRequest (already Pydantic-validated).
    """
    logger.info(
        "Attendance validation attempted session_token=present enrollment_no=%s",
        payload.enrollment_no,
    )

    # Checks 2 + 3 + 4 — QR token, session status, one-minute window.
    token_record, session = qr_service.validate_token_for_check(payload.session_token)
    session_id = session["session_id"]

    # Check 5 — student identity against the authoritative Google Sheet.
    canonical_name, canonical_enrollment = student_service.verify_student_details(
        payload.name, payload.enrollment_no
    )

    # Check 6 — location, calculated only from server-side session data.
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

    # Check 7 — duplicate prevention via deterministic document id.
    db = get_db()
    doc_id = _attendance_doc_id(session_id, canonical_enrollment)
    ref = db.collection(COLLECTION_ATTENDANCE).document(doc_id)
    if ref.get().exists:
        logger.info("Attendance rejected (duplicate) session_id=%s enrollment_no=%s",
                    session_id, canonical_enrollment)
        raise ConflictError("Attendance already marked for this session.",
                            code="ALREADY_MARKED")

    marked_at_iso = datetime.now(timezone.utc).isoformat()

    # Firestore record first (acts as the idempotency lock).
    ref.set({
        "session_id": session_id,
        "student_uid": student_uid,
        "name": canonical_name,
        "enrollment_no": canonical_enrollment,
        "status": "PRESENT",
        "marked_at": marked_at_iso,
        "distance_meters": round(distance, 2),
    })

    # Google Sheet row. If the sheet write fails, roll back the Firestore
    # record so the student can retry cleanly.
    try:
        sheets_service.append_attendance_row(canonical_enrollment, canonical_name, session_id)
    except Exception:
        logger.exception("Sheet write failed; removing Firestore attendance record")
        ref.delete()
        raise

    logger.info("Attendance successful session_id=%s enrollment_no=%s distance=%.1fm",
                session_id, canonical_enrollment, distance)
    return {
        "status": "PRESENT",
        "marked_at": marked_at_iso,
        "session_id": session_id,
    }
