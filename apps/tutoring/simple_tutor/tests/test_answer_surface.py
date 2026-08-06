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

import pathlib
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
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

    def test_it_states_the_surface_and_how_to_read_a_choice(self):
        """Cut to two lines on 2026-08-06.

        The long version also forbade asking anything, and stray hand-offs
        measured 0/8 with it. Without it they measured 1/6 — weak evidence,
        but the direction is real, and the mode block's "INCORRECT — hint, and
        pose nothing" is now the only thing carrying that rule.

        What the block must still do: name the surface, and say to resolve the
        student's choice by letter key. The second half is what stops the
        session-30 failure of refuting an option the student never picked.
        """
        out = _render_in_flight_block(_slot(), ANSWER_MODE_PICKER)
        self.assertIn('TAPPING', out)
        self.assertIn('letter key', out)


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


class RemediationPickerRepaintTest(DjangoTestCase):
    """The exit-ticket submit payload must carry answer_choices.

    Device session 81: the review text asked "two villages at 2543 and 3043 —
    which statement is correct?" while the buttons on screen still read
    "Locate northing 29 and mark where the lines intersect" — the question from
    before the quiz. It looked like the slot had not been reset.

    It had. `_remediation_opening_question` deletes the lesson's leftover slot
    and poses a fresh question, and the DB was right the whole time. What was
    missing is that submit_exit_ticket was the third payload builder and the
    only one that never carried answer_choices, so nothing told the frontend to
    repaint. The student read one question and was handed another one's
    options — worse than no picker, because it is confidently wrong.
    """

    def _lesson(self):
        _n['i'] += 1
        i = _n['i']
        from apps.tutoring.models import ExitTicket, ExitTicketQuestion
        inst = Institution.objects.create(name=f'RM{i}', slug=f'rm{i}')
        user = User.objects.create_user(username=f'stu-rm-{i}', password='x')
        course = Course.objects.create(title=f'C{i}', institution=inst,
                                       grade_level='S3', is_published=True)
        unit = Unit.objects.create(course=course, title='U', order_index=0)
        lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                       order_index=0, is_published=True)
        LessonStep.objects.create(lesson=lesson, order_index=0, phase='explain',
                                  teacher_script='s', enabling_objective='EO-1')
        ModelConfig.objects.create(
            provider='local_ollama', purpose='tutoring',
            model_name='qwen3-4b-jetson', is_active=True, institution=inst)
        # passing_score is an absolute count of questions, not a percent —
        # 3 of the 4 below.
        et = ExitTicket.objects.create(lesson=lesson, passing_score=3)
        qs = [
            ExitTicketQuestion.objects.create(
                exit_ticket=et, order_index=k, question_type='mcq',
                question_text=f'Q{k}?', enabling_objective='EO-1',
                option_a=f'a{k}', option_b=f'b{k}', option_c=f'c{k}',
                option_d=f'd{k}', correct_answer='A',
            ) for k in range(4)
        ]
        session = TutorSession.objects.create(
            institution=inst, student=user, lesson=lesson, engine='simple')
        session.engine_state = {'selected_exit_ticket_ids': [q.id for q in qs]}
        session.save(update_fields=['engine_state'])
        return session, qs

    def test_failed_ticket_repaints_the_picker_to_the_remediation_question(self):
        from apps.tutoring.simple_tutor.exit_ticket import submit_exit_ticket

        session, qs = self._lesson()
        # The state the last pre-quiz turn leaves behind: a live lesson slot.
        InFlightQuestion.objects.create(
            session=session, question_text='STALE — pre-quiz question',
            question_type='mcq', reference_answer='A',
            options=['stale-a', 'stale-b', 'stale-c', 'stale-d'])

        out = submit_exit_ticket(session, ['B'] * len(qs))   # fail every item
        self.assertFalse(out['is_complete'])
        self.assertIn('answer_choices', out,
                      'submit payload has no answer_choices — the frontend '
                      'cannot know the picker changed')

        letters = (out['answer_choices'] or {}).get('letters') or []
        texts = [c['text'] for c in letters]
        self.assertTrue(texts, 'remediation posed a question but offered no buttons')
        self.assertNotIn('stale-a', texts,
                         'the picker still shows the pre-quiz options')

        slot = InFlightQuestion.objects.filter(session=session).first()
        self.assertIsNotNone(slot)
        self.assertNotEqual(slot.question_text, 'STALE — pre-quiz question')
        self.assertEqual(texts, [str(o) for o in slot.options],
                         'the buttons and the live slot disagree')

    def test_passed_ticket_offers_no_picker(self):
        """Nothing is in flight after a pass, so the payload must say so rather
        than leaving the last question's buttons on screen."""
        from apps.tutoring.simple_tutor.exit_ticket import submit_exit_ticket

        session, qs = self._lesson()
        out = submit_exit_ticket(session, ['A'] * len(qs))   # pass every item
        self.assertTrue(out['is_complete'])
        self.assertIsNone(out.get('answer_choices'))


