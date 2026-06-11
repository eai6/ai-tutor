"""Resolve the real client IP behind Azure Application Gateway.

The production app is VNet-internal — the ONLY thing that can reach it is the
App Gateway — so the gateway-appended hop of ``X-Forwarded-For`` is trustworthy.

Two things this gets right that the old ``split(',')[0]`` did not:

1. **Security:** the LEFTMOST X-Forwarded-For entry is client-supplied and
   therefore spoofable. App Gateway appends the *real* client as the LAST hop,
   so we take the last entry — an attacker can prepend fakes but cannot forge
   the gateway-added value (they cannot bypass the gateway to reach us).
2. **Correctness:** App Gateway formats its hop as ``ip:port``
   (e.g. ``67.22.23.225:59633``). Postgres ``inet`` columns reject the ``:port``
   suffix, so we strip it. Invalid values return ``None`` (column stays NULL)
   instead of raising a DataError.
"""
from __future__ import annotations

import ipaddress


def _normalize_ip(raw: str | None) -> str | None:
    """Strip any ``:port`` / brackets and validate; return None if not an IP."""
    if not raw:
        return None
    raw = raw.strip()
    # Bracketed IPv6 with optional port: "[2001:db8::1]:443"
    if raw.startswith('['):
        raw = raw[1:].split(']', 1)[0]
    # IPv4 with port: "1.2.3.4:5678" (exactly one colon → host:port).
    # Bare IPv6 (multiple colons) is left as-is.
    elif raw.count(':') == 1:
        raw = raw.split(':', 1)[0]
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def get_client_ip(request) -> str | None:
    """Best-effort real client IP. Trusts the gateway-added last X-Forwarded-For
    hop, falls back to REMOTE_ADDR. Returns None if neither is a valid IP."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        ip = _normalize_ip(xff.split(',')[-1])
        if ip:
            return ip
    return _normalize_ip(request.META.get('REMOTE_ADDR'))
