"""Tests for the bare-answer eval-signal branches.

Pilot directive (2026-05-12): "the system should just move on when a
correct answer is provided." All probing was disabled — every branch
now resolves to "confirm + advance" or "diagnose + advance," never
"ask for working / reasoning."

We test the INSTRUCTIONAL stance of the eval-signal text — that it
tells the LLM to CONFIRM + ADVANCE (or DIAGNOSE + ADVANCE) — rather
than grepping for banned literal phrases, because the new guidance
text now quotes some banned phrases as negative examples ("Do NOT
ask 'how did you get there?'") which would false-positive a naive
substring check.
"""
import re

from django.test import SimpleTestCase

from apps.tutoring.conversational_tutor import ConversationalTutor
from apps.tutoring.grader import MathCheckResult


def _bind_tutor():
    return object.__new__(ConversationalTutor)


def _correct_check(expected="8", student="8") -> MathCheckResult:
    return MathCheckResult(
        is_correct=True, student_parsed=student,
        expected_parsed=expected, reasoning="matches",
    )


def _wrong_check(expected="8", student="40") -> MathCheckResult:
    return MathCheckResult(
        is_correct=False, student_parsed=student,
        expected_parsed=expected, reasoning="does not match",
    )


def _has_positive_probing_instruction(block: str) -> bool:
    """True iff the block INSTRUCTS the LLM to probe (positive
    imperative), not just quotes a probe as a negative example.

    Strips quoted strings first so the negative examples in
    "Do NOT ask 'how did you get there?'" don't trip the matcher.
    """
    # Remove single + double + curly-quoted spans
    stripped = re.sub(r"'[^']*'", '', block)
    stripped = re.sub(r'"[^"]*"', '', stripped)
    stripped = re.sub(r'[‘’][^‘’]*[‘’]', '', stripped)
    stripped = re.sub(r'[“”][^“”]*[“”]', '', stripped)

    probing_instructions = [
        # Imperative "ask … walk through their steps" etc.
        r'\b(?:then\s+)?ask\s+(?:them\s+)?(?:to\s+)?(?:walk|explain|describe)',
        r'\b(?:then\s+)?ask\s+(?:them\s+)?for\s+(?:their\s+)?(?:reasoning|working)',
        # The legacy "STEP-LEVEL probe" instruction
        r'\bask\s+ONE\s+STEP-LEVEL\s+probe\b',
    ]
    for pat in probing_instructions:
        if re.search(pat, stripped, re.IGNORECASE):
            return True
    return False


class NoProbingOnCorrectAnswersTest(SimpleTestCase):
    """Every correct-answer branch — bare or with working, guided or
    practice — must produce eval-signal text that tells the LLM to
    CONFIRM + ADVANCE, never to probe."""

    def test_bare_correct_guided_step_confirms_no_probe(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="8",
            bare_answer=True, bare_answer_count_for_step=0,
            step_type='worked_example',
        )
        self.assertIn("Confirm briefly", block)
        self.assertIn("guided walkthrough", block)
        self.assertFalse(_has_positive_probing_instruction(block))

    def test_bare_correct_practice_step_advances_no_probe(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="8",
            bare_answer=True, bare_answer_count_for_step=0,
            step_type='practice',
        )
        self.assertIn("ADVANCE", block)
        # Rule 1 still applies — no praise on bare answer.
        self.assertIn("do NOT use", block)
        self.assertFalse(_has_positive_probing_instruction(block))

    def test_correct_with_working_practice_step_advances_no_probe(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="200 / 25 = 8",
            bare_answer=False, bare_answer_count_for_step=0,
            step_type='practice',
        )
        self.assertIn("ADVANCE", block)
        self.assertIn("CONFIRM", block)
        self.assertFalse(_has_positive_probing_instruction(block))

    def test_correct_with_working_guided_step_advances_no_probe(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="200 / 25 = 8",
            bare_answer=False, bare_answer_count_for_step=0,
            step_type='teach',
        )
        # Same correct + non-bare branch fires whether guided or not;
        # the message just confirms + advances either way.
        self.assertIn("ADVANCE", block)
        self.assertFalse(_has_positive_probing_instruction(block))


class WrongAnswerDiagnosesNeverProbesTest(SimpleTestCase):
    """Wrong answers get a diagnostic hint, not a probe."""

    def test_bare_wrong_practice_gets_diagnostic_hint(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _wrong_check(), student_input="40",
            bare_answer=True, bare_answer_count_for_step=0,
            step_type='practice',
        )
        self.assertIn("Name the specific error", block)
        self.assertIn("hint", block)
        self.assertFalse(_has_positive_probing_instruction(block))

    def test_wrong_with_working_practice_names_step(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _wrong_check(), student_input="200 / 25 = 40",
            bare_answer=False, bare_answer_count_for_step=0,
            step_type='practice',
        )
        self.assertIn("Name the specific step", block)
        self.assertIn("Diagnose", block)
        self.assertFalse(_has_positive_probing_instruction(block))

    def test_bare_wrong_guided_step_short_hint_no_escalation(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _wrong_check(), student_input="40",
            bare_answer=True, bare_answer_count_for_step=0,
            step_type='teach',
        )
        self.assertIn("short hint", block)
        self.assertIn("Don't escalate", block)
        self.assertFalse(_has_positive_probing_instruction(block))


class ProbeFrequencyPrincipleTest(SimpleTestCase):
    """The system-prompt principle <probe_frequency> must explicitly
    ban reasoning probes on correct answers."""

    def test_principle_text_says_no_probing(self):
        import inspect
        from apps.tutoring import conversational_tutor as mod
        source = inspect.getsource(mod)
        self.assertIn('id="probe_frequency"', source)
        self.assertIn("NO PROBING ON CORRECT ANSWERS — ADVANCE", source)
        self.assertIn('"How did you get there?"', source)
        self.assertIn('"What was your reasoning?"', source)
        self.assertIn('"Walk me through your steps."', source)
