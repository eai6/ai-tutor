"""Flatten the correct-answer letter distribution in the MCQ bank.

    python manage.py rebalance_mcq_letters              # dry run (default)
    python manage.py rebalance_mcq_letters --apply
    python manage.py rebalance_mcq_letters --apply --institution 17

Measured 2026-08-06 across 7,073 authored MCQs:

    A:  877  12.4%
    B: 4286  60.6%
    C: 1618  22.9%
    D:  292   4.1%

A student who answers B to everything scores 60.6% on a bank where blind
guessing should score 25%. With `ExitTicket.passing_score` as the mastery
threshold, that is a passing grade for no knowledge — the measurement the whole
platform rests on is compromised.

This matters more since the tutor became catalog-only (2026-08-06,
memory/catalog_only_questions_plan.md): the tutor can no longer author its own
questions, so every question a student sees comes from this bank and inherits
its bias undiluted.

HOW IT WORKS

For each eligible question, the correct option's TEXT is swapped with the text
at a target letter, and correct_answer is set to that target. A transposition,
not a reshuffle — every option keeps its wording, only two positions trade
places. Targets are assigned cyclically over a seeded shuffle of the eligible
set, so the result is near-uniform and reproducible for a given --seed.

WHAT IS SKIPPED, AND WHY

- Questions already shown to a student (their id appears in any session's
  `engine_state['selected_exit_ticket_ids']`). Past attempts store the letter
  the student picked, and the dashboard reconstructs their answer against the
  question's CURRENT option text (`dashboard/views.py::_mcq_letter`). Moving
  options under a completed attempt would silently misreport what a student
  chose — pilot data has research value and must not be corrupted.
  Override with --include-answered only if you accept that.
- Position-dependent options: "all/none of the above" style (17 questions).
  Their correctness depends on being last.
- Options that are entirely numeric AND already sorted (97 questions).
  Relocating the answer would unsort them, which reads as a mistake.
- Questions with fewer than 2 non-empty options, or no valid A-D answer.
"""
from __future__ import annotations

import random
import re
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.tutoring.models import ExitTicketQuestion, TutorSession

LETTERS = ('A', 'B', 'C', 'D')

_POSITIONAL = re.compile(
    r'\b(all|none|both)\s+of\s+(the\s+)?(above|these|them)\b'
    r'|\ball\s+of\s+them\b|\bnone\s+of\s+them\b',
    re.I,
)


def _opts(q) -> dict[str, str]:
    return {L: (getattr(q, f'option_{L.lower()}', '') or '').strip() for L in LETTERS}


def _numeric_value(text: str):
    if not re.fullmatch(r'\s*-?[\d,]+(?:\.\d+)?\s*[^\d]{0,4}\s*', text or ''):
        return None
    stripped = re.sub(r'[^\d.\-]', '', text or '')
    try:
        return float(stripped)
    except ValueError:
        return None


def _skip_reason(q, touched: set[int]) -> str | None:
    if q.id in touched:
        return 'already_shown'
    opts = _opts(q)
    present = {L: t for L, t in opts.items() if t}
    if len(present) < 2:
        return 'too_few_options'
    correct = (q.correct_answer or '').strip().upper()
    if correct not in present:
        return 'no_valid_correct_letter'
    if any(_POSITIONAL.search(t) for t in present.values()):
        return 'positional_option'
    values = [_numeric_value(t) for t in present.values()]
    if all(v is not None for v in values):
        ordered = list(present.values())
        nums = [_numeric_value(t) for t in ordered]
        if nums == sorted(nums) or nums == sorted(nums, reverse=True):
            return 'sorted_numeric'
    return None


class Command(BaseCommand):
    help = 'Flatten the correct-answer letter distribution across the MCQ bank.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes. Without this it is a dry run.')
        parser.add_argument('--seed', type=int, default=20260806,
                            help='Seed for target assignment. Same seed = same result.')
        parser.add_argument('--institution', type=int, default=None,
                            help='Restrict to one institution id (scoping is on '
                                 'lesson.unit.course.institution).')
        parser.add_argument('--include-answered', action='store_true',
                            help='Also rebalance questions already shown to a '
                                 'student. Misreports historical attempts — see '
                                 'the module docstring.')

    def handle(self, *args, apply, seed, institution, include_answered, **kwargs):
        qs = ExitTicketQuestion.objects.filter(question_type='mcq')
        if institution is not None:
            qs = qs.filter(
                exit_ticket__lesson__unit__course__institution_id=institution)

        questions = list(qs)
        if not questions:
            self.stdout.write('No MCQs matched.')
            return

        touched: set[int] = set()
        if not include_answered:
            for state in (TutorSession.objects
                          .exclude(engine_state={})
                          .values_list('engine_state', flat=True)):
                for qid in (state or {}).get('selected_exit_ticket_ids') or []:
                    if isinstance(qid, int):
                        touched.add(qid)

        before = Counter((q.correct_answer or '').strip().upper() for q in questions)

        eligible, skipped = [], Counter()
        for q in questions:
            reason = _skip_reason(q, touched)
            if reason:
                skipped[reason] += 1
            else:
                eligible.append(q)

        # Cyclic target assignment over a seeded shuffle → near-uniform and
        # reproducible. Assigning "whatever is rarest so far" would be greedy
        # and order-dependent; this is neither.
        rng = random.Random(seed)
        order = list(eligible)
        rng.shuffle(order)

        changes = []
        for i, q in enumerate(order):
            present = [L for L in LETTERS if _opts(q)[L]]
            target = present[i % len(present)]
            current = (q.correct_answer or '').strip().upper()
            if target == current:
                continue
            changes.append((q, current, target))

        after = Counter(before)
        for q, current, target in changes:
            after[current] -= 1
            after[target] += 1

        total = len(questions)
        self.stdout.write(f'MCQs matched: {total}')
        self.stdout.write(f'  eligible : {len(eligible)}')
        self.stdout.write(f'  skipped  : {sum(skipped.values())}')
        for reason, n in skipped.most_common():
            self.stdout.write(f'      {reason:<24} {n}')
        self.stdout.write(f'  swaps to write: {len(changes)}\n')

        self.stdout.write('distribution   before -> after')
        for L in LETTERS:
            b, a = before.get(L, 0), after.get(L, 0)
            self.stdout.write(
                f'   {L}: {b:>5} ({100*b/total:5.1f}%)  ->  {a:>5} ({100*a/total:5.1f}%)')

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDry run — nothing written. Re-run with --apply.'))
            return

        with transaction.atomic():
            for q, current, target in changes:
                cur_field, tgt_field = f'option_{current.lower()}', f'option_{target.lower()}'
                cur_text = getattr(q, cur_field)
                setattr(q, cur_field, getattr(q, tgt_field))
                setattr(q, tgt_field, cur_text)
                q.correct_answer = target
                q.save(update_fields=[cur_field, tgt_field, 'correct_answer'])

        self.stdout.write(self.style.SUCCESS(f'\nWrote {len(changes)} questions.'))