class EveryPayloadBuilderCarriesAnswerChoicesTest(SimpleTestCase):
    """No student-facing payload may omit ``answer_choices``.

    The picker is drawn from this key and from nothing else, so a builder that
    omits it does not draw an empty picker — it leaves whatever was on screen
    from the previous turn. That is how device session 81 showed the pre-quiz
    question's four options underneath the remediation question. Every letter
    the student could press was an answer to something nobody asked, and it
    would have graded against the live slot.

    Written against the SOURCE rather than by calling three known functions,
    because the bug was never in the builders that existed when the picker
    shipped — it was in the one nobody thought to check. A fourth builder
    added next year fails this test the moment it is written.
    """

    # Payload builders are recognised by returning a dict literal with a
    # 'message' key: that is the student-facing turn shape.
    MARKER_KEY = 'message'
    REQUIRED_KEY = 'answer_choices'

    # Documented exemptions. Add here WITH A REASON, never by loosening
    # MARKER_KEY — that would silently shrink this check to nothing.
    EXEMPT: set = {
        # Its own page with its own submit-and-redirect flow, not a chat turn.
        # The payload's 'message' goes into a results panel; the chat picker is
        # not on screen and there is nothing for answer_choices to control.
        'lesson_pretest',
    }

    # Line-level exemptions for non-turn responses inside a covered function.
    # Same rule: a reason, or it does not go in here.
    EXEMPT_LINES: set = {
        # views.py chat_start_session: a 400 {"error": "prerequisite_not_met"}.
        # The frontend surfaces it as an error, never as a tutor turn, so there
        # is no picker in play.
        'prerequisite_not_met',
    }

    def _builders(self):
        """[(file, line, func, keys)] for every payload-shaped return.

        Covers two shapes, because the bug that motivated the second one was
        invisible to the first: a bare ``return {...}`` in the engine, and a
        ``return JsonResponse({...})`` in the view. The view hand-listed four
        keys and dropped answer_choices on the way to the browser, so the
        engine was correct and the student still got no buttons.
        """
        import ast

        import apps.tutoring.simple_tutor as _pkg

        pkg = pathlib.Path(_pkg.__file__).resolve().parent
        paths = sorted(pkg.glob('*.py')) + [pkg.parent / 'views.py']
        found = []
        for path in paths:
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Return):
                        continue
                    value = sub.value
                    # Unwrap JsonResponse({...}) / Response({...}).
                    if (isinstance(value, ast.Call) and value.args
                            and isinstance(value.args[0], ast.Dict)):
                        value = value.args[0]
                    if not isinstance(value, ast.Dict):
                        continue
                    keys = {
                        k.value for k in value.keys
                        if isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                    }
                    if 'error' in keys and keys & {'unmet_prerequisites'}:
                        continue          # see EXEMPT_LINES
                    if self.MARKER_KEY in keys:
                        found.append((path.name, sub.lineno, node.name, keys))
        return found

    def test_the_scan_finds_the_builders_we_know_about(self):
        """Guards the guard. If the marker stops matching, every assertion
        below passes vacuously and the check is worthless."""
        names = {f for _, _, f, _ in self._builders()}
        for known in ('respond_for_view', '_project_start_payload',
                      'submit_exit_ticket', '_empty_payload'):
            self.assertIn(known, names,
                          f'{known} is no longer detected as a payload builder '
                          f'— the scan has stopped working, not the code')

    def test_every_builder_carries_answer_choices(self):
        missing = [
            f'{fname}:{line} {func}()'
            for fname, line, func, keys in self._builders()
            if func not in self.EXEMPT and self.REQUIRED_KEY not in keys
        ]
        self.assertEqual(
            missing, [],
            "these payload builders omit 'answer_choices', so the frontend "
            "keeps the previous turn's buttons on screen:\n  "
            + '\n  '.join(missing)
            + "\n\nSet it — engine._answer_choices_payload(session) when a "
              "question may be live, or None when nothing is in flight. "
              "Omitting it is not the same as None.",
        )


