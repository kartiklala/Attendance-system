"""Shared-browser (proxy) detection rules.

Pure unit tests: `proxy_detection` takes and returns plain dicts, so the whole
policy is verified without Firestore, the Sheets API or Firebase Admin. Every
scenario below is a case a student could actually be in, and each one asserts
the outcome the CR would see in the dashboard and on the Sheet.
"""
from datetime import datetime, timezone

from app.services import proxy_detection as pd

BROWSER_A = "11111111-1111-4111-8111-111111111111"
BROWSER_B = "22222222-2222-4222-8222-222222222222"
SESSION = "Sess_abc123"
SECRET = "unit-test-pepper"
# The reference wall-clock used by the scenarios below, so auth_time can be
# expressed relative to marked_at instead of as a magic epoch number.
MARK_EPOCH = datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone.utc).timestamp()


def mark(uid, enrollment, *, browser=BROWSER_A, at="2026-09-30T10:00:00+00:00",
         ip="10.0.0.5", ua="Mozilla/5.0 (Linux; Android 14)", auth_time=0,
         changed=False, cleared=False):
    """One attendance record as the detector reads it from Firestore.

    `browser=""` means the client sent no id (or storage was purged): the
    detector must treat that as *unknown*, never as suspicious.
    """
    record = {
        "student_uid": uid,
        "enrollment_no": enrollment,
        "name": f"Student {enrollment}",
        "marked_at": at,
        "browser_id_hash": pd.hash_browser_id(browser, SESSION, SECRET),
        "client_ip": ip,
        "user_agent": ua,
        # Legacy audit value: sha256(ip|ua). Present to prove no rule reads it.
        "device_hash": pd.hash_browser_id(f"{ip}|{ua}", SESSION, SECRET),
        "auth_time": auth_time,
        "browser_id_changed": changed,
    }
    if cleared:
        record["review"] = {"status": pd.REVIEW_CLEARED, "by_uid": "cr-uid"}
    return record


def uids_of(flags):
    return sorted(flag.enrollment_no for flag in flags)


# ---- hashing / validation --------------------------------------------------

def test_browser_id_must_be_a_uuid_or_it_is_dropped():
    assert pd.normalize_browser_id(BROWSER_A) == BROWSER_A
    assert pd.normalize_browser_id("  " + BROWSER_A.upper() + " ") == BROWSER_A
    for junk in ("", "not-a-uuid", "1234", None, 42, [], {"a": 1},
                 BROWSER_A + "trailing", "../etc/passwd", " " * 40):
        assert pd.normalize_browser_id(junk) == "", junk


def test_hash_never_mixes_in_ip_or_user_agent():
    """The identity is the browser alone. Mixing IP/UA in is what produced the
    original false positives (one identical class -> one identical hash)."""
    base = pd.hash_browser_id(BROWSER_A, SESSION, SECRET)
    assert base == pd.hash_browser_id(BROWSER_A, SESSION, SECRET)
    assert len(base) == pd.HASH_LENGTH
    # A different session pepper means the same browser is unlinkable.
    assert base != pd.hash_browser_id(BROWSER_A, "Sess_other", SECRET)
    # A different secret means a leaked row identifies nothing.
    assert base != pd.hash_browser_id(BROWSER_A, SESSION, "other-pepper")
    # No raw material anywhere in the digest.
    assert BROWSER_A not in base and SESSION not in base


def test_missing_inputs_hash_to_empty_not_to_a_value():
    assert pd.hash_browser_id("", SESSION, SECRET) == ""
    assert pd.hash_browser_id(BROWSER_A, "", SECRET) == ""
    assert pd.hash_browser_id(BROWSER_A, SESSION, "") == ""


# ---- required scenario: same browser + multiple students ------------------

def test_shared_browser_two_students_is_flagged():
    flags = pd.detect([
        mark("uid-1", "2025001001", at="2026-09-30T10:00:00+00:00"),
        mark("uid-2", "2026001002", at="2026-09-30T10:00:40+00:00"),
    ])
    shared = [f for f in flags if f.code == pd.CODE_SHARED_BROWSER]
    assert len(shared) == 2
    assert uids_of(shared) == ["2025001001", "2026001002"]
    assert all(f.level == pd.LEVEL_MEDIUM for f in shared), "40s apart is not rapid"
    assert shared[0].total == 2 and shared[0].position != shared[1].position


