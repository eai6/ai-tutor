"""Utility re-export tests — confirm the lifted-forward modules are
reachable through the v2 import surface and no behavior drift."""

from unittest import TestCase


class ReexportImportTest(TestCase):
    def test_bank_grader_reexport(self):
        from apps.tutoring.v2.utilities import (
            BankGradeResult,
            grade_bank_response,
            grade_lesson_step_response,
        )
        # Same callables as the source module — re-export, not copy.
        from apps.tutoring import bank_grader

        self.assertIs(grade_bank_response, bank_grader.grade_bank_response)
        self.assertIs(grade_lesson_step_response,
                      bank_grader.grade_lesson_step_response)
        self.assertIs(BankGradeResult, bank_grader.BankGradeResult)

    def test_question_module_reexport(self):
        from apps.tutoring.v2.utilities import Question
        from apps.tutoring.question import Question as SourceQuestion

        self.assertIs(Question, SourceQuestion)

    def test_repeated_question_reexport(self):
        from apps.tutoring.v2.utilities import repeated_question
        from apps.tutoring import repeated_question as src

        self.assertIs(repeated_question, src)

    def test_praise_filter_reexport(self):
        from apps.tutoring.v2.utilities import praise_filter
        from apps.tutoring import praise_filter as src

        self.assertIs(praise_filter, src)

    def test_answer_leak_reexport(self):
        from apps.tutoring.v2.utilities import answer_leak
        from apps.tutoring import answer_leak as src

        self.assertIs(answer_leak, src)

    def test_student_working_analyzer_reexport(self):
        from apps.tutoring.v2.utilities import student_working_analyzer
        from apps.tutoring import student_working_analyzer as src

        self.assertIs(student_working_analyzer, src)


class FlagAccessorTest(TestCase):
    """Confirm the centralized flag accessor (Phase 1 §6) routes
    correctly for the engine_version pick."""

    def test_select_engine_version_sticky(self):
        from apps.tutoring.v2.config.flags import (
            ENGINE_LEGACY,
            ENGINE_V2,
            select_engine_version,
        )

        self.assertEqual(select_engine_version(ENGINE_LEGACY), ENGINE_LEGACY)
        self.assertEqual(select_engine_version(ENGINE_V2), ENGINE_V2)

    def test_select_engine_version_default_off(self):
        from unittest.mock import patch
        from apps.tutoring.v2.config.flags import (
            ENGINE_LEGACY,
            select_engine_version,
        )

        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            self.assertEqual(select_engine_version(None), ENGINE_LEGACY)

    def test_select_engine_version_on(self):
        from unittest.mock import patch
        from apps.tutoring.v2.config.flags import (
            ENGINE_V2,
            select_engine_version,
        )

        with patch.dict("os.environ", {"NEW_TUTOR": "on"}):
            self.assertEqual(select_engine_version(None), ENGINE_V2)
