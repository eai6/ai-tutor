"""Export the tutor model as a self-contained, registry-free bundle.

    python manage.py build_model_bundle --out dist/bundle

Produces a directory a USB stick can carry to a machine that has never had
internet:

    models/
      qwen3-4b.gguf     the weights (~2.5 GB)
      Modelfile         FROM ./qwen3-4b.gguf  + template + params
      manifest.json     tag, sha256, sizes

Why not just ship `infra/ollama/Modelfile.qwen3-4b-jetson`: its first line is
``FROM qwen3:4b-instruct``, which Ollama resolves against registry.ollama.ai.
That works on a connected build machine and fails on a field machine, which is
the only place it matters.

Why not ship Ollama's blob store directly: it would work, but couples the
bundle to Ollama's internal on-disk layout. A GGUF plus a Modelfile is the
documented, stable interface.

The Modelfile is generated from ``ollama show --modelfile`` rather than
hand-written, so the chat TEMPLATE and PARAMETER lines come from the tag that
was actually tested. Rebuilding it by hand is how a subtly different template
ships — and a wrong chat template on a tool-calling tutor shows up as the model
"ignoring instructions", not as an error.

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

DEFAULT_TAG = 'qwen3-4b-jetson'
GGUF_NAME = 'qwen3-4b.gguf'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class Command(BaseCommand):
    help = 'Export the tutor model as an offline-installable bundle.'

    def add_arguments(self, parser):
        parser.add_argument('--out', default='dist/bundle',
                            help='Bundle directory to write (default dist/bundle).')
        parser.add_argument('--tag', default=DEFAULT_TAG,
                            help=f'Ollama tag to export (default {DEFAULT_TAG}).')
        parser.add_argument('--skip-copy', action='store_true',
                            help='Write the Modelfile + manifest but not the '
                                 '2.5 GB weights (for testing the layout).')

    def handle(self, *args, **opts):
        tag = opts['tag']
        out = Path(opts['out']) / 'models'
        out.mkdir(parents=True, exist_ok=True)

        try:
            shown = subprocess.run(['ollama', 'show', tag, '--modelfile'],
                                   capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise CommandError('ollama is not on PATH on this machine.')
        if shown.returncode != 0:
            raise CommandError(
                f'`ollama show {tag}` failed — is the tag built here?\n'
                f'{shown.stderr.strip()}')

        modelfile = shown.stdout

        # ollama show emits an absolute blob path on the FROM line. That path is
        # meaningless on the target machine, so rewrite it to the bundled file
        # and remember where to copy the weights from.
        match = re.search(r'^FROM\s+(\S+)\s*$', modelfile, re.MULTILINE)
        if not match:
            raise CommandError('Could not find a FROM line in the generated Modelfile.')
        source_blob = Path(match.group(1))
        if not source_blob.exists():
            raise CommandError(
                f'FROM points at {source_blob}, which does not exist. '
                f'Expected an Ollama blob path.')

        modelfile = re.sub(r'^FROM\s+\S+\s*$', f'FROM ./{GGUF_NAME}',
                           modelfile, count=1, flags=re.MULTILINE)
        header = (
            f'# Offline install bundle for the AI Tutor desktop app.\n'
            f'# Build:  ollama create {tag} -f Modelfile\n'
            f'# Generated from `ollama show {tag} --modelfile`; do not hand-edit —\n'
            f'# the TEMPLATE below is the one the tutor was tested against.\n\n'
        )
        (out / 'Modelfile').write_text(header + modelfile)
        self.stdout.write(f'wrote {out / "Modelfile"}')

        target = out / GGUF_NAME
        if opts['skip_copy']:
            self.stdout.write(self.style.WARNING(
                f'--skip-copy: not copying weights ({source_blob.stat().st_size / 1e9:.1f} GB)'))
            checksum = None
        else:
            self.stdout.write(
                f'copying weights ({source_blob.stat().st_size / 1e9:.1f} GB) ...')
            shutil.copy2(source_blob, target)
            checksum = _sha256(target)
            self.stdout.write(f'wrote {target}')

        manifest = {
            'tag': tag,
            'gguf': GGUF_NAME,
            'gguf_sha256': checksum,
            'gguf_bytes': target.stat().st_size if target.exists() else 0,
            # Installing needs room for the bundle copy plus Ollama's own copy
            # into its blob store; `ollama create` does not move the file.
            'install_needs_bytes': (target.stat().st_size * 2) if target.exists() else 0,
        }
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=2))

        self.stdout.write(self.style.SUCCESS(
            f'\nbundle ready at {out.parent}\n'
            f'  tag   : {tag}\n'
            f'  weights: {manifest["gguf_bytes"] / 1e9:.2f} GB\n'
            f'  install needs ~{manifest["install_needs_bytes"] / 1e9:.1f} GB free '
            f'(ollama copies the file into its blob store)'))
