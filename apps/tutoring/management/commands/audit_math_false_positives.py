"""Audit historical chat history for math false-positive praise.

Finds cases where the tutor used praise words ("brilliant", "correct!",
"you got it", etc.) in a response AND the immediately-prior student turn
contained a numeric answer that did NOT match the step's expected answer.

Produces a CSV for manual review (no PII beyond session_id + student_id).
Used to baseline the production impact of the math-tutor false-positive
bug before the fix ships, and to regression-test the fix after.

See memory/math_tutor_fix_plan.md Phase M5.

Usage:
    python manage.py audit_math_false_positives --output /tmp/audit.csv
    python manage.py audit_math_false_positives --limit 500 --since 2026-01-01
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils.dateparse import parse_date

from apps.tutoring.models import SessionTurn
from apps.tutoring.grader import parse_math_answer, numeric_equals
from apps.tutoring.praise_filter import _PRAISE_RE

# Keyword set for identifying math lessons — same heuristic as Course.is_math
# (apps/curriculum/models.py). Kept here as a fallback so the audit also
# picks up math lessons regardless of whether is_math ran at save time.
_MATH_COURSE_RE = re.compile(
    r"\b(math|maths|mathematics|algebra|geometry|calculus|fraction|arithmetic|trigonometry)\b",
    re.IGNORECASE,
)


class Command(BaseCommand):
    help = "Audit historical tutor turns for math false-positive praise."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default="math_false_positives_audit.csv",
            help="Output CSV path (default: math_false_positives_audit.csv)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max tutor turns to scan (0 = unlimited).",
        )
        parser.add_argument(
            "--since",
            type=str,
            default=None,
            help="ISO date (YYYY-MM-DD). Only turns on/after this date.",
        )
        parser.add_argument(
            "--only-course",
            type=str,
            default=None,
            help="Only this course title (exact match). Useful for smoke runs.",
        )
        parser.add_argument(
            "--include-correct",
            action="store_true",
            help="Also include matched-numeric cases in the CSV (for contrast).",
        )

    def handle(self, *args, **options):
        output_path = Path(options["output"])
        limit = options["limit"]
        since_raw = options["since"]
        only_course = options["only_course"]
        include_correct = options["include_correct"]

        since = None
        if since_raw:
            since = parse_date(since_raw)
            if since is None:
                raise CommandError(f"--since must be YYYY-MM-DD, got: {since_raw!r}")

        qs = (
            SessionTurn.objects.filter(role="tutor")
            .select_related(
                "session",
                "step",
                "session__lesson",
                "session__lesson__unit",
                "session__lesson__unit__course",
                "session__student",
            )
            .order_by("created_at")
        )

        if since:
            qs = qs.filter(created_at__date__gte=since)

        if only_course:
            qs = qs.filter(session__lesson__unit__course__title=only_course)
        else:
            qs = qs.filter(
                Q(session__lesson__unit__course__title__iregex=_MATH_COURSE_RE.pattern)
            )

        total_scanned = 0
        total_praise = 0
        total_numeric_checked = 0
        total_false_positive = 0
        total_correct = 0

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "tutor_turn_id",
                    "session_id",
                    "student_id",
                    "course_title",
                    "lesson_title",
                    "step_index",
                    "step_type",
                    "created_at",
                    "student_said",
                    "student_parsed",
                    "expected_answer",
                    "expected_parsed",
                    "verdict",
                    "praise_hits",
                    "tutor_said_first_120",
                ],
            )
            writer.writeheader()

            for tutor_turn in qs.iterator(chunk_size=500):
                total_scanned += 1
                if limit and total_scanned > limit:
                    break

                praise_hits = _PRAISE_RE.findall(tutor_turn.content or "")
                if not praise_hits:
                    continue
                total_praise += 1

                # Prior student turn in the same session
                prior = (
                    SessionTurn.objects.filter(
                        session_id=tutor_turn.session_id,
                        created_at__lt=tutor_turn.created_at,
                        role="student",
                    )
                    .order_by("-created_at")
                    .first()
                )
                if not prior:
                    continue

                # Need a step with an expected_answer for comparison
                step = tutor_turn.step
                if not step or not (step.expected_answer or "").strip():
                    continue

                student_parsed = parse_math_answer(prior.content)
                expected_parsed = parse_math_answer(step.expected_answer)
                if student_parsed is None or expected_parsed is None:
                    continue

                total_numeric_checked += 1
                is_correct = numeric_equals(student_parsed, expected_parsed)
                verdict = "correct" if is_correct else "FALSE_POSITIVE"

                if is_correct:
                    total_correct += 1
                    if not include_correct:
                        continue
                else:
                    total_false_positive += 1

                lesson = tutor_turn.session.lesson if tutor_turn.session else None
                course = lesson.unit.course if (lesson and lesson.unit) else None

                writer.writerow(
                    {
                        "tutor_turn_id": tutor_turn.id,
                        "session_id": tutor_turn.session_id,
                        "student_id": getattr(tutor_turn.session, "student_id", None)
                        if tutor_turn.session
                        else None,
                        "course_title": course.title if course else "",
                        "lesson_title": lesson.title if lesson else "",
                        "step_index": getattr(step, "order_index", ""),
                        "step_type": getattr(step, "step_type", ""),
                        "created_at": tutor_turn.created_at.isoformat(),
                        "student_said": (prior.content or "")[:200],
                        "student_parsed": student_parsed,
                        "expected_answer": (step.expected_answer or "")[:200],
                        "expected_parsed": expected_parsed,
                        "verdict": verdict,
                        "praise_hits": ", ".join(praise_hits)[:200],
                        "tutor_said_first_120": (tutor_turn.content or "")[:120],
                    }
                )

        self.stdout.write(self.style.SUCCESS("Audit complete."))
        self.stdout.write(f"  tutor turns scanned:     {total_scanned}")
        self.stdout.write(f"  praise-containing turns: {total_praise}")
        self.stdout.write(f"  numerically checkable:   {total_numeric_checked}")
        self.stdout.write(f"  FALSE POSITIVES:         {total_false_positive}")
        if include_correct:
            self.stdout.write(f"  correctly praised:       {total_correct}")
        self.stdout.write(f"\n  CSV written to: {output_path.resolve()}")
        if total_numeric_checked > 0:
            rate = 100.0 * total_false_positive / total_numeric_checked
            self.stdout.write(f"  false-positive rate:     {rate:.1f}%")