class RemediationQuestionPoolTest(DjangoTestCase):
    """build_question_pool must not return [] during remediation.

    Device session 83, after a failed exit ticket: current_step_index=5 with
    5 steps, so the LessonStep lookup missed and the pool came back empty.
    Nothing about that was visible from build_question_pool — the damage
    happened two layers away. pose_question(question_index=N) had no entry to
    select, so the only way the tutor could ask anything was to write a
    question in prose. Prose creates no slot, so nothing grades it, and
    offline the student gets no letter buttons either: the transcript showed
    "Now try this: What does the horizontal axis represent?" with no options
    and a typing box.

    The prompt licensed that ("or author your own"), which is why this needs
    both halves fixed — but the pool is the half that made prose the only
    option available.
    """

    def _failed_ticket_session(self):
        from apps.tutoring.models import (
            ExitTicket, ExitTicketAttempt, ExitTicketQuestion,
        )
        from django.utils import timezone

        _n['i'] += 1
        i = _n['i']
        inst = Institution.objects.create(name=f'RQ{i}', slug=f'rq{i}')
        user = User.objects.create_user(username=f'stu-rq-{i}', password='x')
        course = Course.objects.create(title=f'C{i}', institution=inst,
                                       grade_level='S3', is_published=True)
        unit = Unit.objects.create(course=course, title='U', order_index=0)
        lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                       order_index=0, is_published=True)
        # Two steps, and the session sits PAST both — the remediation state.
        for k in range(2):
            LessonStep.objects.create(
                lesson=lesson, order_index=k, phase='explain',
                teacher_script='s', enabling_objective=f'EO-{k}')
        et = ExitTicket.objects.create(lesson=lesson, passing_score=3)
        for eo in ('EO-0', 'EO-1'):
            for k in range(3):
                ExitTicketQuestion.objects.create(
                    exit_ticket=et, order_index=k, question_type='mcq',
                    question_text=f'{eo} q{k}?', enabling_objective=eo,
                    option_a='a', option_b='b', option_c='c', option_d='d',
                    correct_answer='A', difficulty='easy',
                )
        session = TutorSession.objects.create(
            institution=inst, student=user, lesson=lesson, engine='simple',
            current_step_index=2)          # past the last step
        # EO-0 missed badly, EO-1 mastered.
        ExitTicketAttempt.objects.create(
            session=session, student=user, exit_ticket=et,
            completed_at=timezone.now(),
            answers={
                'per_question': [{'index': 0}],
                'eo_competency': {
                    'EO-0': {'asked': 3, 'correct': 0, 'failed_question_ids': []},
                    'EO-1': {'asked': 3, 'correct': 3, 'failed_question_ids': []},
                },
            },
        )
        return session

    def test_pool_is_not_empty_past_the_last_step(self):
        from apps.tutoring.simple_tutor.tools import build_question_pool
        pool = build_question_pool(self._failed_ticket_session())
        self.assertTrue(
            pool,
            'empty pool during remediation — pose_question has nothing to '
            'select and the tutor can only write a question in prose')

    def test_pool_holds_only_missed_objectives(self):
        """Posing an item from a mastered objective wastes the turn and reads
        as the tutor not having looked at the results."""
        from apps.tutoring.simple_tutor.tools import build_question_pool
        pool = build_question_pool(self._failed_ticket_session())
        self.assertTrue(all(q.enabling_objective == 'EO-0' for q in pool),
                        [q.enabling_objective for q in pool])

    def test_no_completed_attempt_means_no_pool(self):
        """Past the last step with nothing failed is a finished lesson, not
        remediation. Posing at random there would be worse than teaching."""
        from apps.tutoring.models import ExitTicketAttempt
        from apps.tutoring.simple_tutor.tools import build_question_pool
        session = self._failed_ticket_session()
        ExitTicketAttempt.objects.filter(session=session).delete()
        self.assertEqual(build_question_pool(session), [])

    def test_remediation_instructions_do_not_license_authoring(self):
        """The prompt half. `pose_question` takes an index and nothing else, so
        "author your own" described a capability the tool does not have — and
        it was the last surviving authoring instruction anywhere in the
        prompt, firing in exactly the mode that had no pool to pose from."""
        from apps.tutoring.simple_tutor.family_prompts import build_family_block_0
        from apps.tutoring.simple_tutor.prompts import (
            _REMEDIATION_PREAMBLE as _P, _REMEDIATION_INSTRUCTIONS as R,
        )
        # Offline: remediation guidance rides on the per-turn mode block the
        # server picks, so Block 0 carries none of it.
        offline = build_family_block_0('qwen', 'BASE')
        self.assertNotIn('REMEDIATION', offline)
        self.assertNotIn('author your own', offline.lower())
        self.assertNotIn('author your own', _P.lower())
        # Production still ships the long form and must not regain authoring.
        self.assertNotIn('author your own', R.lower())
        self.assertNotIn('surface the stem', R.lower(),
                         'contradicts "your reply does not repeat the stem"')
        self.assertIn('pose_question', R)


