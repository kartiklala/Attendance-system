"""Client-IP extraction behind a proxy.

Render APPENDS to X-Forwarded-For rather than replacing it, so the left-most
entry is exactly the value the client decided to send. These tests pin the
behaviour that closes that hole, and prove the recorded address stays a separate
audit value rather than becoming part of any identity.
"""
from app.utils.http import client_info, trusted_client_ip

REAL_CLIENT = "203.0.113.7"
FORGER = "1.1.1.1"
VICTIM = "198.51.100.24"

# Production: one trusted edge proxy (Render) appends the peer it saw.
PROD = {"trust_forwarded": True, "trusted_proxy_count": 1}
# Local dev: uvicorn direct, no proxy, so the header proves nothing.
DIRECT = {"trust_forwarded": False}


# ---- the attack the old code allowed --------------------------------------

def test_forged_leftmost_entry_is_not_the_recorded_ip():
    """Client sends `X-Forwarded-For: 1.1.1.1`; Render appends the truth.

    The old code read entries[0] and recorded 1.1.1.1 — the forged address.
    """
    headers = {"x-forwarded-for": f"{FORGER}, {REAL_CLIENT}"}
    assert trusted_client_ip(headers, "", **PROD) == REAL_CLIENT


def test_a_student_cannot_impersonate_another_students_ip():
    """Injecting a classmate's address does not move the recorded value."""
    headers = {"x-forwarded-for": f"{VICTIM}, {FORGER}, {REAL_CLIENT}"}
    assert trusted_client_ip(headers, "", **PROD) == REAL_CLIENT
    assert trusted_client_ip(headers, "", **PROD) != VICTIM


def test_forged_header_is_ignored_entirely_when_no_proxy_is_in_front():
    """Local development: every XFF present is one a client typed, and the
    socket peer is the only trustworthy address."""
    assert trusted_client_ip({"x-forwarded-for": FORGER}, REAL_CLIENT, **DIRECT) == REAL_CLIENT
    assert trusted_client_ip({"x-forwarded-for": FORGER}, "127.0.0.1", **DIRECT) == "127.0.0.1"


# ---- the normal cases must keep working -----------------------------------

def test_plain_proxied_request_where_the_client_sent_nothing():
    """The common production case: a 1-entry chain, appended by the platform."""
    assert trusted_client_ip({"x-forwarded-for": REAL_CLIENT}, "10.0.0.1", **PROD) == REAL_CLIENT


def test_socket_peer_is_used_when_there_is_no_forwarded_header():
    assert trusted_client_ip({}, "127.0.0.1", **PROD) == "127.0.0.1"
    assert trusted_client_ip({}, "127.0.0.1", **DIRECT) == "127.0.0.1"


def test_more_hops_are_skipped_when_the_deployment_has_more_proxies():
    """A CDN in front of Render appends its own egress address, so with
    trusted_proxy_count=2 the CDN exit is what we can actually attest to."""
    chain = f"{FORGER}, {REAL_CLIENT}, {VICTIM}"
    assert trusted_client_ip({"x-forwarded-for": chain}, "", **PROD) == VICTIM
    assert trusted_client_ip({"x-forwarded-for": chain}, "",
                             trust_forwarded=True, trusted_proxy_count=2) == REAL_CLIENT
    assert trusted_client_ip({"x-forwarded-for": chain}, "",
                             trust_forwarded=True, trusted_proxy_count=3) == FORGER


def test_untrustworthy_chain_falls_back_to_the_socket_peer():
    """A chain shorter than the proxy count still yields a real address."""
    assert trusted_client_ip({"x-forwarded-for": "not-an-ip"}, REAL_CLIENT,
                             **PROD) == REAL_CLIENT
    assert trusted_client_ip({"x-forwarded-for": ""}, REAL_CLIENT, **PROD) == REAL_CLIENT


def test_ipv6_and_messy_whitespace_are_handled():
    assert trusted_client_ip({"x-forwarded-for": "  2001:db8::42 "}, "", **PROD) == "2001:db8::42"
    assert trusted_client_ip({"x-forwarded-for": f"  {FORGER} ,  {REAL_CLIENT} "},
                             "", **PROD) == REAL_CLIENT


def test_garbage_entries_never_become_a_recorded_ip():
    for bad in ("not-an-ip", "999.999.999.999", "1.2.3", "<script>x</script>",
                "203.0.113.7:8080/path", "0x7f000001", "1.2.3.4; drop table"):
        assert trusted_client_ip({"x-forwarded-for": bad}, "", **PROD) == "", bad


def test_oversized_value_is_never_stored_verbatim():
    assert trusted_client_ip({"x-forwarded-for": REAL_CLIENT + "0" * 500}, "", **PROD) == ""


# ---- the fields stay separate ----------------------------------------------

def test_client_info_keeps_ip_and_user_agent_separate_and_bounded():
    headers = {
        "x-forwarded-for": f"{FORGER}, {REAL_CLIENT}",
        "user-agent": "Mozilla/5.0 " + "x" * 900,
    }
    info = client_info(headers, "", **PROD)
    assert info["ip"] == REAL_CLIENT
    assert len(info["user_agent"]) == 512
    assert info["user_agent"].startswith("Mozilla/5.0 ")
    # Neither value is folded into the other (no fused "device" hash).
    assert REAL_CLIENT not in info["user_agent"]
    assert set(info) == {"ip", "user_agent"}


def test_client_info_tolerates_a_missing_user_agent():
    assert client_info({"x-forwarded-for": REAL_CLIENT}, "", **PROD)["user_agent"] == ""
