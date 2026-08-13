"""Tests for the synthetic-session sampling filter (Phase 4 of the
LLM-student-simulator plan).

Pins:
- candidate_tutor_turns(include_synthetic=False) excludes turns whose
  session is tagged is_synthetic=True.
- include_synthetic=True returns both real and synthetic turns.
- stratify() routes synthetic turns into a synthetic_<persona> bucket
  rather than wrong_answer / validator_flagged / random.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.benchmark.sampling import (
    candidate_tutor_turns,
    sample_proportional,
    stratify,
)
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import SessionTurn, TutorSession


def _make_session(*, is_synthetic: bool, sim_persona: str = '',
                  n_turns: int = 1) -> TutorSession:
    """Build a TutorSession with N tutor turns carrying full
    Phase-2.2.5 tracking so the synthetic-vs-real test isn't shadowed
    by the require_full_tracking filter."""
    ts = timezone.now().timestamp()
    inst = Institution.objects.create(
        name=f"Inst-{ts}", slug=f"inst-{int(ts * 1000)}",
    )
    user = User.objects.create_user(
        username=f"user-{ts}", password='x',
    )
    course = Course.objects.create(
        institution=inst, title='Angles', subject_type='mathematics',
    )
    unit = Unit.objects.create(course=course, title='U1', order_index=1)
    lesson = Lesson.objects.create(
        unit=unit, title='L1', objective='find x', order_index=1,
    )
    session = TutorSession.objects.create(
        student=user, lesson=lesson, institution=inst,
        is_synthetic=is_synthetic, sim_persona=sim_persona,
    )
    for i in range(n_turns):
        SessionTurn.objects.create(
            session=session, role='student', content=f'student {i}',
        )
        SessionTurn.objects.create(
            session=session, role='tutor', content=f'tutor {i}',
            metadata={'is_correct': True, 'judge_history_turns': i},
            judge_outputs={'rule': {'violations': []}},
        )
    return session


class SyntheticExclusionTest(TestCase):
    def test_default_excludes_synthetic_sessions(self):
        _make_session(is_synthetic=False, n_turns=2)
        _make_session(is_synthetic=True, sim_persona='struggler', n_turns=3)
        eligible = list(candidate_tutor_turns())
        # Only the 2 real tutor turns; the 3 synthetic are filtered out.
        self.assertEqual(len(eligible), 2)
        for turn in eligible:
            self.assertFalse(turn.session.is_synthetic)

    def test_include_synthetic_pulls_both(self):
        _make_session(is_synthetic=False, n_turns=2)
        _make_session(is_synthetic=True, sim_persona='struggler', n_turns=3)
        eligible = list(candidate_tutor_turns(include_synthetic=True))
        self.assertEqual(len(eligible), 5)

    def test_synthetic_only_when_no_real(self):
        _make_session(is_synthetic=True, sim_persona='struggler', n_turns=2)
        # Default still excludes — empty pool
        self.assertEqual(list(candidate_tutor_turns()), [])
        # Opt in surfaces them
        self.assertEqual(
            len(list(candidate_tutor_turns(include_synthetic=True))),
            2,
        )


class StratifySyntheticBucketTest(TestCase):
    def test_synthetic_persona_gets_own_bucket(self):
        _make_session(is_synthetic=False, n_turns=2)
        _make_session(is_synthetic=True, sim_persona='struggler', n_turns=2)
        _make_session(is_synthetic=True, sim_persona='probe_resistant', n_turns=1)
        eligible = candidate_tutor_turns(include_synthetic=True)
        strata = stratify(eligible)
        # Real turns land in their normal bucket (random — is_correct=True,
        # not validator_flagged).
        self.assertEqual(len(strata['random']), 2)
        # Each persona gets its own synthetic_<persona> stratum.
        self.assertEqual(len(strata['synthetic_struggler']), 2)
        self.assertEqual(len(strata['synthetic_probe_resistant']), 1)

    def test_synthetic_with_blank_persona_falls_back_to_unknown(self):
        _make_session(is_synthetic=True, sim_persona='', n_turns=1)
        strata = stratify(candidate_tutor_turns(include_synthetic=True))
        self.assertEqual(len(strata.get('synthetic_unknown', [])), 1)


class SampleProportionalSyntheticTest(TestCase):
    """sample_proportional() must surface synthetic_<persona> strata via
    the leftover pool when the default mix's three strata can't fill
    the limit. Regression: an earlier version silently dropped any
    stratum not named in the mix dict."""

    def test_synthetic_only_pool_returns_via_leftover(self):
        _make_session(is_synthetic=True, sim_persona='struggler', n_turns=3)
        strata = stratify(candidate_tutor_turns(include_synthetic=True))
        # Mix-defined strata are all empty; only synthetic_struggler has content.
        picks = sample_proportional(strata, limit=2, seed=1)
        self.assertEqual(len(picks), 2)
        for stratum_name, _ in picks:
            self.assertEqual(stratum_name, 'synthetic_struggler')

    def test_mixed_real_and_synthetic_both_sampled(self):
        # 2 real (random bucket) + 5 synthetic
        _make_session(is_synthetic=False, n_turns=2)
        _make_session(is_synthetic=True, sim_persona='struggler', n_turns=5)
        strata = stratify(candidate_tutor_turns(include_synthetic=True))
        # Limit 5 with default mix (50/30/20). With only 'random' having
        # real content (2 turns), the rest of the budget should pull
        # from synthetic via the leftover pool.
        picks = sample_proportional(strata, limit=5, seed=2)
        names = [n for n, _ in picks]
        self.assertEqual(len(picks), 5)
        # Both stratum types should appear in some draw.
        self.assertGreaterEqual(names.count('synthetic_struggler'), 1)