class ServerPicksTheModeTest(SimpleTestCase):
    """Exactly one mode reaches the model, chosen by the platform.

    Block 0 used to carry all four and ask the model to work out which applied
    from <in_flight_question>, <message_intent> and <exit_ticket_review>. All
    three are arguments to build_system_prompt — the platform already holds
    them — so that asked a 4B to re-derive a known fact with three wrong
    answers available and nothing gained by getting it right.
    """

    from types import SimpleNamespace as _N
    SLOT = _N(question_text='Which axis?', question_type='mcq',
              reference_answer='B', source='catalog', attempt_count=0,
              options=['w', 'x', 'y', 'z'], catalog_question_id=1)
    FAILED = {'passed': False, 'missed_objectives': [{'enabling_objective': 'EO'}],
              'mastered_objectives': []}

    def _mode(self, slot, intent, review=None):
        from apps.tutoring.simple_tutor.prompts import _render_active_mode
        return _render_active_mode(slot, intent, review)

    def test_the_three_base_modes(self):
        for label, slot, intent in (
            ('GRADE', self.SLOT, 'answer'),
            ('GRADE', self.SLOT, 'answer_or_other'),
            ('CONVERSATIONAL', self.SLOT, 'clarification'),
            ('CONVERSATIONAL', self.SLOT, 'off_topic'),
            ('POSE / TEACH', None, 'answer'),
        ):
            with self.subTest(slot=bool(slot), intent=intent):
                self.assertIn(f'## This turn: {label}', self._mode(slot, intent))

    def test_a_missing_intent_with_a_live_slot_grades(self):
        """Intent classification can fail. Falling back to CONVERSATIONAL would
        record an empty answer and silently discard a real attempt."""
        self.assertIn('GRADE', self._mode(self.SLOT, None))
        self.assertIn('GRADE', self._mode(self.SLOT, ''))

    def test_remediation_gets_its_own_bodies_not_a_contradicting_suffix(self):
        """Superseded the suffix approach on 2026-08-06.

        Appending "the platform poses the next one for you" to a body that
        already said "call pose_question for the next question in the SAME
        turn" left the model to resolve a contradiction, and it resolved it by
        writing a question in PROSE without the tool call. The server then
        posed as well, so the student read two questions and got buttons for
        the second.

        The remediation bodies now say it once: the platform posts the
        question, write none yourself.
        """
        graded = self._mode(self.SLOT, 'answer', self.FAILED)
        self.assertIn('## This turn: GRADE (remediation)', graded)
        # 2026-08-06: remediation poses like tutoring again. The server-pose
        # backstop stays, but it only fires when the model did not — so the
        # instruction is now "pose", stated once, with nothing arguing back.
        self.assertIn('pose_question', graded)
        self.assertNotIn('Do not call `pose_question`', graded)

        teaching = self._mode(None, 'answer', self.FAILED)
        self.assertIn('## This turn: TEACH (remediation)', teaching)
        self.assertIn('pose_question', teaching)

    def test_remediation_never_points_at_a_step_that_is_not_rendered(self):
        """Remediation runs past the last step, so <current_step> and
        <teaching_notes> are absent from the prompt. The lesson POSE body
        tells the model to read both — carrying that into remediation pointed
        it at two blocks it cannot find."""
        for slot in (self.SLOT, None):
            with self.subTest(live_question=bool(slot)):
                out = self._mode(slot, 'answer', self.FAILED)
                self.assertNotIn('<current_step>', out)
                self.assertNotIn('<teaching_notes>', out)

    def test_a_passed_review_is_not_remediation(self):
        """The review block survives a pass. Re-teaching objectives the student
        just mastered wastes the turn."""
        out = self._mode(self.SLOT, 'answer', {'passed': True})
        self.assertNotIn('in remediation', out)

    def test_exactly_one_mode_section_reaches_the_prompt(self):
        """The property that matters. Two would restore the ambiguity the
        server resolution exists to remove."""
        from apps.tutoring.simple_tutor.prompts import (
            ANSWER_MODE_PICKER, build_system_prompt,
        )
        for slot, intent, review in (
            (self.SLOT, 'answer', None),
            (self.SLOT, 'clarification', None),
            (None, 'answer', None),
            (self.SLOT, 'answer', self.FAILED),
        ):
            with self.subTest(slot=bool(slot), intent=intent, remed=bool(review)):
                blocks, _ = build_system_prompt(
                    session=None, step=None, in_flight_question=slot,
                    student_intent=intent, exit_ticket_review=review,
                    family='qwen', answer_mode=ANSWER_MODE_PICKER)
                text = '\n'.join(b['text'] for b in blocks)
                self.assertEqual(
                    text.count('## This turn:'), 1,
                    'the model must be handed one mode, not a menu')

    def test_production_still_carries_all_four_in_block_0(self):
        """Only the offline template was restructured. Rendering the dynamic
        mode for other families would duplicate what their Block 0 already
        has, and stripping their Block 0 would leave them with neither."""
        from apps.tutoring.simple_tutor.prompts import build_system_prompt
        blocks, _ = build_system_prompt(
            session=None, step=None, in_flight_question=self.SLOT,
            student_intent='answer')
        text = '\n'.join(b['text'] for b in blocks)
        self.assertNotIn('## This turn:', text)
        self.assertIn('GRADE mode', blocks[0]['text'])
