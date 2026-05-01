"""Tests for the thinking-leak strip pattern (P6).

The LLM sometimes verbalises its own plan as the first sentence of a
response — internal monologue that should never reach the student.
The regex in conversational_tutor._THINKING_LEAK_RE drops these
opening sentences as defense-in-depth (a prompt rule asks the model
not to emit them in the first place).
"""

from django.test import SimpleTestCase

from apps.tutoring.conversational_tutor import _THINKING_LEAK_RE


class ThinkingLeakStripTest(SimpleTestCase):
    def _strip(self, text: str) -> str:
        return _THINKING_LEAK_RE.sub('', text, count=1)

    # ------------------------------------------------------------------
    # Patterns that MUST be stripped — observed in production transcripts
    # ------------------------------------------------------------------

    def test_strips_i_need_to_opening(self):
        text = (
            "I need to address the student's incorrect warmup answer first, "
            "then move into the current step.\n\n"
            "Not quite. Angles on a straight line sum to 180°."
        )
        result = self._strip(text)
        self.assertNotIn("I need to address", result)
        self.assertIn("Not quite", result)

    def test_strips_let_me_first_opening(self):
        text = (
            "Let me first clarify the difference for you. "
            "Corresponding angles are in the same position."
        )
        result = self._strip(text)
        self.assertNotIn("Let me first clarify", result)
        self.assertIn("Corresponding angles", result)

    def test_strips_first_ill_opening(self):
        text = (
            "First, I'll address what you got wrong.\n"
            "The answer is actually 180."
        )
        result = self._strip(text)
        self.assertNotIn("First, I'll address", result)
        self.assertIn("The answer is", result)

    def test_strips_my_plan_opening(self):
        text = (
            "My plan is to walk you through this step by step. "
            "Let's look at angle 1."
        )
        result = self._strip(text)
        self.assertNotIn("My plan is", result)
        self.assertIn("Let's look at", result)

    def test_strips_im_going_to_opening(self):
        text = "I am going to start with the basics. Angles on a line sum to 180°."
        result = self._strip(text)
        self.assertNotIn("I am going to", result)
        self.assertIn("Angles on a line", result)

    def test_strips_let_me_address_opening(self):
        text = "Let me address the warmup error. The sum is 180, not 360."
        result = self._strip(text)
        self.assertNotIn("Let me address", result)
        self.assertIn("The sum is 180", result)

    # ------------------------------------------------------------------
    # Patterns that MUST NOT be stripped — legitimate teacher voice
    # ------------------------------------------------------------------

    def test_does_not_strip_actually(self):
        text = "Actually, angles 1 and 5 are corresponding angles."
        self.assertEqual(self._strip(text), text)

    def test_does_not_strip_question(self):
        text = "What rule do you think applies here?"
        self.assertEqual(self._strip(text), text)

    def test_does_not_strip_lets_check(self):
        # "Let's check" is teacher voice (collaborative), not planning.
        # Only "Let me first/start/think/plan" trigger.
        text = "Let's check that calculation together."
        self.assertEqual(self._strip(text), text)

    def test_does_not_strip_great_open(self):
        text = "Great thinking. The next step is to add 95 + 70."
        self.assertEqual(self._strip(text), text)

    def test_does_not_strip_walk_me_through(self):
        text = "Walk me through how you got 220."
        self.assertEqual(self._strip(text), text)

    def test_does_not_strip_imperative_show(self):
        text = "Show me your steps."
        self.assertEqual(self._strip(text), text)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_only_strips_first_sentence(self):
        # If a meta-narration phrase appears later in the response,
        # we leave it alone — only the OPENING is the issue.
        text = (
            "Good thinking. I need to point out one thing: "
            "the sum is 180, not 360."
        )
        result = self._strip(text)
        # First sentence ("Good thinking.") survives unchanged
        self.assertIn("Good thinking", result)

    def test_handles_response_with_only_meta_sentence(self):
        text = "I need to address this carefully."
        result = self._strip(text)
        # Strip leaves an empty/whitespace string — caller's .strip() cleans
        self.assertEqual(result.strip(), "")

    def test_case_insensitive(self):
        text = "i need to clarify. The answer is 90°."
        result = self._strip(text)
        self.assertNotIn("i need to clarify", result.lower())
        self.assertIn("90°", result)
