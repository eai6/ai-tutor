"""Regression tests for the mt50 qwen3-4b bottleneck fixes (2026-08-03).

Bottlenecks from the mt50 multi-turn board (offline_eval/multi_turn_results/
mt50/qwen3_4b.{json,log} — 44/50, all 6 failures max_turn_count):

- Q1: grader false negatives on percent/decimal scaling — a slot posed with a
  bare-number percentage reference ('100', '30') graded decimal-probability
  answers ('1', '0.3') INCORRECT; 5 of the run's 100 incorrect verdicts were
  exactly this, each spiralling into a false-correction hint ladder.
  Fix: _grade_math pass 2b, context-gated, one direction only.
- Q2: 62 turns acknowledged nothing — the reply opened straight onto the next
  question with the graded answer passed over in silence.
  Fix: _align_reply_polarity prepends a rotated verdict-consistent ack.
- Q3: "Exactly —" opened up to 10 of 13 tutor turns per session (judges:
  templated/robotic). Fix: _rotate_repeated_ack.
- Q4: reveals on no-verdict turns ("Let's calculate: 1 − 0.8 = 0.2" while the
  question was open) — the old incorrect-only gate let them through.
  Fix: _filter_reveals also runs when a question is in flight with no verdict.
- Q5: grading metadata leaked into student text ("The reference answer of
  0.166667 suggests…"). Fix: vocab scrub covers "reference answer/value".
- Q6: a mis-authored catalog question was re-posed 3× because every grade came
  back incorrect, so the answered-correct guard never saw it.
  Fix: per-stem pose cap (stage 1b) — 2 poses max per normalised stem.
- Q7: one-call mode writes prose before grading; when the model guessed its
  own verdict wrong the hint content argued with the grader all session.
  Fix: _call1_contradicts_verdict escalates those turns to Call 2.
- Q8: help-intensive sessions idled one slot for 6+ tutor turns without ever
  incrementing attempt_count (clarifications don't count as attempts), then
  died at max_turns. Fix: slot-age pivot trigger (_bump_slot_age).
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase as DjangoTestCase

from apps.tutoring.models import InFlightQuestion, SessionTurn
from apps.tutoring.simple_tutor.engine import (
    _align_reply_polarity,
    _auto_grade_fallback,
    _bump_slot_age,
    _call1_contradicts_verdict,
    _filter_reveals,
    _rotate_repeated_ack,
    _scrub_engine_vocab,
)
from apps.tutoring.simple_tutor.grader import Verdict, _grade_math
from apps.tutoring.simple_tutor.tests.test_engine import _make_session
from apps.tutoring.simple_tutor.tools import (
    handle_pose_question,
    handle_record_answer,
)


def _math_q(question_text: str, reference: str, computed=None):
    ad = {'model_answer': reference}
    if computed is not None:
        ad['computed'] = computed
    return SimpleNamespace(
        pk=1, question_type='short_numeric', question_text=question_text,
        correct_answer=reference, answer_data=ad,
    )


def _graded(verdict: str):
    return [{'tool': 'record_answer',
             'result': {'recorded': True, 'verdict': verdict}}]


# ============================================================================
# Q1 — percent/decimal scale equivalence
# ============================================================================


class ScaleEquivalenceTest(SimpleTestCase):

    def test_decimal_one_matches_percent_100(self):
        q = _math_q("What's 1.00 as a percentage?", '100', 100.0)
        self.assertEqual(_grade_math(q, '1').verdict, Verdict.CORRECT)

    def test_decimal_probability_matches_percent_ref(self):
        q = _math_q(
            'What is the probability that it does not rain, as a percentage?',
            '30', 30.0)
        self.assertEqual(_grade_math(q, '0.3').verdict, Verdict.CORRECT)
        self.assertEqual(_grade_math(q, '0.30').verdict, Verdict.CORRECT)

    def test_wrong_decimal_still_rejected(self):
        # mt50 session 17: 0.25 against ref 30 is genuinely wrong.
        q = _math_q(
            'What is the probability that it does not rain, as a percentage?',
            '30', 30.0)
        self.assertEqual(_grade_math(q, '0.25').verdict, Verdict.INCORRECT)

    def test_same_ratio_outside_probability_range_rejected(self):
        # mt50 session 29: 600 against ref 6 shares the ×100 ratio but is not
        # a (probability, percentage) pair — the range gate rejects it.
        q = _math_q('What percentage of items were defective?', '6', 6.0)
        self.assertEqual(_grade_math(q, '600').verdict, Verdict.INCORRECT)

    def test_reverse_direction_rejected(self):
        # "1% of 100" → ref 1; a student answering "100" computed it wrong
        # and must not be credited via the scaling pass.
        q = _math_q('What is 1% of 100?', '1', 1.0)
        self.assertEqual(_grade_math(q, '100').verdict, Verdict.INCORRECT)

    def test_no_percent_context_no_scaling(self):
        q = _math_q('Add the two angle measures.', '30', 30.0)
        self.assertEqual(_grade_math(q, '0.3').verdict, Verdict.INCORRECT)

    def test_portuguese_context_accepted(self):
        q = _math_q('Qual é a probabilidade, em percentagem?', '40', 40.0)
        self.assertEqual(_grade_math(q, '0.4').verdict, Verdict.CORRECT)


# ============================================================================
# Q2 — missing-ack prepend
# ============================================================================


class AckPrependTest(DjangoTestCase):

    def test_correct_verdict_silent_pose_gets_ack(self):
        session, _ = _make_session()
        out = _align_reply_polarity(
            session, 'A bearing is measured clockwise from which direction?',
            _graded('correct'))
        self.assertNotEqual(
            out, 'A bearing is measured clockwise from which direction?')
        # The original question survives after the prepended ack.
        self.assertIn('clockwise from which direction?', out)

    def test_incorrect_verdict_bare_hint_gets_ack(self):
        session, _ = _make_session()
        out = _align_reply_polarity(
            session, 'Look again at the second step of your working.',
            _graded('incorrect'))
        self.assertTrue(
            out.lower().startswith(('not quite', 'not this time', 'close')))

    def test_existing_ack_not_duplicated(self):
        session, _ = _make_session()
        reply = 'Exactly — 140°. Now try this: what is x?'
        self.assertEqual(
            _align_reply_polarity(session, reply, _graded('correct')), reply)

    def test_unknown_phrasing_with_mid_affirm_not_duplicated(self):
        session, _ = _make_session()
        reply = "You're right — the angles match. Next: what is y?"
        self.assertEqual(
            _align_reply_polarity(session, reply, _graded('correct')), reply)

    def test_no_verdict_untouched(self):
        session, _ = _make_session()
        reply = 'What do angles on a straight line add up to?'
        self.assertEqual(_align_reply_polarity(session, reply, []), reply)


# ============================================================================
# Q3 — repeated-opener rotation
# ============================================================================


class RepeatedAckRotationTest(DjangoTestCase):

    def _add_tutor_turn(self, session, content):
        SessionTurn.objects.create(
            session=session, role=SessionTurn.Role.TUTOR, content=content)

    def test_third_exactly_in_a_row_is_rotated(self):
        session, _ = _make_session()
        self._add_tutor_turn(session, 'Exactly — 120°. Now try this: …')
        out = _rotate_repeated_ack(
            session, 'Exactly — 140°. Next one: what is x?',
            _graded('correct'))
        self.assertFalse(out.lower().startswith('exactly'))
        self.assertIn('140°. Next one: what is x?', out)

    def test_fresh_opener_untouched(self):
        session, _ = _make_session()
        self._add_tutor_turn(session, 'Exactly — 120°.')
        reply = 'Nice — 140°. Next one.'
        self.assertEqual(
            _rotate_repeated_ack(session, reply, _graded('correct')), reply)

    def test_incorrect_verdict_untouched(self):
        session, _ = _make_session()
        self._add_tutor_turn(session, 'Exactly — 120°.')
        reply = 'Exactly — that is the trap here. Look again.'
        self.assertEqual(
            _rotate_repeated_ack(session, reply, _graded('incorrect')), reply)


# ============================================================================
# Q4 — reveal filter on no-verdict turns with a live slot
# ============================================================================


class RevealFilterNoVerdictTest(DjangoTestCase):

    def test_reveal_redacted_on_clarification_turn(self):
        session, _ = _make_session()
        InFlightQuestion.objects.create(
            session=session,
            question_text='P(catch) = 0.8. What is P(no catch)?',
            question_type='short_numeric', reference_answer='0.2',
            source='inline_authored',
        )
        out = _filter_reveals(
            session, "Let's calculate: 1 - 0.8 = 0.2. Try it yourself.", [])
        self.assertNotIn('= 0.2', out)

    def test_correct_verdict_skips_filter(self):
        # After a correct grade the slot holds the NEXT question — prose about
        # the answer just resolved must survive.
        session, _ = _make_session()
        InFlightQuestion.objects.create(
            session=session, question_text='Next question?',
            question_type='short_numeric', reference_answer='230',
            source='inline_authored',
        )
        reply = "That's right — 360 - 130 = 230. Now try this: …"
        self.assertEqual(
            _filter_reveals(session, reply, _graded('correct')), reply)

    def test_no_slot_no_verdict_untouched(self):
        session, _ = _make_session()
        reply = 'The answer is 42 in that worked example.'
        self.assertEqual(_filter_reveals(session, reply, []), reply)


# ============================================================================
# Q5 — "reference answer" is engine vocabulary
# ============================================================================


class ReferenceAnswerScrubTest(SimpleTestCase):

    def test_reference_answer_sentence_dropped(self):
        text = (
            'The probability value is 0.6. The reference answer of 0.166667 '
            'suggests this question tests a different scenario. '
            "Let's focus on expected outcomes."
        )
        out = _scrub_engine_vocab(text)
        self.assertNotIn('reference answer', out.lower())
        self.assertIn('probability value is 0.6', out)
        self.assertIn('expected outcomes', out)


# ============================================================================
# Q6 — per-stem pose cap
# ============================================================================


class PoseStemCapTest(DjangoTestCase):

    def _pose(self, session, stem):
        return handle_pose_question(
            session, question_text=stem, question_type='short_numeric',
            reference_answer='0.6', source='catalog',
        )

    def test_third_pose_of_same_stem_rejected(self):
        session, _ = _make_session()
        stem = "The probability of catching a fish is 0.6. What is the value?"
        self.assertTrue(self._pose(session, stem)['posed'])
        self.assertTrue(self._pose(session, stem)['posed'])
        third = self._pose(session, stem)
        self.assertFalse(third['posed'])
        self.assertTrue(third.get('repeat_of_stem'))
        # The second pose's slot survives the rejected third pose.
        self.assertEqual(
            InFlightQuestion.objects.filter(session=session).count(), 1)

    def test_different_stems_unaffected(self):
        session, _ = _make_session()
        self.assertTrue(self._pose(session, 'Question one?')['posed'])
        self.assertTrue(self._pose(session, 'Question two?')['posed'])
        self.assertTrue(self._pose(session, 'Question three?')['posed'])


# ============================================================================
# Q7 — one-call escalation predicate
# ============================================================================


class Call1ContradictionTest(SimpleTestCase):

    def test_neg_opener_on_correct_escalates(self):
        self.assertTrue(_call1_contradicts_verdict(
            'Not quite — you used 0.65 instead of 0.60.',
            _graded('correct')))

    def test_pos_opener_on_incorrect_escalates(self):
        self.assertTrue(_call1_contradicts_verdict(
            'Exactly — 0.4 is right! Now try this…', _graded('incorrect')))

    def test_agreeing_prose_does_not_escalate(self):
        self.assertFalse(_call1_contradicts_verdict(
            'Exactly — 0.4 is right! Now try this…', _graded('correct')))
        self.assertFalse(_call1_contradicts_verdict(
            'Not quite — check the subtraction.', _graded('incorrect')))

    def test_no_verdict_does_not_escalate(self):
        self.assertFalse(_call1_contradicts_verdict('Not quite.', []))


# ============================================================================
# Q8 — slot-age tracking
# ============================================================================


class SlotAgeTest(DjangoTestCase):

    def test_age_increments_for_same_slot_and_resets_on_new(self):
        session, _ = _make_session()
        slot_a = SimpleNamespace(pk=101)
        self.assertEqual(_bump_slot_age(session, slot_a), 1)
        self.assertEqual(_bump_slot_age(session, slot_a), 2)
        self.assertEqual(_bump_slot_age(session, slot_a), 3)
        slot_b = SimpleNamespace(pk=202)
        self.assertEqual(_bump_slot_age(session, slot_b), 1)


# ============================================================================
# Kiosk session 74 fixes (2026-08-03 screenshot)
# ============================================================================


class EmptyRecordAutogradeTest(DjangoTestCase):
    """K1 — the model called record_answer('') on a bare 'a' ("that was not
    an answer" about a message that plainly was) and the old any-call-counts
    guard let the answer vanish: same question re-asked verbatim, twice."""

    def _slot(self, session, ref='B'):
        return InFlightQuestion.objects.create(
            session=session,
            question_text='Which describes a small scale map?',
            question_type='mcq',
            options=['great detail', '1:1,000,000 or larger', 'urban', 'photo'],
            reference_answer=ref, source='catalog',
        )

    def test_empty_record_call_does_not_block_fallback(self):
        session, _ = _make_session()
        self._slot(session)
        tool_results = [{'tool': 'record_answer',
                         'result': {'recorded': False,
                                    'error': 'extracted_answer is empty'}}]
        _auto_grade_fallback(
            session=session, family='qwen', student_intent='answer',
            user_input='a', tool_results=tool_results,
        )
        graded = [tr for tr in tool_results
                  if tr.get('tool') == 'auto_grade_fallback']
        self.assertEqual(len(graded), 1)
        self.assertEqual(graded[0]['result']['verdict'], 'incorrect')

    def test_recorded_grade_still_trusted(self):
        session, _ = _make_session()
        self._slot(session)
        tool_results = [{'tool': 'record_answer',
                         'result': {'recorded': True, 'verdict': 'correct'}}]
        _auto_grade_fallback(
            session=session, family='qwen', student_intent='answer',
            user_input='b', tool_results=tool_results,
        )
        self.assertFalse(any(tr.get('tool') == 'auto_grade_fallback'
                             for tr in tool_results))


class SameTurnIncorrectPoseBlockTest(DjangoTestCase):
    """K2 — after 'c' graded incorrect, the tutor posed the NEXT question in
    the same reply instead of hinting, leaving the miss unresolved."""

    def _slot(self, session):
        return InFlightQuestion.objects.create(
            session=session,
            question_text='Which describes a small scale map?',
            question_type='mcq',
            options=['great detail', '1:1,000,000 or larger', 'urban', 'photo'],
            reference_answer='B', source='catalog',
        )

    def test_pose_blocked_after_same_turn_incorrect(self):
        session, _ = _make_session()
        self._slot(session)
        graded = handle_record_answer(session, extracted_answer='C')
        self.assertEqual(graded['verdict'], 'incorrect')
        posed = handle_pose_question(
            session, question_text='A brand new question?',
            question_type='short_numeric', reference_answer='4',
            source='inline_authored',
        )
        self.assertFalse(posed['posed'])
        self.assertTrue(posed.get('premature'))
        # The graded question survives for the hint.
        slot = InFlightQuestion.objects.get(session=session)
        self.assertIn('small scale map', slot.question_text)

    def test_engine_initiated_pose_bypasses_block(self):
        session, _ = _make_session()
        self._slot(session)
        handle_record_answer(session, extracted_answer='C')
        posed = handle_pose_question(
            session, question_text='Engine pivot question?',
            question_type='short_numeric', reference_answer='4',
            source='catalog', engine_initiated=True,
        )
        self.assertTrue(posed['posed'])

    def test_pose_allowed_once_flag_cleared(self):
        # respond() clears the flag at the start of the next turn.
        session, _ = _make_session()
        self._slot(session)
        handle_record_answer(session, extracted_answer='C')
        es = session.engine_state
        es.pop('_graded_incorrect_this_turn', None)
        session.engine_state = es
        session.save(update_fields=['engine_state'])
        posed = handle_pose_question(
            session, question_text='Next turn question?',
            question_type='short_numeric', reference_answer='4',
            source='inline_authored',
        )
        self.assertTrue(posed['posed'])


class McqParaphraseRevealTest(DjangoTestCase):
    """K3 — the reveal restated the correct option's content without naming
    its letter ("has a large ratio (like 1:1,000,000 or bigger), meaning it
    covers a vast area but shows less detail"), sailing past the letter
    patterns."""

    def _slot(self, session):
        return InFlightQuestion.objects.create(
            session=session,
            question_text='A small scale map is best described as?',
            question_type='mcq',
            options=[
                'A ratio of 1:10,000 and showing a small area with great detail',
                'A ratio of 1:1,000,000 or larger and showing a large area '
                'with limited detail',
                'A ratio of 1:50,000 and showing only urban centres',
                'A map made from a reduced photograph rather than a survey',
            ],
            reference_answer='B', source='catalog',
        )

    def test_option_paraphrase_redacted(self):
        session, _ = _make_session()
        self._slot(session)
        reply = (
            "You picked C — that's not quite right. A small scale map has a "
            "large ratio (like 1:1,000,000 or bigger), meaning it covers a "
            "vast area but shows less detail. Have another look at the "
            "options."
        )
        out = _filter_reveals(session, reply, _graded('incorrect'))
        self.assertNotIn('1:1,000,000', out)
        self.assertIn('not quite right', out)
        self.assertIn('another look', out)

    def test_legitimate_hint_survives(self):
        session, _ = _make_session()
        self._slot(session)
        reply = ("Not quite. Think about what 'small scale' means — does it "
                 "show more detail or less?")
        self.assertEqual(
            _filter_reveals(session, reply, _graded('incorrect')), reply)
