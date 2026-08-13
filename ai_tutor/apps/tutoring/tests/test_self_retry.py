"""Unit tests for the tutor self-retry path (task #198).

Pins the contract of `apps.tutoring.regen.self_retry`:
  - feedback message format
  - clean-on-first-cycle short-circuits
  - dirty cycles loop until cap, then return best-effort or fallback
  - tool-use snapshot is reset between cycles (engine state clean)
  - llm_client without generate_with_tools → no-op, returns previous
"""

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from ai_tutor.apps.tutoring.combined_judge import CombinedJudgeResult
from ai_tutor.apps.tutoring.regen.self_retry import (
    STOCK_FALLBACK,
    SelfRetryResult,
    _build_feedback_message,
    _detect_leaked_tool_call,
    run_tutor_self_retry,
    summarise_self_retry,
)
from ai_tutor.apps.tutoring.rule_compliance import RULE_RULE_1, RuleViolation


def _validation(issues=None, metadata=None):
    v = MagicMock()
    v.issues = list(issues or [])
    v.metadata = dict(metadata or {})
    return v


def _tutor_with_tool_message(text: str, delta: dict = None):
    """Fake a ConversationalTutor with a generate_with_tools client that
    returns a message and a _pose_dry_run that yields (text, delta).

    The delta defaults to an empty/no-op state change so cycles that
    don't tool produce nothing for _apply_pose_delta to commit. Tests
    that want to verify tool-effect propagation can pass a real delta.
    """
    tutor = MagicMock()
    tutor.session.id = 99
    tutor.llm_client = MagicMock()
    tutor.llm_client.config.model_name = "fake-opus"
    msg = MagicMock()
    msg.content = [MagicMock(type='text', text=text)]
    tutor.llm_client.generate_with_tools.return_value = msg

    _delta = delta or {
        'aa': None,
        'shown_added': set(),
        'turn_q': {},
        'bank_used': False,
        'last_bank': '',
        'meta': {},
    }
    tutor._pose_dry_run.return_value = (text, _delta)
    # Apply just commits the delta's aa onto the mock — enough for
    # tests that verify aa restoration.
    def _apply(d, tm):
        tutor._awaiting_answer = d.get('aa')
        for k in ('bank_question_ref', 'inline_authored_question',
                  'tool_use_count', 'bank_rendered'):
            tm.pop(k, None)
        for k, v in (d.get('meta') or {}).items():
            tm[k] = v
    tutor._apply_pose_delta.side_effect = _apply
    tutor._awaiting_answer = None
    return tutor


# ---------------------------------------------------------------
# Feedback message builder
# ---------------------------------------------------------------

class BuildFeedbackTest(SimpleTestCase):
    def test_includes_previous_response_and_issues(self):
        msg = _build_feedback_message(
            previous_response="some bad text",
            issues=["no_question"],
            validation_metadata={},
        )
        self.assertIn("[system_feedback]", msg)
        self.assertIn("some bad text", msg)
        self.assertIn("NO_QUESTION", msg)

    def test_no_issues_still_returns_revision_request(self):
        msg = _build_feedback_message(
            previous_response="x",
            issues=[],
            validation_metadata={},
        )
        self.assertIn("Please revise", msg)
        self.assertIn("x", msg)

    def test_tool_use_rule_present(self):
        msg = _build_feedback_message(
            previous_response="x",
            issues=["no_question_tool"],
            validation_metadata={},
        )
        self.assertIn("pose_question", msg)
        self.assertIn("pose_inline_question", msg)


# ---------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------

