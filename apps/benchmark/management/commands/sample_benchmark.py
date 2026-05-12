"""Sample tutor turns from production into BenchmarkItem rows.

Usage::

    # Sample 50 math + geography turns, stratified, idempotent
    python manage.py sample_benchmark --limit 50

    # Math only, with a specific stratification mix
    python manage.py sample_benchmark --limit 25 --subject math

    # Dry-run: report what would be sampled without persisting
    python manage.py sample_benchmark --limit 50 --dry-run

    # Deterministic sampling for reproducible benchmark snapshots
    python manage.py sample_benchmark --limit 50 --seed 42

Per ``memory/eval_benchmark_v2_simplified.md``, this is Phase 2.1 of the
benchmark infrastructure. The command:

1. Queries tutor SessionTurn rows joined with TutorSession + Lesson +
   Course (filtering out test/incomplete sessions).
2. Excludes turns already represented by a BenchmarkItem.source_turn FK
   (idempotent across runs).
3. Stratifies into 3 buckets: wrong_answer (50%), validator_flagged
   (30%), random (20%). The v2 plan's step_transition bucket is deferred
   to a later iteration — requires cross-turn comparison.
4. Builds the frozen snapshot per the v2 schema, auto-derives suggested
   labels from pipeline_trace + judge_outputs.
5. Persists BenchmarkItem rows.

Phase 2.2's annotation UI reads ``snapshot['production']['suggested_labels']``
to pre-fill the annotator's ``actual_labels`` form field. Edward
confirms/overrides the suggestions, fills the 4 pure-judgment labels
the pipeline can't catch, and authors expected_labels + rationale.
"""
from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from apps.benchmark.models import BenchmarkItem
from apps.benchmark.sampling import (
    build_item_snapshot,
    candidate_tutor_turns,
    stratify,
    sample_proportional,
)


class Command(BaseCommand):
    help = "Sample tutor turns from production into BenchmarkItem rows."

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=50,
            help='Total number of items to sample (default 50).',
        )
        parser.add_argument(
            '--subject', choices=['math', 'geography', 'science'],
            default=None,
            help='Restrict to one subject. Omit for all eligible subjects.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be sampled without persisting.',
        )
        parser.add_argument(
            '--seed', type=int, default=None,
            help='Random seed for reproducible sampling.',
        )
        parser.add_argument(
            '--include-legacy', action='store_true',
            help='Include pre-Phase-2.2.5 tutor turns that lack the '
                 'full quality-tracking shape (judge_outputs + '
                 'judge_history_turns metadata). Default: only sample '
                 'from new sessions with complete instrumentation.',
        )

    def handle(self, *args, limit, subject, dry_run, seed, include_legacy,
               **options):
        self.stdout.write(self.style.NOTICE(
            f"[sample_benchmark] limit={limit} subject={subject or 'all'} "
            f"dry_run={dry_run} seed={seed} "
            f"include_legacy={include_legacy}"
        ))

        candidates = candidate_tutor_turns(
            subject=subject,
            require_full_tracking=not include_legacy,
        )
        candidate_count = candidates.count()
        self.stdout.write(f"  {candidate_count} eligible tutor turns (excluding already-sampled)")

        if candidate_count == 0:
            raise CommandError("No eligible turns found. Check subject filter or "
                               "whether all matching turns are already sampled.")

        strata = stratify(candidates)
        self.stdout.write("  Stratum counts:")
        for name, items in strata.items():
            self.stdout.write(f"    {name}: {len(items)}")

        picks = sample_proportional(strata, limit=limit, seed=seed)
        self.stdout.write(f"  Sampled {len(picks)} items")

        stratum_counter = Counter(name for name, _ in picks)
        for name, count in stratum_counter.most_common():
            self.stdout.write(f"    {name}: {count}")

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "  Dry run — not persisting. Sampled IDs:"
            ))
            for stratum_name, turn in picks[:10]:
                self.stdout.write(f"    {stratum_name}: turn={turn.id} session={turn.session_id}")
            if len(picks) > 10:
                self.stdout.write(f"    ... and {len(picks) - 10} more")
            return

        created = 0
        skipped = 0
        for stratum_name, turn in picks:
            snapshot = build_item_snapshot(turn)
            item_id = snapshot['item']['item_id']
            subject_value = snapshot['item']['subject']

            obj, was_created = BenchmarkItem.objects.update_or_create(
                item_id=item_id,
                defaults={
                    'source_turn': turn,
                    'subject': subject_value,
                    'lesson_id': snapshot['item']['lesson_id'],
                    'snapshot': snapshot,
                    'stratum': stratum_name,
                },
            )
            if was_created:
                created += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"  Created {created} new BenchmarkItem rows ({skipped} already existed)"
        ))
        self.stdout.write("  Next step: 'python manage.py shell' or browse to the "
                          "BenchmarkItem admin to inspect snapshots; "
                          "Phase 2.2 will add an annotation UI.")
