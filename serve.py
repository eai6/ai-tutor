#!/usr/bin/env python3
"""Serve the tutor web app to devices on the hotspot (or the LAN).

    ./serve.py                 # 0.0.0.0:8000, local Qwen, offline-safe
    ./serve.py --port 8080
    ./serve.py --model local_ollama/qwen3.5:4b

The web sibling of chat.py, and it exists for the same reason: without it the
app silently runs on the WRONG MODEL. `ModelConfig.get_for('tutoring')` resolves
to the active DB row — anthropic/claude-haiku on this machine — unless
TUTOR_MODEL_OVERRIDE says otherwise. Started plainly with `manage.py runserver`,
the app therefore calls the cloud, which on a hotspot with no internet means the
tutor cannot answer at all. That is not hypothetical; it is what was running on
2026-07-27 before this script existed.

Binds 0.0.0.0 by default, because localhost is unreachable from a phone.

Plan: memory/terminal_tutor_client_plan.md
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Same defaults as chat.py, and for the same reasons — see the comments there.
# OLLAMA_KV_CACHE_TYPE in particular must match how `ollama serve` was launched,
# or the fit preflight sizes the KV cache wrongly.
DEFAULTS = {
    'TUTOR_MODEL_OVERRIDE': 'local_ollama/qwen3-4b-jetson',
    'OLLAMA_FLASH_ATTENTION': '1',
    'OLLAMA_KV_CACHE_TYPE': 'q8_0',
    'OLLAMA_NUM_PARALLEL': '1',
    'OLLAMA_MAX_LOADED_MODELS': '1',
    'HF_HUB_OFFLINE': '1',
    'TRANSFORMERS_OFFLINE': '1',
}


def _reexec_under_venv() -> None:
    """Re-run under the project venv. See chat.py for why sys.prefix is the test
    (`.venv/bin/python` is a symlink to the system interpreter, so comparing
    resolved paths wrongly concludes we are already inside)."""
    venv_dir = ROOT / '.venv'
    venv_python = venv_dir / 'bin' / 'python'
    if not venv_python.exists():
        return
    try:
        if Path(sys.prefix).resolve() == venv_dir.resolve():
            return
    except OSError:
        return
    os.execv(str(venv_python),
             [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


def _client_urls(port: int) -> list[str]:
    """Addresses a phone could actually use. 10.42.0.1 is NetworkManager's
    fixed address for a shared-mode AP, so it is the hotspot URL."""
    urls = []
    try:
        out = subprocess.run(['hostname', '-I'], capture_output=True, text=True,
                             timeout=5).stdout.split()
    except Exception:
        out = []
    for ip in out:
        if ip.startswith('10.42.0.'):
            urls.insert(0, f"http://{ip}:{port}/student/login/  (hotspot)")
        elif ip.startswith(('192.168.', '10.', '172.')) and not ip.startswith('172.17.'):
            urls.append(f"http://{ip}:{port}/student/login/  (LAN)")
    return urls


def main() -> int:
    _reexec_under_venv()

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--bind', default='0.0.0.0',
                        help='Default 0.0.0.0 — localhost is unreachable from a phone.')
    parser.add_argument('--model', default=None,
                        help='Tutor model spec (default: local Qwen on Ollama).')
    args = parser.parse_args()

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)
    if args.model:
        os.environ['TUTOR_MODEL_OVERRIDE'] = args.model

    print(f"tutor model: {os.environ['TUTOR_MODEL_OVERRIDE']}", file=sys.stderr)
    for url in _client_urls(args.port) or [f"http://<this-host>:{args.port}/student/login/"]:
        print(f"students:    {url}", file=sys.stderr)
    print(file=sys.stderr)

    from django.core.management import execute_from_command_line
    execute_from_command_line(
        ['manage.py', 'runserver', f'{args.bind}:{args.port}', '--noreload']
    )
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
