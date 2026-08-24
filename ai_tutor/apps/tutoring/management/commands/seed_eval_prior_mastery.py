"""Give the eval's simulator student a realistic history, so the warm-up fires.

Why this exists. A warm-up question is drawn from a lesson the student has
ALREADY MASTERED (simple_tutor/warm_up.py::_mastered_lesson_ids). Every eval
session runs as a fresh simulator-bot with no history, so
``select_warm_up_question`` returns None for every scenario and
``_settle_warm_up_step`` skips straight to step 1. Measured 2026-08-23 across
68 sessions: 0 warm-ups, on a benchmark whose whole reason for moving to this
branch was to exercise the warm-up feature.

The model of a realistic student. Someone reaching lesson N of a unit has
worked the lessons before it. So for every lesson in scope, mark the EARLIER
lessons of the same unit mastered — ordered by (unit.order_index,
lesson.order_index) — and stamp ``last_attempt_at`` going backwards in time so
recency ordering is meaningful. The first lesson of the first unit legitimately
gets nothing: a student on their first lesson has nothing to recall, and the
engine skipping the warm-up there is correct behaviour, not a gap.

``last_attempt_at`` and not ``last_session_at``: warm_up.py orders on the
former, and notes that the latter looks like the right field and is never
written by anything.

Deterministic and idempotent. The eval reuses one simulator-bot per
institution, so mastery earned mid-sweep would make a scenario's warm-up depend
on which scenarios ran before it. Seeding a fixed baseline before the run
removes that ordering dependency; re-running rewrites the same rows.

    python manage.py seed_eval_prior_mastery [--dry-run] [--clear]
"""
import datetime as dt

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.models import Lesson
from ai_tutor.apps.tutoring.models import StudentLessonProgress
from ai_tutor.apps.tutoring.student_sim.driver import _get_or_create_sim_user


class Command(BaseCommand):
    help = "Seed prior-mastery rows for the eval simulator student."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change, write nothing.')
        parser.add_argument('--clear', action='store_true',
                            help='Delete the simulator students\' progress rows '
                                 'and stop. Use to restore a no-history baseline.')

    def handle(self, *args, dry_run=False, clear=False, **kwargs):
        sims = {}
        for inst in Institution.objects.all():
            try:
                sims[inst.id] = _get_or_create_sim_user(inst)
            except Exception as exc:                          # noqa: BLE001
                self.stderr.write(f'  skip institution {inst.id}: {exc}')

        if clear:
            n = StudentLessonProgress.objects.filter(
                student__in=list(sims.values())).count()
            if not dry_run:
                StudentLessonProgress.objects.filter(
                    student__in=list(sims.values())).delete()
            self.stdout.write(self.style.WARNING(
                f'{"would delete" if dry_run else "deleted"} {n} progress row(s)'))
            return

        # Lessons ordered the way a student meets them.
        lessons = list(
            Lesson.objects
            .select_related('unit__course__institution')
            .order_by('unit__course_id', 'unit__order_index', 'order_index', 'id')
        )
        # Grouped by COURSE, not unit. Grouping by unit was the first attempt
        # and it seeded every unit of every course, which left the simulator
        # with 80 mastered lessons spanning maths and geography. warm_up.py has
        # no LessonPrerequisite rows to work from in these fixtures, so it falls
        # through to recency — and recency across an undifferentiated pile
        # opened a weathering lesson with "a shape has a line of symmetry".
        # A student working a geography course has geography history.
        by_course: dict[int, list] = {}
        for les in lessons:
            by_course.setdefault(getattr(les.unit, 'course_id', None), []).append(les)
        by_unit = by_course

        now = timezone.now()
        made = skipped = 0
        rows = []
        for unit_lessons in by_unit.values():
            for pos, les in enumerate(unit_lessons):
                if pos == 0:
                    skipped += 1        # first lesson of a course: nothing prior
                    continue
                course_inst = getattr(
                    getattr(getattr(les, 'unit', None), 'course', None),
                    'institution', None)
                inst_id = getattr(course_inst, 'id', None)
                # Platform-wide courses carry institution=None, but
                # StudentLessonProgress.institution is NOT NULL. Fall back to
                # the institution the simulator student belongs to — that is
                # also what _institution_scope matches on, so a row written
                # under any other institution would be invisible to the
                # warm-up query even though it existed.
                student, owner_inst = None, None
                if inst_id in sims:
                    student, owner_inst = sims[inst_id], inst_id
                elif sims:
                    owner_inst, student = next(iter(sims.items()))
                if student is None or owner_inst is None:
                    continue
                # Everything before it in the unit, most recent last.
                for back, prior in enumerate(reversed(unit_lessons[:pos]), start=1):
                    rows.append((student, prior, now - dt.timedelta(days=back),
                                 owner_inst))
                    made += 1

        if dry_run:
            self.stdout.write(
                f'would write {len(set((s.id, l.id) for s, l, _, _ in rows))} '
                f'distinct progress row(s) across {len(by_unit)} unit(s); '
                f'{skipped} first-in-unit lesson(s) left with no history')
            return

        seen = set()
        with transaction.atomic():
            for student, lesson, stamp, owner_inst in rows:
                key = (student.id, lesson.id)
                if key in seen:
                    continue
                seen.add(key)
                StudentLessonProgress.objects.update_or_create(
                    student=student, lesson=lesson,
                    defaults={
                        'institution_id': owner_inst,
                        'mastery_level': StudentLessonProgress.MasteryLevel.MASTERED,
                        'best_score': 0.9,
                        'last_attempt_at': stamp,
                    },
                )
        # Prerequisites — tier 1 of warm-up selection, ahead of recency.
        #
        # Without these, select_warm_up_question always falls through to
        # _recent_lessons, and the simulator's mastery spans every course in
        # its institution, so a weathering lesson opened with "an isosceles
        # triangle has two equal sides". Production lessons carry prerequisite
        # edges; these fixtures have 126 rows total and only 2 across the eight
        # hg1 lessons. Linking each lesson to its immediate predecessor in the
        # SAME course is the minimum that makes tier 1 fire and keeps a warm-up
        # on-subject.
        from ai_tutor.apps.tutoring.skills_models import LessonPrerequisite

        linked = 0
        with transaction.atomic():
            for course_lessons in by_unit.values():
                for pos, les in enumerate(course_lessons):
                    if pos == 0:
                        continue
                    prev = course_lessons[pos - 1]
                    _, created = LessonPrerequisite.objects.get_or_create(
                        lesson=les, prerequisite=prev,
                        defaults={'strength': 0.9},
                    )
                    linked += created

        self.stdout.write(self.style.SUCCESS(
            f'seeded {len(seen)} mastered-lesson row(s) for '
            f'{len(sims)} simulator student(s); '
            f'{skipped} first-in-course lesson(s) intentionally left with none; '
            f'added {linked} prerequisite edge(s)'))
