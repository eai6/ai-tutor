"""Locate and configure the Ollama binary the desktop app runs.

Deliberately free of Django imports so ``desktop.py`` — which starts the
window before any Django process exists — can import it too. Both sides must
agree on which binary is in play; a shell that starts the bundled server while
the provisioner shells out to a different one on PATH would install a model
into a store the running server cannot see.

**Why bundle Ollama at all.** The requirement is that a school can install and
tutor with no internet, ever. Telling a teacher to "first install Ollama"
breaks that at step one: ollama.com/install.sh downloads, and Homebrew
downloads. So the installer carries the binary and the app runs it directly.

Ollama is MIT-licensed, so redistribution is fine; ``stage_ollama`` copies the
LICENSE alongside the binary so the attribution ships with it.

Layout the packaged app expects:

    vendor/ollama/<platform>/ollama[.exe]
    vendor/ollama/<platform>/LICENSE
    vendor/ollama/<platform>/lib/...      (Linux/Windows GPU runners, if any)

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

# Tuned server settings. These MUST match what the client assumes: the fit
# preflight in apps/llm/client.py reads OLLAMA_KV_CACHE_TYPE to size the KV
# cache, and if the server was launched without it the client projects double
# the real value and refuses a model that fits (chat.py:52-58).
SERVER_ENV = {
    'OLLAMA_FLASH_ATTENTION': '1',
    'OLLAMA_KV_CACHE_TYPE': 'q8_0',
    'OLLAMA_NUM_PARALLEL': '1',
    'OLLAMA_MAX_LOADED_MODELS': '1',
}


def platform_slug() -> str:
    """e.g. darwin-arm64, linux-x86_64, windows-amd64."""
    system = sys.platform
    if system.startswith('win'):
        system = 'windows'
    elif system.startswith('linux'):
        system = 'linux'
    elif system == 'darwin':
        system = 'darwin'
    machine = platform.machine().lower()
    if machine in ('x86_64', 'amd64'):
        machine = 'x86_64' if system != 'windows' else 'amd64'
    elif machine in ('arm64', 'aarch64'):
        machine = 'arm64'
    return f'{system}-{machine}'


def _app_root() -> Path:
    """Repo root when running from source, bundle root when frozen.

    PyInstaller unpacks data files to ``sys._MEIPASS``; everything vendored
    has to be looked up there rather than relative to the source tree, which
    does not exist on a user's machine.
    """
    bundled = getattr(sys, '_MEIPASS', None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent.parent.parent


def vendor_dir() -> Path:
    return _app_root() / 'vendor' / 'ollama' / platform_slug()


def bundled_binary() -> Path | None:
    """The Ollama shipped with the app, if this build has one."""
    name = 'ollama.exe' if os.name == 'nt' else 'ollama'
    candidate = vendor_dir() / name
    return candidate if candidate.exists() else None


def resolve_binary() -> str | None:
    """Which Ollama to run: the bundled one, else one on PATH.

    Bundled wins deliberately. A machine that also has Ollama installed
    system-wide might be running a different version with different defaults,
    and the app should behave the same everywhere it is installed.
    """
    bundled = bundled_binary()
    if bundled is not None:
        return str(bundled)
    return shutil.which('ollama')


def server_env(base: dict | None = None) -> dict:
    """Environment for launching `ollama serve` (and for CLI calls).

    ``OLLAMA_MODELS`` is intentionally NOT set here. Leaving Ollama on its own
    default keeps a system-wide install and the app sharing one model store,
    so a 2.5 GB model does not get downloaded twice on a machine that already
    has it — and it avoids stranding models when the app is reinstalled. Set
    OLLAMA_MODELS in the environment to override.
    """
    env = dict(base if base is not None else os.environ)
    for key, value in SERVER_ENV.items():
        env.setdefault(key, value)
    lib = vendor_dir() / 'lib'
    if lib.exists():
        # Linux/Windows builds ship GPU runners beside the binary; Ollama finds
        # them via this variable rather than a fixed install path.
        env.setdefault('OLLAMA_LIBRARY_PATH', str(lib))
    return env


def describe() -> dict:
    """Diagnostic snapshot for the setup screen and bug reports."""
    binary = resolve_binary()
    return {
        'platform': platform_slug(),
        'binary': binary,
        'bundled': bundled_binary() is not None,
        'vendor_dir': str(vendor_dir()),
        'available': binary is not None,
    }
