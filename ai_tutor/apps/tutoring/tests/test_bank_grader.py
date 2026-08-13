"""Tests for the deterministic bank grader (P3).

Platform-wide rule: LLM never calculates correct answers. The grader
compares student input to schema values that already exist on the
question record.
"""

from types import SimpleNamespace
from unittest import TestCase

from ai_tutor.apps.tutoring.bank_grader import grade_bank_response


def _q(**kwargs):
    """Build a duck-typed ExitTicketQuestion for tests."""
    defaults = {
        "question_type": "mcq",
        "correct_answer": "",
        "option_a": "", "option_b": "", "option_c": "", "option_d": "",
        "answer_data": None,
        "expected_answer": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class GradeMCQTest(TestCase):
    def test_correct_letter(self):
        q = _q(question_type="mcq", correct_answer="B",
               option_a="100°", option_b="115°", option_c="120°", option_d="125°")
        r = grade_bank_response(q, "B")
        self.assertTrue(r.is_correct)
        self.assertEqual(r.expected, "B")

    def test_wrong_letter(self):
        q = _q(question_type="mcq", correct_answer="B",
               option_a="100°", option_b="115°", option_c="120°", option_d="125°")
        r = grade_bank_response(q, "A")
        self.assertFalse(r.is_correct)

    def test_letter_lowercase_with_punctuation(self):
        q = _q(question_type="mcq", correct_answer="B",
               option_a="100°", option_b="115°", option_c="120°", option_d="125°")
        r = grade_bank_response(q, "(b).")
        self.assertTrue(r.is_correct)

    def test_full_text_match_with_unit_drift(self):
        q = _q(question_type="mcq", correct_answer="B",
               option_a="100°", option_b="115°", option_c="120°", option_d="125°")
        # Student typed "115" (no degree) but B is "115°"
        r = grade_bank_response(q, "115")
        self.assertTrue(r.is_correct)
        self.assertEqual(r.student_parsed, "B")

    def test_no_correct_answer_skips(self):
        q = _q(question_type="mcq", correct_answer="")
        r = grade_bank_response(q, "A")
        self.assertIsNone(r.is_correct)
        self.assertEqual(r.skip_reason, "mcq_no_correct_answer")


class GradeNumericTest(TestCase):
    def test_correct_via_computed(self):
        q = _q(question_type="short_numeric",
               answer_data={"computed": 195, "model_answer": "195°", "unit": "°"})
        r = grade_bank_response(q, "195")
        self.assertTrue(r.is_correct)

    def test_correct_with_unit(self):
        q = _q(question_type="short_numeric",
               answer_data={"computed": 195, "model_answer": "195°"})
        r = grade_bank_response(q, "195°")
        self.assertTrue(r.is_correct)

    def test_wrong_value(self):
        q = _q(question_type="short_numeric",
               answer_data={"computed": 195, "model_answer": "195°"})
        r = grade_bank_response(q, "200")
        self.assertFalse(r.is_correct)

    def test_falls_back_to_lessonstep_expected_answer(self):
        # Mirrors the LessonStep fallback path
        q = _q(question_type="short_numeric", answer_data={}, expected_answer="42")
        r = grade_bank_response(q, "42")
        self.assertTrue(r.is_correct)


class GradeFillBlankTest(TestCase):
    def test_all_blanks_correct(self):
        q = _q(question_type="fill_in_blank",
               answer_data={"blanks": ["195°", "360°"]})
        r = grade_bank_response(q, "195, 360")
        self.assertTrue(r.is_correct)

    def test_one_blank_wrong(self):
        q = _q(question_type="fill_in_blank",
               answer_data={"blanks": ["195°", "360°"]})
        r = grade_bank_response(q, "195, 350")
        self.assertFalse(r.is_correct)
        per_blank = r.detail["per_blank"]
        self.assertTrue(per_blank[0]["is_correct"])
        self.assertFalse(per_blank[1]["is_correct"])

    def test_list_input_accepted(self):
        q = _q(question_type="fill_in_blank",
               answer_data={"blanks": ["195°", "360°"]})
        r = grade_bank_response(q, ["195", "360"])
        self.assertTrue(r.is_correct)

    def test_missing_trailing_blank_counts_wrong(self):
        q = _q(question_type="fill_in_blank",
               answer_data={"blanks": ["195°", "360°"]})
        r = grade_bank_response(q, "195")
        self.assertFalse(r.is_correct)


class GradeMatchingTest(TestCase):
    def test_all_pairs_correct(self):
        q = _q(question_type="matching",
               answer_data={"pairs": [
                   {"left": "30 + 45", "right": "75°"},
                   {"left": "60 + 90", "right": "150°"},
               ]})
        r = grade_bank_response(q, [
            {"left": "30 + 45", "right": "75°"},
            {"left": "60 + 90", "right": "150°"},
        ])
        self.assertTrue(r.is_correct)

    def test_one_wrong_pair(self):
        q = _q(question_type="matching",
               answer_data={"pairs": [
                   {"left": "30 + 45", "right": "75°"},
                   {"left": "60 + 90", "right": "150°"},
               ]})
        r = grade_bank_response(q, [
            {"left": "30 + 45", "right": "75°"},
            {"left": "60 + 90", "right": "100°"},  # wrong
        ])
        self.assertFalse(r.is_correct)

    def test_string_input_with_arrows(self):
        q = _q(question_type="matching",
               answer_data={"pairs": [
                   {"left": "30 + 45", "right": "75°"},
                   {"left": "60 + 90", "right": "150°"},
               ]})
        r = grade_bank_response(
            q,
            "30 + 45 -> 75\n60 + 90 -> 150",
        )
        self.assertTrue(r.is_correct)

    def test_skipped_pair_counts_wrong(self):
        q = _q(question_type="matching",
               answer_data={"pairs": [
                   {"left": "30 + 45", "right": "75°"},
                   {"left": "60 + 90", "right": "150°"},
               ]})
        r = grade_bank_response(q, [{"left": "30 + 45", "right": "75°"}])
        self.assertFalse(r.is_correct)


class GradeShortAnswerTest(TestCase):
    def test_correct_final_answer_string(self):
        q = _q(question_type="short_answer",
               answer_data={"model_answer": "195°", "canonical_working": "..."})
        r = grade_bank_response(q, "195")
        self.assertTrue(r.is_correct)

    def test_dict_input_with_final_answer(self):
        q = _q(question_type="short_answer",
               answer_data={"model_answer": "195°"})
        r = grade_bank_response(q, {"final_answer": "195°", "working": "x"})
        self.assertTrue(r.is_correct)

    def test_wrong_final_answer(self):
        q = _q(question_type="short_answer",
               answer_data={"model_answer": "195°"})
        r = grade_bank_response(q, "200")
        self.assertFalse(r.is_correct)

    def test_working_grading_deferred(self):
        """short_answer's working field is LLM-reviewed elsewhere —
        the grader only judges the final-answer field."""
        q = _q(question_type="short_answer",
               answer_data={"model_answer": "195°"})
        r = grade_bank_response(q, "195")
        self.assertEqual(r.detail["working_grading"], "deferred_to_llm_review")


class GradeEdgeCasesTest(TestCase):
    def test_empty_input_skipped(self):
        q = _q(question_type="mcq", correct_answer="A")
        r = grade_bank_response(q, "")
        self.assertIsNone(r.is_correct)
        self.assertEqual(r.skip_reason, "empty_student_input")

    def test_unknown_question_type_skipped(self):
        q = _q(question_type="totally_made_up")
        r = grade_bank_response(q, "anything")
        self.assertIsNone(r.is_correct)
        self.assertTrue(r.skip_reason.startswith("unknown_type:"))

    def test_no_question_skipped(self):
        r = grade_bank_response(None, "x")
        self.assertIsNone(r.is_correct)
