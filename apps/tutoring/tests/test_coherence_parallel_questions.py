"""Coherence judge now treats "two distinct questions in parallel"
as a coherence violation (structural scope). The regen repair
instruction has a dedicated BEFORE→AFTER example for this case.

Production session 252 (2026-05-12) — the tutor posed a
conceptual question ("what was the first thing you noticed?") AND
a separate MCQ from the bank in the same response. Two questions
to the student per turn breaks "one focused ask" pedagogy.
"""
from django.test import SimpleTestCase

from apps.tutoring.judges.coherence import _SYSTEM as COHERENCE_SYSTEM
from apps.tutoring.regen.prompt import build_regen_prompt


class CoherenceSystemPromptMentionsParallelQuestionsTest(SimpleTestCase):
    def test_structural_scope_named(self):
        # The system prompt must explicitly include the structural
        # scope so the LLM knows to flag it.
        self.assertIn("STRUCTURAL", COHERENCE_SYSTEM)
        self.assertIn("TWO OR MORE DISTINCT", COHERENCE_SYSTEM)

    def test_parallel_questions_example_in_violations_list(self):
        # The violations list must show a worked example of the
        # parallel-questions pattern so the LLM has a fix pattern.
        self.assertIn("two parallel questions", COHERENCE_SYSTEM)

    def test_single_followup_kept_as_non_violation(self):
        # A single follow-up question after explanation is normal
        # scaffolding — the prompt must say so explicitly to avoid
        # over-triggering.
        self.assertIn("SINGLE question", COHERENCE_SYSTEM)
        self.assertIn(
            "follow-up question after explaining", COHERENCE_SYSTEM,
        )


class RegenRepairHasParallelQuestionsExampleTest(SimpleTestCase):
    def test_parallel_question_violation_triggers_dual_question_repair(self):
        user_prompt, _ = build_regen_prompt(
            previous_response="X",
            issues=["tutor_incoherent"],
            validation_metadata={
                "coherence_violations": [
                    "two parallel questions: 'what was the first "
                    "thing you noticed?'; 'If x + 15 = 40, what is x?'",
                ],
            },
            bank_stems=[],
            student_input="something",
        )
        # The repair text must label this as the parallel-questions
        # case and show the canonical fix.
        self.assertIn("TUTOR_INCOHERENT (parallel questions)", user_prompt)
        self.assertIn("pick ONE question", user_prompt)
        self.assertIn("BEFORE", user_prompt)
        self.assertIn("AFTER", user_prompt)
        # The fix example specifically shows acknowledgment kept,
        # conceptual probe dropped, MCQ standalone.
        self.assertIn("acknowledgment kept", user_prompt)

    def test_other_incoherent_violation_uses_default_repair(self):
        # When the violation is about contradiction (not parallel
        # questions), the default tutor_incoherent repair fires with
        # its equation-switch example, NOT the parallel-questions
        # variant.
        user_prompt, _ = build_regen_prompt(
            previous_response="X",
            issues=["tutor_incoherent"],
            validation_metadata={
                "coherence_violations": [
                    "Changed equation from 5x + 20 = 35 to 3x + 20 = 80",
                ],
            },
            bank_stems=[],
            student_input="x",
        )
        self.assertNotIn("(parallel questions)", user_prompt)
        # The default tutor_incoherent example IS present
        self.assertIn("5x + 20 = 35", user_prompt)
