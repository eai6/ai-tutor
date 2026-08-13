"""Run the v6 A/B cycle on TOP-TIER models — Claude Sonnet 4.6 and
Gemini 3.1 Pro — instead of the mid-tier Sonnet 4 / Gemini 3 Flash used
in ab-test-reports-v6/.

Why: the v6 baseline showed Sonnet 4 monotonic uplift across cycles
(2.88 -> 3.27) and the prompt + engine work has plateaued. Several v6
top recommendations describe second-order pedagogy refinements rather
than first-order failure modes — at this point top-tier model capacity
may close the remaining gap more cheaply than another prompt cycle.

This run isolates the model-tier variable: same v6 prompt, same engine
(regen dedup + LLM template-repeat judge in code), same lessons, same
personas. Only the tutor model changes.

Output: ab-test-reports-v6-top-tier/
Cost: ~3-5x v6 (~$45-75 estimated); wall time ~25-35 min.

Run with:  caffeinate -i venv/bin/python scripts/run_ab_v6_top_tier_cycle.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ['AB_REPORT_DIR'] = os.environ.get(
    'AB_REPORT_DIR', 'ab-test-reports-v6-top-tier',
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')

_env = Path(__file__).resolve().parents[1] / '.env'
if _env.exists():
    for _line in _env.read_text().splitlines():
        if '=' in _line and not _line.startswith('#'):
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import django  # noqa: E402
django.setup()


# Reuse the v6 prompt template + patcher from the existing wrapper.
# Loading via importlib (the scripts/ dir isn't a package).
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


def _patch_prompt_templates_v6() -> None:
    """Pull the v6 template + patcher from scripts/run_ab_v6_cycle.py so
    this file stays a thin override and any v6 prompt edit propagates
    automatically. If the v6 wrapper goes away, copy V6_TUTOR_SYSTEM_PROMPT_TEMPLATE
    into this file."""
    v6mod = _load_script_module('run_ab_v6_cycle')
    v6mod._patch_prompt_templates()


def _patch_model_specs() -> None:
    """Swap MODELS in scripts/run_ab_test.py to the top-tier pair.

    Mirrors how the v6 wrapper monkey-patches the prompt: leaves the
    runner code untouched, just rewrites the module-level constant
    before run_ab_test.main() consumes it.

    Model IDs:
      - claude-sonnet-4-6  -- per env hint + ModelConfig registry
      - gemini-3.1-pro-preview  -- per
        apps/tutoring/management/commands/run_model_experiment.py:67
    """
    runner = _load_script_module('run_ab_test')
    ModelSpec = runner.ModelSpec
    runner.MODELS = [
        ModelSpec('sonnet-4-6', 'Claude Sonnet 4.6', 'anthropic',
                  'claude-sonnet-4-6', 0.2),
        ModelSpec('gemini-3-1-pro', 'Gemini 3.1 Pro', 'google',
                  'gemini-3.1-pro-preview', 0.2),
    ]
    print("[v6-top-tier] MODELS overridden:")
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
    _patch_prompt_templates_v6()
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