def test_same_browser_same_student_never_flagged():
    """Thirty students, thirty browsers, one class: the normal case."""
    records = [
        mark(f"uid-{n}", f"20250010{n:02d}",
             browser=f"{n:08d}-0000-4000-8000-000000000000")
        for n in range(30)
    ]
    assert pd.detect(records) == []


def test_rapid_marks_on_one_browser_are_stronger():
    flags = pd.detect([
        mark("uid-1", "2025001001", at="2026-09-30T10:00:00+00:00"),
        mark("uid-2", "2025001002", at="2026-09-30T10:00:06+00:00"),
    ])
    assert flags and all(f.level == pd.LEVEL_HIGH for f in flags)
    assert all("marks within seconds of each other" in f.reasons for f in flags)


def test_fresh_google_sessions_support_the_conclusion():
    """auth_time is server-recorded: several brand-new sessions on one browser
    within a minute is account juggling, not a class."""
    just_signed_in = MARK_EPOCH - 60
    flags = pd.detect([
        mark("uid-1", "2025001001", at="2026-09-30T10:00:00+00:00",
             auth_time=just_signed_in),
        mark("uid-2", "2025001002", at="2026-09-30T10:00:50+00:00",
             auth_time=just_signed_in + 4),
    ])
    assert flags and all(f.level == pd.LEVEL_HIGH for f in flags)
    assert all("signed in to Google just now" in " ".join(f.reasons) for f in flags)


def test_old_sessions_do_not_add_the_fresh_signin_reason():
    """A real class stays signed in to Google for hours or days."""
    long_ago = int(MARK_EPOCH) - 86_400
    flags = pd.detect([
        mark("uid-1", "2025001001", at="2026-09-30T10:00:00+00:00", auth_time=long_ago),
        mark("uid-2", "2025001002", at="2026-09-30T10:00:50+00:00", auth_time=long_ago),
    ])
    assert flags, "still a shared browser, just not a fresh-session one"
    assert all(f.level == pd.LEVEL_MEDIUM for f in flags)
    assert all("signed in to Google just now" not in " ".join(f.reasons) for f in flags)


def test_zero_auth_time_means_unknown_not_epoch_1970():
    """Records written before auth_time existed must never look brand-new."""
    flags = pd.detect([
        mark("uid-1", "2025001001", at="2026-09-30T10:00:00+00:00", auth_time=0),
        mark("uid-2", "2025001002", at="2026-09-30T10:00:05+00:00", auth_time=0),
    ])
    # Rapid (5s apart) but no fresh-signin claim, which is the honest answer.
    assert all("signed in to Google just now" not in " ".join(f.reasons) for f in flags)


def test_auth_time_is_supporting_only_never_a_flag_on_its_own():
    """One student, fresh session, one browser: nothing suspicious."""
    flags = pd.detect([mark("uid-1", "2025001001", auth_time=1_760_000_000)])
    assert flags == []


# ---- required scenario: IP must stop being a signal ------------------------

def test_same_ip_only_is_never_flagged():
    """One campus router, identical phones, identical browser strings.

    This is the false positive the old sha256(ip+ua) scheme produced in bulk:
    every device_hash here is identical by construction, and the detector must
    still find nothing.
    """
    records = [
        mark(f"uid-{n}", f"20250010{n:02d}",
             browser=f"{n:08d}-0000-4000-8000-000000000000",
             ip="103.21.1.1", ua="Mozilla/5.0 (Linux; Android 14) Chrome/120")
        for n in range(25)
    ]
    assert len({r["device_hash"] for r in records}) == 1, "identical IP+UA fleet"
    assert pd.detect(records) == []
    assert pd.warnings([]) == []


def test_different_browsers_on_the_same_ip_not_flagged():
    flags = pd.detect([
        mark("uid-1", "2025001001", browser=BROWSER_A, ip="103.21.1.1"),
        mark("uid-2", "2025001002", browser=BROWSER_B, ip="103.21.1.1"),
    ])
    assert flags == []


