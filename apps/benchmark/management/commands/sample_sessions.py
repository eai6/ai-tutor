"""Sample production sessions for session-level pedagogical evaluation.

    python manage.py sample_sessions --limit 200 --keep 20
    python manage.py sample_sessions --dry-run          # screen, write nothing

Every sampled session lands at status='pending_review'. NOTHING this command
writes is annotatable until a human approves it in the review UI — the command
cannot produce an approved item, by construction.

Plan: memory/session_eval_framework_plan.md
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.benchmark import session_sampling as S
from apps.benchmark.models import SessionEvalItem


class Command(BaseCommand):
    help = 'Sample, redact and safety-screen production sessions for evaluation.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=200,
                            help='How many candidate sessions to screen.')
        parser.add_argument('--keep', type=int, default=20,
                            help='How many of the screened survivors to keep '
                                 '(drawn uniformly at random).')
        parser.add_argument('--institution', type=int, default=None,
                            help='Institution id to restrict to.')
        parser.add_argument('--course', type=int, default=None,
                            help='Course id to restrict to.')
        parser.add_argument('--start', default=None,
                            help='Only sessions started on/after YYYY-MM-DD.')
        parser.add_argument('--end', default=None,
                            help='Only sessions started on/before YYYY-MM-DD.')
        parser.add_argument('--prefix', default='SESS',
                            help='item_id prefix.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Screen and report; write nothing.')
        parser.add_argument('--no-llm', action='store_true',
                            help='Skip the LLM name pass. Testing only — it '
                                 'is the only gate that catches a third '
                                 "party's name in free text.")

    def handle(self, *args, **opts):
        institution = None
        if opts['institution']:
            from apps.accounts.models import Institution
            try:
                institution = Institution.objects.get(pk=opts['institution'])
            except Institution.DoesNotExist:
                raise CommandError(f'No institution {opts["institution"]}')

        if opts['no_llm']:
            self.stdout.write(self.style.WARNING(
                'Running WITHOUT the LLM name pass. A classmate\'s name typed '
                'in free text will not be caught. Do not use for a real sample.'
            ))

        from datetime import datetime

        def _date(key):
            raw = opts.get(key)
            if not raw:
                return None
            try:
                return datetime.strptime(raw, '%Y-%m-%d').date()
            except ValueError:
                raise CommandError(f'--{key} must be YYYY-MM-DD, got {raw!r}')

        # draw_pool shuffles before slicing. Slicing the queryset directly
        # would take the NEWEST --limit sessions and silently exclude the
        # older half of the dataset.
        candidates = S.draw_pool(
            S.candidate_sessions(institution=institution,
                                 course_id=opts.get('course'),
                                 start=_date('start'), end=_date('end')),
            opts['limit'], seed=0,
        )
        if not candidates:
            self.stdout.write('No candidate sessions.')
            return

        self.stdout.write(f'Screening {len(candidates)} sessions…')

        # sample() screens everything, then draws uniformly at random.
        selected, rejections = S.sample(
            candidates, keep=opts['keep'],
        ) if not opts['no_llm'] else _sample_no_llm(
            candidates, opts['keep'],
        )

        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('Rejections'))
        if rejections:
            for reason, count in sorted(rejections.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f'  {count:>4}  {reason}')
        else:
            self.stdout.write('  none')

        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Selected {len(selected)} sessions (uniform random draw)'))
        # Reported, not selected on — a wildly lopsided draw is worth seeing.
        strata = {}
        for _, stratum, _ in selected:
            strata[stratum] = strata.get(stratum, 0) + 1
        for stratum, count in sorted(strata.items()):
            self.stdout.write(f'  {count:>4}  {stratum}')

        if opts['dry_run']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('--dry-run: nothing written.'))
            return

        created = 0
        with transaction.atomic():
            for session, stratum, record in selected:
                if SessionEvalItem.objects.filter(source_session=session).exists():
                    continue
                subject = stratum.split('|')[0]
                item_id = f'{opts["prefix"]}_{subject.upper()[:8]}_{session.id}'
                SessionEvalItem.objects.create(
                    item_id=item_id,
                    source_session=session,
                    session_key=record['session_key'],
                    subject=subject,
                    lesson_id=session.lesson_id,
                    engine=session.engine,
                    outcome=stratum.split('|')[-1],
                    turn_count=len(record['transcript']),
                    transcript=record['transcript'],
                    redaction_report=record['redaction_report'],
                    status=record['status'],
                    stratum=stratum,
                )
                created += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {created} items at status=pending_review.'))
        self.stdout.write(
            'None is annotatable yet. Approve them in the review UI '
            '(dashboard → Developer → Session Evaluation).'
        )


def _sample_no_llm(candidates, keep):
    """--no-llm variant of S.sample(). Kept out of the library so the library
    default can never be the weaker path."""
    import random

    survivors, rejections, prepared = [], {}, {}
    for session in candidates:
        record = S.screen_and_prepare(session, use_llm=False)
        if record['reject_reason']:
            rejections[record['reject_reason']] = (
                rejections.get(record['reject_reason'], 0) + 1)
            continue
        prepared[session.id] = record
        survivors.append(session)

    random.Random(0).shuffle(survivors)
    selected = [(s, S.stratum_of(s), prepared[s.id]) for s in survivors[:keep]]
    return selected, rejections
