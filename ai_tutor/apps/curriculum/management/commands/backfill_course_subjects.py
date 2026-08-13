"""Backfill Course.subject_code and Course.grade_levels from legacy fields.

Maps `Course.title` → SubjectCode by keyword, and parses
`Course.grade_level` (free-text CharField, possibly comma-separated) into
the normalised `grade_levels` JSONField list.

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


# Map subject_type → fallback subject_code for rows where title doesn't
# yield a match but the existing subject_type is informative.
SUBJECT_TYPE_FALLBACK = {
    'math': 'mathematics',
}


# Map inferred subject_code → coarse subject_type, so the same backfill
# pass populates BOTH fields. is_math consults subject_type, so leaving
# it empty after backfill forces the legacy MATH_KEYWORDS fallback for
# every read — re-introducing the silent-gap problem v3 audit H3 calls
# out for courses whose titles don't match the keyword list.
SUBJECT_CODE_TO_TYPE = {
    'mathematics':      'math',
    'physics':          'science',
    'chemistry':        'science',
    'biology':          'science',
    'geography':        'humanities',
    'history':          'humanities',
    'english':          'language',
    'french':           'language',
    'computer_science': 'other',
}


# Recognised grade tokens. Match case-insensitively, normalise to upper.
GRADE_PATTERN = re.compile(r'\bs([1-6])\b', re.IGNORECASE)


def infer_subject_code(title: str, subject_type: str = '') -> Optional[str]:
    """Return SubjectCode value or None if no rule matched."""
    if not title:
        return SUBJECT_TYPE_FALLBACK.get(subject_type)
    lower = title.lower()
    for pattern, code in KEYWORD_RULES:
        if re.search(pattern, lower):
            return code
    return SUBJECT_TYPE_FALLBACK.get(subject_type)


def parse_grade_levels(grade_level: str) -> List[str]:
    """Parse 'S1,S2,S3' or 'S3' or 'S1 S2 S3' → ['S1','S2','S3'] (sorted, deduped)."""
    if not grade_level:
        return []
    found = sorted({f"S{m.group(1)}" for m in GRADE_PATTERN.finditer(grade_level)})
    return found


class Command(BaseCommand):
    help = "Backfill Course.subject_code + Course.grade_levels from legacy fields."

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
            help="Overwrite existing subject_code/grade_levels if already set. "
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
                proposed_code = infer_subject_code(c.title, c.subject_type or '')
                proposed_grades = parse_grade_levels(c.grade_level or '')
                proposed_type = (
                    SUBJECT_CODE_TO_TYPE.get(proposed_code) if proposed_code else None
                )

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

                if proposed_type and (overwrite or not c.subject_type):
                    if c.subject_type != proposed_type:
                        changes.append(f"subject_type: {c.subject_type!r} → {proposed_type!r}")
                        if apply_changes:
                            c.subject_type = proposed_type

                if proposed_grades and (overwrite or not c.grade_levels):
                    if c.grade_levels != proposed_grades:
                        changes.append(f"grade_levels: {c.grade_levels!r} → {proposed_grades!r}")
                        if apply_changes:
                            c.grade_levels = proposed_grades

                if changes:
                    will_update += 1
                    inst = c.institution.name if c.institution else 'PLATFORM-WIDE'
                    self.stdout.write(
                        f"  [{c.id:>4}] {c.title[:50]!r:50} ({inst[:25]})\n"
                        + "\n".join(f"        {ch}" for ch in changes) + "\n"
                    )
                    if apply_changes:
                        c.save(update_fields=[
                            'subject_code', 'subject_type', 'grade_levels',
                        ])
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
