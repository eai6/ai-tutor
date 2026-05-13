"""Seed a real-curriculum BenchmarkItem for CI runs of the annotator agent.

Loads ``ops/annotator_agent/fixtures/lesson_angles.json`` — a Django
fixture containing the production-pulled "Angles around a point" lesson
(course, unit, lesson, 10 steps, exit ticket with 35 questions) PLUS
a synthetic-student session and seven SessionTurns from that session
(turns 478–484 spanning the most violation-rich tutor moments).

Then samples one BenchmarkItem from a tutor turn carrying real
``judge_outputs`` (rule violations + multiple-choice authoring), so the
annotator agent has a real-shape item to label — same content the
annotator already saw locally on session 20.

Idempotent — safe to re-run. Used by .github/workflows/annotator_agent.yml
in `full` mode.

Compared to the v1 of this command (hardcoded fake constants), the
fixture-based approach gives the annotator real curriculum context
(actual lesson title, real step text, real prior-conversation history
in the snapshot) without depending on a live database connection.
"""
from __future__ import annotations

import pathlib

from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.benchmark.models import BenchmarkItem
from apps.benchmark.sampling import build_item_snapshot
from apps.tutoring.models import SessionTurn


# Fixture file relative to repo root. The container's WORKDIR is /app
# in the django service, so the path is the same as on the host.
FIXTURE_PATH = pathlib.Path('ops/annotator_agent/fixtures/lesson_angles.json')

# Pick the tutor turn that has the juiciest judge_outputs payload. This
# is the same turn (T481, MATH_S20_T481) that the annotator agent
# successfully labelled as bank_authoring in our local validation —
# pinning it as the CI canonical item keeps run-to-run results
# comparable.
TARGET_TURN_PK = 481


class Command(BaseCommand):
    help = "Idempotently seed one realistic BenchmarkItem for CI agent runs."

    def handle(self, *args, **kwargs) -> None:
        # 1) Load the fixture if the target SessionTurn isn't already
        # in the DB. fixture_path is repo-relative; resolve so we don't
        # depend on the caller's cwd.
        if not SessionTurn.objects.filter(pk=TARGET_TURN_PK).exists():
            if not FIXTURE_PATH.exists():
                raise FileNotFoundError(
                    f"Fixture not found at {FIXTURE_PATH}. "
                    "Run from the repo root, or check that the fixture "
                    "was committed."
                )
            self.stdout.write(self.style.NOTICE(
                f"Loading fixture {FIXTURE_PATH}..."
            ))
            call_command('loaddata', str(FIXTURE_PATH), verbosity=1)
        else:
            self.stdout.write(
                f"Fixture already loaded — turn {TARGET_TURN_PK} exists"
            )

        # 2) Build a snapshot from the target turn and persist as a
        # BenchmarkItem. update_or_create is idempotent across re-runs.
        target = SessionTurn.objects.select_related(
            'session__lesson__unit__course',
        ).get(pk=TARGET_TURN_PK)
        snapshot = build_item_snapshot(target)
        item, created = BenchmarkItem.objects.update_or_create(
            item_id=snapshot['item']['item_id'],
            defaults={
                'source_turn': target,
                'subject': snapshot['item']['subject'],
                'lesson_id': snapshot['item']['lesson_id'],
                'snapshot': snapshot,
                'stratum': 'synthetic_struggler',
            },
        )
        verdict = 'created' if created else 'updated (idempotent)'
        self.stdout.write(self.style.SUCCESS(
            f"BenchmarkItem {item.item_id} {verdict} "
            f"(turn={target.pk}, session={target.session_id}, "
            f"lesson={target.session.lesson.title!r})"
        ))
