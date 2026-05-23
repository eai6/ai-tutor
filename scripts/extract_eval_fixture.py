#!/usr/bin/env python
"""Extract the eval-baseline curriculum into a Django fixture.

Pulls Course / Unit / Lesson / LessonStep / ExitTicket /
ExitTicketQuestion rows for the lessons used in the eval baseline
(L1137 math, L1425 geography), plus the Institution FK target needed
by L1425's parent Course. Writes a Django JSON fixture that
``manage.py loaddata`` can replay on any fresh DB (SQLite or Postgres).

Run from a checkout that has the full prod data loaded
(``python manage.py load_prod_dump prod_content_dump.sql``):

    python scripts/extract_eval_fixture.py > eval-fixtures/baseline.json

Re-run + commit whenever the underlying eval lessons change.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.core import serializers  # noqa: E402

from apps.accounts.models import Institution  # noqa: E402
from apps.curriculum.models import Course, Lesson, LessonStep, Unit  # noqa: E402
from apps.tutoring.models import ExitTicket, ExitTicketQuestion  # noqa: E402


LESSON_IDS = [1137, 1425]


def main():
    lessons = list(Lesson.objects.filter(id__in=LESSON_IDS))
    if len(lessons) != len(LESSON_IDS):
        missing = set(LESSON_IDS) - {l.id for l in lessons}
        sys.stderr.write(
            f"[extract_eval_fixture] missing lessons: {missing}. "
            f"Run `python manage.py load_prod_dump prod_content_dump.sql` first.\n"
        )
        sys.exit(1)

    units = list(Unit.objects.filter(id__in={l.unit_id for l in lessons if l.unit_id}))
    courses = list(Course.objects.filter(id__in={u.course_id for u in units}))
    institutions = list(Institution.objects.filter(
        id__in={c.institution_id for c in courses if c.institution_id},
    ))
    steps = list(LessonStep.objects.filter(lesson_id__in=LESSON_IDS).order_by('lesson_id', 'order_index'))
    exit_tickets = list(ExitTicket.objects.filter(lesson_id__in=LESSON_IDS))
    et_questions = list(ExitTicketQuestion.objects.filter(
        exit_ticket_id__in={e.id for e in exit_tickets},
    ).order_by('exit_ticket_id', 'order_index'))

    # FK order — loaddata respects natural ordering inside a single
    # serialize() call, but we still concatenate parent→child so a
    # human reading the file sees the dependency chain.
    all_objs = (
        institutions
        + courses
        + units
        + lessons
        + steps
        + exit_tickets
        + et_questions
    )

    counts = {
        'institutions': len(institutions),
        'courses': len(courses),
        'units': len(units),
        'lessons': len(lessons),
        'steps': len(steps),
        'exit_tickets': len(exit_tickets),
        'et_questions': len(et_questions),
        'total_rows': len(all_objs),
    }
    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    sys.stderr.write(f"[extract_eval_fixture] {summary}\n")

    serializers.serialize('json', all_objs, indent=2, stream=sys.stdout)


if __name__ == '__main__':
    main()
