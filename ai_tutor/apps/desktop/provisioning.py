"""Acquire the tutor model after the app is installed.

The installer deliberately does not carry the model: 2.5 GB makes an installer
impractical to distribute and to re-download for every app update, while the
weights themselves change rarely. So a freshly installed app has no model, and
this module is how it gets one — by either route:

  **From a file.** A GGUF on a USB stick, produced by
  ``manage.py build_model_bundle``. This is the path that works with no
  internet at any point, which is the requirement for the pilot schools.

  **From the internet.** ``ollama pull`` against the registry, for whoever has
  bandwidth. Convenience, never a dependency.

Both converge on the same final step: ``ollama create <tag> -f Modelfile``,
where the tag is ``qwen3-4b-jetson``. The tag name is not cosmetic —
``apps/llm/model_profiles.py:209`` keys its Jetson-safe profile on that exact
spec, and a bare ``qwen3`` tag falls through to a *cloud* profile that sets
``num_ctx`` 24192, which evicts and reloads the runner on every graded turn.

Installs run in a background thread because ``ollama create`` on 2.5 GB takes
minutes; progress is polled rather than streamed, since the desktop app has no
SSE and does not need it.

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ai_tutor.apps.desktop import ollama_runtime

logger = logging.getLogger(__name__)


def _binary() -> str:
    """The Ollama the app runs — bundled for a packaged build, else PATH.

    Raises rather than falling back to the bare name 'ollama': on a packaged
    offline install there is no system Ollama, and a FileNotFoundError from
    Popen deep inside a background thread is a much worse error to debug than
    this one.
    """
    binary = ollama_runtime.resolve_binary()
    if binary is None:
        raise RuntimeError(
            'No Ollama binary available. A packaged build ships one; from '
            'source, run `python manage.py stage_ollama`.')
    return binary

MODEL_TAG = 'qwen3-4b-jetson'

# The registry tag the committed Modelfile builds from. NOT plain `qwen3:4b` —
# as of Ollama 0.30 that resolves to the *Thinking* checkpoint, whose chat
# template opens <think> unconditionally, so there is no way to get a direct
# answer out of it. See infra/ollama/Modelfile.qwen3-4b-jetson.
REGISTRY_BASE = 'qwen3:4b-instruct'

# ollama create copies the GGUF into its blob store rather than moving it, so
# installation transiently needs room for both. Checked before starting: dying
# half-way through a 2.5 GB copy leaves a confusing partial state.
DISK_HEADROOM_MULTIPLIER = 2.2


class ProvisionState:
    """Progress for the in-flight install. One at a time, by construction.

    Deliberately in-memory: it describes work owned by this process, and a
    restart means the install died with it — persisting it would only let the
    UI report progress on a job that is no longer running.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.running = False
            self.phase = 'idle'
            self.detail = ''
            self.percent = 0
            self.error = None
            self.done = False

    def update(self, phase=None, detail=None, percent=None):
        with self._lock:
            if phase is not None:
                self.phase = phase
            if detail is not None:
                self.detail = detail
            if percent is not None:
                self.percent = max(0, min(100, int(percent)))

    def fail(self, message):
        with self._lock:
            self.running = False
            self.error = str(message)
            self.phase = 'failed'

    def finish(self):
        with self._lock:
            self.running = False
            self.done = True
            self.phase = 'ready'
            self.percent = 100
            self.detail = f'{MODEL_TAG} installed'

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'running': self.running, 'phase': self.phase,
                'detail': self.detail, 'percent': self.percent,
                'error': self.error, 'done': self.done,
            }


STATE = ProvisionState()


# ─── helpers ────────────────────────────────────────────────────────────

def ollama_host() -> str:
    return os.environ.get('OLLAMA_HOST') or 'http://localhost:11434'


def model_installed(tag: str = MODEL_TAG) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f'{ollama_host()}/api/tags', timeout=5) as resp:
            models = json.loads(resp.read()).get('models') or []
        return tag in {(m.get('name') or '').split(':')[0] for m in models}
    except Exception:
        return False


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _committed_modelfile_body() -> str:
    """The template + parameters, read from the repo's Modelfile.

    Used for the registry path, where the base model comes from Ollama and only
    the PARAMETER lines are ours. The FROM line is replaced by the caller.
    """
    from django.conf import settings
    path = Path(settings.BASE_DIR) / 'infra' / 'ollama' / 'Modelfile.qwen3-4b-jetson'
    if not path.exists():
        # num_ctx is the load-bearing one: the model OOMs at the stock window on
        # 8 GB machines, and a mismatch with the client's request evicts the
        # runner every turn.
        return 'PARAMETER num_ctx 16384\nPARAMETER temperature 0.7\n' \
               'PARAMETER top_p 0.8\nPARAMETER top_k 20\n'
    body = [line for line in path.read_text().splitlines()
            if not line.strip().startswith('FROM ')]
    return '\n'.join(body)


def _run_ollama_create(modelfile_text: str, workdir: Path, tag: str) -> None:
    """Run `ollama create`, surfacing progress lines as they arrive."""
    modelfile = workdir / 'Modelfile'
    modelfile.write_text(modelfile_text)

    binary = _binary()
    proc = subprocess.Popen(
        [binary, 'create', tag, '-f', str(modelfile)],
        cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=ollama_runtime.server_env(),
    )
    tail = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        tail.append(line)
        tail[:] = tail[-8:]
        STATE.update(detail=line[:120])
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(
            f'ollama create failed (exit {proc.returncode}): ' + ' | '.join(tail[-3:]))


