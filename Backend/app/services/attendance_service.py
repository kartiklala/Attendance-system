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
Then: write Firestore attendance document (with server-side audit fields:
client IP, user-agent, derived device hash) + Google Sheet PRESENT row +
per-session worksheet status flip, and flag suspicious same-IP/device use.

Also provides the session statistics (water fill / popups / proxy warnings)
and the final absentee summary — both computed strictly from server-side data.
"""
import hashlib
import logging
from collections import defaultdict
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


def derive_device_hash(client_ip: str, user_agent: str) -> str:
    """Privacy-conscious server-generated device identifier: a one-way hash
    of the request's IP + user-agent (never sent to or computed by the
    browser, never reversed into the raw values by the UI)."""
    seed = f"{client_ip}|{user_agent}".encode("utf-8", errors="replace")
    return hashlib.sha256(seed).hexdigest()[:16]


def mark_attendance(student_uid: str, payload, client: dict | None = None) -> dict:
    """Validate everything and mark the student PRESENT.

    `payload` is an AttendanceCheckRequest (session_token + coordinates).
    `client` carries the SERVER-observed request info {ip, user_agent} —
    the request body can never supply or override these fields. The
    student's identity is taken from the backend-verified enrollment saved
    against their Firebase UID — never from the request body.
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

    # Audit fields come from the server-observed request only.
    client = client or {}
    client_ip = str(client.get("ip") or "")
    user_agent = str(client.get("user_agent") or "")[:512]
    device_hash = (
        derive_device_hash(client_ip, user_agent) if client_ip else ""
    )

    # Firestore record first (acts as the idempotency lock).
    ref.set({
        "session_id": session_id,
        "session_name": session.get("session_name", ""),
        "student_uid": student_uid,
        "name": roster_name,
        "enrollment_no": roster_enrollment,
        "status": "PRESENT",
        "marked_at": marked_at_iso,
        "distance_meters": round(distance, 2),
        "student_latitude": payload.latitude,
        "student_longitude": payload.longitude,
        "client_ip": client_ip,
        "user_agent": user_agent,
        "device_hash": device_hash,
    })

    # Google Sheet row. If the sheet write fails, roll back the Firestore
    # record so the student can retry cleanly.
    try:
        sheets_service.append_attendance_row(roster_enrollment, roster_name, session_id)
    except Exception:
        logger.exception("Sheet write failed; removing Firestore attendance record")
        ref.delete()
        raise

    # Per-session worksheet: flip this student to PRESENT (best-effort —
    # Firestore already recorded the attendance, so failures never reject it).
    sheet_title = session.get("sheet_title")
    if sheet_title:
        try:
            sheets_service.mark_student_present(sheet_title, roster_enrollment)
        except Exception:
            logger.exception("Could not update the session sheet tab %s",
                             sheet_title)

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


# ---- Live statistics (water fill + present popups + proxy warnings) --------

def _proxy_warnings(entries: list[dict]) -> list[str]:
    """Flag (never reject) suspicious patterns: several DIFFERENT students
    marking from the same device hash or the same client IP in one session.
    Only the count is surfaced to the CR — raw IP/device data stays here."""
    by_device = defaultdict(set)
    by_ip = defaultdict(set)
    for e in entries:
        uid = e.get("student_uid")
        if not uid:
            continue
        if e.get("device_hash"):
            by_device[e["device_hash"]].add(uid)
        if e.get("client_ip"):
            by_ip[e["client_ip"]].add(uid)
    warnings: list[str] = []
    device_shares = sum(1 for uids in by_device.values() if len(uids) > 1)
    ip_shares = sum(1 for uids in by_ip.values() if len(uids) > 1)
    if device_shares:
        warnings.append("\u26a0 Multiple students marked from the same device")
    if ip_shares:
        warnings.append("\u26a0 Multiple students detected from the same IP")
    return warnings


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
        "session_name": session.get("session_name", ""),
        "session_status": session.get("status", "unknown"),
        "total_students": total,
        "present_count": present_count,
        "percentage": percentage,
        "recent": recent,
        "warnings": _proxy_warnings(entries),
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

    # Absentees sorted ASCENDING by enrollment number (string compare keeps
    # leading zeros intact; never sorted by name).
    absentees.sort(key=lambda a: a["enrollment_no"])

    return {
        "session_id": session_id,
        "session_name": session_data.get("session_name", ""),
        "status": session_data.get("status", "unknown"),
        "total_students": total,
        "present_count": present_count,
        "absent_count": absent_count,
        "percentage": percentage,
        "absentees": absentees,
    }
