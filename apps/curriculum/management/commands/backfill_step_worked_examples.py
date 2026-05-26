"""Backfill ``LessonStep.educational_content.worked_example`` for steps
that have a ``teacher_script`` but no authored worked-example JSON.

Motivation. The v2 tutor's ``worked_example`` move (and its safety-
terminal fallback) lifts content from
``LessonStep.educational_content.worked_example`` to produce labelled-
subgoal walkthroughs. Older lessons were generated before that field
was a first-class contract, so many steps have the narrative
``teacher_script`` only — when the safety floor fires on those steps
it degrades to "restate the open question and ask for the first step"
instead of shipping an actual worked example.

This command walks ``LessonStep`` rows where:
  * ``step_type`` is one of ``teach`` / ``worked_example`` /
    ``practice`` (steps the tutor can land on under ``worked_example``);
  * ``teacher_script`` is non-empty;
  * ``educational_content.worked_example`` is missing or empty.

For each match it asks the platform's GENERATION ModelConfig to render
the teacher_script into the structured
``{problem, steps:[{action, explanation}], final_answer}`` JSON used
by ``apps/tutoring/v2/services/context_manager._render_worked_example_text``.
Subject-agnostic — the prompt asks the model to use the lesson's own
vocabulary.

Behaviour:
  * No LLM client configured → exits cleanly with a count of skipped
    rows (no destructive side effects).
  * Invalid LLM JSON → skipped with a warning; the row is left
    untouched so a future run can retry.
  * ``--dry-run`` lists the rows that would be backfilled without
    issuing LLM calls.

Usage::

    python manage.py backfill_step_worked_examples
    python manage.py backfill_step_worked_examples --dry-run
    python manage.py backfill_step_worked_examples --course 15
    python manage.py backfill_step_worked_examples --lesson 1167
    python manage.py backfill_step_worked_examples --limit 25
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.curriculum.models import Lesson, LessonStep

logger = logging.getLogger(__name__)


_WORKED_EXAMPLE_SYSTEM = """\
You convert a teacher's narrative explanation into a structured worked
example a student can follow step by step. Output JSON only.

Schema:
{
  "problem": "<one-sentence statement of the example problem>",
  "steps": [
    {"action": "<short imperative step>", "explanation": "<one sentence WHY>"}
  ],
  "final_answer": "<short final value / phrase / conclusion>"
}

Rules:
- Use the lesson's vocabulary and notation verbatim where possible.
- 2 to 4 steps.
- Numbers and named entities must come from the lesson text.
- If the lesson text defines a concept (no numeric problem), make
  ``problem`` a recall question on the concept and the steps a
  narrated decomposition; ``final_answer`` is the concept name or
  short definition.
- Return JSON only — no markdown, no prose.
"""


def _build_user_prompt(lesson: Lesson, step: LessonStep) -> str:
    return (
        f"Lesson title: {lesson.title}\n"
        f"Lesson objective: {lesson.objective or ''}\n"
        f"Step objective: {step.enabling_objective or ''}\n"
        f"Step question (if any): {step.question or ''}\n\n"
        f"Teacher narrative:\n{step.teacher_script.strip()}\n\n"
        f"Worked-example JSON:"
    )


class Command(BaseCommand):
    help = "Backfill LessonStep.educational_content.worked_example via LLM."

    def add_arguments(self, parser):
        parser.add_argument("--lesson", type=int, default=None,
                            help="Single lesson id")
        parser.add_argument("--course", type=int, default=None,
                            help="All lessons in this course")
        parser.add_argument("--limit", type=int, default=None,
                            help="Max steps to backfill in this run")
        parser.add_argument("--dry-run", action="store_true",
                            help="List candidates only; no LLM calls")

    def handle(self, *args, **options):
        steps_qs = LessonStep.objects.filter(
            step_type__in=("teach", "worked_example", "practice"),
        ).exclude(teacher_script="").select_related("lesson", "lesson__unit", "lesson__unit__course")
        if options["lesson"]:
            steps_qs = steps_qs.filter(lesson_id=options["lesson"])
        if options["course"]:
            steps_qs = steps_qs.filter(lesson__unit__course_id=options["course"])

        candidates: list[LessonStep] = []
        for step in steps_qs:
            edu = step.educational_content or {}
            if not isinstance(edu, dict):
                edu = {}
            we = edu.get("worked_example") or {}
            if isinstance(we, dict) and (we.get("problem") or we.get("steps") or we.get("final_answer")):
                continue
            candidates.append(step)

        if options["limit"]:
            candidates = candidates[: options["limit"]]

        self.stdout.write(f"[backfill_we] {len(candidates)} step(s) need backfill")

        if options["dry_run"] or not candidates:
            for s in candidates[:25]:
                self.stdout.write(
                    f"  L{s.lesson_id} step#{s.order_index}: "
                    f"{(s.teacher_script or '')[:80]!r}"
                )
            return

        client = self._build_llm_client()
        if client is None:
            self.stdout.write(self.style.WARNING(
                "[backfill_we] no GENERATION ModelConfig available — skipping LLM calls. "
                "Set ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY and seed a GENERATION ModelConfig."
            ))
            return

        backfilled = 0
        skipped = 0
        for step in candidates:
            lesson = step.lesson
            try:
                resp = client.generate(
                    messages=[{"role": "user", "content": _build_user_prompt(lesson, step)}],
                    system_prompt=_WORKED_EXAMPLE_SYSTEM,
                    max_tokens=600,
                )
                raw = (resp.content or "").strip()
                # Strip optional fences just in case.
                if raw.startswith("```"):
                    raw = raw.strip("`")
                    if raw.lower().startswith("json"):
                        raw = raw[4:].strip()
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("LLM response is not a JSON object")
                if not payload.get("steps"):
                    raise ValueError("LLM response missing 'steps'")
                edu = step.educational_content or {}
                if not isinstance(edu, dict):
                    edu = {}
                edu["worked_example"] = payload
                step.educational_content = edu
                step.save(update_fields=["educational_content", "updated_at"])
                backfilled += 1
                self.stdout.write(
                    f"  ✓ L{lesson.id} step#{step.order_index}: "
                    f"problem={(payload.get('problem') or '')[:60]!r}"
                )
            except Exception as exc:
                skipped += 1
                logger.warning(
                    "[backfill_we] L%s step#%s skipped: %s",
                    lesson.id, step.order_index, exc,
                )
                self.stdout.write(self.style.WARNING(
                    f"  ⚠ L{lesson.id} step#{step.order_index} skipped: {type(exc).__name__}"
                ))

        self.stdout.write(self.style.SUCCESS(
            f"[backfill_we] done — backfilled={backfilled}, skipped={skipped}"
        ))

    def _build_llm_client(self):
        """Resolve a generation-purpose LLM client. Returns None if absent."""
        try:
            from apps.llm.models import ModelConfig
            from apps.llm.client import get_llm_client
            cfg = (
                ModelConfig.objects.filter(purpose="generation", is_active=True).first()
                or ModelConfig.objects.filter(purpose="tutoring", is_active=True).first()
            )
            if cfg is None:
                return None
            return get_llm_client(cfg)
        except Exception as exc:
            logger.warning("[backfill_we] could not build LLM client: %s", exc)
            return None
