"""Attendance marking pipeline — all validation lives on the server.

Order of checks performed for every /attendance/check call:
1. JWT (FastAPI dependency, before this service runs)
2. Backend-verified enrollment linked to the Firebase UID (users/{uid})
3. Individual attendance attempt: exists, bound to THIS uid, not used,
   not expired (server timestamps) and its session still active
4. Enrollment still matches the authoritative Google Sheet roster
5. Distance between CR location and student location <= radius (Haversine)
6. Duplicate prevention (per student per session, server-side)
Then: write the Firestore attendance document (with separate server-observed
audit fields: client IP, user-agent, session-peppered browser id hash, verified
auth_time) + Google Sheet PRESENT row + per-session worksheet status flip, close
the attempt, then RECOMPUTE the session's shared-browser remarks from every
record and rewrite the Remarks column.

The QR token's own short lifetime is deliberately NOT re-checked here: the QR
governs ENTRY into the flow, the attempt governs COMPLETION.

Also provides the session statistics (water fill / popups / shared-browser
review) and the final absentee summary — both computed strictly from server-side
data. Detection itself lives in app/services/proxy_detection.py so the rules are
pure, testable, and shared by the dashboard, the Sheet and the CR override.
"""
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

from app.core.config import settings
from app.core.firebase import COLLECTION_ATTENDANCE, COLLECTION_SESSIONS, get_db
from app.services import (
    proxy_detection,
    qr_service,
    session_service,
    sheets_service,
    student_service,
)
from app.services.errors import BadRequestError, ConflictError, NotFoundError
from app.utils.location import haversine_distance_meters

logger = logging.getLogger(__name__)


def _attendance_doc_id(session_id: str, enrollment_no: str) -> str:
    """Deterministic id: one attendance record per student per session."""
    normalized = student_service.normalize_enrollment(enrollment_no).upper()
    return f"{session_id}__{normalized}"


def derive_device_hash(client_ip: str, user_agent: str) -> str:
    """LEGACY audit value — still written for history, read by NOTHING.

    This was sha256(ip + user-agent), which is not a device identity at all: a
    class of identical phones on one campus network collapsed into a single
    hash (innocent students flagged in bulk), while anyone who changed either
    string escaped completely. Shared-browser detection now uses the per-browser
    id in proxy_detection and never looks here. The request's IP and user-agent
    are stored alongside it, unhashed and unblended, for the audit trail.
    """
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


def _session_records(session_id: str) -> list[dict]:
    """Every attendance record for one session as plain dicts.

    A single equality filter — no orderBy, so no composite index is required.
    This is the ONLY input the detector reads, which is what lets the Sheet
    remarks, the dashboard warnings and the CR override agree by construction:
    they are one calculation over the same rows rather than three separate
    write-ups that can accumulate and drift.
    """
    docs = (
        get_db()
        .collection(COLLECTION_ATTENDANCE)
        .where("session_id", "==", session_id)
        .stream()
    )
    return [doc.to_dict() or {} for doc in docs]


def _detect(records: list[dict]) -> list[proxy_detection.ProxyFlag]:
    """One detection entry point, so every caller uses the same thresholds."""
    return proxy_detection.detect(
        records,
        short_window_seconds=settings.PROXY_SHORT_WINDOW_SECONDS,
        fresh_signin_seconds=settings.PROXY_FRESH_SIGNIN_SECONDS,
    )


def sync_remarks_best_effort(sheet_title: str, session_id: str) -> None:
    """Rewrite the session tab's Remarks column from CURRENT session state.

    Replaces the old append-only behaviour, where a remark written once stayed
    forever and every later flag re-stamped the whole group. An overridden or
    no-longer-suspicious student is now visibly cleared. Best-effort: a Sheet
    failure is logged and never touches the student's or CR's request.
    """
    if not sheet_title:
        return
    try:
        sheets_service.sync_proxy_remarks(
            sheet_title,
            proxy_detection.remarks_by_enrollment(_detect(_session_records(session_id))),
        )
    except Exception:
        logger.exception("Could not sync proxy remarks for tab %s", sheet_title)


