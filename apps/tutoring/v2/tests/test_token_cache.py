"""Token-cache tests: HMAC verification, peek (read-only), atomic
single-use consume, replay rejection, TTL eviction."""

from unittest import TestCase

from apps.tutoring.v2.tools.token_cache import (
    TokenAlreadyConsumed,
    TokenInvalid,
    _TokenCache,
    token_cache,
)


class TokenCacheTest(TestCase):
    def setUp(self):
        # Always start from a clean cache so test ordering is irrelevant.
        token_cache._reset()

    def test_issue_then_peek_returns_canonical(self):
        token = token_cache.issue(
            session_id=1,
            canonical="42",
            visible_context_json="{}",
        )
        entry = token_cache.peek(1, token)
        self.assertEqual(entry.canonical, "42")
        self.assertFalse(entry.consumed)

    def test_peek_does_not_mark_consumed(self):
        token = token_cache.issue(session_id=1, canonical="x",
                                  visible_context_json="{}")
        token_cache.peek(1, token)
        token_cache.peek(1, token)  # second peek still passes
        entry = token_cache.peek(1, token)
        self.assertFalse(entry.consumed)

    def test_consume_marks_consumed_atomically(self):
        token = token_cache.issue(session_id=1, canonical="x",
                                  visible_context_json="{}")
        token_cache.consume(1, token)
        with self.assertRaises(TokenAlreadyConsumed):
            token_cache.consume(1, token)

    def test_peek_after_consume_raises(self):
        token = token_cache.issue(session_id=1, canonical="x",
                                  visible_context_json="{}")
        token_cache.consume(1, token)
        with self.assertRaises(TokenAlreadyConsumed):
            token_cache.peek(1, token)

    def test_signature_bound_to_session_id(self):
        token = token_cache.issue(session_id=1, canonical="x",
                                  visible_context_json="{}")
        with self.assertRaises(TokenInvalid):
            token_cache.peek(2, token)  # wrong session

    def test_malformed_token_rejected(self):
        with self.assertRaises(TokenInvalid):
            token_cache.peek(1, "not-a-token")

    def test_tampered_signature_rejected(self):
        token = token_cache.issue(session_id=1, canonical="x",
                                  visible_context_json="{}")
        payload, sig = token.split(".", 1)
        tampered = payload + "." + ("A" * len(sig))
        with self.assertRaises(TokenInvalid):
            token_cache.peek(1, tampered)

    def test_capacity_eviction(self):
        cache = _TokenCache(capacity=3, ttl_seconds=3600)
        toks = []
        for _ in range(5):
            toks.append(cache.issue(1, "x", "{}"))
        # First two should have been evicted; last 3 still present.
        with self.assertRaises(TokenInvalid):
            cache.peek(1, toks[0])
        with self.assertRaises(TokenInvalid):
            cache.peek(1, toks[1])
        cache.peek(1, toks[2])
        cache.peek(1, toks[-1])
