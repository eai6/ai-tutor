"""Generate `docs/recent_updates.md` from recent git history.

Run on every container boot (Dockerfile CMD) BEFORE
`build_help_index` so the help-assistant KB reflects the latest
platform changes without anyone having to write a changelog.

Strategy:
  - Pull the last N days (default 30) of commits on the current branch.
  - Filter out chore commits (merge / docs-only / typo / WIP).
  - Group by week-bucket so the file reads as a changelog.
  - Cap at ~80 entries so the doc stays under 1 page after rendering.

We don't need every commit — just the ones whose subject says
something about platform behaviour. The help assistant only needs
recent shifts to override stale docs; it doesn't need a full audit
trail.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


# Commits whose subject matches these patterns are skipped — they
# don't describe platform behaviour the assistant needs to know.
_SKIP_PATTERNS = [
    re.compile(r'^merge\b', re.IGNORECASE),
    re.compile(r'^bump version', re.IGNORECASE),
    re.compile(r'^typo', re.IGNORECASE),
    re.compile(r'^wip\b', re.IGNORECASE),
    re.compile(r'^chore:', re.IGNORECASE),
    re.compile(r'^\s*$'),
]

DEFAULT_DAYS = 30
DEFAULT_MAX_ENTRIES = 80
DEFAULT_OUTPUT = Path('docs/recent_updates.md')


class Command(BaseCommand):
    help = (
        "Generate docs/recent_updates.md from git history so the help "
        "assistant always knows what shipped recently."
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=DEFAULT_DAYS)
        parser.add_argument('--max', type=int, default=DEFAULT_MAX_ENTRIES)
        parser.add_argument('--output', type=str, default=str(DEFAULT_OUTPUT))

    def handle(self, *args, days=DEFAULT_DAYS, max=DEFAULT_MAX_ENTRIES,  # noqa: A002
               output=None, **opts):
        repo_root = Path(settings.BASE_DIR)
        out_path = repo_root / (output or str(DEFAULT_OUTPUT))

        since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d')
        try:
            raw = subprocess.check_output(
                [
                    'git', 'log',
                    f'--since={since}',
                    '--pretty=format:%h%x09%ad%x09%s',
                    '--date=short',
                    '--no-merges',
                    '-n', str(max * 2),  # over-fetch then filter
                ],
                cwd=repo_root, stderr=subprocess.DEVNULL,
            ).decode('utf-8', errors='replace')
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            self.stdout.write(self.style.WARNING(
                f'[recent_updates] git log failed: {e} — writing placeholder'
            ))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(_placeholder())
            return

        rows = []
        for line in raw.splitlines():
            parts = line.split('\t', 2)
            if len(parts) != 3:
                continue
            sha, date, subject = parts
            if any(p.search(subject) for p in _SKIP_PATTERNS):
                continue
            rows.append((sha, date, subject))
            if len(rows) >= max:
                break

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_render(rows, days))
        self.stdout.write(self.style.SUCCESS(
            f'[recent_updates] wrote {len(rows)} entries to {out_path}'
        ))


def _render(rows, days_window: int) -> str:
    """Group rows by ISO week and render as Markdown."""
    by_week: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for sha, date_str, subject in rows:
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d')
            week_key = d.strftime('%Y · week %W')
        except ValueError:
            week_key = 'recent'
        by_week[week_key].append((sha, date_str, subject))

    today = datetime.utcnow().strftime('%Y-%m-%d')
    lines = [
        '# Recent platform updates',
        '',
        f'`[STAFF]` Auto-generated from git history. Window: last '
        f'{days_window} days · regenerated on every deploy. '
        f'Last refresh: {today}.',
        '',
        'The help assistant indexes this file so it knows what '
        'shipped recently. If a doc elsewhere disagrees with an '
        'entry here, this file wins for time-sensitive answers.',
        '',
    ]

    if not rows:
        lines.append('_No notable commits in the window._')
        return '\n'.join(lines) + '\n'

    for week in sorted(by_week.keys(), reverse=True):
        lines.append(f'## {week}')
        lines.append('')
        for sha, date_str, subject in by_week[week]:
            # Render subject with a backticked SHA so the assistant
            # can cite it back to the user precisely.
            lines.append(f'- **{date_str}** · `{sha}` — {subject}')
        lines.append('')
    return '\n'.join(lines) + '\n'


def _placeholder() -> str:
    return (
        "# Recent platform updates\n\n"
        "`[STAFF]` Auto-generated changelog (placeholder — git log "
        "was unavailable when this file was written). Re-run "
        "`python manage.py generate_recent_updates` to refresh.\n"
    )