def mark_attendance(student_uid: str, payload, client: dict | None = None,
                   background=None) -> dict:
    """Validate everything and mark the student PRESENT.

    `payload` is an AttendanceCheckRequest (attempt_id + coordinates + optional
    browser_id).
    `client` carries the SERVER-observed request info {ip, user_agent} — the
    request body can never supply or override these fields. The
    student's identity is taken from the backend-verified enrollment saved
    against their Firebase UID — never from the request body.
    `background` (optional FastAPI BackgroundTasks) receives the cosmetic
    Sheet work so the response is not delayed by it.

    Detection can never block or fail a mark: it only annotates the record and
    the Sheet for the CR to review.
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

    # Audit fields come from the server-observed request only, and are stored as
    # SEPARATE values. Nothing below compares on them.
    client = client or {}
    client_ip = str(client.get("ip") or "")
    user_agent = str(client.get("user_agent") or "")[:512]

    # The browser identity, hashed server-side against THIS session with a
    # server secret. IP and user-agent are deliberately NOT inputs, so a
    # student who walks off campus Wi-Fi mid-class is still the same browser,
    # and a class of identical phones on one router is no longer one identity.
    browser_id = proxy_detection.normalize_browser_id(getattr(payload, "browser_id", ""))
    browser_id_hash = proxy_detection.hash_browser_id(
        browser_id, session_id, settings.browser_id_secret
    )
    # The attempt's scan-time anchor vs the browser that is submitting now.
    attempt_anchor = str(attempt.get("browser_id_hash") or "")
    browser_id_changed = bool(
        proxy_detection.browser_id_changed_on_attempt(attempt_anchor, browser_id_hash)
        or attempt.get("browser_id_changed")
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
        # ---- detection inputs: one field each, never fused together ----
        # Submitting browser ("" = unknown, which is never evidence).
        "browser_id_hash": browser_id_hash,
        # Browser that scanned the QR, for the mid-attempt comparison.
        "attempt_browser_id_hash": attempt_anchor,
        "browser_id_changed": browser_id_changed,
        # Written by the backend from the verified Firebase ID token; a value in
        # a request body can never reach this field.
        "auth_time": int(saved.get("auth_time") or 0),
        # ---- audit trail only: read by no rule ----
        "client_ip": client_ip,
        "user_agent": user_agent,
        "device_hash": (
            derive_device_hash(client_ip, user_agent) if client_ip else ""
        ),
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

    # Shared-browser detection: recompute the session's remarks from EVERY
    # record and rewrite the column, so what the CR sees is the current state
    # rather than a log of everything ever suspected. It can never block or fail
    # the mark — the student is already PRESENT at this point.
    if sheet_title:
        if background is not None:
            background.add_task(sync_remarks_best_effort, sheet_title, session_id)
        else:
            sync_remarks_best_effort(sheet_title, session_id)

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


# ---- Live statistics (water fill + present popups + shared-browser review) --
# Detection policy itself is in app/services/proxy_detection.py. Both the
# warnings and the per-student review list below come from ONE detect() call over
# the session's records, so the dashboard can never disagree with the Sheet.

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
    # One detection pass over the rows already in hand: `warnings` and
    # `proxy_review` are two views of this single answer (and so is the Sheet,
    # which is synced from the same function). A cleared or no-longer-suspicious
    # record simply stops appearing — nothing here accumulates.
    flags = _detect(entries)
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
        "warnings": proxy_detection.warnings(flags),
        "proxy_review": proxy_detection.review_entries(flags),
    }


def review_shared_browser_mark(session_id: str, enrollment_no: str,
                               cr_uid: str, note: str = "") -> dict:
    """CR clears one shared-browser suspicion after reviewing it.

    Detection never blocks a mark, so this is the entire remedy: the CR confirms
    the two students were legitimately at one device (dead battery, shared
    family phone, lab machine). The decision is written on the attendance
    document with who made it and when, and that record then drops out of the
    evidence — so clearing one half of a pair leaves one student on that browser,
    which is not a shared browser, and the remark is removed on the re-sync.

    Returns fresh stats so the dashboard updates without waiting for a poll.
    """
    target = student_service.normalize_enrollment(enrollment_no).upper()
    ref = get_db().collection(COLLECTION_ATTENDANCE).document(
        _attendance_doc_id(session_id, target)
    )
    if not ref.get().exists:
        raise NotFoundError(
            "No attendance record found for that student in this session.",
            code="ATTENDANCE_NOT_FOUND",
        )
    ref.set({
        "review": {
            "status": proxy_detection.REVIEW_CLEARED,
            "by_uid": cr_uid,
            "at": datetime.now(timezone.utc).isoformat(),
            "note": str(note or "")[:200],
        }
    }, merge=True)
    logger.info("Shared-browser flag cleared by CR session_id=%s", session_id)

    session = get_db().collection(COLLECTION_SESSIONS).document(session_id).get()
    sheet_title = (session.to_dict() or {}).get("sheet_title") if session.exists else None
    # Synchronous here: the CR is looking at this tab and expects the remark to
    # go away when they press the button.
    if sheet_title:
        sync_remarks_best_effort(sheet_title, session_id)
    return get_session_stats(session_id)


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
