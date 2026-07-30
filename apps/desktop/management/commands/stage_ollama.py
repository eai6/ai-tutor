"""Vendor the Ollama binary into the tree so the installer can carry it.

    python manage.py stage_ollama                    # copy the local install
    python manage.py stage_ollama --from /path/to/ollama

Run once per target platform on a machine of that platform — Ollama is a
native binary and cannot be cross-copied. macOS builds on macOS, Windows on
Windows, Linux on Linux, which is the same constraint the installer build
already has.

Writes to ``vendor/ollama/<platform>/``, which is gitignored: a 31 MB binary
per platform does not belong in git history, and it is reproducible from a
released Ollama at any time.

Why vendor it rather than tell people to install Ollama: the requirement is a
school installing and tutoring with no internet at any point. Every documented
way to install Ollama downloads something.

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.desktop.ollama_runtime import platform_slug, vendor_dir


class Command(BaseCommand):
    help = 'Copy the Ollama binary into vendor/ for offline packaging.'

    def add_arguments(self, parser):
        parser.add_argument('--from', dest='source', default=None,
                            help='Path to an ollama binary. Default: the one on PATH.')
        parser.add_argument('--force', action='store_true',
                            help='Overwrite an already-staged binary.')

    def handle(self, *args, **opts):
        source = opts['source'] or shutil.which('ollama')
        if not source:
            raise CommandError(
                'No ollama binary found on PATH. Install Ollama on this build '
                'machine, or pass --from /path/to/ollama.')
        source = Path(source).resolve()
        if not source.exists():
            raise CommandError(f'{source} does not exist')

        target_dir = vendor_dir()
        name = 'ollama.exe' if os.name == 'nt' else 'ollama'
        target = target_dir / name

        if target.exists() and not opts['force']:
            raise CommandError(
                f'{target} already staged. Use --force to replace it.')

        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | 0o111)

        # Ollama is MIT — redistribution is fine, attribution is required.
        # Look for a LICENSE beside the binary (Homebrew keeps one in the
        # Cellar); note it loudly rather than silently shipping without.
        license_src = None
        for candidate in (source.parent / 'LICENSE',
                          source.parent.parent / 'LICENSE'):
            if candidate.exists():
                license_src = candidate
                break
        if license_src:
            shutil.copy2(license_src, target_dir / 'LICENSE')
            self.stdout.write(f'  copied {license_src.name}')
        else:
            self.stdout.write(self.style.WARNING(
                '  no LICENSE found beside the binary — add Ollama\'s MIT '
                'licence to the bundle manually before distributing.'))

        # GPU runner libraries, where the platform has them. macOS uses Metal
        # compiled into the binary and ships none.
        for lib_name in ('lib', 'lib/ollama'):
            lib_src = source.parent.parent / lib_name
            if lib_src.is_dir():
                shutil.copytree(lib_src, target_dir / 'lib', dirs_exist_ok=True)
                self.stdout.write(f'  copied runner libraries from {lib_src}')
                break

        version = 'unknown'
        try:
            out = subprocess.run([str(target), '--version'], capture_output=True,
                                 text=True, timeout=30)
            version = (out.stdout or out.stderr).strip().splitlines()[-1]
        except Exception:                                  # noqa: BLE001
            pass

        size_mb = target.stat().st_size / 1e6
        self.stdout.write(self.style.SUCCESS(
            f'\nstaged {target}\n'
            f'  platform : {platform_slug()}\n'
            f'  size     : {size_mb:.0f} MB\n'
            f'  version  : {version}'))
        self.stdout.write(
            'The app now prefers this binary over any system Ollama.')
