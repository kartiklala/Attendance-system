"""Attendance marking pipeline — all validation lives on the server.

Order of checks performed for every /attendance/check call:
1. JWT (FastAPI dependency, before this service runs)
2. Backend-verified enrollment linked to the Firebase UID (users/{uid})
3. Individual attendance attempt: exists, bound to THIS uid, not used,
   not expired (server timestamps) and its session still active
4. Enrollment still matches the authoritative Google Sheet roster
5. Distance between CR location and student location <= radius (Haversine)
6. Duplicate prevention (per student per session, server-side)
Then: write Firestore attendance document (with server-side audit fields:
client IP, user-agent, derived device hash) + Google Sheet PRESENT row +
per-session worksheet status flip, close the attempt, and flag suspicious
same-IP/device use.

The QR token's own short lifetime is deliberately NOT re-checked here: the QR
governs ENTRY into the flow, the attempt governs COMPLETION.

Also provides the session statistics (water fill / popups / proxy warnings)
and the final absentee summary — both computed strictly from server-side data.
"""
import hashlib
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

from app.core.config import settings
from app.core.firebase import COLLECTION_ATTENDANCE, COLLECTION_SESSIONS, get_db
from app.services import qr_service, session_service, sheets_service, student_service
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


def _gather(*calls):
    """Run independent blocking reads concurrently.

    Firestore document reads and the (process-cached) roster lookup are
    independent network round trips; issuing them in parallel removes the
    serial latency a student would otherwise wait through on every submit.
    Exceptions propagate from .result() so validation order is unchanged.
    """
    if len(calls) == 1:
        return [calls[0]()]
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(fn) for fn in calls]
        return [f.result() for f in futures]


def _present_flip_best_effort(sheet_title: str, enrollment_no: str) -> None:
    """Cosmetic per-session tab update: never blocks or fails the student."""
    try:
        sheets_service.mark_student_present(sheet_title, enrollment_no)
    except Exception:
        logger.exception("Could not update the session sheet tab %s", sheet_title)


def _same_device_arrivals(session_id: str, device_hash: str) -> list[tuple[str, str]]:
    """(enrollment_no, marked_at) for every attendance recorded this session on
    the SAME device_hash, ordered OLDEST-FIRST (the order they actually marked).

    device_hash is sha256(client_ip + user_agent), so a match means the same
    browser on the same IP — NOT merely the same Wi-Fi router. Equality-only
    filters need no composite index. Includes the just-written record."""
    if not device_hash:
        return []
    docs = (
        get_db()
        .collection(COLLECTION_ATTENDANCE)
        .where("session_id", "==", session_id)
        .where("device_hash", "==", device_hash)
        .stream()
    )
    arrivals: list[tuple[str, str]] = []
    for doc in docs:
        data = doc.to_dict() or {}
        enrollment = data.get("enrollment_no", "")
        if enrollment:
            arrivals.append((enrollment, str(data.get("marked_at", ""))))
    # Oldest-first so the sheet can show who marked first vs later.
    arrivals.sort(key=lambda item: item[1])
    return arrivals


def _proxy_flag_best_effort(sheet_title: str, device_hash: str,
                            arrivals: list[tuple[str, str]]) -> None:
    """Cosmetic proxy remark on the session tab: never blocks or fails the
    student. Best-effort like the PRESENT flip."""
    try:
        sheets_service.flag_proxy_remarks(sheet_title, device_hash, arrivals)
    except Exception:
        logger.exception("Could not write proxy remarks on the session sheet tab %s",
                         sheet_title)


def _proxy_check_and_flag_best_effort(sheet_title: str, session_id: str,
                                      device_hash: str) -> None:
    """After a successful mark, flag EVERY student sharing this device_hash
    (same browser + same IP) once two or more have used it. Each remark records
    the device group and the student's order-of-arrival, so two different
    devices that each proxied (e.g. dev1's pair vs dev2's pair) stay separately
    identifiable and the first marker is distinguishable from the second. Runs
    as a background task so the extra read never delays the student's response."""
    if not sheet_title or not device_hash:
        return
    arrivals = _same_device_arrivals(session_id, device_hash)
    if len(arrivals) > 1:
        _proxy_flag_best_effort(sheet_title, device_hash, arrivals)


