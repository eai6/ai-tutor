"""Stage the model assets into one folder for a USB stick.

The desktop app ships without weights and acquires them after install. Schools
with bandwidth can download them; schools without cannot, and this is how they
are served — copy the folder this produces onto the setup drive beside the
GGUF that ``build_model_bundle`` writes, and the setup screen installs from it
with no network at any point.

    python manage.py build_asset_pack --out /Volumes/SETUP/assets

Each asset is copied into its own subfolder, named exactly as the installer
expects to find it.
"""
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ai_tutor.apps.desktop import assets as asset_registry


class Command(BaseCommand):
    help = "Copy the model assets into a folder for offline installation."

    def add_arguments(self, parser):
        parser.add_argument(
            '--out', required=True,
            help="Destination folder (created if missing).",
        )
        parser.add_argument(
            '--only', default='',
            help="Comma-separated asset keys; default is all of them.",
        )

    def handle(self, *args, **options):
        out = Path(options['out']).expanduser()
        out.mkdir(parents=True, exist_ok=True)

        wanted = [k.strip() for k in (options['only'] or '').split(',') if k.strip()]
        chosen = [a for a in asset_registry.ASSETS
                  if not wanted or a.key in wanted]
        if not chosen:
            raise CommandError(f"No assets match {options['only']!r}")

        staged = 0
        for asset in chosen:
            source = next(
                (d for d in asset.search_dirs() if (d / asset.marker).is_file()),
                None,
            )
            if source is None:
                self.stdout.write(self.style.WARNING(
                    f"{asset.key}: not present on this machine "
                    f"(looked in {', '.join(str(d) for d in asset.search_dirs())})"
                    " — skipped."
                ))
                continue

            dest = out / asset.dirname
            dest.mkdir(parents=True, exist_ok=True)
            for name in (asset.marker,) + tuple(asset.extra_files):
                src = source / name
                if not src.is_file():
                    self.stdout.write(self.style.WARNING(
                        f"{asset.key}: {name} missing from {source} — skipped."))
                    continue
                shutil.copyfile(src, dest / name)
            size_mb = sum(f.stat().st_size for f in dest.iterdir() if f.is_file()) / 1e6
            self.stdout.write(self.style.SUCCESS(
                f"{asset.key}: staged to {dest} ({size_mb:.0f} MB)"))
            staged += 1

        if not staged:
            raise CommandError("Nothing staged — no assets found on this machine.")
        self.stdout.write(self.style.SUCCESS(
            f"Asset pack ready at {out}. Point the setup screen at this folder."))
