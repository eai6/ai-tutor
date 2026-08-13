"""Bulk-regenerate generated math content.

Three independent scopes — pick what you want to refresh based on
how aggressive a refresh you can afford:

  --scope=steps          (default — non-destructive to attempts)
      Regenerates LessonStep rows ONLY. Triggers Layer 1+3 defenses
      on step content (teacher_script, hints, educational_content,
      worked examples). Exit-ticket questions and summative bank
      are LEFT AS-IS — so existing ExitTicketAttempt history
      survives.

  --scope=exit-tickets   (drops attempts)
      Regenerates ExitTicketQuestion rows ONLY (no step regen).
      Triggers Layer 1+2+4 on questions. Existing
      ExitTicketAttempt history is wiped (cascade-delete from
      ExitTicket replacement).

  --scope=summative      (drops summative attempts only)
      Rebuilds the per-course summative bank from each lesson's
      CURRENT exit ticket — no LLM call for sampling, but old
      summative attempts are dropped.

  --scope=all            (everything)
      Steps + exit tickets + summative. Maximum coverage,
      maximum disruption to history.

What's preserved REGARDLESS of scope:
  - StudentLessonProgress, StudentSkillMastery, TutorSession
  - StudentCompetencyRecord (permanent mastery transcript)
  - Lesson, Unit, Course rows (PKs stable, FKs survive)

Cost guides:
  - Steps: ~$0.10–0.40 per lesson + ~2 min each
  - Exit tickets: ~$0.05–0.20 per lesson + ~30s each
  - Summative: free (sampling from existing exit tickets)

Examples:
    # Dry run — list scope, no LLM calls
    python manage.py regenerate_math_content --dry-run

    # Steps only (default, preserves attempts)
    python manage.py regenerate_math_content

    # Exit tickets only (drops attempts; required to fix
    # Layer 1+2 errors in already-generated questions)
    python manage.py regenerate_math_content --scope=exit-tickets

    # Full refresh of everything math
    python manage.py regenerate_math_content --scope=all

    # Pilot one course before wider run
    python manage.py regenerate_math_content --scope=all --course-id 5

Run against production:
    DATABASE_URL='<prod_pg_url>' \
    python manage.py regenerate_math_content --dry-run
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from ai_tutor.apps.curriculum.models import Course, Lesson


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
        parser.add_argument(
            "--scope",
            choices=["steps", "exit-tickets", "summative", "all"],
            default="steps",
            help=(
                "Which content to regenerate. 'steps' (default) is "
                "non-destructive to attempt history. 'exit-tickets' "
                "and 'all' drop ExitTicketAttempt rows. See module "
                "docstring for details."
            ),
        )

    def handle(self, *args, **opts):
        from ai_tutor.apps.curriculum.content_generator import (
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

        scope = opts["scope"]
        regen_steps = scope in ("steps", "all")
        regen_et = scope in ("exit-tickets", "all")
        regen_summative = scope in ("summative", "all")

        # Pre-flight summary.
        total_lessons = sum(
            Lesson.objects.filter(unit__course=c).count() for c in courses
        )
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{'=' * 70}"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"BULK MATH CONTENT REGENERATION  scope={scope}"
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
        self.stdout.write(f"  Will regenerate:")
        self.stdout.write(
            f"    • Lesson steps:          {'YES' if regen_steps else 'no'}"
        )
        self.stdout.write(
            f"    • Exit tickets:          "
            f"{'YES (drops attempts)' if regen_et else 'no (preserved)'}"
        )
        self.stdout.write(
            f"    • Summative bank:        "
            f"{'YES (drops summative attempts)' if regen_summative else 'no'}"
        )

        # Cost / time estimates per scope. Steps dominate; exit-
        # ticket regen is one LLM call per lesson (~30s); summative
        # is sampling-only, no LLM.
        est_min = 0
        est_dollar_low = 0.0
        est_dollar_high = 0.0
        if regen_steps:
            est_min += total_lessons * 2
            est_dollar_low += total_lessons * 0.10
            est_dollar_high += total_lessons * 0.40
        if regen_et:
            est_min += int(total_lessons * 0.5)
            est_dollar_low += total_lessons * 0.05
            est_dollar_high += total_lessons * 0.20
        self.stdout.write(
            f"  Estimated time:          {est_min}m+ (serial within a course)"
        )
        self.stdout.write(
            f"  Estimated cost:          "
            f"${est_dollar_low:.2f} – ${est_dollar_high:.2f}"
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
            "About to regenerate math content in every listed course."
        ))
        if regen_et:
            self.stdout.write(self.style.WARNING(
                "Exit-ticket regeneration WILL drop ExitTicketAttempt "
                "history. Student mastery / competency-transcript "
                "records are preserved."
            ))
        if regen_summative:
            self.stdout.write(self.style.WARNING(
                "Summative-bank rebuild drops summative-attempt rows "
                "for these courses."
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
        # Lazy imports for the scope-specific generators so an
        # 'exit-tickets' or 'summative' run doesn't pay for the
        # instructor client unless it needs it.
        from ai_tutor.apps.curriculum.content_generator import (
            generate_exit_ticket_for_lesson,
        )
        from ai_tutor.apps.tutoring.summative_generator import (
            generate_summative_for_course,
        )

        results = []
        t_start = time.time()
        for i, c in enumerate(courses, 1):
            t0 = time.time()
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n[{i}/{len(courses)}] {c.title} (id={c.id})"
            ))
            course_result = {"course": c.title}

            # 1. Steps regen (uses _generate_exit_ticket internally
            # but with skip-if-exists semantics; exit tickets are
            # NOT replaced here — that happens in step 2 if requested).
            if regen_steps:
                try:
                    r = generate_content_for_course(c.id, force=True)
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ steps: {r.get('total_success', 0)} OK, "
                        f"{r.get('total_failed', 0)} failed, "
                        f"{r.get('total_skipped', 0)} skipped"
                    ))
                    course_result.update({
                        "steps_success": r.get("total_success", 0),
                        "steps_failed": r.get("total_failed", 0),
                        "steps_skipped": r.get("total_skipped", 0),
                    })
                except Exception as e:
                    self.stderr.write(self.style.ERROR(
                        f"  ✗ steps regen failed: {e}"
                    ))
                    course_result["steps_error"] = str(e)

            # 2. Exit-ticket regen (force_regenerate=True replaces
            # the ExitTicketQuestion rows + drops ExitTicketAttempt
            # history via cascade).
            if regen_et:
                lessons = Lesson.objects.filter(unit__course=c).order_by(
                    "unit__order_index", "order_index",
                )
                et_ok = 0
                et_fail = 0
                for lesson in lessons:
                    try:
                        r = generate_exit_ticket_for_lesson(
                            lesson, c.institution_id,
                            force_regenerate=True,
                        )
                        if r.get("success"):
                            et_ok += 1
                        else:
                            et_fail += 1
                            self.stderr.write(self.style.WARNING(
                                f"    ! [{lesson.title[:40]}] "
                                f"exit ticket: {r.get('error')}"
                            ))
                    except Exception as e:
                        et_fail += 1
                        self.stderr.write(self.style.ERROR(
                            f"    ✗ [{lesson.title[:40]}] "
                            f"exit ticket crashed: {e}"
                        ))
                self.stdout.write(self.style.SUCCESS(
                    f"  ✓ exit tickets: {et_ok} OK, {et_fail} failed"
                ))
                course_result["et_success"] = et_ok
                course_result["et_failed"] = et_fail

            # 3. Summative bank rebuild (no LLM call — samples from
            # current lesson exit tickets).
            if regen_summative:
                try:
                    r = generate_summative_for_course(c)
                    self.stdout.write(self.style.SUCCESS(
                        f"  ✓ summative: "
                        f"{r.get('questions_created', 0)} questions, "
                        f"{r.get('lessons_processed', 0)} lessons sampled"
                    ))
                    course_result.update({
                        "sum_questions": r.get("questions_created", 0),
                        "sum_lessons": r.get("lessons_processed", 0),
                    })
                except Exception as e:
                    self.stderr.write(self.style.ERROR(
                        f"  ✗ summative rebuild failed: {e}"
                    ))
                    course_result["summative_error"] = str(e)

            elapsed = time.time() - t0
            self.stdout.write(
                f"  course total: {elapsed:.0f}s"
            )
            results.append(course_result)

        total_elapsed = time.time() - t_start
        ok = sum(r.get("steps_success", 0) for r in results)
        failed = sum(r.get("steps_failed", 0) for r in results)
        skipped = sum(r.get("steps_skipped", 0) for r in results)
        et_ok_total = sum(r.get("et_success", 0) for r in results)
        et_fail_total = sum(r.get("et_failed", 0) for r in results)
        sum_q_total = sum(r.get("sum_questions", 0) for r in results)
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{'=' * 70}"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"COMPLETE in {total_elapsed / 60:.1f}m  scope={scope}"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{'=' * 70}"
        ))
        if regen_steps:
            self.stdout.write(
                f"  Steps:           {ok} OK, {failed} failed, "
                f"{skipped} skipped"
            )
        if regen_et:
            self.stdout.write(
                f"  Exit tickets:    {et_ok_total} OK, "
                f"{et_fail_total} failed (across all courses)"
            )
        if regen_summative:
            self.stdout.write(
                f"  Summative bank:  {sum_q_total} questions sampled "
                f"across {len(courses)} course(s)"
            )
        self.stdout.write(
            "\nNext: open each course's lesson_detail to inspect "
            "the 🧮 Arithmetic Verification panel for unresolved "
            "warnings (READY_WITH_WARNINGS status)."
        )
