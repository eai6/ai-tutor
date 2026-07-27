#!/usr/bin/env python3
"""Chat with the tutor in the terminal.

    ./chat.py --lesson 1137
    ./chat.py --list-lessons
    ./chat.py --lesson 1137 --show-all

A convenience front door for ``manage.py tutor_chat``. It exists to remove the
three papercuts that make the management command tedious to run repeatedly on
the Jetson: activating the venv, remembering TUTOR_MODEL_OVERRIDE, and noticing
too late that the Ollama server is not running.

Deliberately thin. Every piece of real logic lives in
``apps/tutoring/management/commands/tutor_chat.py`` and ``apps/tutoring/cli/``,
so there is exactly one implementation of the chat loop and it stays testable
by pytest without a subprocess. This file only does bootstrap + defaults, then
hands off with full argument passthrough.

Plan: memory/terminal_tutor_client_plan.md
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Derive the repo root from this file rather than a hardcoded path or the
# current working directory, so ./chat.py works from anywhere. A hardcoded
# default is what made offline_eval/seed_ollama_configs.py unrunnable on this
# machine (it pointed at another developer's home directory).
ROOT = Path(__file__).resolve().parent

# The local model the Jetson runs. Overridable — export TUTOR_MODEL_OVERRIDE
# before invoking to point at something else, or set it to a cloud spec to
# compare against. setdefault, never assignment, so an explicit export wins.
DEFAULT_TUTOR_MODEL = 'local_ollama/qwen3-4b-jetson'

OLLAMA_HOST = os.environ.get('OLLAMA_HOST') or 'http://localhost:11434'

# These MUST match how `ollama serve` was actually started. They are duplicated
# in ~/.bashrc for interactive shells, but .bashrc returns early for
# non-interactive ones, so a bare `./chat.py` from a script, cron job, or piped
# invocation would not see them.
#
# That is not cosmetic. apps/llm/client.py::_ollama_fit_preflight reads
# OLLAMA_KV_CACHE_TYPE to size the KV cache, but the value that actually governs
# the server is the one `ollama serve` was launched with. When the client cannot
# see it, it assumes Ollama's f16 default and projects DOUBLE the real KV —
# measured 2.2 GB instead of 1.1 GB, which pushed the total to 5.1 GB and made
# the guard refuse a model that fits comfortably.
OLLAMA_DEFAULTS = {
    'OLLAMA_FLASH_ATTENTION': '1',
    'OLLAMA_KV_CACHE_TYPE': 'q8_0',
    'OLLAMA_NUM_PARALLEL': '1',
    'OLLAMA_MAX_LOADED_MODELS': '1',
}

# sentence-transformers phones home to huggingface.co on load even when the
# model is cached locally. With no network that is not a graceful degradation:
# measured 2026-07-27 with WiFi off, the load spent 20.6 s on DNS retries and
# then raised RuntimeError, taking the grader's embedding gate (tier 1.5) down
# with it and forcing every free-text answer onto the slower verifier-LLM tier.
# The same load with these set succeeds from cache in 11.4 s.
#
# setdefault, so a machine WITHOUT the cache can still populate it by exporting
# HF_HUB_OFFLINE=0 — offline mode turns a slow first download into an immediate
# failure, which is wrong on a fresh checkout and right on this box.
OFFLINE_DEFAULTS = {
    'HF_HUB_OFFLINE': '1',
    'TRANSFORMERS_OFFLINE': '1',
}


def _reexec_under_venv() -> None:
    """Re-run this script with the project venv's interpreter if needed.

    Without this, `./chat.py` on a bare shell picks up the system Python, which
    has no Django, and the failure is an ImportError that says nothing about
    the actual problem.
    """
    venv_dir = ROOT / '.venv'
    venv_python = venv_dir / 'bin' / 'python'
    if not venv_python.exists():
        return
    # Compare sys.prefix, NOT the interpreter path. `.venv/bin/python` is a
    # symlink to the system interpreter, so resolving both paths makes them
    # compare equal and the re-exec never fires — the script then dies on
    # `ModuleNotFoundError: No module named 'django'`. Inside a venv sys.prefix
    # is the venv directory; outside it is /usr.
    try:
        if Path(sys.prefix).resolve() == venv_dir.resolve():
            return
    except OSError:
        return
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


def _check_ollama() -> str | None:
    """Return a human-readable problem with the local model server, or None.

    Only advisory — a cloud TUTOR_MODEL_OVERRIDE does not need Ollama at all, so
    this warns rather than exits.
    """
    if not (os.environ.get('TUTOR_MODEL_OVERRIDE') or '').startswith('local_ollama/'):
        return None
    try:
        import urllib.request
        with urllib.request.urlopen(f'{OLLAMA_HOST}/api/tags', timeout=5):
            return None
    except Exception:
        return (
            f"Ollama does not appear to be running at {OLLAMA_HOST}.\n"
            f"  Start it in another terminal:  ollama serve\n"
            f"  (your ~/.bashrc already exports the tuned OLLAMA_* settings)"
        )


def main() -> int:
    _reexec_under_venv()

    sys.path.insert(0, str(ROOT))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    os.environ.setdefault('TUTOR_MODEL_OVERRIDE', DEFAULT_TUTOR_MODEL)
    for key, value in {**OLLAMA_DEFAULTS, **OFFLINE_DEFAULTS}.items():
        os.environ.setdefault(key, value)

    problem = _check_ollama()
    if problem:
        print(problem, file=sys.stderr)
        print(file=sys.stderr)

    # The model is announced by tutor_chat's banner, not here — --model may
    # override the default installed above, and printing it at this point would
    # report the wrong one.
    from django.core.management import execute_from_command_line

    # Full passthrough, so every tutor_chat flag (--lesson, --list-lessons,
    # --show-all, --help, …) works here identically and this wrapper never has
    # to be updated when the command grows an option.
    execute_from_command_line(['manage.py', 'tutor_chat', *sys.argv[1:]])
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
