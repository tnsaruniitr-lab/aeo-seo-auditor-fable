"""
safety.py — SSRF guards and request-safety helpers for the audit service.

The auditor fetches whatever URL a caller submits. Without a guard, a caller
can point it at cloud-metadata endpoints (169.254.169.254), localhost, or
RFC1918 hosts and read internal responses back through the public audit JSON.
This module rejects such targets BEFORE an audit is dispatched, and offers the
same check for webhook callback URLs.

Stdlib only.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse


# Hostnames that must never be fetched regardless of DNS resolution.
_BLOCKED_HOSTNAMES = frozenset({
    'localhost',
    'metadata.google.internal',         # GCP metadata
    'metadata',
})

# Cloud metadata IPs (link-local already blocks 169.254/16, but be explicit).
_BLOCKED_IPS = frozenset({
    '169.254.169.254',                  # AWS/GCP/Azure/DO/etc. IMDS
    '100.100.100.200',                  # Alibaba metadata
    'fd00:ec2::254',                    # AWS IMDSv6
})


def _ip_is_disallowed(ip: ipaddress._BaseAddress) -> bool:
    """True for any address that must not be the target of a server-side fetch."""
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        or str(ip) in _BLOCKED_IPS
        # IPv4-mapped IPv6 (::ffff:169.254.169.254) must be unwrapped & checked
        or (getattr(ip, 'ipv4_mapped', None) is not None
            and _ip_is_disallowed(ip.ipv4_mapped))
    )


def check_url_safe(url: str, *, resolve: bool = True) -> Tuple[bool, Optional[str]]:
    """Validate that `url` is safe to fetch server-side.

    Returns (ok, reason). When ok is False, reason explains why (for logging /
    a 400 response). Blocks non-http(s) schemes, credentialed URLs, blocked
    hostnames, and — when resolve=True — any hostname that resolves to a
    private/loopback/link-local/reserved/metadata address.
    """
    if not url or not isinstance(url, str):
        return False, 'empty url'

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ('http', 'https'):
        return False, f"scheme '{parsed.scheme}' not allowed (http/https only)"

    host = (parsed.hostname or '').strip().lower()
    if not host:
        return False, 'no host in url'

    # user:pass@host can smuggle the real host past naive checks
    if parsed.username or parsed.password:
        return False, 'credentials in url are not allowed'

    if host in _BLOCKED_HOSTNAMES:
        return False, f"host '{host}' is blocked"

    # Literal IP in the URL — check directly without DNS.
    try:
        literal = ipaddress.ip_address(host)
        if _ip_is_disallowed(literal):
            return False, f"host resolves to disallowed address {literal}"
        return True, None
    except ValueError:
        pass  # not a literal IP — fall through to DNS resolution

    if not resolve:
        return True, None

    # Resolve ALL addresses; reject if ANY is internal (DNS-rebinding-resistant
    # to the extent a pre-flight check can be — the fetch layer should ideally
    # pin to a vetted IP, but blocking here stops the trivial attacks).
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, f"dns resolution failed for '{host}': {e}"
    except Exception as e:  # noqa: BLE001 — never let a resolver quirk crash submission
        return False, f"dns error for '{host}': {type(e).__name__}"

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split('%')[0])  # strip zone id
        except ValueError:
            continue
        if _ip_is_disallowed(ip):
            return False, f"host '{host}' resolves to disallowed address {ip}"

    return True, None