class RunSelfRetryTest(SimpleTestCase):
    def test_no_tool_support_returns_previous(self):
        tutor = MagicMock()
        tutor.llm_client = object()  # has no generate_with_tools
        result = run_tutor_self_retry(
            tutor,
            previous_response="original",
            validation=_validation(["tutor_incoherent"]),
            combined_judge_result=None,
            turn_metadata={},
            student_input="hi",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            judge_runner=lambda t: CombinedJudgeResult(corrected_response=t),
            score_fn=lambda jr: (0.0, True),
            max_cycles=2,
        )
        self.assertEqual(result.text, "original")
        self.assertFalse(result.clean)
        self.assertEqual(result.cycles_run, 0)

    def test_clean_first_cycle_short_circuits(self):
        tutor = _tutor_with_tool_message("fixed response")
        judge_calls = []

        def _judge(text):
            judge_calls.append(text)
            return CombinedJudgeResult(corrected_response=text)

        result = run_tutor_self_retry(
            tutor,
            previous_response="bad",
            validation=_validation(["tutor_incoherent"]),
            combined_judge_result=None,
            turn_metadata={},
            student_input="hi",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            judge_runner=_judge,
            score_fn=lambda jr: (0.0, True),
            max_cycles=2,
        )
        self.assertTrue(result.clean)
        self.assertEqual(result.text, "fixed response")
        self.assertEqual(result.cycles_run, 1)
        self.assertEqual(len(judge_calls), 1)
        # LLM was called exactly once
        self.assertEqual(tutor.llm_client.generate_with_tools.call_count, 1)

    def test_dirty_all_cycles_picks_best_of_three(self):
        """No clean candidate; loop picks the highest-scoring of
        {previous, cycle_1, cycle_2}. STOCK_FALLBACK is no longer used."""
        tutor = _tutor_with_tool_message("retry text")
        # Score: previous=-3.0, cycle_1/cycle_2=-1.0 → retry wins
        result = run_tutor_self_retry(
            tutor,
            previous_response="bad original",
            validation=_validation(["tutor_incoherent"]),
            combined_judge_result=CombinedJudgeResult(corrected_response="bad original"),
            turn_metadata={},
            student_input="hi",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            judge_runner=lambda t: CombinedJudgeResult(corrected_response=t),
            score_fn=lambda jr: (-1.0 if jr.corrected_response == "retry text" else -3.0, False),
            max_cycles=2,
        )
        self.assertEqual(result.cycles_run, 2)
        self.assertFalse(result.clean)
        self.assertFalse(result.fallback_used)
        # Retry text wins on score
        self.assertEqual(result.text, "retry text")

    def test_previous_wins_when_retries_score_lower(self):
        """When previous_response scores HIGHER than all retry cycles
        (e.g. retries leak or judge worse), ship previous_response."""
        tutor = _tutor_with_tool_message("worse retry")
        result = run_tutor_self_retry(
            tutor,
            previous_response="not great but ok",
            validation=_validation(["tutor_incoherent"]),
            combined_judge_result=CombinedJudgeResult(corrected_response="not great but ok"),
            turn_metadata={},
            student_input="hi",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            judge_runner=lambda t: CombinedJudgeResult(corrected_response=t),
            # Previous scores -1.0, retries score -5.0 → previous wins
            score_fn=lambda jr: (
                -1.0 if jr.corrected_response == "not great but ok" else -5.0,
                False,
            ),
            max_cycles=2,
        )
        self.assertEqual(result.text, "not great but ok")
        self.assertFalse(result.fallback_used)
        # Engine state was restored to pre-retry snapshot (previous_response's)
        self.assertIsNone(tutor._awaiting_answer)

    def test_all_failures_ship_previous(self):
        """When every retry cycle errors, ship previous_response.
        STOCK_FALLBACK is no longer used (pilot directive 2026-05-17)."""
        tutor = MagicMock()
        tutor.session.id = 7
        tutor.llm_client = MagicMock()
        tutor.llm_client.config.model_name = "broken"
        tutor.llm_client.generate_with_tools.side_effect = RuntimeError("API down")
        tutor._awaiting_answer = None

        result = run_tutor_self_retry(
            tutor,
            previous_response="bad but real",
            validation=_validation(["tutor_incoherent"]),
            combined_judge_result=CombinedJudgeResult(corrected_response="bad but real"),
            turn_metadata={'bank_question_ref': {'id': 1}},
            student_input="hi",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            judge_runner=lambda t: CombinedJudgeResult(corrected_response=t),
            score_fn=lambda jr: (0.0, True),
            max_cycles=2,
        )
        # Both cycles errored → only previous in the pool → ship previous
        self.assertEqual(result.text, "bad but real")
        self.assertFalse(result.fallback_used)

    def test_clears_engine_state_between_cycles(self):
        """Each cycle starts with cleared bank_question_ref + awaiting
        so the retry's tool calls (or lack thereof) are authoritative."""
        tutor = _tutor_with_tool_message("retry response")
        # Pre-populate with stale state from the original turn
        turn_metadata = {
            'bank_question_ref': {'id': 1234, 'kind': 'exit_ticket_question'},
            'inline_authored_question': {'question': 'old', 'answer_key': 'old'},
            'tool_use_count': 1,
            'bank_rendered': True,
        }
        tutor._awaiting_answer = {'kind': 'exit_ticket_question', 'question_id': 1234}

        run_tutor_self_retry(
            tutor,
            previous_response="bad",
            validation=_validation(["tutor_incoherent"]),
            combined_judge_result=None,
            turn_metadata=turn_metadata,
            student_input="hi",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            judge_runner=lambda t: CombinedJudgeResult(corrected_response=t),
            score_fn=lambda jr: (0.0, True),
            max_cycles=1,
        )
        # After retry (no tool call from the mock), engine state was
        # cleared. _handle_pose_question_message was called — if it
        # had set anything, it'd be back; here it returns plain text
        # so the cleared state holds.
        self.assertIsNone(tutor._awaiting_answer)
        self.assertNotIn('bank_question_ref', turn_metadata)
        self.assertNotIn('inline_authored_question', turn_metadata)