# ─── install from a local GGUF ──────────────────────────────────────────

def install_from_file(gguf_path: str, tag: str = MODEL_TAG) -> None:
    """Install the model from a GGUF on disk. Fully offline.

    ``gguf_path`` may also be a bundle directory produced by
    ``build_model_bundle`` — the one containing Modelfile + qwen3-4b.gguf —
    since that is what a teacher will actually be pointed at on a USB stick.
    """
    source = Path(gguf_path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f'{source} does not exist')

    bundled_modelfile = None
    if source.is_dir():
        candidates = sorted(source.rglob('*.gguf'))
        if not candidates:
            raise FileNotFoundError(
                f'No .gguf file found in {source}. Point at the model file or '
                f'the bundle folder that contains it.')
        # A Modelfile beside the weights carries the tested chat TEMPLATE;
        # prefer it over reconstructing one.
        sibling = candidates[0].parent / 'Modelfile'
        if sibling.exists():
            bundled_modelfile = sibling
        source = candidates[0]

    if source.suffix.lower() != '.gguf':
        raise ValueError(f'{source.name} is not a .gguf file')

    size = source.stat().st_size
    STATE.update(phase='checking', detail=f'{source.name} ({size / 1e9:.1f} GB)', percent=2)

    # ollama create copies rather than moves, so the blob store needs its own
    # copy alongside the source. Fail now with a clear number instead of
    # part-way through with ENOSPC.
    blob_root = Path.home() / '.ollama'
    blob_root.mkdir(parents=True, exist_ok=True)
    need = int(size * DISK_HEADROOM_MULTIPLIER)
    free = _free_bytes(blob_root)
    if free < need:
        raise RuntimeError(
            f'Not enough disk space: need about {need / 1e9:.1f} GB free '
            f'(Ollama copies the model into its own store), but only '
            f'{free / 1e9:.1f} GB is available.')

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        if bundled_modelfile is not None:
            text = bundled_modelfile.read_text()
            # The bundle's FROM is relative to the bundle folder; point it at
            # the resolved absolute path so cwd cannot change the meaning.
            text = '\n'.join(
                f'FROM {source}' if line.strip().startswith('FROM ') else line
                for line in text.splitlines()
            )
        else:
            text = f'FROM {source}\n\n' + _committed_modelfile_body()

        STATE.update(phase='installing',
                     detail='Ollama is importing the model — this takes a few minutes',
                     percent=10)
        _run_ollama_create(text, workdir, tag)

    if not model_installed(tag):
        raise RuntimeError(
            f'`ollama create` reported success but the tag {tag} is not present.')


# ─── install from the registry ──────────────────────────────────────────

def install_from_registry(tag: str = MODEL_TAG, base: str = REGISTRY_BASE) -> None:
    """Pull the base model and build the tuned tag from it. Needs internet."""
    STATE.update(phase='downloading', detail=f'Pulling {base} (~2.5 GB)', percent=2)

    proc = subprocess.Popen([_binary(), 'pull', base],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=ollama_runtime.server_env())
    tail = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        tail.append(line)
        tail[:] = tail[-8:]
        # Ollama prints "... 34%" progress; surface whatever number it gives.
        percent = None
        for token in line.replace('%', '% ').split():
            if token.endswith('%'):
                try:
                    # Scale the pull into 5..80 so the create step has room.
                    percent = 5 + int(float(token[:-1]) * 0.75)
                except ValueError:
                    pass
        STATE.update(detail=line[:120], percent=percent)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(
            f'ollama pull failed (exit {proc.returncode}): ' + ' | '.join(tail[-3:]))

    STATE.update(phase='installing', detail=f'Building {tag}', percent=85)
    with tempfile.TemporaryDirectory() as tmp:
        text = f'FROM {base}\n\n' + _committed_modelfile_body()
        _run_ollama_create(text, Path(tmp), tag)

    if not model_installed(tag):
        raise RuntimeError(
            f'`ollama create` reported success but the tag {tag} is not present.')


# ─── background driver ──────────────────────────────────────────────────

def start_install(source: str, path: str | None = None, tag: str = MODEL_TAG) -> dict:
    """Kick off an install in a background thread. Returns the initial state.

    ``source`` is 'file' or 'download'. Refuses to start a second install while
    one is running — two concurrent `ollama create` runs on the same tag is not
    a state worth reasoning about.
    """
    snapshot = STATE.snapshot()
    if snapshot['running']:
        return snapshot

    STATE.reset()
    STATE.running = True
    STATE.update(phase='starting', detail='', percent=1)

    def worker():
        try:
            if source == 'file':
                if not path:
                    raise ValueError('No file selected.')
                install_from_file(path, tag)
            elif source == 'download':
                install_from_registry(tag)
            else:
                raise ValueError(f'Unknown install source {source!r}')
        except Exception as exc:                     # noqa: BLE001
            logger.exception('model provisioning failed')
            STATE.fail(exc)
        else:
            STATE.finish()

    threading.Thread(target=worker, daemon=True, name='model-provision').start()
    return STATE.snapshot()
