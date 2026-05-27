"""QuestionExtractor unit tests — Phase 4 Fix 2c."""

from __future__ import annotations

from apps.tutoring.v2.services.question_extractor import (
    ExtractionResult,
    QuestionExtractor,
)


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tokens_in = 0
        self.tokens_out = 0


class _FakeClient:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def generate(self, **_kw):
        return _FakeResp(self._payload)


def test_empty_text_returns_zero_actions_without_llm_call():
    """Whitespace-only tutor text short-circuits — no LLM call needed."""

    def _exploding():
        raise AssertionError("must not call client on empty text")

    extractor = QuestionExtractor(client_factory=_exploding)
    result = extractor.extract(tutor_text="   ", selected_move="explain")
    assert result.action_count == 0
    assert result.has_active_end is False
    assert result.available is True


def test_single_action_prompt_returns_one():
    payload = (
        '{"action_count": 1, "primary_action": "What is 12 plus 13?", '
        '"has_active_end": true, "stacked_examples": []}'
    )
    extractor = QuestionExtractor(client_factory=lambda: _FakeClient(payload))
    result = extractor.extract(
        tutor_text="Twelve plus thirteen — what's the sum?",
        selected_move="pose_question",
    )
    assert result.action_count == 1
    assert result.has_active_end is True
    assert result.available is True
    assert result.primary_action.startswith("What is")


def test_stacked_questions_detected():
    """The GEO run-6 T16 failure mode: worked_example ends with TWO
    asks (boat problem follow-on + Port Louis MCQ). The extractor must
    surface both as stacked_examples so the engine treats it as a
    one_question_per_turn violation.
    """
    payload = (
        '{"action_count": 2, '
        '"primary_action": "Give the six-figure reference for the boat.", '
        '"has_active_end": true, '
        '"stacked_examples": ['
        '  "Give the six-figure reference for the boat.",'
        '  "Which of the following describes the search-area change?"'
        ']}'
    )
    extractor = QuestionExtractor(client_factory=lambda: _FakeClient(payload))
    result = extractor.extract(
        tutor_text="(...long worked example...) Now you try the boat. "
                   "Then: which of the following best describes...",
        selected_move="worked_example",
    )
    assert result.action_count == 2
    assert len(result.stacked_examples) == 2


def test_missing_active_end_flagged():
    """A tutor turn that explains but ends without an action prompt
    violates the active-end rule.
    """
    payload = (
        '{"action_count": 0, "primary_action": "", '
        '"has_active_end": false, "stacked_examples": []}'
    )
    extractor = QuestionExtractor(client_factory=lambda: _FakeClient(payload))
    result = extractor.extract(
        tutor_text="The hydrological cycle has four main stages.",
        selected_move="explain",
    )
    assert result.action_count == 0
    assert result.has_active_end is False


def test_llm_outage_is_fail_soft():
    """When the extractor client raises, the result reports
    available=False AND defaults action_count=1 / has_active_end=True
    so the engine does not raise spurious violations on outage. The
    deterministic conformance gates remain the safety floor.
    """

    class _BrokenClient:
        def generate(self, **_kw):
            raise RuntimeError("boom")

    extractor = QuestionExtractor(client_factory=lambda: _BrokenClient())
    result = extractor.extract(
        tutor_text="Some real tutor text", selected_move="pose_question",
    )
    assert result.available is False
    assert result.action_count == 1
    assert result.has_active_end is True


def test_no_client_factory_returns_unavailable():
    """Without a configured client (no DB / no ModelConfig), the
    extractor still returns a deterministic fail-soft result.
    """

    def _no_client():
        return None

    extractor = QuestionExtractor(client_factory=_no_client)
    result = extractor.extract(
        tutor_text="Some tutor text", selected_move="explain",
    )
    assert result.available is False
    assert result.action_count == 1