def test_changing_ip_does_not_break_the_same_browser_link():
    """A student who walks from Wi-Fi to mobile data is still one browser."""
    flags = pd.detect([
        mark("uid-1", "2025001001", browser=BROWSER_A, ip="103.21.1.1"),
        mark("uid-2", "2025001002", browser=BROWSER_A, ip="172.16.9.9",
             ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5)"),
    ])
    shared = [f for f in flags if f.code == pd.CODE_SHARED_BROWSER]
    assert len(shared) == 2, "different IPs and different user-agents still group"


# ---- required scenario: browser id changing during one attempt -------------

def test_browser_id_changed_during_attempt_is_flagged():
    flags = pd.detect([
        mark("uid-1", "2025001001", browser=BROWSER_A,
             at="2026-09-30T10:00:00+00:00", changed=True),
    ])
    changed = [f for f in flags if f.code == pd.CODE_BROWSER_ID_CHANGED]
    assert len(changed) == 1
    assert changed[0].level == pd.LEVEL_HIGH
    assert changed[0].enrollment_no == "2025001001"


def test_missing_anchor_is_not_reported_as_a_change():
    """Storage purged, or the scan came from a client that sent no id: unknown,
    therefore never a violation."""
    assert not pd.browser_id_changed_on_attempt("", "abc123")
    assert not pd.browser_id_changed_on_attempt("abc123", "")
    assert not pd.browser_id_changed_on_attempt("", "")
    assert pd.browser_id_changed_on_attempt("abc123", "def456")
    flags = pd.detect([mark("uid-1", "2025001001", browser="")])
    assert flags == []


# ---- required scenario: duplicate students / missing ids ------------------

def test_duplicate_record_for_one_student_is_not_a_proxy():
    """Same identity twice (a retry landing next to the original): still one
    student on that browser."""
    flags = pd.detect([
        mark("uid-1", "2025001001", at="2026-09-30T10:00:00+00:00"),
        mark("uid-1", "2025001001", at="2026-09-30T10:00:05+00:00"),
    ])
    assert flags == []


def test_records_without_a_browser_id_are_excluded_entirely():
    """Old attendance records (written before this feature) must never be
    flagged, and must not drag a modern record into a group either."""
    legacy = [
        {"student_uid": "uid-9", "enrollment_no": "2025000009", "name": "Old",
         "marked_at": "2026-09-30T09:59:00+00:00", "browser_id_hash": "",
         "client_ip": "103.21.1.1", "user_agent": "Mozilla/5.0"},
    ]
    assert pd.detect(legacy) == []
    mixed = legacy + [
        mark("uid-1", "2025001001"),
        mark("uid-2", "2025001002"),
    ]
    flagged = uids_of(pd.detect(mixed))
    assert flagged == ["2025001001", "2025001002"], "legacy row stays unflagged"


def test_uid_less_records_cannot_form_a_group():
    """A record with no identity carries no evidence even with a browser hash."""
    flags = pd.detect([
        {"student_uid": "", "enrollment_no": "2025001001", "marked_at":
         "2026-09-30T10:00:00+00:00",
         "browser_id_hash": pd.hash_browser_id(BROWSER_A, SESSION, SECRET)},
        {"student_uid": "", "enrollment_no": "2025001002", "marked_at":
         "2026-09-30T10:00:10+00:00",
         "browser_id_hash": pd.hash_browser_id(BROWSER_A, SESSION, SECRET)},
    ])
    assert flags == []


# ---- required scenario: CR override + no accumulation ---------------------

def test_cr_override_clears_the_pair():
    records = [
        mark("uid-1", "2025001001"),
        mark("uid-2", "2025001002"),
    ]
    assert len(pd.detect(records)) == 2
    records[1]["review"] = {"status": pd.REVIEW_CLEARED, "by_uid": "cr-uid"}
    # One student left on that browser is not a shared browser.
    assert pd.detect(records) == []
    assert pd.remarks_by_enrollment(pd.detect(records)) == {}


def test_detection_is_recomputed_not_accumulated():
    """The same records always yield the same conclusion: nothing from an
    earlier pass survives, and repeated calls add nothing."""
    records = [
        mark("uid-1", "2025001001", browser=BROWSER_A),
        mark("uid-2", "2025001002", browser=BROWSER_A),
        mark("uid-3", "2025001003", browser=BROWSER_B),
    ]
    first = pd.detect(records)
    second = pd.detect(records)
    assert [f.as_dict() for f in first] == [f.as_dict() for f in second]
    # A third student arriving on BROWSER_B does not touch BROWSER_A's pair.
    records.append(mark("uid-4", "2025001004", browser=BROWSER_B))
    assert len(pd.detect(records)) == 4


