"""Tests for the server-side probe-on-correct stripper.

Pilot directive (2026-05-12): "probing should stop. It should only
probe when the student got the question wrong, not when they are
correct."

The system prompt + eval-signal block try to suppress probing at
generation time. This is the post-generation backstop: if the LLM
still emits "How did you solve…?" after a correct answer, the server
strips the sentence before the response reaches the student.
"""
from django.test import SimpleTestCase

from ai_tutor.apps.tutoring.conversational_tutor import _strip_probe_sentences


class StripProbeSentencesTest(SimpleTestCase):

    def test_strips_how_did_you_solve_question(self):
        text = "Correct! x = 5. How did you solve for x in that equation?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("How did you solve", out)
        self.assertIn("x = 5", out)

    def test_strips_walk_me_through(self):
        text = "Nice — that's right. Walk me through your steps."
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("Walk me through", out)

    def test_strips_what_was_your_reasoning(self):
        text = "Good! What was your reasoning for choosing A?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("What was your reasoning", out)

    def test_strips_how_did_you_approach(self):
        # The exact phrase from the user's pilot transcript.
        text = "How did you approach that subtraction problem?\n\nSolve: 5x + 20 = 35. What is x?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("How did you approach", out)
        self.assertIn("Solve: 5x + 20 = 35", out)

    def test_preserves_legitimate_followup_question(self):
        # A genuine new MCQ question must survive — it's not a probe.
        text = "Great! Try this one: Solve 3x = 12. What is x?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 0)
        self.assertIn("Solve 3x = 12", out)
        self.assertIn("What is x?", out)

    def test_preserves_unrelated_how_question(self):
        # "How do you know" is a probe. "How do bees make honey" is not.
        text = "How do bees make honey? They collect nectar."
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 0)
        self.assertIn("How do bees make honey", out)

    def test_strips_multiple_probes(self):
        text = (
            "Correct! How did you get that? Walk me through your steps. "
            "Now let's try the next one."
        )
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 2)
        self.assertNotIn("How did you", out)
        self.assertNotIn("Walk me through", out)
        self.assertIn("next one", out)

    def test_empty_input_returns_empty(self):
        out, n = _strip_probe_sentences("")
        self.assertEqual(out, "")
        self.assertEqual(n, 0)

    def test_strips_can_you_walk_me_through(self):
        text = "Nice work! Can you walk me through how you got there?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("walk me through", out.lower())

    def test_strips_explain_your_thinking(self):
        text = "✓ Correct. Explain your thinking for that step."
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("Explain your thinking", out)

    def test_strips_what_equation_did_you(self):
        # Pilot 2026-05-12 transcript: tutor re-asked something the
        # student JUST answered in their previous turn.
        text = (
            "What equation did you set up to represent the postcard "
            "problem, and how did you decide to divide both sides?"
        )
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("What equation did you", out)

    def test_strips_how_did_you_decide(self):
        text = "Nice. How did you decide which operation to use?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("How did you decide", out)

    def test_strips_why_did_you_divide(self):
        text = "Correct. Why did you divide both sides by 25?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("Why did you divide", out)

    def test_strips_first_thing_you_noticed(self):
        # Direct from 2026-05-12 pilot transcript.
        text = "What was the first thing you noticed about how to solve this type of equation?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("first thing you noticed", out)

    def test_strips_what_did_you_notice(self):
        text = "Right. What did you notice about the pattern here?"
        out, n = _strip_probe_sentences(text)
        self.assertEqual(n, 1)
        self.assertNotIn("What did you notice", out)


class ScaffoldConsistencyPrincipleTest(SimpleTestCase):
    """Pilot 2026-05-12: tutor scaffolded with "x + 15 = 25" when the
    posed problem said the result was 40. The tutor system prompt
    must carry an explicit principle that scaffolds copy numbers
    verbatim from the posed problem."""

    def test_principle_present_in_system_prompt(self):
        import inspect
        from ai_tutor.apps.tutoring import conversational_tutor as mod
        source = inspect.getsource(mod)
        self.assertIn('id="scaffold_consistency"', source)
        self.assertIn("SCAFFOLD CONSISTENCY", source)
        # Both the wrong + right shapes are illustrated so the LLM
        # has a concrete contrast to learn from.
        self.assertIn("x + 15 = 25", source)
        self.assertIn("x + 15 = 40", source)
