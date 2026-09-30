"""Trusted HTTP request metadata.

Why this module exists
----------------------
Render APPENDS to `X-Forwarded-For` rather than replacing it, so any value the
client put in that header stays in the list. The previous code read the
LEFT-MOST entry, which meant:

  * a student could choose their own recorded IP (spoof another student's, or
    make every submission look like it came from a different address), and
  * the "same IP" evidence in the audit trail was not evidence at all.

The address our own proxy appended is the one nobody else can write, so the
RIGHT-MOST entry is the trustworthy one. Where no proxy is in front at all
(local development), the header is ignored outright and the socket peer is used.
"""
from typing import Mapping

MAX_IP_LENGTH = 64
MAX_USER_AGENT_LENGTH = 512

# IPv4 / IPv6 characters only: a header fragment is never stored as an "IP".
_IP_CHARS = set("0123456789abcdefABCDEF.:")


def _is_plausible_ip(value: str) -> bool:
    """Shape check only — deliberately no DNS, no resolution, no network I/O."""
    if not value or len(value) > MAX_IP_LENGTH:
        return False
    if ":" in value:  # IPv6 (possibly with an embedded IPv4 suffix)
        return set(value) <= _IP_CHARS
    parts = value.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return False
    return True


def trusted_client_ip(
    headers: Mapping[str, str],
    client_host: str = "",
    trusted_proxy_count: int = 1,
    trust_forwarded: bool = True,
) -> str:
    """Return the client IP the platform observed, never the one claimed.

    Behind one trusted edge proxy (production Render):
        client sends `X-Forwarded-For: 1.2.3.4` (forged)
        header reaches us as `1.2.3.4, 9.9.9.9`   (9.9.9.9 appended by Render)
        ->  index = len(chain) - 1 = 1  ->  "9.9.9.9", the real peer.

        client sends nothing
        header reaches us as `9.9.9.9`  ->  a 1-entry chain, which can only be
        the platform's own append, and the same arithmetic still lands on it.

    `trust_forwarded=False` ignores the header entirely and uses the socket peer.
    That is what local development wants: uvicorn runs with no proxy in front,
    so every X-Forwarded-For value present is one a client typed, and
    `request.client.host` is the real address. Without this switch a forged
    single-entry header would be recorded verbatim on localhost.

    If the chain is shorter than the trusted proxy count, or the selected entry
    is not an address at all, fall back to the socket peer rather than guessing.
    """
    if trust_forwarded:
        forwarded = str(headers.get("x-forwarded-for", "") or "")
        chain = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
        if chain:
            index = len(chain) - max(1, int(trusted_proxy_count))
            candidate = chain[index] if index >= 0 else chain[-1]
            if _is_plausible_ip(candidate):
                return candidate

    if client_host and _is_plausible_ip(client_host):
        return client_host
    return ""


def client_info(headers: Mapping[str, str], client_host: str = "",
                trusted_proxy_count: int = 1, trust_forwarded: bool = True) -> dict:
    """Server-observed audit fields, kept SEPARATE and never fused into a hash.

    `ip` / `user_agent` are stored for the record only. Shared-browser detection
    does not read them (see app/services/proxy_detection.py): a whole lab of
    identical machines on one network shares both values, which is a property of
    the fleet, not of a proxy.
    """
    return {
        "ip": trusted_client_ip(headers, client_host, trusted_proxy_count,
                                 trust_forwarded),
        "user_agent": str(headers.get("user-agent", "") or "")[:MAX_USER_AGENT_LENGTH],
    }
