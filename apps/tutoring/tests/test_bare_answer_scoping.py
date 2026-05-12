"""Tests for step-type + count scoping on the bare-answer "show working" rule.

Production transcript (2026-05-12, session 251): the tutor was
asking "how did you calculate 200 ÷ 25?" and "how did you calculate
8 × 25?" on elementary sub-steps of a worked example. The student
was being interrogated on every single arithmetic operation. The
math_teaching principle says "ask ONCE — in your own words — for
their reasoning. Do not drip-feed step-by-step follow-ups across
multiple turns; that's interrogation, not teaching." The
_build_math_eval_signal_block was enforcing show-working
unconditionally regardless of step_type or prior probing.

New scoping:
  - teach / worked_example / summary → guided walkthrough; the
    calculation IS the working. Don't demand show-working.
  - practice / quiz, first bare answer in step → ask ONCE for
    working.
  - practice / quiz, second+ bare answer in same step → accept
    and continue. No repeated probing.
"""
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from apps.tutoring.conversational_tutor import ConversationalTutor
from apps.tutoring.grader import MathCheckResult


def _bind_tutor():
    tutor = object.__new__(ConversationalTutor)
    return tutor


def _correct_check(expected="8", student="8") -> MathCheckResult:
    r = MathCheckResult(
        is_correct=True, student_parsed=student,
        expected_parsed=expected, reasoning="matches",
    )
    return r


def _wrong_check(expected="8", student="40") -> MathCheckResult:
    return MathCheckResult(
        is_correct=False, student_parsed=student,
        expected_parsed=expected, reasoning="does not match",
    )


class GuidedStepBareAnswerTest(SimpleTestCase):
    """Inside teach/worked_example/summary, bare answers don't trigger
    the show-working interrogation. Confirm + advance."""

    def test_guided_correct_bare_answer_does_not_demand_working(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="8",
            bare_answer=True, bare_answer_count_for_step=0,
            step_type='worked_example',
        )
        # Should NOT instruct the LLM to ask for working
        self.assertNotIn("walk you through each step", block)
        self.assertNotIn("MUST NOT say 'correct'", block)
        # SHOULD instruct it to confirm briefly + advance
        self.assertIn("Confirm briefly", block)
        self.assertIn("calculation IS the working", block)

    def test_guided_incorrect_bare_answer_gives_short_hint(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _wrong_check(), student_input="40",
            bare_answer=True, bare_answer_count_for_step=0,
            step_type='teach',
        )
        self.assertIn("specific arithmetic error", block)
        self.assertIn("short hint", block)
        # Should NOT instruct LLM to ask the student to walk through each step
        # (i.e. trigger the show-working interrogation).
        self.assertNotIn("ask them to walk you through each step", block)
        self.assertNotIn("walk you through each step they took", block)

    def test_guided_correct_non_bare_still_advances(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="200/25 = 8",
            bare_answer=False, bare_answer_count_for_step=0,
            step_type='worked_example',
        )
        self.assertIn("guided walkthrough", block)
        # No bare-answer interrogation
        self.assertNotIn("MUST NOT say", block)


class PracticeStepBareAnswerCountedTest(SimpleTestCase):
    """On practice/quiz, ask ONCE on first bare answer; relax on subsequent."""

    def test_practice_first_bare_answer_asks_for_working(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="8",
            bare_answer=True, bare_answer_count_for_step=0,
            step_type='practice',
        )
        self.assertIn("BARE numeric answer", block)
        self.assertIn("MUST NOT say 'correct'", block)
        # The "ask once" guarantee
        self.assertIn("ONLY turn this step where you ask for working", block)
        # Step-level probe guidance — explicit GOOD vs BAD probes
        self.assertIn("STEP-LEVEL probe", block)
        self.assertIn("GOOD probes", block)
        self.assertIn("BAD probes", block)
        # Specifically calls out the value-level interrogation pattern
        self.assertIn("calculate 50 / 10", block)

    def test_practice_subsequent_bare_answer_does_not_re_ask(self):
        tutor = _bind_tutor()
        # bare_answer_count_for_step=1 means this is the second bare
        # answer in the step (counter was already incremented).
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="8",
            bare_answer=True, bare_answer_count_for_step=1,
            step_type='practice',
        )
        self.assertIn("probe already fired earlier", block)
        self.assertIn("Confirm correctness briefly and advance", block)
        self.assertNotIn("walk you through each step", block)

    def test_practice_subsequent_bare_wrong_gets_hint_not_interrogation(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _wrong_check(), student_input="40",
            bare_answer=True, bare_answer_count_for_step=2,
            step_type='quiz',
        )
        self.assertIn("Name the specific arithmetic", block)
        self.assertIn("short hint", block)
        self.assertNotIn(
            "Echo the student's answer back verbatim", block,
        )

    def test_practice_first_bare_wrong_still_routes_to_wrong_branch(self):
        """A wrong-and-bare on practice still hits the wrong-answer
        block (which has its own show-working text). This test guards
        that we didn't accidentally make all wrong-bare cases silent."""
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _wrong_check(), student_input="40",
            bare_answer=True, bare_answer_count_for_step=0,
            step_type='practice',
        )
        # First bare on practice (regardless of correctness) goes through
        # the standard show-working branch.
        self.assertIn("BARE numeric answer", block)


class NonBareAnswerStillRespectsStepType(SimpleTestCase):
    """When the student showed working (non-bare), the rule is moot —
    but the guided-step branch should still emit lighter guidance."""

    def test_guided_correct_with_working_uses_guided_text(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="200 / 25 = 8",
            bare_answer=False, bare_answer_count_for_step=0,
            step_type='teach',
        )
        self.assertIn("guided walkthrough", block)

    def test_practice_correct_with_working_keeps_existing_text(self):
        tutor = _bind_tutor()
        block = tutor._build_math_eval_signal_block(
            _correct_check(), student_input="200 / 25 = 8",
            bare_answer=False, bare_answer_count_for_step=0,
            step_type='practice',
        )
        # Existing branch for "correct + non-bare + practice"
        self.assertIn("ask them to walk you through", block)
