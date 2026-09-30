"""Shared-browser (proxy) detection — pure, deterministic, side-effect free.

Every rule lives here and operates on plain dicts, so the whole policy is
unit-testable without Firestore, the Sheets API or Firebase Admin, and the CR
dashboard, the Google Sheet remarks column and the CR override all read exactly
the same conclusion instead of each re-deriving (and each drifting from) it.

THE MODEL
    A browser mints one random id (crypto.randomUUID) and persists it. The
    server stores only sha256(id | session_id | pepper): truncated, peppered and
    scoped to one session, so a leaked row identifies nothing anywhere else,
    and the same phone looks different in a different class.

THE RULES (and deliberately NOT the old ones)
    same browser + same student          -> normal
    same browser + 2+ distinct students  -> suspicious
    ...+ marks seconds apart             -> strong suspicion
    ...+ accounts signed in just now     -> strong suspicion
    browser id swapped mid-attempt       -> suspicious
    same IP only                         -> NEVER a signal
    any record without a browser id      -> NEVER a signal

    client_ip, user_agent and the legacy device_hash are written to the record
    for the audit trail and are never read by this module. That is the whole
    point: an identical fleet of phones on one campus Wi-Fi used to collapse
    into a single IP+UA hash, so innocent students were flagged in bulk, while
    anyone who changed either string escaped detection completely.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# A UUID is the only accepted shape for a browser id: a fixed 36-character
# random value that cannot be used to smuggle arbitrary text into storage.
BROWSER_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAX_BROWSER_ID_LENGTH = 64
HASH_LENGTH = 16

LEVEL_MEDIUM = "medium"
LEVEL_HIGH = "high"

CODE_SHARED_BROWSER = "possible_shared_browser"
CODE_BROWSER_ID_CHANGED = "browser_id_changed"

REMARK_SHARED = "Possible shared browser"
REMARK_ID_CHANGED = "Possible shared browser (id changed mid-attempt)"

REASON_RAPID = "marks within seconds of each other"

# A CR decision on a suspicion. Only "cleared" exists: review removes evidence,
# it can never manufacture it.
REVIEW_CLEARED = "cleared"

DEFAULT_SHORT_WINDOW_SECONDS = 20
DEFAULT_FRESH_SIGNIN_SECONDS = 900


def normalize_browser_id(raw: object) -> str:
    """Validate a client-supplied browser id, or return '' (never raise).

    Anything that is not a UUID — empty, oversized, a list, an object, a
    hand-forged string — becomes '', and '' is definitionally unflaggable. This
    runs in the request model, so no caller has to remember to sanitize.
    """
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or len(value) > MAX_BROWSER_ID_LENGTH:
        return ""
    return value.lower() if BROWSER_ID_RE.match(value) else ""


def hash_browser_id(browser_id: str, session_id: str, secret: str) -> str:
    """One-way, session-scoped browser identity ('' when anything is missing).

    Note what is NOT in the seed: no IP, no user agent. Mixing them in makes the
    identity shift when a student walks out of Wi-Fi range — splitting one real
    browser into several — and re-opens the identical-fleet false positive this
    module exists to fix.
    """
    if not browser_id or not session_id or not secret:
        return ""
    seed = f"{browser_id}|{session_id}|{secret}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:HASH_LENGTH]


def browser_id_changed_on_attempt(attempt_anchor: str, current_hash: str) -> bool:
    """True only when BOTH ends of the attempt are known and genuinely differ.

    A missing id on either side is unknown, not evidence: iOS can purge storage,
    and a student who scanned in a browser that refused storage simply has no
    anchor. Neither is ever treated as a violation.
    """
    return bool(attempt_anchor and current_hash and attempt_anchor != current_hash)


def _epoch(value: object) -> float | None:
    """Unix seconds from an ISO-8601 string, a number, or None if unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _clock(value: object) -> str:
    """HH:MM:SS for the remark, or '' when the timestamp is unreadable."""
    epoch = _epoch(value)
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M:%S")


