"""Seed a minimal BenchmarkItem for CI runs of the annotator agent.

The annotator-agent workflow boots an empty SQLite. Running the full
simulator + sampler in CI is real-money work (~$1/run on Anthropic);
for the loop validation we only need ONE realistic-shape BenchmarkItem
the agent can annotate.

This command is idempotent — safe to re-run. Creates:
    Institution(slug='ci-fixture')
    Course(title='CI Fixture — Math', subject_type='math')
    Unit(title='CI U1')
    Lesson(title='CI L1', content_status='ready')
    User(username='ci-sim-bot')
    TutorSession(is_synthetic=True, sim_persona='struggler')
    SessionTurn(role='student'), SessionTurn(role='tutor', judge_outputs=...)
    BenchmarkItem(item_id='CI_S<id>_T<id>', stratum='synthetic_struggler')

The tutor turn carries a realistic ``judge_outputs`` payload mirroring
what the live judge stack populates, so the agent's auto-population
verification rule fires the same way it would on real data.

Used by .github/workflows/annotator_agent.yml in `full` mode.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.accounts.models import Institution
from apps.benchmark.models import BenchmarkItem
from apps.benchmark.sampling import build_item_snapshot
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import SessionTurn, TutorSession


# Realistic judge_outputs shape — mirrors what
# apps/tutoring/conversational_tutor.py:_save_turn writes to the row.
# The tutor turn we author is one where the tutor invented MCQ choices
# not in the bank (rule.violations=['NO_AUTHORING']) AND used unfounded
# praise (rule.violations also includes 'RULE_1') — gives the agent
# concrete labels to verify.
_TUTOR_TEXT = (
    "Excellent work! You correctly identified that all angles around a "
    "point sum to 360°. Now try this: 5 equal angles meet at a point. "
    "What is the measure of each angle? A) 90° B) 72° C) 60° D) 45°"
)
_STUDENT_TEXT = "the rule is they add up to 360 degrees."

_JUDGE_OUTPUTS = {
    'step_eval': {
        'answer_correct': True,
        'step_complete': False,
        'reasoning': 'Student stated the 360° rule correctly.',
    },
    'arithmetic': {'corrections': []},
    'factual': {'claims_checked': 0, 'claims_contradicted': []},
    'rule': {'violations': ['NO_AUTHORING', 'RULE_1']},
    'coherence': {'violations': []},
    'figure_ref': {'issues': []},
    'figure_vision': {'aligned': None, 'mismatch_reason': ''},
    'safety': {'severity': 'none', 'categories': [], 'reasoning': ''},
    'history_turns_used': 0,
    'prompt_versions': {'tutor': 'ci-fixture-v1'},
    'skipped': False,
    'skip_reason': '',
    'sub_skipped': [],
}

_METADATA = {
    'is_correct': True,
    'validator_passed': False,
    'validator_issues': ['rule1_violation', 'authoring_violation'],
    'regenerated': True,
    'judge_history_turns': 0,
    'eval_layer': 'combined_judge',
    'step_type': 'evaluate',
    'bare_answer': False,
}


class Command(BaseCommand):
    help = "Idempotently seed one synthetic BenchmarkItem for CI agent runs."

    def handle(self, *args, **kwargs) -> None:
        inst, _ = Institution.objects.get_or_create(
            slug='ci-fixture',
            defaults={'name': 'CI Fixture'},
        )
        course, _ = Course.objects.get_or_create(
            institution=inst, title='CI Fixture — Math',
            defaults={'subject_type': 'math'},
        )
        unit, _ = Unit.objects.get_or_create(
            course=course, title='CI U1',
            defaults={'order_index': 1},
        )
        lesson, _ = Lesson.objects.get_or_create(
            unit=unit, title='CI L1',
            defaults={
                'objective': 'Find missing angles around a point.',
                'order_index': 1,
                'content_status': 'ready',
            },
        )
        bot, _ = User.objects.get_or_create(
            username='ci-sim-bot',
            defaults={
                'email': 'ci-sim-bot@simulator.local',
                'is_active': False,
            },
        )

        session = TutorSession.objects.filter(
            student=bot, lesson=lesson, sim_persona='struggler',
        ).first()
        if session is None:
            session = TutorSession.objects.create(
                student=bot, lesson=lesson, institution=inst,
                status=TutorSession.Status.ACTIVE,
                is_synthetic=True, sim_persona='struggler',
            )
            SessionTurn.objects.create(
                session=session, role='student', content=_STUDENT_TEXT,
            )
            SessionTurn.objects.create(
                session=session, role='tutor', content=_TUTOR_TEXT,
                metadata=_METADATA, judge_outputs=_JUDGE_OUTPUTS,
            )

        tutor_turn = (
            SessionTurn.objects.filter(session=session, role='tutor')
            .order_by('id').last()
        )
        snapshot = build_item_snapshot(tutor_turn)
        item, created = BenchmarkItem.objects.update_or_create(
            item_id=snapshot['item']['item_id'],
            defaults={
                'source_turn': tutor_turn,
                'subject': snapshot['item']['subject'],
                'lesson_id': snapshot['item']['lesson_id'],
                'snapshot': snapshot,
                'stratum': 'synthetic_struggler',
            },
        )
        verdict = 'created' if created else 'updated (idempotent)'
        self.stdout.write(self.style.SUCCESS(
            f"BenchmarkItem {item.item_id} {verdict} "
            f"(session={session.id}, lesson={lesson.id})"
        ))