def mark_attendance(student_uid: str, payload, client: dict | None = None,
                   background=None) -> dict:
    """Validate everything and mark the student PRESENT.

    `payload` is an AttendanceCheckRequest (attempt_id + coordinates).
    `client` carries the SERVER-observed request info {ip, user_agent} —
    the request body can never supply or override these fields. The
    student's identity is taken from the backend-verified enrollment saved
    against their Firebase UID — never from the request body.
    `background` (optional FastAPI BackgroundTasks) receives the cosmetic
    Sheet tab flip so the response is not delayed by it.
    """
    started = time.monotonic()
    logger.info("Attendance validation started")

    # Checks 2 + 3 (entry reads) in parallel: the verified enrollment and the
    # student's own attendance attempt are independent documents.
    saved, attempt = _gather(
        lambda: student_service.get_saved_enrollment(student_uid),
        lambda: qr_service.get_attempt(payload.attempt_id),
    )
    if saved is None:
        raise BadRequestError(
            "Please verify your enrollment details first.",
            code="ENROLLMENT_NOT_VERIFIED",
        )

    # The attempt (NOT the QR token) authorizes this submission: bound to this
    # UID, still active, not expired, and its session still live.
    session = session_service.get_session(attempt["session_id"])
    qr_service.validate_attempt_for_check(attempt, session, student_uid)
    session_id = session["session_id"]
    db = get_db()
    # Deterministic id from the SAVED enrollment: normalize_enrollment is
    # idempotent, so this equals the id derived from the roster value below.
    doc_id = _attendance_doc_id(session_id, saved["enrollment_no"])
    ref = db.collection(COLLECTION_ATTENDANCE).document(doc_id)

    # Checks 4 + 6 (roster match, duplicate records) in parallel.
    roster_pair, doc_exists, uid_exists = _gather(
        lambda: _match_roster_enrollment(saved["enrollment_no"]),
        lambda: ref.get().exists,
        lambda: _uid_already_marked(session_id, student_uid),
    )
    roster_enrollment, roster_name = roster_pair

    # Check 5 — location, calculated only from server-side session data.
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

    if doc_exists or uid_exists:
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
        "attempt_id": attempt.get("attempt_id", ""),
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
        # Google avatar (public profile image) for the CR present-popup.
        "photo_url": str(saved.get("photo_url") or ""),
    })

    # The attempt is spent: it can never be replayed for a second record.
    qr_service.close_attempt(attempt["_doc_id"], "used")

    # Google Sheet row. If the sheet write fails, roll back the Firestore
    # record so the student can retry cleanly.
    try:
        sheets_service.append_attendance_row(roster_enrollment, roster_name, session_id)
    except Exception:
        logger.exception("Sheet write failed; removing Firestore attendance record")
        ref.delete()
        raise

    # Per-session worksheet: flip this student to PRESENT. This is a cosmetic
    # convenience view (Firestore is the source of truth and the absentee
    # summary is computed from it), so it is deferred to a background task
    # when available — the student's response is never blocked by a Sheet
    # round trip. Failures are logged only, exactly as before.
    sheet_title = session.get("sheet_title")
    if sheet_title:
        if background is not None:
            background.add_task(_present_flip_best_effort, sheet_title,
                                roster_enrollment)
        else:
            _present_flip_best_effort(sheet_title, roster_enrollment)

    # Proxy remarks: flag every student sharing this device_hash (same browser
    # + same IP) once two or more have marked. A shared IP alone (one Wi-Fi
    # router, different browsers) never triggers it — device_hash includes the
    # user-agent too.
    if sheet_title and device_hash:
        if background is not None:
            background.add_task(_proxy_check_and_flag_best_effort, sheet_title,
                                session_id, device_hash)
        else:
            _proxy_check_and_flag_best_effort(sheet_title, session_id, device_hash)

    logger.info("Attendance successful session_id=%s distance=%.1fm pipeline_ms=%s",
                session_id, distance, int((time.monotonic() - started) * 1000))
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
        {
            "name": d.get("name", ""),
            "marked_at": str(d.get("marked_at", "")),
            # Avatar only — enrollment, IP, device and coordinates stay here.
            "photo_url": str(d.get("photo_url") or ""),
        }
        for d in entries[:10]
    ]
    # Full present list for the CR's live 'Students Present' panel. Built from
    # the SAME in-memory `entries` (already filtered to this session and sorted
    # most-recent-first) — no additional query. Enrollment is included here (it
    # is intentionally omitted from the popup `recent`), so the CR can verify
    # exactly who marked; no IP/device/location data is exposed.
    attendees = [
        {
            "name": d.get("name", ""),
            "enrollment_no": str(d.get("enrollment_no", "")),
            "marked_at": str(d.get("marked_at", "")),
        }
        for d in entries
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
        "attendees": attendees,
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
