"""Tests for Layer 1 content-time arithmetic verification.

Coverage:
  - Auto-correct fields (teacher_script, hints, educational_content)
  - Detect-only fields (question, expected_answer, choices)
  - Audit shape and entry types
  - Empty / missing / non-string field handling
  - Exit-ticket question variant
  - has_unresolved_corrections helper
"""

from __future__ import annotations

import unittest

from apps.curriculum.content_verifier import (
    build_arithmetic_constraint_block,
    has_unresolved_corrections,
    verify_exit_ticket_question,
    verify_lesson_step,
)


# ============================================================================
# verify_lesson_step — auto-correct fields
# ============================================================================


class TestVerifyLessonStepAutoCorrect(unittest.TestCase):
    def test_teacher_script_with_wrong_arithmetic_corrected(self):
        step = {
            "order_index": 3,
            "teacher_script": (
                "Step 5: Check 60 + 80 + 75 + 70 + 75 = 220 ✓ — looks good!"
            ),
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        # The wrong sum is rewritten in place.
        self.assertIn("= 360", step["teacher_script"])
        self.assertNotIn("220", step["teacher_script"])
        # Audit captures it as auto-corrected.
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["step_order"], 3)
        self.assertEqual(audit[0]["field"], "teacher_script")
        self.assertTrue(audit[0]["auto_corrected"])

    def test_correct_teacher_script_passes_through(self):
        step = {"order_index": 0, "teacher_script": "60 + 80 = 140 is correct."}
        audit: list = []
        verify_lesson_step(step, audit=audit)
        self.assertEqual(audit, [])
        self.assertEqual(step["teacher_script"], "60 + 80 = 140 is correct.")

    def test_hints_list_each_verified(self):
        step = {
            "order_index": 0,
            "hints": [
                "Try 5 + 3 = 9 first.",   # wrong, should be 8
                "Then 8 × 2 = 16.",         # correct
                "Finally check: 16 + 4 = 30.",  # wrong, should be 20
            ],
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        # Two corrections expected — hints[0] and hints[2].
        self.assertEqual(len(audit), 2)
        fields = {e["field"] for e in audit}
        self.assertEqual(fields, {"hints[0]", "hints[2]"})
        # In-place rewrite confirmed.
        self.assertIn("= 8", step["hints"][0])
        self.assertIn("= 20", step["hints"][2])

    def test_educational_content_string_field_corrected(self):
        step = {
            "order_index": 0,
            "educational_content": {
                "worked_example": "Add 60 + 80 + 75 + 70 + 75 = 220 to verify.",
            },
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        self.assertIn(
            "= 360", step["educational_content"]["worked_example"]
        )
        self.assertEqual(len(audit), 1)
        self.assertEqual(
            audit[0]["field"], "educational_content.worked_example"
        )

    def test_educational_content_list_field_walks_each(self):
        step = {
            "order_index": 0,
            "educational_content": {
                "common_mistakes": [
                    "Don't write 95 + 70 = 175 — that's wrong.",  # 165
                    "Always check your sums.",
                ],
            },
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        # Only the first list item had bad math.
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["field"], "educational_content.common_mistakes[0]")
        self.assertIn("= 165", step["educational_content"]["common_mistakes"][0])


# ============================================================================
# verify_lesson_step — detect-only fields
# ============================================================================


class TestVerifyLessonStepDetectOnly(unittest.TestCase):
    def test_question_with_wrong_arithmetic_NOT_rewritten(self):
        # The question stem has a baked-in arithmetic claim that the
        # answer key depends on. Rewriting only one side desyncs the
        # pair — Layer 1 records but does NOT auto-fix.
        step = {
            "order_index": 2,
            "question": "If 60 + 80 + 75 + 70 + 75 = 220, what is x?",
            "expected_answer": "85",
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        # The question text is unchanged.
        self.assertIn("220", step["question"])
        self.assertNotIn("360", step["question"])
        # But the audit has an entry, marked detect-only.
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["field"], "question")
        self.assertFalse(audit[0]["auto_corrected"])

    def test_expected_answer_detect_only(self):
        step = {
            "order_index": 2,
            "expected_answer": "8 × 2 = 20",  # wrong, should be 16
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        # Not rewritten.
        self.assertEqual(step["expected_answer"], "8 × 2 = 20")
        # Recorded.
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["field"], "expected_answer")
        self.assertFalse(audit[0]["auto_corrected"])

    def test_choices_detect_only(self):
        step = {
            "order_index": 1,
            "choices": [
                "A) 60 + 80 + 75 + 70 = 220",  # 285
                "B) 285",
                "C) 360",
                "D) None of the above",
            ],
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        # Choice A still says 220 (not auto-rewritten).
        self.assertIn("220", step["choices"][0])
        # But the audit has an entry.
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["field"], "choices[0]")
        self.assertFalse(audit[0]["auto_corrected"])


# ============================================================================
# verify_lesson_step — edge cases
# ============================================================================


class TestVerifyLessonStepEdgeCases(unittest.TestCase):
    def test_empty_step_no_corrections(self):
        step = {"order_index": 0}
        audit: list = []
        verify_lesson_step(step, audit=audit)
        self.assertEqual(audit, [])

    def test_none_field_handled(self):
        step = {
            "order_index": 0,
            "teacher_script": None,
            "hints": None,
            "educational_content": None,
            "question": None,
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        # No crash, no spurious corrections.
        self.assertEqual(audit, [])

    def test_non_string_hint_skipped(self):
        step = {
            "order_index": 0,
            "hints": ["3 + 4 = 8", 42, None, "5 × 5 = 25"],
        }
        audit: list = []
        verify_lesson_step(step, audit=audit)
        # Only hints[0] gets a correction; others passed through.
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["field"], "hints[0]")
        # Non-string elements preserved.
        self.assertEqual(step["hints"][1], 42)

    def test_returns_step_data_for_chaining(self):
        step = {"order_index": 0, "teacher_script": "3 + 4 = 7"}
        audit: list = []
        result = verify_lesson_step(step, audit=audit)
        self.assertIs(result, step)


# ============================================================================
# verify_exit_ticket_question
# ============================================================================


class TestVerifyExitTicketQuestion(unittest.TestCase):
    def test_explanation_auto_corrected(self):
        q = {
            "explanation": "We compute 60 + 80 + 75 = 220 then subtract.",
        }
        audit: list = []
        verify_exit_ticket_question(q, question_index=2, audit=audit)
        self.assertIn("= 215", q["explanation"])  # 60+80+75=215
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["question_index"], 2)
        self.assertEqual(audit[0]["field"], "explanation")
        self.assertTrue(audit[0]["auto_corrected"])

    def test_question_text_detect_only(self):
        q = {
            "question_text": "Three angles around a point: 95° + 70° + 110° = 285°. Find x.",
            "correct_answer": "75",  # would be 75 if sum is 285, but real sum is 275
        }
        audit: list = []
        verify_exit_ticket_question(q, question_index=4, audit=audit)
        # Stem not rewritten.
        self.assertIn("285", q["question_text"])
        # Recorded for Layer 3.
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["field"], "question_text")
        self.assertFalse(audit[0]["auto_corrected"])

    def test_options_detect_only(self):
        # An option that quotes a full <expr>=<num> WITH a wrong claim
        # should land in the audit as detect-only. Plain numeric
        # options without an equals-claim don't trigger the regex —
        # that's by design (the verifier's job is arithmetic checking,
        # not value range).
        q = {
            "option_a": "A) 60 + 80 + 75 + 70 + 75 = 220",  # wrong, 360
            "option_b": "B) 360",
            "option_c": "C) Cannot determine",
            "option_d": "D) None of the above",
            "question_text": "Which of these is correct?",
        }
        audit: list = []
        verify_exit_ticket_question(q, question_index=0, audit=audit)
        fields = {e["field"] for e in audit}
        # option_a quotes wrong arithmetic — recorded as detect-only.
        self.assertIn("option_a", fields)
        a_entry = next(e for e in audit if e["field"] == "option_a")
        self.assertFalse(a_entry["auto_corrected"])
        # Original option text is unchanged (detect-only, no rewrite).
        self.assertIn("220", q["option_a"])


