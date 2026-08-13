"""The offline engine's server-owned hooks must not run for an online session.

Server-owned question control — pivoting a stalled question, keeping a
remediation question in flight — exists because a 4B will not act on a
conditional instruction. For a while it ran for every session, cloud included.
That was reversed on 2026-08-08: the hosted tutor keeps the shape production
has been evaluated against, and offline requirements are not pushed onto it.

These tests pin the gate, because the failure is silent. Nothing errors when an
online session gets an extra server-posed question — the reply just quietly
stops matching production, which is exactly the drift this is meant to prevent.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from ai_tutor.apps.tutoring.simple_tutor.engine import _is_offline_session
from ai_tutor.apps.tutoring.simple_tutor.model_choice import LOCAL_PROVIDER


class _Cfg:
    def __init__(self, provider):
        self.provider = provider


class OfflineSessionPredicateTest(SimpleTestCase):
    def test_local_provider_is_offline(self):
        self.assertTrue(_is_offline_session(object(), _Cfg(LOCAL_PROVIDER)))

    def test_cloud_providers_are_not_offline(self):
        for provider in ('anthropic', 'openai', 'google', 'ollama_cloud', ''):
            with self.subTest(provider=provider):
                self.assertFalse(_is_offline_session(object(), _Cfg(provider)))

    def test_a_lookup_failure_defaults_to_online(self):
        """Fail-soft direction matters: an error must not switch cloud sessions
        into offline behaviour. It defaults to False for that reason."""
        with patch(
            'ai_tutor.apps.tutoring.simple_tutor.model_choice.resolve_for_session',
            side_effect=RuntimeError('boom'),
        ):
            self.assertFalse(_is_offline_session(object()))

    def test_none_config_is_not_offline(self):
        self.assertFalse(_is_offline_session(object(), None) or False)


class HookGatingTest(SimpleTestCase):
    """The three hooks are called only when the predicate says offline.

    Asserted against the module source rather than by driving a full turn: a
    real turn needs an LLM round-trip, and what is being protected here is the
    call-site guard, which is a structural property.
    """

    def _source(self):
        import inspect
        from ai_tutor.apps.tutoring.simple_tutor import engine
        return inspect.getsource(engine.respond)

    def test_every_offline_hook_call_is_guarded(self):
        """CODE, not comments, must carry the guard.

        The first version of this test scanned a few lines either side of the
        call for the string '_is_offline_session' and passed happily when the
        guard was deleted — because the COMMENT above the call says
        "OFFLINE ONLY - see _is_offline_session". Stripping comments first is
        what gives this test teeth; verified by deleting a real guard and
        watching it fail.
        """
        lines = []
        for raw in self._source().splitlines():
            code = raw.split('#', 1)[0]
            lines.append(code)

        for hook in (
            'ensure_remediation_question',
            'maybe_pivot_stalled_question',
            'maybe_pose_remediation_next',
        ):
            with self.subTest(hook=hook):
                call_lines = [
                    i for i, line in enumerate(lines)
                    if f'{hook}(session)' in line
                ]
                self.assertTrue(call_lines, f'{hook} call not found in respond()')
                for idx in call_lines:
                    window = '\n'.join(lines[max(0, idx - 2):idx + 3])
                    self.assertIn(
                        '_is_offline_session', window,
                        f'{hook} is called without an offline guard in code — '
                        f'an online session would get offline behaviour',
                    )
