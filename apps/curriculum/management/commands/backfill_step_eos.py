"""Backfill `LessonStep.enabling_objective` for steps that lack one.

Martin's pilot showed lessons with most steps untagged (1/10 on
"Angles around a point"). The content-generation pipeline now passes
an llm_client to `_normalize_enabling_objective` so new lessons get
proper tagging — but existing lessons need a one-off pass.

For each lesson with at least one untagged step:
  1. Walk steps with empty enabling_objective.
  2. Ask the snap-LLM to pick the best canonical EO from the
     parent lesson's `enabling_objectives` list, given the step's
     teacher_script + question.
  3. Persist the snap result. If the LLM says "none", leave empty
     and log it for teacher review.

Usage:
    python manage.py backfill_step_eos                    # all lessons
    python manage.py backfill_step_eos --dry-run          # log only
    python manage.py backfill_step_eos --lesson 638       # one lesson
    python manage.py backfill_step_eos --course 12        # one course
    python manage.py backfill_step_eos --limit 50         # cap rows
"""

from __future__ import annotations

import logging
from typing import List, Optional

from django.core.management.base import BaseCommand, CommandError

from apps.curriculum.models import Lesson, LessonStep

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill missing LessonStep.enabling_objective via snap-LLM."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Log proposed snaps without writing to the DB.",
        )
        parser.add_argument(
            "--lesson", type=int, default=None,
            help="Process only this Lesson PK.",
        )
        parser.add_argument(
            "--course", type=int, default=None,
            help="Process all lessons in this Course PK.",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Cap the number of steps processed (canary).",
        )

    def handle(self, *args, **opts):
        from apps.llm.client import get_llm_client
        from apps.llm.models import ModelConfig
        from apps.curriculum.content_generator import (
            _normalize_enabling_objective,
        )

        cfg = ModelConfig.get_for("generation")
        if cfg is None:
            raise CommandError(
                "No active generation ModelConfig — cannot snap EOs without "
                "an LLM. Activate a generation config and retry."
            )
        snap_client = get_llm_client(cfg)

        # Build the lesson queryset
        lessons_qs = Lesson.objects.all()
        if opts.get("lesson") is not None:
            lessons_qs = lessons_qs.filter(pk=opts["lesson"])
        if opts.get("course") is not None:
            lessons_qs = lessons_qs.filter(unit__course__pk=opts["course"])

        dry_run = bool(opts.get("dry_run"))
        limit = opts.get("limit")
        processed = 0
        snapped = 0
        kept_empty = 0
        skipped_no_canonical = 0

        for lesson in lessons_qs.iterator():
            canonical: List[str] = [
                (eo or "").strip()
                for eo in (lesson.enabling_objectives or [])
                if (eo or "").strip()
            ]
            if not canonical:
                # Lesson has no canonical EO list — nothing to snap to.
                # Log so the teacher can populate via the dashboard.
                empties = LessonStep.objects.filter(
                    lesson=lesson, enabling_objective="",
                ).count()
                if empties:
                    self.stdout.write(self.style.WARNING(
                        f"[skip] lesson {lesson.id} '{lesson.title}': "
                        f"{empties} untagged step(s) but lesson has no "
                        f"canonical enabling_objectives. Populate via "
                        f"dashboard, then re-run."
                    ))
                    skipped_no_canonical += empties
                continue

            steps = LessonStep.objects.filter(
                lesson=lesson, enabling_objective="",
            ).order_by("order_index")
            if not steps.exists():
                continue

            self.stdout.write(
                f"[lesson {lesson.id}] '{lesson.title}' — "
                f"{steps.count()} untagged step(s); "
                f"{len(canonical)} canonical EO(s)"
            )

            for step in steps:
                if limit is not None and processed >= limit:
                    break

                # Build the "raw EO" from the step's teacher_script +
                # question. Truncated so the snap LLM has enough to
                # match on without ballooning the prompt.
                signal_parts = [
                    (step.teacher_script or "").strip(),
                    (step.question or "").strip(),
                    (step.concept_tag or "").strip(),
                ]
                raw = " | ".join(p for p in signal_parts if p)[:600]
                if not raw:
                    self.stdout.write(self.style.WARNING(
                        f"  step {step.id} (order {step.order_index}): "
                        f"empty content — leaving untagged"
                    ))
                    kept_empty += 1
                    processed += 1
                    continue

                normalised, status = _normalize_enabling_objective(
                    raw, canonical, llm_client=snap_client,
                )

                if normalised:
                    snapped += 1
                    msg = (
                        f"  step {step.id} (order {step.order_index}, "
                        f"{step.step_type}): SNAPPED → {normalised[:80]}"
                    )
                    self.stdout.write(self.style.SUCCESS(msg))
                    if not dry_run:
                        step.enabling_objective = normalised
                        step.save(update_fields=["enabling_objective"])
                else:
                    kept_empty += 1
                    self.stdout.write(self.style.WARNING(
                        f"  step {step.id} (order {step.order_index}, "
                        f"{step.step_type}): NO MATCH ({status}) — "
                        f"leaving untagged"
                    ))
                processed += 1

            if limit is not None and processed >= limit:
                break

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. processed={processed} snapped={snapped} "
            f"kept_empty={kept_empty} "
            f"skipped_no_canonical={skipped_no_canonical} "
            f"(dry_run={dry_run})"
        ))
