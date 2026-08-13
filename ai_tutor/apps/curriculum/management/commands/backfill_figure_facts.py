"""Backfill `MediaAsset.figure_facts` for existing figures (F3 of
memory/figure_facts_plan.md).

For every MediaAsset with `figure_facts IS NULL`, sends the image to
a vision-capable LLM, parses the structured response, and saves it.
Idempotent — re-running skips already-extracted rows unless --force.

Usage:
    python manage.py backfill_figure_facts             # extract every NULL row
    python manage.py backfill_figure_facts --dry-run   # log what would be done
    python manage.py backfill_figure_facts --limit 10  # extract only 10
    python manage.py backfill_figure_facts --force     # re-extract all
    python manage.py backfill_figure_facts --asset 42  # one specific row
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from django.core.management.base import BaseCommand, CommandError

from ai_tutor.apps.curriculum.figure_facts_extractor import extract_figure_facts
from ai_tutor.apps.media_library.models import MediaAsset

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill MediaAsset.figure_facts via vision LLM (one-time)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List which rows would be extracted without calling the LLM.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Cap the number of rows processed (useful for canary runs).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Re-extract even rows that already have figure_facts. "
                "Use after editing a figure's source image."
            ),
        )
        parser.add_argument(
            "--asset",
            type=int,
            default=None,
            help="Process only the MediaAsset with this id.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.0,
            help="Seconds to sleep between LLM calls (rate-limit cushion).",
        )

    def handle(self, *args, **opts):
        dry_run: bool = opts["dry_run"]
        limit: Optional[int] = opts["limit"]
        force: bool = opts["force"]
        only_id: Optional[int] = opts["asset"]
        sleep_s: float = opts["sleep"]

        qs = MediaAsset.objects.filter(asset_type=MediaAsset.AssetType.IMAGE)
        if only_id is not None:
            qs = qs.filter(id=only_id)
        elif not force:
            qs = qs.filter(figure_facts__isnull=True)
        if limit:
            qs = qs[:limit]

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No assets to process."))
            return

        self.stdout.write(
            f"Processing {total} asset(s) "
            f"({'DRY-RUN' if dry_run else 'LIVE'}, "
            f"force={force}, limit={limit or 'none'}, sleep={sleep_s}s)"
        )

        extracted = 0
        skipped = 0
        errored = 0

        for asset in qs.iterator():
            label = f"#{asset.id} {asset.title!r}"
            if dry_run:
                self.stdout.write(f"  [DRY] would extract {label}")
                skipped += 1
                continue

            file_field = getattr(asset, "file", None)
            if not file_field or not file_field.name:
                self.stdout.write(self.style.WARNING(f"  [SKIP] {label} has no file"))
                skipped += 1
                continue

            try:
                file_path = file_field.path
            except (NotImplementedError, ValueError):
                # Remote storage — read via .read() instead of file path
                try:
                    image_bytes = file_field.read()
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f"  [ERROR] {label} could not read file: {e}")
                    )
                    errored += 1
                    continue
                facts, err = extract_figure_facts(image_bytes)
            else:
                facts, err = extract_figure_facts(file_path)

            if err is not None:
                self.stdout.write(
                    self.style.ERROR(f"  [ERROR] {label}: {err}")
                )
                errored += 1
                if sleep_s:
                    time.sleep(sleep_s)
                continue

            asset.figure_facts = facts.model_dump(mode="json")
            asset.save(update_fields=["figure_facts", "updated_at"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"  [OK] {label}: type={facts.type}, "
                    f"features={len(facts.labelled_features)}, "
                    f"relationships={len(facts.angle_relationships)}"
                )
            )
            extracted += 1
            if sleep_s:
                time.sleep(sleep_s)

        self.stdout.write("")
        self.stdout.write(
            f"Done. extracted={extracted} skipped={skipped} errored={errored}"
        )
