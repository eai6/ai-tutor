"""HMAC-signed single-use pre_pose_token cache.

Per Phase 1 §4.2:
  - Process-local LRU keyed by ``(session_id, token)``.
  - Tokens HMAC-signed with a per-process secret derived from
    Django ``SECRET_KEY``.
  - Phase A validation is READ-ONLY (``peek``).
  - Phase B commit consumes single-use atomically (``consume``).

Known boundary: process-local. Azure Container Apps runs a small
replica count and tokens are single-turn-scoped, so this is
acceptable; if we ever scale out replicas, move to Redis.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional


class TokenInvalid(Exception):
    """Token signature did not verify or token is not present."""


class TokenAlreadyConsumed(Exception):
    """Token has already been single-use consumed."""


def _process_secret() -> bytes:
    """Per-process secret for HMAC signing.

    Derived from Django ``SECRET_KEY`` if available, otherwise from a
    one-shot urandom bound to the module load. Either way, tokens
    don't outlive a process — which matches their single-turn scope.
    """
    try:
        from django.conf import settings

        return hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    except Exception:
        global _FALLBACK_SECRET
        if _FALLBACK_SECRET is None:
            _FALLBACK_SECRET = os.urandom(32)
        return _FALLBACK_SECRET


_FALLBACK_SECRET: Optional[bytes] = None


@dataclass
class _CachedToken:
    session_id: int
    canonical: str
    visible_context_json: str
    issued_at: float
    consumed: bool = False


class _TokenCache:
    """Process-local LRU cache of signed pre_pose_token entries.

    Default capacity 2,048 entries — comfortably more than concurrent
    in-flight tutoring turns on a single Azure replica.
    """

    def __init__(self, capacity: int = 2048, ttl_seconds: int = 600) -> None:
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[int, str], _CachedToken] = OrderedDict()

    # ------------------------------------------------------------------
    # Issue / verify
    # ------------------------------------------------------------------

    def issue(
        self,
        session_id: int,
        canonical: str,
        visible_context_json: str,
    ) -> str:
        """Create a single-use signed token and cache it."""
        nonce = secrets.token_urlsafe(16)
        payload = f"{session_id}:{nonce}".encode()
        sig = hmac.new(_process_secret(), payload, hashlib.sha256).digest()
        token = (
            base64.urlsafe_b64encode(payload).decode().rstrip("=")
            + "."
            + base64.urlsafe_b64encode(sig).decode().rstrip("=")
        )

        with self._lock:
            self._evict_expired_locked()
            self._entries[(session_id, token)] = _CachedToken(
                session_id=session_id,
                canonical=canonical,
                visible_context_json=visible_context_json,
                issued_at=time.time(),
                consumed=False,
            )
            self._entries.move_to_end((session_id, token))
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return token

    def _verify_signature(self, session_id: int, token: str) -> None:
        try:
            payload_b64, sig_b64 = token.split(".", 1)
        except ValueError:
            raise TokenInvalid("malformed token")

        payload_b64_p = payload_b64 + "=" * (-len(payload_b64) % 4)
        sig_b64_p = sig_b64 + "=" * (-len(sig_b64) % 4)
        try:
            payload = base64.urlsafe_b64decode(payload_b64_p.encode())
            sig = base64.urlsafe_b64decode(sig_b64_p.encode())
        except Exception:
            raise TokenInvalid("token decode failed")

        expected = hmac.new(_process_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise TokenInvalid("signature mismatch")

        # Bind the token to the session it was issued for.
        try:
            sess_str, _nonce = payload.decode().split(":", 1)
            if int(sess_str) != session_id:
                raise TokenInvalid("session mismatch")
        except (ValueError, UnicodeDecodeError):
            raise TokenInvalid("payload structure invalid")

    def peek(self, session_id: int, token: str) -> _CachedToken:
        """Phase A — read-only verify + lookup. Does NOT mark consumed."""
        self._verify_signature(session_id, token)
        with self._lock:
            self._evict_expired_locked()
            entry = self._entries.get((session_id, token))
            if entry is None:
                raise TokenInvalid("token unknown or evicted")
            if entry.consumed:
                raise TokenAlreadyConsumed("token already consumed")
            # Refresh LRU position on read so an in-flight token isn't
            # evicted between Phase A and Phase B under load.
            self._entries.move_to_end((session_id, token))
            return entry

    def consume(self, session_id: int, token: str) -> _CachedToken:
        """Phase B — atomic single-use consumption.

        Raises ``TokenAlreadyConsumed`` if the token has been
        consumed already (i.e., a duplicate commit). Raises
        ``TokenInvalid`` if signature is bad or the entry is missing.
        """
        self._verify_signature(session_id, token)
        with self._lock:
            self._evict_expired_locked()
            entry = self._entries.get((session_id, token))
            if entry is None:
                raise TokenInvalid("token unknown or evicted")
            if entry.consumed:
                raise TokenAlreadyConsumed("token already consumed")
            entry.consumed = True
            return entry

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evict_expired_locked(self) -> None:
        if not self._entries:
            return
        now = time.time()
        cutoff = now - self._ttl_seconds
        expired_keys = [
            key for key, ent in self._entries.items() if ent.issued_at < cutoff
        ]
        for key in expired_keys:
            self._entries.pop(key, None)

    # Test affordance — reset cache between unit tests.
    def _reset(self) -> None:
        with self._lock:
            self._entries.clear()


# Module-level singleton — callers import this directly.
token_cache = _TokenCache()
