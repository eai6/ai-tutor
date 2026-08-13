"""Run one full A/B cycle on the v6 system prompt + v5-derived engine fixes.

What's new vs v5:

Prompt-side (v6):
  - Pulled the "every turn ends with a question" rule out of the
    <student_visible_output> ban list into its own standalone block
    <must_end_with_question>. v5 regressed Gemini's no_question
    counter 0 -> 2 -> 6 because the rule was buried in a format list.
  - Pulled the "no phantom figure references" rule out of the same
    ban list into <figure_rules>. v5 also regressed figure_ref
    behaviour (recommendation #9 in v5 FINAL_REPORT).
  - Kept JSON/dev-field/self-talk/mode-name bans inside
    <student_visible_output> — those held in v5 (no v5 recs flagged
    them).
  - Strengthened <every_turn> rule 3 with redundancy ("Every turn. No
    exceptions.") — mid-tier models follow rules repeated across
    sections better than singletons.
  - All other v5 content kept verbatim.

Engine-side (carries through automatically; harness uses live code):
  - regen/self_retry.py: dedup penalty on each candidate vs the prior
    emitted tutor turn (engine recs #1 + #10).
  - validator.py + repeated_question.py + exit_ticket_grader.py +
    conversational_tutor.py: detect_template_repeat() — LLM-judge
    (JUDGE_TEMPLATE_REPEAT) catches same-procedure questions even
    when surface details differ (engine rec #9).
  - regen/prompt.py: feedback line for ISSUE_SAME_TEMPLATE_REPEAT
    instructing the regen to vary structure or advance.

Run with:  caffeinate -i venv/bin/python scripts/run_ab_v6_cycle.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ['AB_REPORT_DIR'] = os.environ.get('AB_REPORT_DIR', 'ab-test-reports-v6')

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


# V6 template body lives in apps/tutoring/prompts/variants.py
# (production code, single source of truth). The wrapper re-exports
# the constant locally so existing tooling that greps for
# `V6_TUTOR_SYSTEM_PROMPT_TEMPLATE` in this file still finds it.
from ai_tutor.apps.tutoring.prompts.variants import (  # noqa: E402
    V6_TUTOR_SYSTEM_PROMPT_TEMPLATE,
)


def _patch_prompt_templates() -> None:
    from ai_tutor.apps.tutoring.prompts import anthropic as _ant
    from ai_tutor.apps.tutoring.prompts import gemini as _gem
    _ant.TUTOR_SYSTEM_PROMPT_TEMPLATE = V6_TUTOR_SYSTEM_PROMPT_TEMPLATE
    _gem.GEMINI_TUTOR_SYSTEM_PROMPT_TEMPLATE = V6_TUTOR_SYSTEM_PROMPT_TEMPLATE
    print(f"[v6] Patched prompt templates "
          f"({len(V6_TUTOR_SYSTEM_PROMPT_TEMPLATE)} chars)")


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
    _patch_prompt_templates()

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
