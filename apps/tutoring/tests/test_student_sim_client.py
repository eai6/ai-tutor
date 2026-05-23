"""Tests for the empty-content guard in StudentClient.

Regression coverage for the Gemini 400 bug surfaced across all 5 A/B
cycles (v3-v7): when the tutor (esp. Gemini Flash/Pro) emits a turn
that's only a tool call with no prose, the synthetic-student history
accumulates a message with empty content. A later turn's full-history
call to Anthropic 400s with:

    invalid_request_error: messages.N: user messages must have
    non-empty content

The fix in apps/tutoring/student_sim/client.py substitutes a
placeholder for any empty content (both user/tutor and assistant/
student sides), at both the append site (next_reply) and the
_build_messages defense-in-depth layer.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.tutoring.student_sim.client import (
    StudentClient,
    StudentTurn,
    _EMPTY_ASSISTANT_PLACEHOLDER,
    _EMPTY_USER_PLACEHOLDER,
)


def _bare_client() -> StudentClient:
    """Build a StudentClient without invoking the LLM resolver / API.

    The real __init__ calls ModelConfig.objects.filter(...) which needs
    the database. We only exercise _build_messages here, so a subclass
    that skips init is enough.
    """
    sc = StudentClient.__new__(StudentClient)
    sc._history = []
    return sc


class BuildMessagesEmptyContentGuardTest(SimpleTestCase):
    """`_build_messages` substitutes a placeholder for any empty turn
    content, preserving alternating user/assistant ordering so the
    Anthropic API does not reject the request."""

    def test_empty_user_substituted(self):
        sc = _bare_client()
        sc._history = [
            StudentTurn(role='tutor', content='Hello'),
            StudentTurn(role='student', content='Hi'),
            StudentTurn(role='tutor', content=''),
            StudentTurn(role='student', content='What did you say?'),
        ]
        msgs = sc._build_messages()
        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[2]['role'], 'user')
        self.assertEqual(msgs[2]['content'], _EMPTY_USER_PLACEHOLDER)

    def test_empty_assistant_substituted(self):
        sc = _bare_client()
        sc._history = [
            StudentTurn(role='tutor', content='What is 3 + 4?'),
            StudentTurn(role='student', content=''),
        ]
        msgs = sc._build_messages()
        self.assertEqual(msgs[1]['role'], 'assistant')
        self.assertEqual(msgs[1]['content'], _EMPTY_ASSISTANT_PLACEHOLDER)

    def test_whitespace_only_treated_as_empty(self):
        sc = _bare_client()
        sc._history = [
            StudentTurn(role='tutor', content='   \n\t  '),
            StudentTurn(role='student', content=''),
        ]
        msgs = sc._build_messages()
        self.assertEqual(msgs[0]['content'], _EMPTY_USER_PLACEHOLDER)
        self.assertEqual(msgs[1]['content'], _EMPTY_ASSISTANT_PLACEHOLDER)

    def test_non_empty_content_preserved(self):
        sc = _bare_client()
        sc._history = [
            StudentTurn(role='tutor', content='Hello'),
            StudentTurn(role='student', content='Hi!'),
        ]
        msgs = sc._build_messages()
        self.assertEqual(msgs[0]['content'], 'Hello')
        self.assertEqual(msgs[1]['content'], 'Hi!')

    def test_no_empty_content_anywhere(self):
        """The whole point: after _build_messages, no message has
        empty/whitespace-only content. This is the invariant Anthropic
        requires."""
        sc = _bare_client()
        sc._history = [
            StudentTurn(role='tutor', content='Hello'),
            StudentTurn(role='student', content=''),
            StudentTurn(role='tutor', content=''),
            StudentTurn(role='student', content='What?'),
            StudentTurn(role='tutor', content='   '),
            StudentTurn(role='student', content='\n'),
            StudentTurn(role='tutor', content='Ok'),
        ]
        msgs = sc._build_messages()
        for i, m in enumerate(msgs):
            content = (m['content'] or '').strip()
            self.assertTrue(
                bool(content),
                msg=f"message {i} has empty content: {m!r}",
            )


class NextReplyEmptyContentGuardTest(SimpleTestCase):
    """`next_reply` substitutes placeholders BEFORE history append, so
    the in-memory history is also well-formed (not just the rendered
    message list). Also exercised via _build_messages so this is
    belt-and-braces."""

    def test_empty_tutor_msg_substituted_in_history(self):
        from unittest.mock import MagicMock

        sc = _bare_client()
        sc.persona = MagicMock(system_prompt='x', temperature=0.7)
        sc.model_config = MagicMock(max_tokens=256)
        # Stub the LLM so next_reply returns a real string.
        sc.client = MagicMock()
        sc.client.generate.return_value = MagicMock(content='ok')
        sc.last_response = None

        reply = sc.next_reply('')

        # History's first user message is the placeholder, not "".
        self.assertEqual(sc._history[0].role, 'tutor')
        self.assertEqual(sc._history[0].content, _EMPTY_USER_PLACEHOLDER)
        # Reply is the LLM's response unchanged.
        self.assertEqual(reply, 'ok')

    def test_empty_student_reply_substituted_in_history(self):
        from unittest.mock import MagicMock

        sc = _bare_client()
        sc.persona = MagicMock(system_prompt='x', temperature=0.7)
        sc.model_config = MagicMock(max_tokens=256)
        sc.client = MagicMock()
        sc.client.generate.return_value = MagicMock(content='')
        sc.last_response = None

        reply = sc.next_reply('What is 2 + 2?')

        # History's student turn is the placeholder, not "".
        self.assertEqual(sc._history[1].role, 'student')
        self.assertEqual(sc._history[1].content, _EMPTY_ASSISTANT_PLACEHOLDER)
        # Returned reply is also the placeholder (caller may use it).
        self.assertEqual(reply, _EMPTY_ASSISTANT_PLACEHOLDER)
