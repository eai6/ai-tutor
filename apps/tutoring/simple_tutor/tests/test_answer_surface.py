"""The ``<answer_surface>`` block — what the hint ladder is allowed to be.

Every hint instruction we ship assumes the student can type back: "ask a
clarifying sub-question", "carry at most ONE micro-step per hint ... once the
student answers it", and both hint-vs-reveal examples are themselves questions.

Offline that is false. The student has four buttons and no text box, so device
session 30 produced this:

    tutor:  Not quite. [...] Now try this: what does the horizontal axis
            represent?
    buttons: A) The easting or horizontal distance   B) The northing or
             vertical distance   C) The scale of the map   D) The longitude
             lines

— four options belonging to the vertical-axis question, under a prompt asking
about the horizontal one. The student cannot answer what was asked and cannot
ask for clarification.

The block only helps if it appears exactly when the buttons do, which is why
``engine._uses_answer_picker`` is the single predicate behind both.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.llm.models import ModelConfig
from apps.tutoring.models import InFlightQuestion, TutorSession
from apps.tutoring.simple_tutor.engine import (
    _answer_choices_payload, _uses_answer_picker,
)
from apps.tutoring.simple_tutor.prompts import (
    ANSWER_MODE_FREE_TEXT, ANSWER_MODE_PICKER, _render_in_flight_block,
)
from apps.tutoring.simple_tutor.tools import _resolve_student_choice

User = get_user_model()
_n = {'i': 0}

MARKER = '<answer_surface mode="letter_picker">'


def _slot(qtype='mcq', options=('north', 'south', 'east', 'west')):
    return SimpleNamespace(
        question_text='Which axis is the northing?',
        question_type=qtype,
        reference_answer='B',
        source='catalog',
        attempt_count=1,
        options=list(options),
        catalog_question_id=7,
    )


class AnswerSurfaceBlockTest(DjangoTestCase):
    """Rendering — does the instruction appear where it should."""

    def test_picker_mcq_gets_the_block(self):
        out = _render_in_flight_block(_slot(), ANSWER_MODE_PICKER)
        self.assertIn(MARKER, out)

    def test_free_text_never_gets_it(self):
        """Every hosted session. The ladder's sub-questions work fine when the
        student has a text box, and this is the default for a reason."""
        self.assertNotIn(MARKER, _render_in_flight_block(_slot()))
        self.assertNotIn(
            MARKER, _render_in_flight_block(_slot(), ANSWER_MODE_FREE_TEXT))

    def test_non_mcq_never_gets_it_even_offline(self):
        """A short-answer slot has no buttons to click, so the student is
        typing and the ladder applies as written."""
        out = _render_in_flight_block(
            _slot(qtype='short_answer'), ANSWER_MODE_PICKER)
        self.assertNotIn(MARKER, out)

    def test_no_options_means_no_block(self):
        """The frontend needs at least two options to render a picker. If it
        draws nothing, telling the tutor 'they can only tap' is a lie that
        would gag the hint for no reason."""
        out = _render_in_flight_block(_slot(options=()), ANSWER_MODE_PICKER)
        self.assertNotIn(MARKER, out)

    def test_it_forbids_asking_rather_than_only_describing_the_surface(self):
        """The failure was a well-formed sub-question, which the ladder
        explicitly asks for. Stating 'there is no text box' without also
        overriding that instruction leaves the model with two live rules."""
        out = _render_in_flight_block(_slot(), ANSWER_MODE_PICKER)
        self.assertIn('ask none', out)


class ResolveStudentChoiceTest(DjangoTestCase):
    """``student_choice`` on the record_answer result.

    Device session 30: the student clicked D ("It shows the compass direction
    between the two points") and was told "it doesn't help pick grid squares"
    — a refutation of option A. The tutor passed the letter in itself, and the
    options were in its prompt, but the lookup sits ~7,000 tokens up and the
    4B missed it. Resolving server-side removes the lookup instead of asking
    the model to be more careful with it.
    """

    OPTS = [
        'It helps you identify which grid square to use',
        'It converts the measured ruler distance into real-world distance',
        'It eliminates the need to read grid references',
        'It shows the compass direction between the two points',
    ]

    def test_the_session_30_case(self):
        self.assertEqual(
            _resolve_student_choice(self.OPTS, 'mcq', 'D'),
            {'letter': 'D', 'text': self.OPTS[3]},
        )

    def test_letter_forms_the_grader_already_accepts(self):
        for raw, letter in (('B', 'B'), ('Option C', 'C'), ('a.', 'A')):
            with self.subTest(raw=raw):
                got = _resolve_student_choice(self.OPTS, 'mcq', raw)
                self.assertEqual(got['letter'], letter)
                self.assertEqual(got['text'], self.OPTS['ABCD'.index(letter)])

    def test_prose_resolves_to_nothing_rather_than_a_guess(self):
        """Typed prose online. There is no option to name, and naming the
        wrong one is the failure being fixed — silence beats a guess."""
        self.assertIsNone(
            _resolve_student_choice(self.OPTS, 'mcq', 'the compass direction'))

    def test_letter_past_the_end_of_the_options(self):
        """A three-option question graded against 'D'. Indexing without the
        bound would either raise inside a tool handler or name nothing."""
        self.assertIsNone(_resolve_student_choice(self.OPTS[:3], 'mcq', 'D'))
        self.assertIsNone(_resolve_student_choice(self.OPTS, 'mcq', 'E'))

    def test_not_an_mcq(self):
        self.assertIsNone(_resolve_student_choice(self.OPTS, 'short_answer', 'D'))
        self.assertIsNone(_resolve_student_choice([], 'mcq', 'D'))


class UsesAnswerPickerTest(DjangoTestCase):
    """The shared predicate. If it and the frontend ever disagree, the bug is
    silent from either side — the tutor hints into a surface that isn't there,
    or stays gagged while the student has a text box."""

    def _setup(self, *, provider='local_ollama'):
        _n['i'] += 1
        i = _n['i']
        inst = Institution.objects.create(name=f'AS{i}', slug=f'as{i}')
        user = User.objects.create_user(username=f'stu-as-{i}', password='x')
        course = Course.objects.create(
            title=f'C{i}', institution=inst, grade_level='S3',
            is_published=True)
        unit = Unit.objects.create(course=course, title='U', order_index=0)
        lesson = Lesson.objects.create(
            unit=unit, title='L', objective='o', order_index=0,
            is_published=True)
        LessonStep.objects.create(
            lesson=lesson, order_index=0, phase='explain', teacher_script='s')
        ModelConfig.objects.create(
            provider=provider, purpose='tutoring',
            model_name='qwen3-4b-jetson' if provider == 'local_ollama'
            else 'claude-opus-4-7',
            is_active=True, institution=inst,
        )
        session = TutorSession.objects.create(
            institution=inst, student=user, lesson=lesson, engine='simple')
        return session

    def _live_mcq(self, session, **kw):
        return InFlightQuestion.objects.create(
            session=session,
            question_text=kw.get('text', 'Which axis?'),
            question_type=kw.get('qtype', 'mcq'),
            reference_answer='B',
            options=kw.get('options', ['north', 'south', 'east', 'west']),
        )

    def test_local_model_with_a_live_mcq(self):
        s = self._setup()
        slot = self._live_mcq(s)
        self.assertTrue(_uses_answer_picker(s, slot))
        self.assertIsNotNone(_answer_choices_payload(s))

    def test_cloud_model_never_uses_the_picker(self):
        """Typing is not broken online — a cloud tutor reads 'northing' as
        option B without difficulty, and taking the text box away would be a
        regression for every hosted student."""
        s = self._setup(provider='anthropic')
        slot = self._live_mcq(s)
        self.assertFalse(_uses_answer_picker(s, slot))
        self.assertIsNone(_answer_choices_payload(s))

    def test_no_live_question(self):
        s = self._setup()
        self.assertFalse(_uses_answer_picker(s, None))
        self.assertIsNone(_answer_choices_payload(s))

    def test_single_option_is_not_a_picker(self):
        s = self._setup()
        slot = self._live_mcq(s, options=['only one'])
        self.assertFalse(_uses_answer_picker(s, slot))
        self.assertIsNone(_answer_choices_payload(s))

    def test_prompt_and_buttons_agree_across_every_case(self):
        """The invariant that matters, asserted directly rather than inferred
        from the two sides passing separately."""
        for provider in ('local_ollama', 'anthropic'):
            for qtype in ('mcq', 'short_answer'):
                for options in (['a', 'b', 'c', 'd'], ['a'], []):
                    s = self._setup(provider=provider)
                    slot = self._live_mcq(s, qtype=qtype, options=options)
                    self.assertEqual(
                        _uses_answer_picker(s, slot),
                        _answer_choices_payload(s) is not None,
                        f'{provider}/{qtype}/{len(options)} options: the tutor '
                        f'and the student disagree about the answer surface',
                    )
