"""Tests for the EO-driven remediation walkthrough (P4).

Covers:
  - _build_eo_competency_map: counts asked/correct + failed_question_ids
  - _ordered_failed_questions: walks lesson EOs in published order,
    appends untagged at the end
  - _generate_remediation_opening: returns (message, turn_metadata)
    with bank_question_ref so the next student reply gets graded.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.conversational_tutor import ConversationalTutor
from apps.tutoring.models import ExitTicket, TutorSession


class _StubLesson:
    """Minimal duck for lesson with enabling_objectives."""
    def __init__(self, enabling_objectives):
        self.enabling_objectives = enabling_objectives


def _make_tutor(lesson=None):
    """Bypass __init__; only set the fields these helpers touch."""
    t = ConversationalTutor.__new__(ConversationalTutor)
    t.lesson = lesson or _StubLesson([])
    return t


class BuildEOCompetencyMapTest(TestCase):
    def test_counts_asked_and_correct(self):
        t = _make_tutor()
        results = [
            {'enabling_objective': 'EO1', 'is_correct': True, 'index': 0},
            {'enabling_objective': 'EO1', 'is_correct': False, 'index': 1, 'question_id': 11},
            {'enabling_objective': 'EO2', 'is_correct': True, 'index': 2},
            {'enabling_objective': 'EO2', 'is_correct': True, 'index': 3},
        ]
        m = t._build_eo_competency_map(results)
        self.assertEqual(m['EO1']['asked'], 2)
        self.assertEqual(m['EO1']['correct'], 1)
        self.assertEqual(m['EO1']['failed_question_ids'], [11])
        self.assertFalse(m['EO1']['is_mastered'])
        self.assertTrue(m['EO2']['is_mastered'])

    def test_falls_back_to_concept_tag(self):
        t = _make_tutor()
        results = [
            # No enabling_objective — fall back to concept_tag
            {'concept_tag': 'Broad LO', 'is_correct': True, 'index': 0},
        ]
        m = t._build_eo_competency_map(results)
        self.assertIn('Broad LO', m)

    def test_skips_untagged_questions(self):
        t = _make_tutor()
        results = [
            {'is_correct': True, 'index': 0},  # no tag at all
        ]
        m = t._build_eo_competency_map(results)
        self.assertEqual(m, {})


class OrderedFailedQuestionsTest(TestCase):
    def test_orders_by_lesson_eo_sequence(self):
        # Lesson EOs in published order: EO1, EO2, EO3
        lesson = _StubLesson(['EO1', 'EO2', 'EO3'])
        t = _make_tutor(lesson)
        # Failed questions arrive in mixed order — the walkthrough
        # should re-order them by lesson EO.
        failed = [
            {'id': 30, 'enabling_objective': 'EO3'},
            {'id': 10, 'enabling_objective': 'EO1'},
            {'id': 31, 'enabling_objective': 'EO3'},
            {'id': 20, 'enabling_objective': 'EO2'},
        ]
        eo_map = t._build_eo_competency_map([
            {'enabling_objective': 'EO1', 'is_correct': False, 'question_id': 10},
            {'enabling_objective': 'EO3', 'is_correct': False, 'question_id': 30},
            {'enabling_objective': 'EO3', 'is_correct': False, 'question_id': 31},
            {'enabling_objective': 'EO2', 'is_correct': False, 'question_id': 20},
        ])
        ordered = t._ordered_failed_questions(failed, eo_map)
        self.assertEqual([fq['id'] for fq in ordered], [10, 20, 30, 31])

    def test_groups_multiple_failures_per_eo(self):
        """All failed questions tagged to one EO are walked through
        before moving to the next EO (per the locked decision)."""
        lesson = _StubLesson(['EO1', 'EO2'])
        t = _make_tutor(lesson)
        failed = [
            {'id': 11, 'enabling_objective': 'EO1'},
            {'id': 21, 'enabling_objective': 'EO2'},
            {'id': 12, 'enabling_objective': 'EO1'},
            {'id': 22, 'enabling_objective': 'EO2'},
        ]
        eo_map = {'EO1': {}, 'EO2': {}}
        ordered = t._ordered_failed_questions(failed, eo_map)
        # All EO1 first, then all EO2
        self.assertEqual([fq['id'] for fq in ordered], [11, 12, 21, 22])

    def test_appends_untagged_at_end(self):
        lesson = _StubLesson(['EO1'])
        t = _make_tutor(lesson)
        failed = [
            {'id': 99, 'enabling_objective': 'untagged_drift'},  # not in lesson EOs
            {'id': 11, 'enabling_objective': 'EO1'},
        ]
        eo_map = {'EO1': {}, 'untagged_drift': {}}
        ordered = t._ordered_failed_questions(failed, eo_map)
        self.assertEqual([fq['id'] for fq in ordered], [11, 99])

    def test_empty_lesson_eos_keeps_input_order(self):
        lesson = _StubLesson([])
        t = _make_tutor(lesson)
        failed = [
            {'id': 1, 'enabling_objective': 'X'},
            {'id': 2, 'enabling_objective': 'Y'},
        ]
        ordered = t._ordered_failed_questions(failed, {'X': {}, 'Y': {}})
        # No lesson EO order → fall through to input order via second pass
        self.assertEqual([fq['id'] for fq in ordered], [1, 2])

    def test_dedupes_by_id(self):
        lesson = _StubLesson(['EO1'])
        t = _make_tutor(lesson)
        failed = [
            {'id': 11, 'enabling_objective': 'EO1'},
            {'id': 11, 'enabling_objective': 'EO1'},  # duplicate id
        ]
        ordered = t._ordered_failed_questions(failed, {'EO1': {}})
        self.assertEqual([fq['id'] for fq in ordered], [11])


class RemediationOpeningMetadataTest(TestCase):
    """The opener must return turn_metadata carrying bank_question_ref
    so _save_turn persists it on the tutor turn — otherwise the next
    student reply has nothing for _grade_against_last_bank_question
    to grade against, and the walkthrough never advances."""

    def _stub_tutor(self, queue, mastered=None, missed=None):
        t = ConversationalTutor.__new__(ConversationalTutor)
        t.lesson = _StubLesson(['EO1'])
        t.session = MagicMock()
        t.session.engine_state = {
            'remediation_eo_competency': {
                **{eo: {'is_mastered': True, 'asked': 1, 'correct': 1}
                   for eo in (mastered or [])},
                **{eo: {'is_mastered': False, 'asked': 2, 'correct': 0}
                   for eo in (missed or [])},
            },
            'remediation_walkthrough_queue': queue,
            'remediation_walkthrough_index': 0,
            'remediation_phase': 'walkthrough',
        }
        t.remediation_attempt = 1
        t._generate_response = MagicMock(return_value='Opening prose.')
        t._exit_ticket_passing_score = MagicMock(return_value=8)
        return t

    def test_returns_metadata_with_bank_ref_for_first_question(self):
        t = self._stub_tutor(
            queue=[{'id': 42, 'eo': 'EO1'}],
            mastered=['Easy EO'], missed=['Hard EO'],
        )
        fake_q = MagicMock(id=42, question_type='mcq')
        fake_q.option_a = 'A'  # rendering hits real attrs
        with patch(
            'apps.tutoring.models.ExitTicketQuestion.objects.filter'
        ) as mock_filter, patch(
            'apps.tutoring.question_bank.render_question_to_prose',
            return_value='Q rendered text',
        ):
            mock_filter.return_value.first.return_value = fake_q
            message, metadata = t._generate_remediation_opening(
                score=3, total=10, failed_questions=[],
            )
        self.assertIn('Question 1 of 1', message)
        self.assertEqual(
            metadata.get('bank_question_ref'),
            {'kind': 'exit_ticket_question', 'id': 42, 'question_type': 'mcq'},
        )

    def test_empty_queue_returns_empty_metadata(self):
        t = self._stub_tutor(queue=[])
        message, metadata = t._generate_remediation_opening(
            score=3, total=10, failed_questions=[],
        )
        self.assertEqual(metadata, {})
        self.assertNotIn('Question 1 of', message)


class FinishRemediationTest(TestCase):
    """When the re-quiz exhausts, EOs that scored 100% must be
    promoted to mastered in the engine_state competency map. Without
    promotion, the next session would re-flag those EOs as 'failed'
    and weight them 5x in sampling — defeats the whole point."""

    def _stub_tutor(self, results, competency):
        t = ConversationalTutor.__new__(ConversationalTutor)
        t.lesson = _StubLesson([])
        t.session = MagicMock()
        t.session.engine_state = {
            'remediation_eo_competency': competency,
            'remediation_requiz_results': results,
            'remediation_phase': 'requiz',
        }
        return t

    def test_promotes_eo_with_100_percent_correct(self):
        t = self._stub_tutor(
            results={'EO X': {'asked': 2, 'correct': 2}},
            competency={'EO X': {'is_mastered': False, 'asked': 2, 'correct': 0}},
        )
        out = t._finish_remediation(
            t.session.engine_state, clean_response='OK.',
        )
        comp = t.session.engine_state['remediation_eo_competency']
        self.assertTrue(comp['EO X']['is_mastered'])
        self.assertIn('Promoted to mastered', out)
        self.assertIn('EO X', out)

    def test_does_not_promote_eo_with_partial_score(self):
        t = self._stub_tutor(
            results={'EO X': {'asked': 2, 'correct': 1}},
            competency={'EO X': {'is_mastered': False, 'asked': 2, 'correct': 0}},
        )
        out = t._finish_remediation(
            t.session.engine_state, clean_response='OK.',
        )
        comp = t.session.engine_state['remediation_eo_competency']
        self.assertFalse(comp['EO X']['is_mastered'])
        self.assertIn('Still needs work', out)

    def test_no_results_emits_brief_wrap(self):
        """Re-quiz skipped (no failed EOs) → brief wrap, no
        promoted/still-weak headings."""
        t = self._stub_tutor(results={}, competency={})
        out = t._finish_remediation(
            t.session.engine_state, clean_response='OK.',
        )
        self.assertIn('Walkthrough complete', out)
        self.assertNotIn('Promoted to mastered', out)
        self.assertNotIn('Still needs work', out)
        self.assertEqual(t.session.engine_state['remediation_phase'], 'done')


class RecordRequizResultTest(TestCase):
    """Per-question verdict accumulation drives _finish_remediation's
    promote check. Off-by-one or mis-keyed EO → wrong promotion."""

    def _stub_tutor(self, queue, idx):
        t = ConversationalTutor.__new__(ConversationalTutor)
        t.session = MagicMock()
        t.session.engine_state = {
            'remediation_requiz_queue': queue,
            'remediation_requiz_index': idx,
        }
        return t

    def test_increments_correct(self):
        t = self._stub_tutor([{'id': 1, 'eo': 'EO A'}], idx=0)
        t._record_requiz_result(t.session.engine_state, is_correct=True)
        bucket = t.session.engine_state['remediation_requiz_results']['EO A']
        self.assertEqual(bucket, {'asked': 1, 'correct': 1})

    def test_increments_incorrect(self):
        t = self._stub_tutor([{'id': 1, 'eo': 'EO A'}], idx=0)
        t._record_requiz_result(t.session.engine_state, is_correct=False)
        bucket = t.session.engine_state['remediation_requiz_results']['EO A']
        self.assertEqual(bucket, {'asked': 1, 'correct': 0})

    def test_skips_when_idx_out_of_bounds(self):
        t = self._stub_tutor([{'id': 1, 'eo': 'EO A'}], idx=5)
        t._record_requiz_result(t.session.engine_state, is_correct=True)
        # Nothing recorded — guard clause prevents out-of-bounds writes
        self.assertEqual(
            t.session.engine_state.get('remediation_requiz_results', {}), {},
        )
