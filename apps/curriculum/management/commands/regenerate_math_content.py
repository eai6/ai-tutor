"""Bulk-regenerate every lesson in every math course.

Triggers the defense layers (1, 2, 3, 4) on every math lesson at
content-generation time. New errors get auto-corrected (Layer 1),
answer-key mismatches get caught (Layer 2), Layer 3 retries once
on unresolved arithmetic, and templated questions (Layer 4) come
out of code rather than the LLM.

What gets preserved across regen:
  - StudentLessonProgress, StudentSkillMastery, TutorSession
  - StudentCompetencyRecord (permanent mastery transcript)
  - Lesson row (PK is stable; FKs survive)

What gets wiped per lesson:
  - LessonStep rows (replaced with new generation)
  - ExitTicket + its ExitTicketQuestion rows (replaced)
  - ExitTicketAttempt history (cascade-deletes with ExitTicket)
    NOTE: the existing course_regenerate_all view has the same
    behaviour. If you need to preserve attempt history, regenerate
    on a per-lesson basis instead.

Cost: ~$0.10–0.40 per lesson + ~2 min each. For ~150 math
lessons that's ~$15–60 and ~50 min serial (or faster if parallel
processing is enabled in your environment).

Usage:
    # Dry run — list what WOULD be regenerated, no LLM calls
    python manage.py regenerate_math_content --dry-run

    # Regenerate all math courses
    python manage.py regenerate_math_content

    # Regenerate one specific course
    python manage.py regenerate_math_content --course-id 5

    # Regenerate first 2 math courses (smoke test before wider run)
    python manage.py regenerate_math_content --max-courses 2

Run against production:
    DATABASE_URL='<prod_pg_url>' \
    python manage.py regenerate_math_content --dry-run
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from apps.curriculum.models import Course, Lesson


class Command(BaseCommand):
    help = "Bulk-regenerate every lesson in every math course."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List affected courses + lessons without running generation.",
        )
        parser.add_argument(
            "--course-id", type=int, default=None,
            help="Regenerate just this one course (still gated to is_math).",
        )
        parser.add_argument(
            "--max-courses", type=int, default=None,
            help="Stop after this many courses (for staged rollouts).",
        )
        parser.add_argument(
            "--force", action="store_true", default=True,
            help="Always regenerate (ignored — bulk regen is force=True by definition).",
        )
        parser.add_argument(
            "--no-confirm", action="store_true",
            help=(
                "Skip the YES prompt. Required when running unattended "
                "(GitHub Actions / cron / az containerapp exec)."
            ),
        )

    def handle(self, *args, **opts):
        from apps.curriculum.content_generator import (
            generate_content_for_course,
        )

        # Collect target courses.
        if opts["course_id"]:
            try:
                course = Course.objects.get(id=opts["course_id"])
            except Course.DoesNotExist:
                self.stderr.write(self.style.ERROR(
                    f"course_id={opts['course_id']} not found"
                ))
                return
            if not course.is_math:
                self.stderr.write(self.style.ERROR(
                    f"Course '{course.title}' is_math=False — skipping"
                ))
                return
            courses = [course]
        else:
            courses = [c for c in Course.objects.all() if c.is_math]

        if opts["max_courses"]:
            courses = courses[: opts["max_courses"]]

        # Pre-flight summary.
        total_lessons = sum(
            Lesson.objects.filter(unit__course=c).count() for c in courses
        )
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{'=' * 70}"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"BULK MATH CONTENT REGENERATION"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{'=' * 70}\n"
        ))
        self.stdout.write(
            f"  Courses to regenerate:   {len(courses)}"
        )
        self.stdout.write(
            f"  Lessons (across all):    {total_lessons}"
        )
        self.stdout.write(
            f"  Estimated time:          {total_lessons * 2 // 60}m+ "
            f"(~2 min/lesson, serial within a course)"
        )
        self.stdout.write(
            f"  Estimated cost:          "
            f"${total_lessons * 0.10:.2f} – ${total_lessons * 0.40:.2f}"
        )
        self.stdout.write("")

        for c in courses:
            n = Lesson.objects.filter(unit__course=c).count()
            self.stdout.write(
                f"  • [{c.id}] {c.title}: {n} lesson(s)"
            )
        self.stdout.write("")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "Dry run — no generation triggered."
            ))
            return

        # Confirm before launching real generation. The check below
        # is paranoid by design: bulk regen is a high-cost, hard-to-
        # reverse operation. Bypassed when --no-confirm is set —
        # required for unattended runs from GitHub Actions, etc.
        self.stdout.write(self.style.WARNING(
            "About to regenerate every math lesson in every listed course."
        ))
        self.stdout.write(self.style.WARNING(
            "This will overwrite LessonStep + ExitTicketQuestion rows + "
            "drop ExitTicketAttempt history. Student progress / mastery "
            "/ competency-transcript records are preserved."
        ))
        if opts["no_confirm"]:
            self.stdout.write(self.style.WARNING(
                "--no-confirm set; proceeding without prompt."
            ))
        else:
            confirm = input("Type YES to proceed (anything else aborts): ")
            if confirm.strip() != "YES":
                self.stdout.write(self.style.WARNING("Aborted."))
                return

        # Run generation, course by course. Lessons within a course
        # run serially (per generate_content_for_course). For
        # parallelism across courses, the dashboard's UI button
        # spawns a thread per course — but a management command
        # runs cleaner serially with deterministic progress.
        results = []
        t_start = time.time()
        for i, c in enumerate(courses, 1):
            t0 = time.time()
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n[{i}/{len(courses)}] {c.title} (id={c.id})"
            ))
            try:
                result = generate_content_for_course(c.id, force=True)
                elapsed = time.time() - t0
                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ {result.get('total_success', 0)} lessons "
                    f"OK, {result.get('total_failed', 0)} failed, "
                    f"{result.get('total_skipped', 0)} skipped "
                    f"in {elapsed:.0f}s"
                ))
                results.append(result)
            except Exception as e:
                elapsed = time.time() - t0
                self.stderr.write(self.style.ERROR(
                    f"  ✗ Course {c.title} failed after {elapsed:.0f}s: {e}"
                ))
                results.append({"course": c.title, "error": str(e)})

        total_elapsed = time.time() - t_start
        ok = sum(r.get("total_success", 0) for r in results)
        failed = sum(r.get("total_failed", 0) for r in results)
        skipped = sum(r.get("total_skipped", 0) for r in results)
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{'=' * 70}"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"COMPLETE — {ok} OK, {failed} failed, {skipped} skipped "
            f"in {total_elapsed / 60:.1f}m"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{'=' * 70}"
        ))
        self.stdout.write(
            "\nNext: open each course's lesson_detail to inspect "
            "the 🧮 Arithmetic Verification panel for unresolved "
            "warnings (READY_WITH_WARNINGS status)."
        )
