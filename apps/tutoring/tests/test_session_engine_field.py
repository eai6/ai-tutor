"""M1 acceptance tests — TutorSession.engine + current_question_id.

See memory/simple_tutor_engine_milestones.md (M1).

Fixture shape mirrors apps/tutoring/tests/test_question_bank.py per the
testing-patterns-expert guidance: Course / Unit / Lesson use
`order_index` (not `order`); Institution needs both `name` and `slug`.
"""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.auth import get_user_model

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import TutorSession

User = get_user_model()


_counter = {'n': 0}


def _make_session(**kwargs):
    """Build a TutorSession with the minimum FK chain. Each call gets a
    unique institution/user so tests can run multiple times in the same
    TestCase without unique-constraint collisions.
    """
    _counter['n'] += 1
    i = _counter['n']
    institution = Institution.objects.create(name=f'Test School {i}', slug=f'test-{i}')
    user = User.objects.create_user(username=f'student-{i}', password='x')
    course = Course.objects.create(
        title=f'Test Course {i}',
        institution=institution,
        grade_level='S3',
        is_published=True,
    )
    unit = Unit.objects.create(course=course, title='Test Unit', order_index=0)
    lesson = Lesson.objects.create(
        unit=unit, title='Test Lesson', objective='x',
        order_index=0, is_published=True,
    )
    return TutorSession.objects.create(
        institution=institution,
        student=user,
        lesson=lesson,
        **kwargs,
    )


class EngineFieldDefaultsTest(TestCase):
    """Field defaults — existing sessions must be unaffected."""

    def test_default_engine_is_v1(self):
        session = _make_session()
        self.assertEqual(session.engine, 'v1')

    def test_current_question_id_default_is_none(self):
        session = _make_session()
        self.assertIsNone(session.current_question_id)

    def test_explicit_simple_engine(self):
        session = _make_session(engine='simple')
        self.assertEqual(session.engine, 'simple')


class EngineFieldValidationTest(TestCase):
    """Engine choice validation rejects unknown values.

    Uses ``clean_fields(exclude=...)`` to scope validation to just the
    ``engine`` field — ``full_clean`` would trip on unrelated required
    fields (prompt_pack, model_config) that aren't set in our minimal
    fixture.
    """

    _EXCLUDE = [
        f.name for f in TutorSession._meta.get_fields()
        if f.name != 'engine' and hasattr(f, 'attname')
    ]

    def test_unknown_engine_choice_fails(self):
        session = _make_session()
        session.engine = 'gpt5'  # not in choices
        with self.assertRaises(ValidationError) as cm:
            session.clean_fields(exclude=self._EXCLUDE)
        # Defensive — make sure the error is about engine, not something else.
        self.assertIn('engine', cm.exception.message_dict)

    def test_valid_choices_pass(self):
        for value in ['v1', 'simple']:
            session = _make_session()
            session.engine = value
            session.clean_fields(exclude=self._EXCLUDE)  # should not raise


class CurrentQuestionIdLifecycleTest(TestCase):
    """current_question_id can be set + cleared."""

    def test_set_and_clear(self):
        session = _make_session()
        session.current_question_id = 42
        session.save(update_fields=['current_question_id'])
        session.refresh_from_db()
        self.assertEqual(session.current_question_id, 42)

        session.current_question_id = None
        session.save(update_fields=['current_question_id'])
        session.refresh_from_db()
        self.assertIsNone(session.current_question_id)


class EngineFieldQueryTest(TestCase):
    """Querying by engine works and respects the db_index."""

    def test_filter_by_engine_simple(self):
        v1 = _make_session(engine='v1')
        simple = _make_session(engine='simple')

        results = list(TutorSession.objects.filter(engine='simple'))
        self.assertEqual(results, [simple])

        results = list(TutorSession.objects.filter(engine='v1'))
        self.assertEqual(results, [v1])

    def test_engine_choices_class_helper(self):
        # Verifies Engine.SIMPLE matches the stored value (no typos).
        session = _make_session(engine=TutorSession.Engine.SIMPLE)
        self.assertEqual(session.engine, 'simple')


class MathGradingDepsImportableTest(TestCase):
    """M1 acceptance — math-verify + latex2sympy2_extended are installed.

    The grader (M3) will use these; importing them is part of M1's
    acceptance so a future deploy that drops them fails loudly.
    """

    def test_math_verify_importable(self):
        import math_verify  # noqa: F401

    def test_latex2sympy2_extended_importable(self):
        import latex2sympy2_extended  # noqa: F401
