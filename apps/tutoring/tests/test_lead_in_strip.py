"""Tests for the pose_question lead_in defensive strip.

Production session 252 (2026-05-12) showed the tutor putting an
AUTHORED word problem into the pose_question tool's lead_in field
while also posing a verified MCQ from the BANK — the student saw
TWO questions in one turn. The tool's schema description tells the
LLM "lead_in is a transition, not a question" but the LLM ignored
it.

_looks_like_authored_question detects the failure mode (numeric
setup, question verbs, '?' termination, very long) so the engine
can drop the bad lead_in before composing the final tutor turn.
"""
from django.test import SimpleTestCase

from apps.tutoring.conversational_tutor import _looks_like_authored_question


class LeadInDetectorTest(SimpleTestCase):
    # Benign transitions — must NOT be flagged.
    def test_short_transition_kept(self):
        self.assertFalse(_looks_like_authored_question("Try this:"))
        self.assertFalse(_looks_like_authored_question("Now apply that."))
        self.assertFalse(_looks_like_authored_question("Here's one more."))
        self.assertFalse(_looks_like_authored_question(
            "Let's check your understanding.",
        ))
        self.assertFalse(_looks_like_authored_question("Right — try this:"))
        self.assertFalse(_looks_like_authored_question(""))
        self.assertFalse(_looks_like_authored_question("   "))

    def test_medium_friendly_lead_in_kept(self):
        # No numbers, no question verb, no '?', under 120 chars.
        self.assertFalse(_looks_like_authored_question(
            "Now that you understand the rule, let's apply it.",
        ))

    # Production failures — MUST be flagged.
    def test_boat_word_problem_flagged(self):
        bad = (
            "A boat travelling between two Seychelles islands covers a "
            "distance in three equal legs. If each leg is x km and the "
            "total distance is 48 km, write and solve the equation."
        )
        self.assertTrue(_looks_like_authored_question(bad))

    def test_short_question_verb_flagged(self):
        # "Solve for x." is short but carries a question verb.
        self.assertTrue(_looks_like_authored_question("Solve for x."))

    def test_authored_question_ending_in_question_mark(self):
        self.assertTrue(_looks_like_authored_question(
            "What's the total distance?",
        ))

    def test_numeric_setup_flagged(self):
        # Numbers with units → authored math content.
        self.assertTrue(_looks_like_authored_question(
            "Here's the setup: 48 km in three legs.",
        ))
        self.assertTrue(_looks_like_authored_question(
            "The angle is 95° and the side is 70°.",
        ))
        self.assertTrue(_looks_like_authored_question(
            "If 5x + 20 = 35:",
        ))

    def test_long_lead_in_flagged_even_without_other_signals(self):
        # Transitions don't need 130+ chars.
        very_long = (
            "Now that you understand the rule about angles and applying "
            "the inverse operation when needed for problems, here's a "
            "thing to try."
        )
        # No numbers, no question verb, no '?'. Length > 120.
        self.assertGreater(len(very_long), 120)
        self.assertTrue(_looks_like_authored_question(very_long))

    def test_find_x_phrasing_flagged(self):
        self.assertTrue(_looks_like_authored_question(
            "Try this — find x in the equation.",
        ))

    def test_calculate_phrasing_flagged(self):
        self.assertTrue(_looks_like_authored_question(
            "Calculate the missing value.",
        ))