@dataclass(frozen=True)
class ProxyFlag:
    """One student's CURRENT suspicion, recomputed from live session state."""
    code: str
    level: str
    enrollment_no: str
    student_uid: str
    name: str
    marked_at: str
    reasons: list[str] = field(default_factory=list)
    group_label: str = ""
    position: int = 0
    total: int = 0
    remark: str = ""

    def as_dict(self) -> dict:
        """CR-facing shape: who, how strong, why. Never a hash, IP or fix."""
        return {
            "code": self.code,
            "level": self.level,
            "enrollment_no": self.enrollment_no,
            "name": self.name,
            "marked_at": self.marked_at,
            "reasons": list(self.reasons),
            "group_label": self.group_label,
            "position": self.position,
            "total": self.total,
        }


def group_label(browser_hash: str) -> str:
    """Short handle so a CR can tell two browser groups apart in the sheet.

    Truncation is cosmetic ONLY: grouping always compares full hashes, so two
    groups that happen to share these 4 characters can never be merged by the
    detector.
    """
    return f"Browser #{(browser_hash or '')[:4].upper()}"


def format_remark(flag: ProxyFlag) -> str:
    """Sheet remark: what, which group, order within it, how fast, when."""
    if flag.code == CODE_BROWSER_ID_CHANGED:
        return REMARK_ID_CHANGED
    parts = [REMARK_SHARED, flag.group_label]
    if flag.total > 1:
        parts.append(f"#{flag.position}/{flag.total}")
    if REASON_RAPID in flag.reasons:
        parts.append("rapid")
    clock = _clock(flag.marked_at)
    if clock:
        parts.append(f"@ {clock}")
    return " · ".join(parts)


def _is_cleared(record: dict) -> bool:
    """A CR override removes that record from the evidence entirely.

    Clearing one member of a two-member group leaves a single student on that
    browser, which is not a shared browser — so the flag disappears on the next
    recalculation instead of sitting in the sheet forever.
    """
    review = record.get("review")
    return isinstance(review, dict) and review.get("status") == REVIEW_CLEARED


def _id_change_flag(record: dict) -> ProxyFlag | None:
    """The attempt was opened in one browser profile and submitted in another."""
    if not record.get("browser_id_changed"):
        return None
    browser_hash = str(record.get("browser_id_hash") or "")
    flag = ProxyFlag(
        code=CODE_BROWSER_ID_CHANGED,
        level=LEVEL_HIGH,
        enrollment_no=str(record.get("enrollment_no") or ""),
        student_uid=str(record.get("student_uid") or ""),
        name=str(record.get("name") or ""),
        marked_at=str(record.get("marked_at") or ""),
        reasons=["the attempt was scanned in a different browser profile "
                 "than the one that submitted attendance"],
        group_label=group_label(browser_hash),
    )
    return flag


