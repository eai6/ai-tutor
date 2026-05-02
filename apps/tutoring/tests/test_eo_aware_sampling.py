"""Tests for P5 — EO-aware bank sampling + remediation re-quiz queue.

Covers:
  - compute_student_eo_competency: latest attempt wins per EO,
    mastered/failed/unattempted classification
  - sample_session_pool with student=...: biases the draw toward
    failed > unattempted > mastered EOs
  - build_remediation_requiz_queue: 2 fresh questions per failed EO,
    excludes walked questions, falls back when fresh pool empty
"""

from django.test import TestCase

from apps.tutoring.models import ExitTicketAttempt, ExitTicketQuestion
from apps.tutoring.question_bank import (
    EO_WEIGHT_FAILED,
    EO_WEIGHT_MASTERED,
    EO_WEIGHT_UNATTEMPTED,
    build_remediation_requiz_queue,
    compute_student_eo_competency,
    sample_session_pool,
)
from apps.tutoring.tests.fixtures import BaseTutoringTestCase


class ComputeStudentEOCompetencyTest(BaseTutoringTestCase):
    """The competency map drives sampling weights AND the re-quiz
    promote-to-mastered check, so mis-classifying an EO has knock-on
    effects on what the student sees."""

    def _make_attempt(self, eo_competency, completed_at=None, score=5):
        return ExitTicketAttempt.objects.create(
            exit_ticket=self.exit_ticket,
            student=self.student_user,
            score=score,
            passed=False,
            answers={'eo_competency': eo_competency},
            completed_at=completed_at,
        )

    def test_no_attempts_returns_empty(self):
        result = compute_student_eo_competency(self.student_user, self.lesson)
        self.assertEqual(result, {})

    def test_single_attempt_classifies_per_eo(self):
        self._make_attempt({
            'EO Mastered': {'asked': 2, 'correct': 2, 'is_mastered': True},
            'EO Failed': {'asked': 3, 'correct': 1, 'is_mastered': False},
        })
        result = compute_student_eo_competency(self.student_user, self.lesson)
        self.assertEqual(result.get('EO Mastered'), 'mastered')
        self.assertEqual(result.get('EO Failed'), 'failed')

    def test_latest_attempt_wins(self):
        """A student who failed an EO once and mastered it on retake
        should be classified 'mastered'. Old attempts must NOT override
        the latest verdict (otherwise the bank keeps drilling them on
        EOs they've already locked in)."""
        from django.utils import timezone
        old = timezone.now() - timezone.timedelta(hours=2)
        new = timezone.now() - timezone.timedelta(minutes=10)
        # Older attempt: failed
        self._make_attempt(
            {'EO X': {'asked': 2, 'correct': 0, 'is_mastered': False}},
            completed_at=old,
        )
        # Newer attempt: mastered
        self._make_attempt(
            {'EO X': {'asked': 2, 'correct': 2, 'is_mastered': True}},
            completed_at=new,
        )
        result = compute_student_eo_competency(self.student_user, self.lesson)
        self.assertEqual(result.get('EO X'), 'mastered')

    def test_eo_only_in_old_attempt_still_counts(self):
        """If the student's latest attempt didn't include EO Y but an
        older attempt did, the older-attempt verdict surfaces (better
        than treating it as unattempted)."""
        from django.utils import timezone
        old = timezone.now() - timezone.timedelta(hours=2)
        new = timezone.now() - timezone.timedelta(minutes=10)
        self._make_attempt(
            {'EO Y': {'asked': 2, 'correct': 0, 'is_mastered': False}},
            completed_at=old,
        )
        self._make_attempt(
            {'EO Z': {'asked': 1, 'correct': 1, 'is_mastered': True}},
            completed_at=new,
        )
        result = compute_student_eo_competency(self.student_user, self.lesson)
        self.assertEqual(result.get('EO Y'), 'failed')
        self.assertEqual(result.get('EO Z'), 'mastered')

    def test_missing_eo_competency_block_skipped(self):
        """Pre-P4 attempts have no eo_competency key — they don't crash
        the helper, they just don't contribute."""
        ExitTicketAttempt.objects.create(
            exit_ticket=self.exit_ticket,
            student=self.student_user,
            score=3,
            passed=False,
            answers={'per_question': []},  # legacy shape
        )
        result = compute_student_eo_competency(self.student_user, self.lesson)
        self.assertEqual(result, {})


