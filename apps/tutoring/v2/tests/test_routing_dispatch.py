"""Routing-dispatch tests.

Per Phase 1 exit criterion:
  - ``NEW_TUTOR=off`` (default): existing routing is unchanged.
  - ``NEW_TUTOR=on``: new sessions are stamped engine_version='v2'
    and runtime_state is initialized with a valid SessionRuntimeState
    snapshot.

These use a duck-typed session object so the test runs without the
DB (Phase 1 contract test).
"""

from unittest import TestCase
from unittest.mock import patch

from apps.tutoring.v2.contracts import SessionRuntimeState
from apps.tutoring.v2.routing import (
    ensure_engine_version_set,
    is_v2_session,
)


class _FakeSession:
    def __init__(self):
        self.engine_version = ""
        self.runtime_state = {}
        self.saved_fields: list[list[str]] = []

    def save(self, update_fields=None):
        self.saved_fields.append(list(update_fields or []))


class RoutingDispatchTest(TestCase):
    def test_new_tutor_off_picks_legacy(self):
        sess = _FakeSession()
        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            chosen = ensure_engine_version_set(sess)
        self.assertEqual(chosen, "legacy")
        self.assertEqual(sess.engine_version, "legacy")
        self.assertFalse(is_v2_session(sess))
        # runtime_state untouched on legacy
        self.assertEqual(sess.runtime_state, {})

    def test_new_tutor_on_picks_v2_and_writes_runtime_state(self):
        sess = _FakeSession()
        with patch.dict("os.environ", {"NEW_TUTOR": "on"}):
            chosen = ensure_engine_version_set(sess)
        self.assertEqual(chosen, "v2")
        self.assertEqual(sess.engine_version, "v2")
        self.assertTrue(is_v2_session(sess))
        self.assertNotEqual(sess.runtime_state, {})
        # runtime_state must round-trip through SessionRuntimeState
        state = SessionRuntimeState.from_jsonable(sess.runtime_state)
        self.assertIsInstance(state, SessionRuntimeState)
        self.assertEqual(state.schema_version, 1)

    def test_sticky_engine_version_does_not_flip(self):
        sess = _FakeSession()
        sess.engine_version = "legacy"
        with patch.dict("os.environ", {"NEW_TUTOR": "on"}):
            chosen = ensure_engine_version_set(sess)
        self.assertEqual(chosen, "legacy")

    def test_sticky_v2_preserved(self):
        sess = _FakeSession()
        sess.engine_version = "v2"
        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            chosen = ensure_engine_version_set(sess)
        self.assertEqual(chosen, "v2")
