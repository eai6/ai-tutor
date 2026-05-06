"""Tests for the praise-filter defense-in-depth layer of the math-tutor
false-positive fix. See memory/math_tutor_fix_plan.md Phase M3."""

import unittest

from apps.tutoring.praise_filter import strip_praise_if_wrong


class TestStripPraiseIfWrong(unittest.TestCase):
    def test_passes_through_when_correct(self):
        text = "Brilliant, you've got it! 21/4 = 5 1/4."
        result, modified = strip_praise_if_wrong(text, is_correct=True)
        self.assertEqual(result, text)
        self.assertFalse(modified)

    def test_passes_through_when_none(self):
        text = "Brilliant, you've got it!"
        result, modified = strip_praise_if_wrong(text, is_correct=None)
        self.assertEqual(result, text)
        self.assertFalse(modified)

    def test_strips_single_praise(self):
        text = "Brilliant, Vaani! Let's try another one."
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertNotIn("Brilliant", result)
        # Rest of the text should survive
        self.assertIn("Let's try another one", result)

    def test_production_bug_exact_case(self):
        """The exact wrong response from the screenshot."""
        text = (
            "Brilliant, Vaani! You've got it — 21/4 = 5 1/4 kg. "
            "You correctly divided 21 by 4 to get 5 whole groups with 1 left over."
        )
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        result_lower = result.lower()
        self.assertNotIn("brilliant", result_lower)
        self.assertNotIn("you've got it", result_lower)
        self.assertNotIn("you got it", result_lower)

    def test_heavy_praise_replaces_first_sentence(self):
        text = "Brilliant, perfect, excellent work, exactly right! Now let's move on."
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        # Heavy praise should be replaced, not just stripped
        self.assertNotIn("Brilliant", result)
        self.assertNotIn("perfect", result.lower())
        # Replaced with one of the rotating "wrong" openers — they no
        # longer include the verbatim "Let's check this one together"
        # phrase that leaked in pilot. Just confirm the rest survives.
        self.assertIn("Now let's move on", result)
        # And the replacement opener is non-empty (not just stripped).
        self.assertGreater(len(result), len("Now let's move on"))

    def test_preserves_second_sentence_with_correct_in_context(self):
        """'The correct next step' in the explanation isn't praise."""
        text = (
            "You got it! The correct next step is to divide by the common factor."
        )
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertNotIn("You got it", result)
        # 'The correct next step' survives
        self.assertIn("correct next step", result)

    def test_no_praise_no_change(self):
        text = "Let me check that — can you show me how you got 3 3/4?"
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertFalse(modified)
        self.assertEqual(result, text)

    def test_sentence_starter_correct_with_exclamation(self):
        text = "Correct! 21/4 is indeed 5 1/4."
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertFalse(result.lower().startswith("correct"))

    def test_that_is_right(self):
        text = "That's right! You divided correctly."
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertNotIn("That's right", result)

    def test_yes_starter(self):
        text = "Yes! Perfect answer."
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        # Both 'Yes!' at start and 'Perfect' should be stripped
        self.assertFalse(result.lower().startswith("yes"))
        self.assertNotIn("Perfect", result)

    def test_empty_string(self):
        result, modified = strip_praise_if_wrong("", is_correct=False)
        self.assertEqual(result, "")
        self.assertFalse(modified)

    def test_praise_stripped_from_later_sentences_too(self):
        """Full-text scan: the production bug had praise in sentence 2
        ('Brilliant, Vaani! You've got it — ...'), so stripping must cover
        all sentences. Rare collisions like 'brilliant mathematician' in a
        later sentence are considered acceptable collateral."""
        text = (
            "Let's look at this. The answer 5 1/4 is brilliant math history."
        )
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertNotIn("brilliant", result.lower())
        self.assertIn("Let's look at this", result)
        self.assertIn("math history", result)

    def test_light_strip_preserves_content(self):
        """Medium-size first sentence with one praise word should survive
        most of its content."""
        text = "That's correct — you divided the fraction the right way."
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertNotIn("That's correct", result)
        # The rest of the sentence (content) should survive
        self.assertIn("divided", result)

    def test_checkmark_treated_as_praise(self):
        """A bare ✓ inline is the LLM affirming an answer; strip it so a
        wrong-arithmetic line like '60 + 80 + 75 + 70 + 75 = 220 ✓'
        loses the misleading checkmark."""
        text = "60 + 80 + 75 + 70 + 75 = 220 ✓"
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertNotIn("✓", result)

    def test_great_work_treated_as_praise(self):
        """'Great work' is praise-equivalent to 'great job' and was
        slipping past the original patterns."""
        text = "Great work, that's the right approach!"
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertNotIn("Great work", result)

    def test_bare_correct_uses_echo_opener(self):
        """When the student gave a bare-but-correct answer, the heavy-
        praise replacement should echo their answer back rather than
        use the generic 'walk me through' opener that contradicted the
        '✓ correct' that came later in the same response."""
        text = "Perfect! 275 is right!"
        result, modified = strip_praise_if_wrong(
            text,
            is_correct=False,
            context="bare_correct",
            student_input="275",
        )
        self.assertTrue(modified)
        self.assertNotIn("Let's check this one together", result)
        self.assertIn("275", result)

    def test_bare_unknown_uses_neutral_opener(self):
        """Bare answer with no canonical correctness signal — should
        ask for working without echoing or implying correctness."""
        text = "Excellent! Now let's check that."
        result, modified = strip_praise_if_wrong(
            text,
            is_correct=False,
            context="bare_unknown",
        )
        self.assertTrue(modified)
        self.assertNotIn("Excellent", result)
        # The bare_unknown openers (post-2026-05-06) all neutrally
        # request the working without claiming a verdict. Any of:
        # "How did you get there?", "What did you do first?",
        # "Show me your working before I confirm anything.",
        # "What's the working behind that?".
        self.assertTrue(
            any(
                phrase in result.lower()
                for phrase in (
                    "how did you get there",
                    "what did you do first",
                    "show me your working",
                    "working behind that",
                )
            ),
            f"opener missing expected neutral phrase: {result!r}",
        )

    def test_default_context_is_backward_compatible(self):
        """Callers that don't pass context still get an opener from
        the wrong-answer pool. The verbatim 'Let's check this one
        together' was removed 2026-05-06 because it leaked across
        pilot transcripts; we just verify the heavy praise was
        replaced with SOME non-empty opener."""
        text = "Brilliant, perfect, excellent!"
        result, modified = strip_praise_if_wrong(text, is_correct=False)
        self.assertTrue(modified)
        self.assertNotIn("Brilliant", result)
        self.assertNotIn("perfect", result.lower())
        # Forbidden phrases must not reappear.
        self.assertNotIn("Let's check this one together", result)
        # And we got a non-empty opener.
        self.assertTrue(result.strip())


if __name__ == "__main__":
    unittest.main()
