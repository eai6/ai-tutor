"""Tests for P1.1-P1.5 Curriculum Intelligence features."""

from unittest.mock import MagicMock, patch
from django.test import TestCase

from apps.curriculum.models import LessonStep, SeychellesContext
from apps.tutoring.models import ExitTicketQuestion
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


# ==========================================================================
# P1.1 + P1.2 — Enabling Objectives
# ==========================================================================

class TestEnablingObjectivesSchema(BaseTutoringTestCase):
    """Test that enabling objectives fields exist and work."""

    def test_unit_has_enabling_objectives_field(self):
        self.lesson.unit.enabling_objectives = ["EO1: Define GNP", "EO2: Explain HDI"]
        self.lesson.unit.save()
        self.lesson.unit.refresh_from_db()
        self.assertEqual(len(self.lesson.unit.enabling_objectives), 2)

    def test_unit_has_terminal_objectives_field(self):
        self.lesson.unit.terminal_objectives = ["TO1: Understand development"]
        self.lesson.unit.save()
        self.lesson.unit.refresh_from_db()
        self.assertEqual(len(self.lesson.unit.terminal_objectives), 1)

    def test_lesson_has_enabling_objectives_field(self):
        self.lesson.enabling_objectives = ["Define GNP", "State why GNP is in USD"]
        self.lesson.save()
        self.lesson.refresh_from_db()
        self.assertEqual(len(self.lesson.enabling_objectives), 2)

    def test_lesson_step_has_enabling_objective_field(self):
        step = LessonStep.objects.filter(lesson=self.lesson).first()
        if step:
            step.enabling_objective = "Define GNP"
            step.save()
            step.refresh_from_db()
            self.assertEqual(step.enabling_objective, "Define GNP")


class TestEnablingObjectivesCoverage(BaseTutoringTestCase):
    """Test objective-based coverage tracking in tutor engine."""

    def _make_tutor(self):
        from apps.tutoring.conversational_tutor import ConversationalTutor
        session = self._create_session(engine_state={})
        return ConversationalTutor(session)

    def test_load_enabling_objectives_from_lesson(self):
        self.lesson.enabling_objectives = ["Define GNP", "Explain HDI"]
        self.lesson.save()
        tutor = self._make_tutor()
        self.assertEqual(len(tutor.enabling_objectives), 2)

    def test_load_enabling_objectives_from_steps(self):
        """Objectives from step-level fields are included."""
        steps = LessonStep.objects.filter(lesson=self.lesson).order_by('order_index')
        if steps.exists():
            step = steps.first()
            step.enabling_objective = "Step-level objective"
            step.save()
        tutor = self._make_tutor()
        objectives = [o['objective'] for o in tutor.enabling_objectives]
        self.assertIn("Step-level objective", objectives)

    def test_empty_objectives_graceful_fallback(self):
        """Old lessons with no enabling_objectives still produce a usable
        list — the canonical helper falls back to lesson.objective then
        lesson.title (see apps/curriculum/content_generator.py::
        combined_objectives_for_lesson). The contract is "never empty",
        not "empty when EOs are unset" — the matrix and summative
        tagging both depend on this guarantee."""
        self.lesson.enabling_objectives = []
        self.lesson.save()
        # Also clear step-level EOs so the only signal left is the
        # lesson's own objective/title fallback.
        LessonStep.objects.filter(lesson=self.lesson).update(enabling_objective='')
        tutor = self._make_tutor()
        # The objective fallback must surface — non-empty list, single entry.
        self.assertGreaterEqual(len(tutor.enabling_objectives), 1)
        objs = [o['objective'] for o in tutor.enabling_objectives]
        self.assertIn(self.lesson.objective, objs)

    def test_objective_coverage_state_persistence(self):
        """Covered objectives should persist in engine_state."""
        self.lesson.enabling_objectives = ["OBJ1", "OBJ2"]
        self.lesson.save()
        tutor = self._make_tutor()
        tutor.enabling_objectives[0]['covered'] = True
        tutor._save_state()
        session = tutor.session
        session.refresh_from_db()
        self.assertIn("OBJ1", session.engine_state.get('covered_objectives', []))

    def test_enabling_objectives_block_not_empty(self):
        """Prompt block should include objectives when present."""
        self.lesson.enabling_objectives = ["Define GNP"]
        self.lesson.save()
        tutor = self._make_tutor()
        block = tutor._build_enabling_objectives_block()
        self.assertIn("ENABLING OBJECTIVES", block)
        self.assertIn("Define GNP", block)

    def test_enabling_objectives_block_empty_when_no_objectives(self):
        """Block is empty only when every fallback (lesson EOs, lesson
        objective, lesson title, every step's enabling_objective) is
        also blank. With the canonical-helper contract there's no other
        way to get an empty block."""
        self.lesson.enabling_objectives = []
        self.lesson.objective = ''
        self.lesson.title = ''
        self.lesson.save()
        LessonStep.objects.filter(lesson=self.lesson).update(enabling_objective='')
        tutor = self._make_tutor()
        block = tutor._build_enabling_objectives_block()
        self.assertEqual(block, "")