def _group_flag(
    record: dict,
    records: list[dict],
    short_window_seconds: int,
    fresh_signin_seconds: int,
) -> ProxyFlag | None:
    browser_hash = str(record.get("browser_id_hash") or "")
    # No identity -> no evidence -> never flag (this is what protects every
    # attendance record written before the browser id existed).
    if not browser_hash:
        return None

    group = [
        other for other in records
        if str(other.get("browser_id_hash") or "") == browser_hash
        and not _is_cleared(other)
    ]
    uids = {str(other.get("student_uid") or "") for other in group}
    uids.discard("")
    # Same browser, one student: completely normal. This is the entire class.
    if len(uids) < 2:
        return None

    ordered = sorted(group, key=lambda item: _epoch(item.get("marked_at")) or 0.0)
    stamps = [value for value in (_epoch(item.get("marked_at")) for item in ordered)
              if value is not None]
    rapid = len(stamps) > 1 and any(
        stamps[index + 1] - stamps[index] <= short_window_seconds
        for index in range(len(stamps) - 1)
    )

    # Supporting signal only. auth_time is the time Google created the session,
    # written by the backend from the verified ID token; a value in a request
    # body can never reach the field, so this cannot be gamed from the client.
    fresh = 0
    for member in ordered:
        auth_time = _epoch(member.get("auth_time"))
        marked = _epoch(member.get("marked_at"))
        # 0 / absent means "never recorded", which is unknown, NOT "signed in at
        # the epoch" — an old record must not look permanently brand-new.
        if not auth_time or marked is None:
            continue
        if (marked - auth_time) <= fresh_signin_seconds:
            fresh += 1

    reasons = [f"{len(uids)} distinct students marked from this browser"]
    if rapid:
        reasons.append(REASON_RAPID)
    if fresh >= 2:
        reasons.append(f"{fresh} of them signed in to Google just now")

    enrollment = str(record.get("enrollment_no") or "")
    position = next(
        (index for index, item in enumerate(ordered, start=1)
         if str(item.get("enrollment_no") or "") == enrollment),
        0,
    )
    flag = ProxyFlag(
        code=CODE_SHARED_BROWSER,
        level=LEVEL_HIGH if (rapid or fresh >= 2) else LEVEL_MEDIUM,
        enrollment_no=enrollment,
        student_uid=str(record.get("student_uid") or ""),
        name=str(record.get("name") or ""),
        marked_at=str(record.get("marked_at") or ""),
        reasons=reasons,
        group_label=group_label(browser_hash),
        position=position,
        total=len(ordered),
    )
    return flag


def detect(
    records: list[dict],
    *,
    short_window_seconds: int = DEFAULT_SHORT_WINDOW_SECONDS,
    fresh_signin_seconds: int = DEFAULT_FRESH_SIGNIN_SECONDS,
) -> list[ProxyFlag]:
    """Recompute every current suspicion for one session's records.

    Idempotent: the same records always produce the same flags, in the same
    order. Nothing is persisted here — that is what stops remarks from
    accumulating across writes.
    """
    flags: list[ProxyFlag] = []
    for record in records:
        if _is_cleared(record):
            continue
        for candidate in (
            _id_change_flag(record),
            _group_flag(record, records, short_window_seconds, fresh_signin_seconds),
        ):
            if candidate is not None:
                flags.append(candidate)
    return flags


def warnings(flags: list[ProxyFlag]) -> list[str]:
    """CR dashboard text: counts only. No names, no identifiers, no IPs."""
    shared = [flag for flag in flags if flag.code == CODE_SHARED_BROWSER]
    changed = [flag for flag in flags if flag.code == CODE_BROWSER_ID_CHANGED]
    messages: list[str] = []
    if shared:
        groups = {flag.group_label for flag in shared}
        strength = ("Strong" if any(flag.level == LEVEL_HIGH for flag in shared)
                    else "Possible")
        messages.append(
            f"\u26a0 {strength} shared browser: {len(shared)} mark(s) across "
            f"{len(groups)} browser(s) used by more than one student"
        )
    if changed:
        messages.append(
            f"\u26a0 {len(changed)} mark(s) where the browser id changed "
            f"mid-attempt"
        )
    return messages


def review_entries(flags: list[ProxyFlag]) -> list[dict]:
    """Per-student suspicions for the CR review panel, strongest first.

    Names are included because the CR needs to know whom to ask. No IP, no
    browser id, no hash and no coordinates are ever exposed.
    """
    ordered = sorted(
        flags,
        key=lambda flag: (0 if flag.level == LEVEL_HIGH else 1, flag.marked_at),
    )
    return [flag.as_dict() for flag in ordered if flag.enrollment_no]


def remarks_by_enrollment(flags: list[ProxyFlag]) -> dict[str, str]:
    """The Sheet column as {enrollment_no: remark}. Absent means "clear it".

    A student can only earn one remark cell, so the strongest level wins.
    """
    remarks: dict[str, str] = {}
    for flag in flags:
        if not flag.enrollment_no:
            continue
        text = format_remark(flag)
        existing = remarks.get(flag.enrollment_no)
        if existing is None or flag.level == LEVEL_HIGH:
            remarks[flag.enrollment_no] = text
    return remarks