# ============================================================================
# has_unresolved_corrections
# ============================================================================


class TestHasUnresolvedCorrections(unittest.TestCase):
    def test_empty_audit_returns_false(self):
        self.assertFalse(has_unresolved_corrections([]))

    def test_only_auto_corrected_returns_false(self):
        audit = [
            {"field": "teacher_script", "auto_corrected": True, "corrections": []},
            {"field": "hints[0]", "auto_corrected": True, "corrections": []},
        ]
        self.assertFalse(has_unresolved_corrections(audit))

    def test_any_detect_only_returns_true(self):
        audit = [
            {"field": "teacher_script", "auto_corrected": True, "corrections": []},
            {"field": "question", "auto_corrected": False, "corrections": []},
        ]
        self.assertTrue(has_unresolved_corrections(audit))


# ============================================================================
# Layer 3 — build_arithmetic_constraint_block (C1)
# ============================================================================


class TestBuildArithmeticConstraintBlock(unittest.TestCase):
    def test_empty_inputs_return_empty_string(self):
        self.assertEqual(build_arithmetic_constraint_block(), "")
        self.assertEqual(build_arithmetic_constraint_block([], []), "")
        # Auto-corrected entries don't trigger a constraint (they're
        # already silently fixed in place).
        audit = [{
            "step_order": 0,
            "field": "teacher_script",
            "auto_corrected": True,
            "corrections": [],
        }]
        self.assertEqual(build_arithmetic_constraint_block(audit, []), "")

    def test_detect_only_steps_listed(self):
        audit = [{
            "step_order": 2,
            "field": "question",
            "auto_corrected": False,
            "corrections": [
                {"expression": "60 + 80 + 75 + 70 + 75",
                 "claimed": "220", "correct": "360"},
            ],
        }]
        block = build_arithmetic_constraint_block(audit, [])
        self.assertIn("ARITHMETIC ERRORS", block)
        self.assertIn("Step 3", block)  # 1-indexed in output
        self.assertIn("question", block)
        self.assertIn("220", block)
        self.assertIn("360", block)
        self.assertIn("correct value is 360", block)
        # Closing instruction is present so the LLM knows it should
        # regenerate, not just acknowledge.
        self.assertIn("Re-verify EVERY arithmetic claim", block)

    def test_answer_key_mismatches_listed(self):
        mismatches = [{
            "question_index": 4,
            "pattern": "sum",
            "computed": 275.0,
            "claimed": 165.0,
            "reason": "...",
        }]
        block = build_arithmetic_constraint_block([], mismatches)
        self.assertIn("Q5", block)  # 1-indexed
        self.assertIn("pattern sum", block)
        self.assertIn("275", block)
        self.assertIn("165", block)

    def test_both_inputs_combined(self):
        audit = [{"step_order": 0, "field": "question", "auto_corrected": False,
                  "corrections": [{"expression": "1 + 2", "claimed": "4", "correct": "3"}]}]
        mismatches = [{"question_index": 0, "pattern": "sum",
                       "computed": 10.0, "claimed": 5.0, "reason": ""}]
        block = build_arithmetic_constraint_block(audit, mismatches)
        # Both sections appear.
        self.assertIn("step content", block.lower())
        self.assertIn("exit-ticket questions", block.lower())


if __name__ == "__main__":
    unittest.main()