def test_two_groups_stay_separate():
    flags = pd.detect([
        mark("uid-1", "2025001001", browser=BROWSER_A),
        mark("uid-2", "2025001002", browser=BROWSER_A),
        mark("uid-3", "2025001003", browser=BROWSER_B,
             at="2026-09-30T10:00:10+00:00"),
        mark("uid-4", "2025001004", browser=BROWSER_B,
             at="2026-09-30T10:00:20+00:00"),
    ])
    labels = {f.group_label for f in flags}
    assert len(labels) == 2, "each browser group is individually identifiable"
    for label in labels:
        group = [f for f in flags if f.group_label == label]
        assert {f.total for f in group} == {2}


# ---- remarks / dashboard output -------------------------------------------

def test_remark_text_is_clear_and_carries_no_identity():
    flags = pd.detect([
        mark("uid-1", "2025001001", at="2026-09-30T10:21:05+00:00"),
        mark("uid-2", "2025001002", at="2026-09-30T10:21:11+00:00"),
    ])
    remarks = pd.remarks_by_enrollment(flags)
    assert set(remarks) == {"2025001001", "2025001002"}
    for text in remarks.values():
        assert text.startswith(pd.REMARK_SHARED)
        assert "Browser #" in text and "rapid" in text and "10:21" in text
        # Nothing forgeable or personally identifying reaches the Sheet.
        assert BROWSER_A not in text and "10.0.0.5" not in text
        for flag in flags:
            assert flag.student_uid not in text


def test_id_changed_remark_text():
    flags = pd.detect([mark("uid-1", "2025001001", changed=True)])
    remarks = pd.remarks_by_enrollment(flags)
    assert remarks["2025001001"] == pd.REMARK_ID_CHANGED


def test_one_cell_per_student_even_when_both_rules_apply():
    """A student can be in a shared group AND have changed browser mid-attempt:
    one remark cell, and the stronger of the two is kept."""
    records = [
        mark("uid-1", "2025001001", browser=BROWSER_A, changed=True),
        mark("uid-2", "2025001002", browser=BROWSER_A,
             at="2026-09-30T10:00:02+00:00"),
    ]
    remarks = pd.remarks_by_enrollment(pd.detect(records))
    assert set(remarks) == {"2025001001", "2025001002"}
    assert remarks["2025001001"]  # still exactly one value per enrollment


def test_warnings_expose_counts_only():
    flags = pd.detect([
        mark("uid-1", "2025001001", browser=BROWSER_A),
        mark("uid-2", "2025001002", browser=BROWSER_A),
        mark("uid-3", "2025001003", browser=BROWSER_B, changed=True),
    ])
    messages = " ".join(pd.warnings(flags))
    assert "shared browser" in messages.lower()
    assert "2 mark(s)" in messages
    assert "2025001001" not in messages, "warnings never name a student"
    assert "10.0.0.5" not in messages and BROWSER_A not in messages
    assert "same IP" not in messages, "a shared IP is not reported at all"


def test_review_entries_names_the_students_to_review():
    flags = pd.detect([
        mark("uid-1", "2025001001", browser=BROWSER_A),
        mark("uid-2", "2025001002", browser=BROWSER_A),
    ])
    entries = pd.review_entries(flags)
    assert len(entries) == 2
    assert {"enrollment_no", "name", "level", "reasons"} <= set(entries[0])
    for entry in entries:
        assert "browser_id_hash" not in entry
        assert "client_ip" not in entry
        assert "student_uid" not in entry


def test_unparseable_timestamps_degrade_safely():
    """A garbage marked_at must not raise and must not invent 'rapid'."""
    flags = pd.detect([
        mark("uid-1", "2025001001", at="not-a-date"),
        mark("uid-2", "2025001002", at=""),
    ])
    assert len([f for f in flags if f.code == pd.CODE_SHARED_BROWSER]) == 2
    assert all(pd.LEVEL_HIGH != f.level for f in flags), "no rapid without times"


def test_empty_session_detects_nothing():
    assert pd.detect([]) == []
    assert pd.warnings([]) == []
    assert pd.remarks_by_enrollment([]) == {}
    assert pd.review_entries([]) == []
