"""Remove the accounts a security scanner left behind.

The 2026-08-13 assessment submitted registration and login attempts as the
placeholder identity 'ZAP' / zaproxy@example.com. Section 2.1 of the report
notes those records may exist in the production database and asks for them to be
purged; the remediation roadmap lists it as a P1 alongside the crash fix, and it
is the one P1 that no code change accomplishes on its own.

Dry-run by default. A command that deletes user accounts and defaults to doing
it is a command that eventually deletes the wrong ones, so the safe invocation
is the one you get by forgetting a flag:

    python manage.py purge_scanner_accounts                 # show what matches
    python manage.py purge_scanner_accounts --delete        # actually delete
    python manage.py purge_scanner_accounts --username zap --email pentest@…

Matching is exact and case-insensitive on username, and exact on email. Not a
substring match: 'zap' appears inside real names, and a `username__icontains`
that catches a student called Zapata is a worse outcome than leaving a scanner
record behind.

Refuses to touch anything with is_staff or is_superuser set. If a scanner
genuinely created a staff account, that is an incident to investigate by hand,
not something to quietly delete.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

DEFAULT_USERNAMES = ['ZAP']
DEFAULT_EMAILS = ['zaproxy@example.com']


class Command(BaseCommand):
    help = 'Delete accounts created by a security scanner (dry-run unless --delete).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--username', action='append', default=None,
            help='Username to purge, exact and case-insensitive. Repeatable. '
                 f'Default: {", ".join(DEFAULT_USERNAMES)}',
        )
        parser.add_argument(
            '--email', action='append', default=None,
            help='Email address to purge, exact. Repeatable. '
                 f'Default: {", ".join(DEFAULT_EMAILS)}',
        )
        parser.add_argument(
            '--delete', action='store_true',
            help='Actually delete. Without this the command only reports.',
        )

    def handle(self, *args, **options):
        usernames = options['username'] or DEFAULT_USERNAMES
        emails = options['email'] or DEFAULT_EMAILS

        criteria = Q()
        for name in usernames:
            criteria |= Q(username__iexact=name)
        for address in emails:
            criteria |= Q(email__iexact=address)

        matches = User.objects.filter(criteria)

        privileged = matches.filter(Q(is_staff=True) | Q(is_superuser=True))
        if privileged.exists():
            self.stderr.write(self.style.ERROR(
                'Refusing to run: these matches carry staff or superuser rights. '
                'A scanner should not have been able to create them, so treat '
                'this as an incident and review them by hand:'
            ))
            for user in privileged:
                self.stderr.write(f'  #{user.pk} {user.username} <{user.email}> '
                                  f'staff={user.is_staff} super={user.is_superuser}')
            return

        if not matches.exists():
            self.stdout.write(self.style.SUCCESS('No scanner accounts found.'))
            return

        self.stdout.write(f'{matches.count()} account(s) match:')
        for user in matches:
            self.stdout.write(
                f'  #{user.pk} {user.username} <{user.email}> joined={user.date_joined:%Y-%m-%d}'
            )

        if not options['delete']:
            self.stdout.write(self.style.WARNING(
                'Dry run — nothing deleted. Re-run with --delete to remove them.'
            ))
            return

        # One transaction: the cascade reaches StudentProfile, Membership,
        # TutorSession and everything hanging off them. A partial delete would
        # leave orphaned sessions pointing at a user that no longer exists.
        with transaction.atomic():
            deleted, per_model = matches.delete()

        self.stdout.write(self.style.SUCCESS(f'Deleted {deleted} row(s):'))
        for model, count in sorted(per_model.items()):
            self.stdout.write(f'  {model}: {count}')