# ---------------------------------------------------------------
# Leaked-tool-call detection
# ---------------------------------------------------------------

class DetectLeakedToolCallTest(SimpleTestCase):
    def test_xml_tool_use_block_detected(self):
        text = (
            "Some prose.\n\n<tool_use>\n<invoke name=\"pose_inline_question\">\n"
            "<parameter name=\"answer_key\">secret</parameter>\n</invoke>\n</tool_use>"
        )
        self.assertEqual(_detect_leaked_tool_call(text), "xml_tool_use_block")

    def test_invoke_tag_detected(self):
        text = "Some prose <invoke name='pose_question'> ... </invoke>"
        self.assertEqual(_detect_leaked_tool_call(text), "xml_tool_use_block")

    def test_function_call_syntax_detected(self):
        text = "Some prose pose_question(slot=2) more text"
        self.assertEqual(_detect_leaked_tool_call(text), "function_call_syntax")

    def test_clean_prose_returns_none(self):
        text = "What feature of a map shows direction?"
        self.assertIsNone(_detect_leaked_tool_call(text))

    def test_word_pose_question_alone_does_not_trip(self):
        # The function-call pattern requires `(` after — bare word
        # "pose_question" in prose is fine.
        text = "I'll pose_question soon."  # no `(`
        self.assertIsNone(_detect_leaked_tool_call(text))

    def test_empty_text_returns_none(self):
        self.assertIsNone(_detect_leaked_tool_call(""))
        self.assertIsNone(_detect_leaked_tool_call(None))


class LeakedToolCallDiscardsCycleTest(SimpleTestCase):
    def test_xml_leak_cycle_is_discarded(self):
        """When the LLM types <tool_use> as text and no real tool call
        fires (tool_use_count==0), the cycle is marked dirty + skipped
        without going through the judge runner."""
        leaky_text = (
            "Hint here.\n\n<tool_use>\n<invoke name=\"pose_inline_question\">"
            "\n<parameter name=\"answer_key\">leak</parameter>\n</invoke>\n</tool_use>"
        )
        tutor = _tutor_with_tool_message(leaky_text)
        judge_called = []

        def _judge(t):
            judge_called.append(t)
            return CombinedJudgeResult(corrected_response=t)

        result = run_tutor_self_retry(
            tutor,
            previous_response="bad",
            validation=_validation(["tutor_incoherent"]),
            combined_judge_result=None,
            turn_metadata={},
            student_input="hi",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            judge_runner=_judge,
            score_fn=lambda jr: (0.0, True),
            max_cycles=2,
        )
        # Judge never ran (cycle was discarded pre-judge)
        self.assertEqual(judge_called, [])
        self.assertEqual(result.cycles_run, 2)
        self.assertFalse(result.clean)
        # Two leaky cycles → both excluded from candidate pool →
        # previous_response wins (no STOCK_FALLBACK)
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.text, "bad")
        # Cycle error should mention the leak
        for c in result.cycles:
            self.assertIn("leaked_tool_call", c.error)


# ---------------------------------------------------------------
# Audit summary
# ---------------------------------------------------------------

class SummariseSelfRetryTest(SimpleTestCase):
    def test_shape(self):
        from ai_tutor.apps.tutoring.regen.self_retry import SelfRetryCycle

        r = SelfRetryResult(
            text="ok",
            picked_model="fake-opus",
            clean=True,
            cycles_run=1,
            fallback_used=False,
            cycles=[SelfRetryCycle(cycle=1, text="ok", clean=True, score=0.0)],
            elapsed_seconds=1.234,
        )
        s = summarise_self_retry(r)
        self.assertEqual(s['mechanism'], 'self_retry')
        self.assertEqual(s['picked_model'], 'fake-opus')
        self.assertTrue(s['clean'])
        self.assertEqual(s['cycles_run'], 1)
        self.assertEqual(s['elapsed_seconds'], 1.23)
        self.assertEqual(len(s['cycles']), 1)
        self.assertEqual(s['cycles'][0]['cycle'], 1)
        self.assertEqual(s['cycles'][0]['text_preview'], 'ok')