class SampleSessionPoolWeightingTest(BaseTutoringTestCase):
    """Verify the weighted draw biases toward weak EOs without
    starving mastered ones (1x weight is small, not zero)."""

    def setUp(self):
        super().setUp()
        # Build a bank with 3 EOs, 6 published questions each (18 total)
        # so the 12-question pool MUST drop some — exposes weighting.
        self.exit_ticket.is_published = True
        self.exit_ticket.save()
        for eo in ('EO Failed', 'EO Unattempted', 'EO Mastered'):
            for i in range(6):
                ExitTicketQuestion.objects.create(
                    exit_ticket=self.exit_ticket,
                    question_text=f'{eo} Q{i}',
                    enabling_objective=eo,
                    correct_answer='A',
                    order_index=100 + i,
                )
        # Past attempt: EO Failed=failed, EO Mastered=mastered, EO
        # Unattempted untouched.
        ExitTicketAttempt.objects.create(
            exit_ticket=self.exit_ticket,
            student=self.student_user,
            score=4, passed=False,
            answers={'eo_competency': {
                'EO Failed': {'asked': 2, 'correct': 0, 'is_mastered': False},
                'EO Mastered': {'asked': 2, 'correct': 2, 'is_mastered': True},
            }},
        )

    def test_uniform_when_student_none(self):
        """No student → vanilla random.sample, no weighting."""
        pool = sample_session_pool(self.lesson, seed=42, pool_size=12)
        self.assertEqual(len(pool), 12)

    def test_weighted_biases_toward_failed_eos(self):
        """Across many seeds, weighted sampling should pick MORE
        failed-EO questions and FEWER mastered-EO questions than a
        uniform draw would. Uniform expectation = 12*6/18 = 4 of each.
        Weighted: failed≈5/9*12≈6.7, unattempted≈3/9*12≈4, mastered≈
        1/9*12≈1.3 (rough — sampling without replacement adjusts)."""
        failed_total = 0
        mastered_total = 0
        trials = 30
        for seed in range(trials):
            pool = sample_session_pool(
                self.lesson, seed=seed, pool_size=12,
                student=self.student_user,
            )
            for q in pool:
                if q.enabling_objective == 'EO Failed':
                    failed_total += 1
                elif q.enabling_objective == 'EO Mastered':
                    mastered_total += 1
        # Failed should clearly outweigh mastered. Don't pin exact
        # numbers — weighted sampling without replacement isn't
        # uniform across draws — but the bias must be strong.
        self.assertGreater(failed_total, mastered_total * 1.5)

    def test_pool_smaller_than_bank_returns_full_bank(self):
        """When the published bank is <= pool_size, weighting is moot
        — return everything regardless."""
        # Add one more EO with 1 question, then shrink pool > bank size
        # with a tiny pool_size that still covers everything? Easier:
        # ask for a pool larger than the bank.
        big_pool = sample_session_pool(
            self.lesson, seed=1, pool_size=100,
            student=self.student_user,
        )
        # All published questions returned (18 added here + 2 from
        # the base fixture's exit_q1/exit_q2 = 20).
        bank_count = ExitTicketQuestion.objects.filter(
            exit_ticket=self.exit_ticket,
        ).count()
        self.assertEqual(len(big_pool), bank_count)


