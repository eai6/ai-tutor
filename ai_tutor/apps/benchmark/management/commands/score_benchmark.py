"""Score the benchmark — compute pass-rate over annotations and persist a BenchmarkRun.

Usage:

    python manage.py score_benchmark
    python manage.py score_benchmark --annotator-role llm_judge
    python manage.py score_benchmark --run-llm-judge \
        --judge-model gemini-2.5-pro --notes "after coherence history shipped"
    python manage.py score_benchmark --notes "baseline" --with-agreement

The default reads annotations matching ``--system-variant`` and
``--annotator-role`` and computes pass rate sliced by subject,
eval_layer, stratum, and history-aware vs not.

``--run-llm-judge`` invokes the Gemini cross-check judge first to
populate any missing LLM annotations, then scores. ``--with-agreement``
attaches a human↔LLM agreement block when both annotation sets exist
for the same items.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from ai_tutor.apps.benchmark.llm_judge import (
    DEFAULT_JUDGE_MODEL,
    run_llm_judge_on_items,
)
from ai_tutor.apps.benchmark.models import (
    BenchmarkAnnotation,
    BenchmarkRun,
)
from ai_tutor.apps.benchmark.scoring import compute_metrics


class Command(BaseCommand):
    help = "Score the benchmark and persist a BenchmarkRun."

    def add_arguments(self, parser):
        parser.add_argument(
            '--system-variant', default='production_v1',
            choices=[v for v, _ in BenchmarkAnnotation.SystemVariant.choices],
            help="Which tutor system to score. Default: production_v1.",
        )
        parser.add_argument(
            '--annotator-role', default='human',
            choices=[v for v, _ in BenchmarkAnnotation.Annotator.choices],
            help="Whose annotations to score (human or llm_judge). "
                 "Default: human.",
        )
        parser.add_argument(
            '--run-llm-judge', action='store_true',
            help="Invoke the Gemini cross-check judge to populate "
                 "LLM-judge annotations BEFORE scoring.",
        )
        parser.add_argument(
            '--judge-model', default=DEFAULT_JUDGE_MODEL,
            help=f"Model for the LLM cross-check. Default: {DEFAULT_JUDGE_MODEL}.",
        )
        parser.add_argument(
            '--overwrite-llm-annotations', action='store_true',
            help="When --run-llm-judge, re-label items that already have "
                 "an LLM annotation for this model.",
        )
        parser.add_argument(
            '--with-agreement', action='store_true',
            help="Attach human↔LLM agreement metrics. Requires both "
                 "annotator roles to cover the same items.",
        )
        parser.add_argument(
            '--notes', default='',
            help="Free-form context tag for this run.",
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Print metrics without persisting a BenchmarkRun.",
        )

    def handle(self, *args, **options):
        system_variant = options['system_variant']
        annotator_role = options['annotator_role']
        notes = options['notes']

        if options['run_llm_judge']:
            self.stdout.write(self.style.NOTICE(
                f"[score_benchmark] running LLM judge ({options['judge_model']}) "
                f"on items missing an annotation…"
            ))
            try:
                judge_result = run_llm_judge_on_items(
                    model_name=options['judge_model'],
                    system_variant=system_variant,
                    overwrite=options['overwrite_llm_annotations'],
                )
            except RuntimeError as e:
                raise CommandError(str(e))
            self.stdout.write(self.style.NOTICE(
                f"  judge ran: total={judge_result.total} "
                f"succeeded={judge_result.succeeded} "
                f"skipped={judge_result.skipped}"
            ))
            if judge_result.skip_reasons:
                self.stdout.write("  skip reasons:")
                for item_id, reason in list(judge_result.skip_reasons.items())[:20]:
                    self.stdout.write(f"    {item_id}: {reason}")

        primary = BenchmarkAnnotation.objects.filter(
            system_variant=system_variant,
            annotator_role=annotator_role,
        ).select_related('item')

        primary_list = list(primary)
        if not primary_list:
            raise CommandError(
                f"No annotations found for system_variant={system_variant} "
                f"annotator_role={annotator_role}. Annotate some items first."
            )

        cross = None
        if options['with_agreement']:
            other_role = (
                BenchmarkAnnotation.Annotator.LLM_JUDGE
                if annotator_role == BenchmarkAnnotation.Annotator.HUMAN
                else BenchmarkAnnotation.Annotator.HUMAN
            )
            cross = list(BenchmarkAnnotation.objects.filter(
                system_variant=system_variant,
                annotator_role=other_role,
            ).select_related('item'))
            if not cross:
                self.stdout.write(self.style.WARNING(
                    "  --with-agreement requested but no cross-check "
                    f"annotations exist (looking for annotator_role={other_role})."
                ))

        metrics = compute_metrics(primary_list, cross_check_annotations=cross)

        passed = metrics['overall']['passed']
        total = metrics['overall']['total']
        failed = metrics['overall']['failed']
        pass_rate = metrics['overall']['pass_rate']

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"[score_benchmark] {system_variant} / {annotator_role}"
        ))
        self.stdout.write(
            f"  overall: {passed}/{total} pass ({pass_rate * 100:.1f}%)"
        )
        for slice_name, buckets in metrics['slices'].items():
            self.stdout.write(f"  {slice_name}:")
            for bucket, stats in sorted(buckets.items()):
                self.stdout.write(
                    f"    {bucket}: {stats['passed']}/{stats['total']} "
                    f"({stats['pass_rate'] * 100:.1f}%)"
                )
        if metrics['failure_categories']:
            self.stdout.write("  failure_categories:")
            for cat, count in metrics['failure_categories'].items():
                self.stdout.write(f"    {cat}: {count}")
        if 'agreement' in metrics:
            ag = metrics['agreement']
            self.stdout.write(
                f"  agreement: {ag['agree']}/{ag['overlap_items']} "
                f"({ag['agreement_rate'] * 100:.1f}%)"
            )

        if options['dry_run']:
            self.stdout.write(self.style.NOTICE(
                "[score_benchmark] --dry-run — no BenchmarkRun saved."
            ))
            return

        run = BenchmarkRun.objects.create(
            system_variant=system_variant,
            annotator_role=annotator_role,
            total_items=total,
            passed=passed,
            failed=failed,
            metrics=metrics,
            notes=notes,
        )
        self.stdout.write(self.style.SUCCESS(
            f"[score_benchmark] saved BenchmarkRun #{run.id}"
        ))
