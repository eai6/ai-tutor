"""Run the v7 A/B cycle on TOP-TIER models (Sonnet 4.6 + Gemini 3.1 Pro).

STAGED, NOT FOR IMMEDIATE EXECUTION. The recommended sequence is:
mid-tier first via `scripts/run_ab_v7_cycle.py`; if v7 shows
improvement on mid-tier, then run this wrapper as the top-tier
confirmation. The §1.7 hypothesis in `design/prompts/v6-prompt-feedback.md`
is that the v7 state-machine restructure helps top-tier specifically
(top-tier was over-philosophizing under v6); this run tests that
claim directly.

Output: ab-test-reports-v7-top-tier/
Cost: ~3-5x v7 mid-tier (~$45-75); wall ~35-45 min.

Run with:  caffeinate -i venv/bin/python scripts/run_ab_v7_top_tier_cycle.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ['AB_REPORT_DIR'] = os.environ.get(
    'AB_REPORT_DIR', 'ab-test-reports-v7-top-tier',
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

_env = Path(__file__).resolve().parents[1] / '.env'
if _env.exists():
    for _line in _env.read_text().splitlines():
        if '=' in _line and not _line.startswith('#'):
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import django  # noqa: E402
django.setup()


def _load_script_module(name: str):
    import importlib.util
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _patch_prompt_templates_v7() -> None:
    """Reuse the v7 template + patcher from scripts/run_ab_v7_cycle.py
    so this file stays a thin override."""
    v7mod = _load_script_module('run_ab_v7_cycle')
    v7mod._patch_prompt_templates()


def _patch_model_specs() -> None:
    runner = _load_script_module('run_ab_test')
    ModelSpec = runner.ModelSpec
    runner.MODELS = [
        ModelSpec('sonnet-4-6', 'Claude Sonnet 4.6', 'anthropic',
                  'claude-sonnet-4-6', 0.2),
        ModelSpec('gemini-3-1-pro', 'Gemini 3.1 Pro', 'google',
                  'gemini-3.1-pro-preview', 0.2),
    ]
    print("[v7-top-tier] MODELS overridden:")
    for m in runner.MODELS:
        print(f"  - {m.label} ({m.provider}/{m.model_name})")


def _phase_run() -> None:
    print("\n=== PHASE 1: run_ab_test.main() ===")
    _load_script_module('run_ab_test').main()


def _phase_judge() -> None:
    print("\n=== PHASE 2: judge_transcripts.main() ===")
    _load_script_module('judge_transcripts').main()


def _phase_report() -> None:
    print("\n=== PHASE 3: generate_reports.main() ===")
    _load_script_module('generate_reports').main()


def main() -> None:
    print(f"AB_REPORT_DIR = {os.environ['AB_REPORT_DIR']}")
    _patch_prompt_templates_v7()
    _patch_model_specs()

    skip = set(sys.argv[1:])
    if 'run' not in skip:
        _phase_run()
    if 'judge' not in skip:
        _phase_judge()
    if 'report' not in skip:
        _phase_report()

    print("\nDone. See", os.environ['AB_REPORT_DIR'])


if __name__ == '__main__':
    main()
