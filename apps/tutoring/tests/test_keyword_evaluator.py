"""Tests for the keyword-based answer evaluator fallback."""

from unittest.mock import patch, MagicMock
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class TestKeywordEvaluateResponse(BaseTutoringTestCase):
    """Test _keyword_evaluate_response handles positive/negative signals correctly."""

    def _get_tutor(self):
        from apps.tutoring.conversational_tutor import ConversationalTutor
        session = self._create_session()
        return ConversationalTutor(session)

    def test_negative_signal_overrides_positive(self):
        """'not quite right' should be False despite containing 'right'."""
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "Not quite right. Let's try again."
        )
        self.assertFalse(result["correct"])

    def test_not_correct_is_negative(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "That's not correct. Think about what happens at the boundary."
        )
        self.assertFalse(result["correct"])

    def test_try_again_is_negative(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "Hmm, try again. What type of boundary is it?"
        )
        self.assertFalse(result["correct"])

    def test_incorrect_is_negative(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "That answer is incorrect."
        )
        self.assertFalse(result["correct"])

    def test_think_again_is_negative(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "Think again — which plate boundary creates mountains?"
        )
        self.assertFalse(result["correct"])

    def test_positive_correct(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "That's correct! Well done."
        )
        self.assertTrue(result["correct"])

    def test_positive_exactly_right(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "Exactly right! Divergent boundaries form when plates move apart."
        )
        self.assertTrue(result["correct"])

    def test_positive_excellent(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "Excellent work! You got it."
        )
        self.assertTrue(result["correct"])

    def test_positive_perfect(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "Perfect — that's the right answer."
        )
        self.assertTrue(result["correct"])

    def test_no_signals_returns_false(self):
        """Ambiguous response with no clear signal defaults to False."""
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "Interesting answer. Can you tell me more about that?"
        )
        self.assertFalse(result["correct"])

    def test_lets_reconsider_is_negative(self):
        tutor = self._get_tutor()
        result = tutor._keyword_evaluate_response(
            "Let's reconsider. What happens when plates collide?"
        )
        self.assertFalse(result["correct"])
