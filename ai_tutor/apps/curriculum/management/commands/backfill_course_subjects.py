"""Backfill Course.subject_code from the title.

Maps `Course.title` → SubjectCode by keyword, for courses created before the
upload form collected a subject.

One field, because the others are derived now — `Course.subject_type` maps
from `subject_code` and `Course.grade_levels` parses `grade_level`, so there
is nothing left to keep in step (steps 3 and 4 of
`memory/subject_grade_unification_plan.md`). Grade tokens are still parsed,
but only to decide whether a course with no inferable subject is worth
reporting as unmapped.

Usage:
    python manage.py backfill_course_subjects --dry-run
    python manage.py backfill_course_subjects --apply

Always dry-run first. Unmapped rows print to stderr — admin should
manually fix them via the dashboard rather than guessing.
"""

import re
import sys
from typing import List, Optional

from django.core.management.base import BaseCommand
from django.db import transaction


# Curated keyword → SubjectCode mapping. Order matters: longer / more
# specific patterns FIRST so e.g. "computer science" matches before
# "science". Match against lowercased title.
KEYWORD_RULES = [
    # (pattern, SubjectCode value)
    (r'\bcomputer\s*science\b|\bcompsci\b|\bcs\b',     'computer_science'),
    (r'\bmath(s|ematics)?\b|\balgebra\b|\bgeometry\b|\bcalculus\b|\btrigonometry\b', 'mathematics'),
    (r'\bgeography\b|\bgeographie\b|\bgeo\b',           'geography'),
    (r'\bphysics\b',                                     'physics'),
    (r'\bchemistry\b|\bchem\b',                          'chemistry'),
    (r'\bbiology\b|\bbio\b',                             'biology'),
    (r'\benglish\b',                                     'english'),
    (r'\bfrench\b|\bfran[cç]ais\b',                      'french'),
    (r'\bhistory\b|\bhistoire\b',                        'history'),
]


# Recognised grade tokens. Match case-insensitively, normalise to upper.
GRADE_PATTERN = re.compile(r'\bs([1-6])\b', re.IGNORECASE)


def infer_subject_code(title: str) -> Optional[str]:
    """Return SubjectCode value or None if no rule matched.

    The subject_type fallback that used to sit here ('math' → 'mathematics',
    for a title the keywords missed) is gone: subject_type maps FROM
    subject_code now, so it is empty exactly when subject_code is, and
    consulting it to fill subject_code was asking the answer to supply itself.
    """
    if not title:
        return None
    lower = title.lower()
    for pattern, code in KEYWORD_RULES:
        if re.search(pattern, lower):
            return code
    return None


def parse_grade_levels(grade_level: str) -> List[str]:
    """Parse 'S1,S2,S3' or 'S3' or 'S1 S2 S3' → ['S1','S2','S3'] (sorted, deduped)."""
    if not grade_level:
        return []
    found = sorted({f"S{m.group(1)}" for m in GRADE_PATTERN.finditer(grade_level)})
    return found


class Command(BaseCommand):
    help = "Backfill Course.subject_code from the title."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true', default=False,
            help="Print proposed changes; do NOT save. Default behavior.",
        )
        parser.add_argument(
            '--apply', action='store_true', default=False,
            help="Actually write the inferred fields to the DB.",
        )
        parser.add_argument(
            '--overwrite', action='store_true', default=False,
            help="Overwrite an existing subject_code if already set. "
                 "Default: only fill empty fields.",
        )

    def handle(self, *args, **options):
        from ai_tutor.apps.curriculum.models import Course

        if not options['dry_run'] and not options['apply']:
            self.stderr.write(self.style.ERROR(
                "Specify --dry-run (preview) or --apply (write to DB)."
            ))
            sys.exit(1)
        if options['dry_run'] and options['apply']:
            self.stderr.write(self.style.ERROR("Pass either --dry-run OR --apply, not both."))
            sys.exit(1)

        apply_changes = options['apply']
        overwrite = options['overwrite']

        courses = Course.objects.all().order_by('id')
        total = courses.count()
        will_update = 0
        unmapped = []

        self.stdout.write(f"Surveying {total} course(s)...\n")

        @transaction.atomic
        def _do_backfill():
            nonlocal will_update
            for c in courses:
                proposed_code = infer_subject_code(c.title)
                proposed_grades = parse_grade_levels(c.grade_level or '')

                # Skip when nothing usable — log and move on
                if not proposed_code and not proposed_grades:
                    unmapped.append(c)
                    continue

                changes = []
                if proposed_code and (overwrite or not c.subject_code):
                    if c.subject_code != proposed_code:
                        changes.append(f"subject_code: {c.subject_code!r} → {proposed_code!r}")
                        if apply_changes:
                            c.subject_code = proposed_code

                # No subject_type branch. It maps from subject_code now, so
                # writing the code above is the whole job — and the column it
                # used to write is not a field any more.

                # No grade branch any more. grade_levels is derived from
                # grade_level, and proposed_grades came from parsing that same
                # field — so it could only ever propose what the property
                # already returns. There is nothing left to keep in step.

                if changes:
                    will_update += 1
                    inst = c.institution.name if c.institution else 'PLATFORM-WIDE'
                    self.stdout.write(
                        f"  [{c.id:>4}] {c.title[:50]!r:50} ({inst[:25]})\n"
                        + "\n".join(f"        {ch}" for ch in changes) + "\n"
                    )
                    if apply_changes:
                        c.save(update_fields=['subject_code'])
                elif not c.subject_code and not c.grade_levels:
                    # Nothing to write but also nothing was set — log
                    inst = c.institution.name if c.institution else 'PLATFORM-WIDE'
                    self.stdout.write(self.style.WARNING(
                        f"  [{c.id:>4}] {c.title[:50]!r:50} ({inst[:25]}) — proposed nothing"
                    ))

        _do_backfill()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"{will_update}/{total} course(s) {'updated' if apply_changes else 'WOULD be updated'}"
        ))
        if unmapped:
            self.stderr.write(self.style.WARNING(
                f"\n{len(unmapped)} course(s) had NO inferable subject_code AND no grade tokens — "
                f"manual fix needed via dashboard:"
            ))
            for c in unmapped:
                inst = c.institution.name if c.institution else 'PLATFORM-WIDE'
                self.stderr.write(
                    f"  [{c.id:>4}] {c.title!r} grade={c.grade_level!r} ({inst})"
                )
        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                "\n(dry-run: nothing written. Re-run with --apply when satisfied.)"
            ))