# ==========================================================================
# P1.3 — Multi-Format Exit Tickets
# ==========================================================================

class TestExitTicketQuestionTypes(BaseTutoringTestCase):
    """Test multi-format exit ticket question types."""

    def _get_or_create_exit_ticket(self):
        from apps.tutoring.models import ExitTicket
        et, _ = ExitTicket.objects.get_or_create(lesson=self.lesson)
        return et

    def test_mcq_question_type_default(self):
        et = self._get_or_create_exit_ticket()
        q = ExitTicketQuestion.objects.create(
            exit_ticket=et,
            question_text="What is GNP?",
            option_a="A", option_b="B", option_c="C", option_d="D",
            correct_answer="A",
        )
        self.assertEqual(q.question_type, 'mcq')

    def test_fill_in_blank_question(self):
        et = self._get_or_create_exit_ticket()
        q = ExitTicketQuestion.objects.create(
            exit_ticket=et,
            question_type='fill_in_blank',
            question_text="Complete the sentence:",
            answer_data={
                'text_template': 'The ___ is measured in ___.',
                'blanks': ['GNP', 'US dollars'],
                'accept_alternatives': [['gross national product'], ['USD']],
            },
        )
        self.assertEqual(q.question_type, 'fill_in_blank')
        self.assertEqual(len(q.answer_data['blanks']), 2)

    def test_matching_question(self):
        et = self._get_or_create_exit_ticket()
        q = ExitTicketQuestion.objects.create(
            exit_ticket=et,
            question_type='matching',
            question_text="Match terms:",
            answer_data={
                'pairs': [{'left': 'GNP', 'right': 'Total value'}],
                'distractor_rights': ['Population'],
            },
        )
        self.assertEqual(q.question_type, 'matching')

    def test_short_answer_question(self):
        et = self._get_or_create_exit_ticket()
        q = ExitTicketQuestion.objects.create(
            exit_ticket=et,
            question_type='short_answer',
            question_text="Explain why HDI is better than GNP.",
            answer_data={
                'model_answer': 'HDI includes health, education, income.',
                'keywords': ['health', 'education', 'income'],
                'min_keywords': 2,
            },
        )
        self.assertEqual(q.question_type, 'short_answer')


class TestExitTicketGrading(BaseTutoringTestCase):
    """Test _grade_exit_question for each question type."""

    def _make_tutor(self):
        from apps.tutoring.conversational_tutor import ConversationalTutor
        session = self._create_session(engine_state={})
        return ConversationalTutor(session)

    def test_grade_mcq_correct(self):
        tutor = self._make_tutor()
        q = MagicMock(question_type='mcq', correct_answer='B')
        self.assertTrue(tutor._grade_exit_question(q, 'B'))

    def test_grade_mcq_incorrect(self):
        tutor = self._make_tutor()
        q = MagicMock(question_type='mcq', correct_answer='B')
        self.assertFalse(tutor._grade_exit_question(q, 'C'))

    def test_grade_mcq_case_insensitive(self):
        tutor = self._make_tutor()
        q = MagicMock(question_type='mcq', correct_answer='B')
        self.assertTrue(tutor._grade_exit_question(q, 'b'))

    def test_grade_fill_in_blank_correct(self):
        tutor = self._make_tutor()
        q = MagicMock(
            question_type='fill_in_blank',
            answer_data={
                'blanks': ['GNP', 'US dollars'],
                'accept_alternatives': [['gross national product'], ['USD']],
            },
        )
        self.assertTrue(tutor._grade_exit_question(q, ['GNP', 'US dollars']))

    def test_grade_fill_in_blank_alternatives(self):
        tutor = self._make_tutor()
        q = MagicMock(
            question_type='fill_in_blank',
            answer_data={
                'blanks': ['GNP'],
                'accept_alternatives': [['gross national product']],
            },
        )
        self.assertTrue(tutor._grade_exit_question(q, ['gross national product']))

    def test_grade_fill_in_blank_wrong(self):
        tutor = self._make_tutor()
        q = MagicMock(
            question_type='fill_in_blank',
            answer_data={'blanks': ['GNP'], 'accept_alternatives': [[]]},
        )
        self.assertFalse(tutor._grade_exit_question(q, ['GDP']))

    def test_grade_matching_correct(self):
        tutor = self._make_tutor()
        q = MagicMock(
            question_type='matching',
            answer_data={
                'pairs': [{'left': 'GNP', 'right': 'Total value'}, {'left': 'HDI', 'right': 'Development index'}],
            },
        )
        self.assertTrue(tutor._grade_exit_question(q, {'GNP': 'Total value', 'HDI': 'Development index'}))

    def test_grade_matching_wrong(self):
        tutor = self._make_tutor()
        q = MagicMock(
            question_type='matching',
            answer_data={
                'pairs': [{'left': 'GNP', 'right': 'Total value'}],
            },
        )
        self.assertFalse(tutor._grade_exit_question(q, {'GNP': 'Wrong'}))

    def test_grade_short_answer_correct(self):
        tutor = self._make_tutor()
        q = MagicMock(
            question_type='short_answer',
            answer_data={
                'keywords': ['health', 'education', 'income'],
                'min_keywords': 2,
            },
        )
        self.assertTrue(tutor._grade_exit_question(q, 'HDI includes health and education measures'))

    def test_grade_short_answer_insufficient_keywords(self):
        tutor = self._make_tutor()
        q = MagicMock(
            question_type='short_answer',
            answer_data={
                'keywords': ['health', 'education', 'income'],
                'min_keywords': 2,
            },
        )
        self.assertFalse(tutor._grade_exit_question(q, 'it is a number'))

    def test_grade_data_interpretation(self):
        tutor = self._make_tutor()
        q = MagicMock(
            question_type='data_interpretation',
            answer_data={
                'keywords': ['Country C', 'highest HDI'],
                'min_keywords': 2,
            },
        )
        self.assertTrue(tutor._grade_exit_question(q, 'Country C has the highest HDI'))