class BuildRemediationRequizQueueTest(BaseTutoringTestCase):
    """The re-quiz queue is what closes the loop — it must give every
    failed EO a fresh shot at promotion. Locked decision: 2 questions
    per EO, drawn with VARIETY so retakes see different items."""

    def setUp(self):
        super().setUp()
        self.exit_ticket.is_published = True
        self.exit_ticket.save()
        # 4 published questions for EO A, 4 for EO B
        self.eo_a_qs = [
            ExitTicketQuestion.objects.create(
                exit_ticket=self.exit_ticket,
                question_text=f'A Q{i}',
                enabling_objective='EO A',
                correct_answer='A',
                order_index=200 + i,
            ) for i in range(4)
        ]
        self.eo_b_qs = [
            ExitTicketQuestion.objects.create(
                exit_ticket=self.exit_ticket,
                question_text=f'B Q{i}',
                enabling_objective='EO B',
                correct_answer='A',
                order_index=210 + i,
            ) for i in range(4)
        ]

    def test_two_per_eo_default(self):
        queue = build_remediation_requiz_queue(
            self.lesson, failed_eos=['EO A', 'EO B'],
            seed=1,
        )
        self.assertEqual(len(queue), 4)  # 2 per EO * 2 EOs
        eos = [(q.enabling_objective or '') for q in queue]
        # Order: all EO A first, then EO B (caller passes lesson order)
        self.assertEqual(eos, ['EO A', 'EO A', 'EO B', 'EO B'])

    def test_excludes_walked_questions(self):
        """Walked questions must NOT reappear in re-quiz when fresh
        ones are available — the user explicitly asked for variety."""
        walked = [self.eo_a_qs[0].id, self.eo_a_qs[1].id]
        queue = build_remediation_requiz_queue(
            self.lesson, failed_eos=['EO A'],
            walkthrough_question_ids=walked, seed=1,
        )
        chosen_ids = {q.id for q in queue}
        self.assertEqual(chosen_ids & set(walked), set())

    def test_falls_back_to_walked_when_no_fresh_left(self):
        """Two walked, two fresh — but if we ask for 3, the fallback
        SHOULD pull the walked ones rather than dropping the EO. Failed
        EO coverage matters more than freshness (per locked decision)."""
        walked = [q.id for q in self.eo_a_qs]  # all 4 walked
        queue = build_remediation_requiz_queue(
            self.lesson, failed_eos=['EO A'],
            walkthrough_question_ids=walked, seed=1, per_eo=2,
        )
        self.assertEqual(len(queue), 2)  # falls back to walked

    def test_variety_across_seeds(self):
        """Different seeds → different picks. Verifies the random.sample
        actually shuffles — the user wants variety across retakes."""
        s1 = build_remediation_requiz_queue(
            self.lesson, failed_eos=['EO A'], seed=1, per_eo=2,
        )
        s2 = build_remediation_requiz_queue(
            self.lesson, failed_eos=['EO A'], seed=999, per_eo=2,
        )
        # Possible (but unlikely) for two seeds to coincide on the same
        # 2-of-4 pick. Run a third to make the test robust.
        s3 = build_remediation_requiz_queue(
            self.lesson, failed_eos=['EO A'], seed=12345, per_eo=2,
        )
        ids1 = tuple(sorted(q.id for q in s1))
        ids2 = tuple(sorted(q.id for q in s2))
        ids3 = tuple(sorted(q.id for q in s3))
        self.assertGreaterEqual(len({ids1, ids2, ids3}), 2)

    def test_empty_failed_eos_returns_empty(self):
        queue = build_remediation_requiz_queue(
            self.lesson, failed_eos=[], seed=1,
        )
        self.assertEqual(queue, [])

    def test_unknown_eo_skipped(self):
        """An EO with no published bank questions just gets skipped —
        it doesn't crash, doesn't pollute other EOs' picks."""
        queue = build_remediation_requiz_queue(
            self.lesson, failed_eos=['EO A', 'No-Such-EO'],
            seed=1,
        )
        self.assertEqual(len(queue), 2)  # only EO A contributes
        self.assertTrue(all(q.enabling_objective == 'EO A' for q in queue))


class WeightConstantsTest(TestCase):
    """The plan locks specific multipliers (5 / 3 / 1) — guard the
    constants so a future tweak is forced through a deliberate edit."""

    def test_failed_outweighs_unattempted_outweighs_mastered(self):
        self.assertGreater(EO_WEIGHT_FAILED, EO_WEIGHT_UNATTEMPTED)
        self.assertGreater(EO_WEIGHT_UNATTEMPTED, EO_WEIGHT_MASTERED)

    def test_locked_values(self):
        # Per the plan: failed=5x, unattempted=3x, mastered=1x.
        self.assertEqual(EO_WEIGHT_FAILED, 5)
        self.assertEqual(EO_WEIGHT_UNATTEMPTED, 3)
        self.assertEqual(EO_WEIGHT_MASTERED, 1)
