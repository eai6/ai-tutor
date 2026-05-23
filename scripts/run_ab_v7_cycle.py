"""Run one full A/B cycle on the v7 system prompt.

v7 is a structural restructure responding to the feedback in
`design/prompts/v6-prompt-feedback.md`. Headline changes vs v6:

1. <valid_turn_contract> as the FIRST operational block (7 mechanical
   rules). Replaces the v6 mix of <every_turn>, <must_end_with_question>,
   <figure_rules>, <tools>, and parts of <student_visible_output> with
   one canonical place per invariant.

2. <turn_algorithm> + <branch_templates> -- the model picks ONE of five
   branches per turn (FEEDBACK / WORKED_EXAMPLE / PRACTICE / REMEDIATION
   / TEACH) and follows that branch's compact template. The 13
   principles from v6 are demoted to a <principles> background block
   that explicitly defers to the templates on conflict.

3. <wrong_answer_policy> resolves the v6 collision between "redo only
   that step on the SAME problem" (tier 4) and "ask a simpler
   prerequisite OR structurally different question" (closing
   paragraph). Attempt 1 = same problem; attempt 2+ = REMEDIATION
   prereq; attempt 3 = full walk-through.

4. <media_contract> reframes |||MEDIA:N||| as a system-side marker
   stripped before the student sees the turn, so it no longer competes
   with "end with question". The CLAUDE.md infrastructure constraint
   (frontend parses marker as last line) is preserved.

5. FEEDBACK branch step 1 explicitly says READ the bank grader verdict
   FIRST and NEVER override it. Directly targets the v6 case where a
   correct 170 deg answer was treated as suspect.

6. <final_check> -- silent 7-item self-validation before sending.
   Concrete repair path, not "you have nothing to send".

7. Engine validators we shipped in v6 (no_question, figure_ref_without_signal,
   repeated_question, same_template_repeat, regen dedup) are TRUSTED;
   their rules are not duplicated in v7 prose. v7 ends up ~30% shorter
   than v6 as a result.

Run with:  caffeinate -i venv/bin/python scripts/run_ab_v7_cycle.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ['AB_REPORT_DIR'] = os.environ.get('AB_REPORT_DIR', 'ab-test-reports-v7')

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


# V7 template body lives in apps/tutoring/prompts/variants.py
# (production code, single source of truth). Re-exported here so
# existing tooling that greps for `V7_TUTOR_SYSTEM_PROMPT_TEMPLATE`
# in this file still finds it.
from apps.tutoring.prompts.variants import (  # noqa: E402
    V7_TUTOR_SYSTEM_PROMPT_TEMPLATE,
)


def _patch_prompt_templates() -> None:
    from apps.tutoring.prompts import anthropic as _ant
    from apps.tutoring.prompts import gemini as _gem
    _ant.TUTOR_SYSTEM_PROMPT_TEMPLATE = V7_TUTOR_SYSTEM_PROMPT_TEMPLATE
    _gem.GEMINI_TUTOR_SYSTEM_PROMPT_TEMPLATE = V7_TUTOR_SYSTEM_PROMPT_TEMPLATE
    print(f"[v7] Patched prompt templates "
          f"({len(V7_TUTOR_SYSTEM_PROMPT_TEMPLATE)} chars)")


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