# ==========================================================================
# P1.4 — Content Quality Tier
# ==========================================================================

class TestContentQualityTier(BaseTutoringTestCase):
    """Test content quality tier field and auto-detection."""

    def test_lesson_has_content_quality_field(self):
        self.lesson.content_quality = 'tier_1'
        self.lesson.save()
        self.lesson.refresh_from_db()
        self.assertEqual(self.lesson.content_quality, 'tier_1')

    def test_lesson_default_tier(self):
        from apps.curriculum.models import Lesson
        lesson = Lesson.objects.create(
            unit=self.lesson.unit,
            title='Test Lesson',
            objective='Test',
        )
        self.assertEqual(lesson.content_quality, 'tier_3')

    def test_lesson_has_teacher_approved_field(self):
        self.lesson.teacher_approved = True
        self.lesson.save()
        self.lesson.refresh_from_db()
        self.assertTrue(self.lesson.teacher_approved)

    def test_determine_content_quality_tier_1(self):
        from apps.curriculum.content_generator import LessonContentGenerator
        gen = LessonContentGenerator.__new__(LessonContentGenerator)
        result = gen._determine_content_quality({
            'related_content': ['some textbook content'],
            'figure_descriptions': [{'description': 'a diagram'}],
            'objectives': ['learn stuff'],
        })
        self.assertEqual(result, 'tier_1')

    def test_determine_content_quality_tier_3(self):
        from apps.curriculum.content_generator import LessonContentGenerator
        gen = LessonContentGenerator.__new__(LessonContentGenerator)
        result = gen._determine_content_quality({
            'related_content': [],
            'objectives': ['learn stuff'],
        })
        self.assertEqual(result, 'tier_3')

    def test_determine_content_quality_tier_4(self):
        from apps.curriculum.content_generator import LessonContentGenerator
        gen = LessonContentGenerator.__new__(LessonContentGenerator)
        result = gen._determine_content_quality({
            'related_content': [],
            'objectives': [],
        })
        self.assertEqual(result, 'tier_4')


# ==========================================================================
# P1.5 — Seychelles Context Library
# ==========================================================================

class TestSeychellesContextLibrary(TestCase):
    """Test SeychellesContext model and queries."""

    def test_create_context_entry(self):
        entry = SeychellesContext.objects.create(
            category='economic',
            title='GDP',
            content='GNP is approximately $1.59 billion.',
            subject_tags=['geography', 'economics'],
            grade_levels=['S3'],
        )
        self.assertEqual(entry.category, 'economic')
        self.assertEqual(str(entry), '[Economic] GDP')

    def test_filter_by_subject_tag(self):
        # Clear any seeded data first to test in isolation
        SeychellesContext.objects.all().delete()
        SeychellesContext.objects.create(
            category='economic', title='GDP Test', content='...',
            subject_tags=['geography'],
        )
        SeychellesContext.objects.create(
            category='climate', title='Rainfall Test', content='...',
            subject_tags=['science'],
        )
        # Filter in Python since SQLite JSON contains varies
        all_entries = SeychellesContext.objects.all()
        geo = [e for e in all_entries if 'geography' in (e.subject_tags or [])]
        self.assertEqual(len(geo), 1)
        self.assertEqual(geo[0].title, 'GDP Test')

    def test_inactive_entries_excluded(self):
        SeychellesContext.objects.create(
            category='economic', title='Old Data', content='...',
            is_active=False,
        )
        active = SeychellesContext.objects.filter(is_active=True)
        old_count = active.filter(title='Old Data').count()
        self.assertEqual(old_count, 0)
