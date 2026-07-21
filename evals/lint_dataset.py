"""Lint the eval dataset: structure, groundedness, and balance.

The dataset is only worth what its weakest scenario is worth. Two failure modes
are silent and expensive, so they are checked here rather than discovered in a
sweep:

  * **A wrong or missing reference answer** makes a scenario unwinnable. The
    grader compares against it, so a false-accept scenario with a bad reference
    can never pass, and the model gets blamed for the dataset's bug. A prior pass
    had to replace 26 of these.

  * **Silent re-skew.** The dataset is balanced by construction today (see
    evals/matrix.py). Nothing stops a future hand-edit from adding nine more
    `struggler` scenarios and quietly restoring the skew this expansion existed to
    remove. So balance is asserted, not assumed.

Run it directly, or via `pytest evals/test_dataset_balance.py`:

    ./venv/bin/python -m evals.lint_dataset

Exit code is nonzero if any check fails.

See memory/eval_dataset_400_plan.md.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

from evals.matrix import (
    ARCHETYPE_BY_KEY, LESSONS, PERSONAS, PLAN_PATH, SHAPE_BY_KEY,
    SUBJECT_OF, TARGET_PER_MODE,
)

DATASET_ROOT = Path(__file__).parent / 'dataset'
TEMPLATE = Path(__file__).parent / 'TEMPLATE_single_turn.yaml'
FIXTURES_LESSONS = Path(__file__).parent / 'fixtures' / 'lessons.json'

# Broken catalog content poisons every session that draws it, and the tutor
# gets blamed for the confusion. Two classes surfaced in the 2026-07-18
# multi-turn sweep:
#   * float-noise stems ("probability 0.7000000000000001") — template
#     parameters rendered from raw float arithmetic;
#   * statement-asking MCQs whose four options are bare integers — the
#     option texts were lost at generation ("Which statement is true?"
#     A) 2  B) 1  C) 3  D) 4), leaving the student nothing to choose from.
_FLOAT_NOISE_RE = re.compile(r'\d\.\d{9,}')
_STATEMENT_STEM_RE = re.compile(
    r'(?i)which (?:statement|representation|expansion|description|'
    r'explanation|interpretation|of the following statements)'
    r'|what does this mean|in practical terms|order these'
)

# Judge labels a scenario may assert on. Anything else is a typo that would
# silently never fire.
VALID_LABELS = {
    'ASK_WORKING', 'BANNED_OPENER', 'CLAIM_CONTRADICTED', 'CLAIM_UNVERIFIED',
    'FIGURE_REF_UNATTACHED', 'INCOHERENT', 'INFO_DUMP', 'LEAKS_ANSWER',
    'MULTI_PARAGRAPH', 'OFF_TOPIC', 'PREMATURE_ADVANCE', 'REPEATS',
    'SAFETY_HARMFUL', 'SAFETY_INAPPROPRIATE', 'THINKING_LEAK', 'TOOL_LEAK',
    'UNFOUNDED_PRAISE', 'WRONG_VERDICT',
}

# Marginals are balanced by construction, but a hand-edit can break them. Allow a
# small band rather than exact equality — the persona-eligibility mask means some
# cells cannot be filled perfectly evenly, and demanding exactness would make the
# test brittle for no benefit.
PERSONA_TOLERANCE = 6      # target is ~33/persona/mode
LESSON_TOLERANCE = 5       # target is ~12.5/lesson/mode


def _bea_items() -> dict[str, list[str]]:
    """The BEA standard rubric, per mode.

    Single-turn scores ONE response; multi-turn scores a whole trajectory, so the
    same eight dimensions are worded per-response vs across-the-session. They are
    different blocks and must not be cross-checked.
    """
    from evals.gen_multi_turn import BEA_SESSION_RUBRIC
    tpl = yaml.safe_load(TEMPLATE.read_text())
    return {
        'single_turn': tpl['rubric'][-8:],
        'multi_turn': list(BEA_SESSION_RUBRIC),
    }


def _scenarios() -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(DATASET_ROOT.rglob('*.yaml')):
        if 'smoke' in p.parts:
            continue
        out.append((p, yaml.safe_load(p.read_text())))
    return out


def lint() -> list[str]:
    """Return a list of human-readable failures (empty == clean)."""
    errors: list[str] = []
    scenarios = _scenarios()
    plan = {r['scenario_id']: r for r in json.loads(PLAN_PATH.read_text())}
    bea = _bea_items()

    by_id = {}
    for path, d in scenarios:
        sid = d.get('id')
        rel = path.relative_to(DATASET_ROOT)

        if not sid:
            errors.append(f"{rel}: missing `id`")
            continue
        if sid != path.stem:
            errors.append(f"{rel}: id {sid!r} != filename stem {path.stem!r}")
        if sid in by_id:
            errors.append(f"{rel}: duplicate id {sid!r} (also {by_id[sid]})")
        by_id[sid] = rel

        # --- fields every scenario must carry -----------------------------
        for field in ('persona', 'subject', 'lesson_id', 'mode', 'rubric',
                      'pass_threshold', 'assertions'):
            if field not in d:
                errors.append(f"{sid}: missing `{field}`")
        if d.get('persona') not in PERSONAS:
            errors.append(f"{sid}: unknown persona {d.get('persona')!r}")
        if d.get('lesson_id') not in LESSONS:
            errors.append(f"{sid}: lesson_id {d.get('lesson_id')!r} is not a "
                          f"frozen fixture lesson")
        elif d.get('subject') != SUBJECT_OF[d['lesson_id']]:
            errors.append(f"{sid}: subject {d.get('subject')!r} contradicts "
                          f"lesson {d['lesson_id']} "
                          f"({SUBJECT_OF[d['lesson_id']]})")

        # --- the BEA standard rubric, byte-identical within each mode -------
        rubric = d.get('rubric') or []
        expected_bea = bea.get(d.get('mode'))
        if expected_bea and rubric[-8:] != expected_bea:
            errors.append(f"{sid}: last 8 rubric items are not the verbatim "
                          f"{d.get('mode')} BEA block (the judge must score every "
                          f"scenario in a mode against one identical standard)")
        if len(rubric) < 10:
            errors.append(f"{sid}: only {len(rubric)} rubric items — needs the 8 "
                          f"BEA items plus >=2 scenario-specific ones")

        # --- assertion vocabulary ------------------------------------------
        asserts = d.get('assertions') or {}
        for verb in ('must_label', 'must_not_label'):
            for label in asserts.get(verb, []) or []:
                if label not in VALID_LABELS:
                    errors.append(f"{sid}: {verb} references unknown label "
                                  f"{label!r} — it would never fire")

        # --- mode-specific --------------------------------------------------
        if d.get('mode') == 'single_turn':
            if not d.get('student_turn'):
                errors.append(f"{sid}: single_turn needs a `student_turn`")

            row = plan.get(sid)
            if row and row['mode'] == 'single_turn':
                arch = ARCHETYPE_BY_KEY.get(row['kind'])
                has_q = 'seed_inflight_question' in d
                # Keyed on question_pending, NOT grades_answer. A student who says
                # "idk" has not answered — but the question is still open, and the
                # engine needs to see it or it will re-pose instead of scaffolding.
                if arch and arch.question_pending and not has_q:
                    errors.append(
                        f"{sid}: archetype {arch.key!r} leaves a tutor question "
                        f"OPEN but has no `seed_inflight_question` — the engine "
                        f"will re-pose instead of responding to the student")
                # The reverse is NOT an error. A student can go off-topic, signal
                # distress, or bait a tool-leak *while a question is open* — and
                # an open question makes those scenarios harder, not invalid. Only
                # the missing-when-required direction breaks the engine.

            q = d.get('seed_inflight_question')
            if q is not None:
                ref = str(q.get('reference_answer', '')).strip()
                if not ref:
                    errors.append(f"{sid}: empty reference_answer — the grader "
                                  f"has nothing to compare against")
                if ref.upper() in ('TODO', 'TBD', 'PLACEHOLDER', 'N/A'):
                    errors.append(f"{sid}: placeholder reference_answer {ref!r}")
                if not str(q.get('question_text', '')).strip():
                    errors.append(f"{sid}: empty question_text")

                # Mirrors runner.py's own validation, so a bad scenario fails the
                # lint rather than blowing up mid-sweep after paid LLM calls.
                qtype = q.get('question_type')
                if qtype not in ('mcq', 'short_numeric', 'short_answer'):
                    errors.append(
                        f"{sid}: question_type {qtype!r} is invalid — the runner "
                        f"accepts only mcq / short_numeric / short_answer")
                if qtype == 'mcq' and len(q.get('options') or []) < 2:
                    errors.append(
                        f"{sid}: question_type 'mcq' needs an `options` list with "
                        f">= 2 entries")

        elif d.get('mode') == 'multi_turn':
            if not d.get('max_turns'):
                errors.append(f"{sid}: multi_turn needs `max_turns`")
        else:
            errors.append(f"{sid}: unknown mode {d.get('mode')!r}")

    # --- dataset-level: size + balance -------------------------------------
    for mode in ('single_turn', 'multi_turn'):
        sub = [d for _, d in scenarios if d.get('mode') == mode]
        if len(sub) != TARGET_PER_MODE:
            errors.append(f"{mode}: {len(sub)} scenarios, expected "
                          f"{TARGET_PER_MODE}")

        target_p = TARGET_PER_MODE / len(PERSONAS)
        pc = Counter(d['persona'] for d in sub)
        for p in PERSONAS:
            if abs(pc[p] - target_p) > PERSONA_TOLERANCE:
                errors.append(
                    f"{mode}: persona {p!r} has {pc[p]} scenarios, target "
                    f"~{target_p:.0f} (tolerance ±{PERSONA_TOLERANCE}). The "
                    f"dataset has re-skewed.")

        target_l = TARGET_PER_MODE / len(LESSONS)
        lc = Counter(d['lesson_id'] for d in sub)
        for l in LESSONS:
            if abs(lc[l] - target_l) > LESSON_TOLERANCE:
                errors.append(
                    f"{mode}: lesson {l} has {lc[l]} scenarios, target "
                    f"~{target_l:.0f} (tolerance ±{LESSON_TOLERANCE})")

    # --- every planned scenario exists, and nothing unplanned exists --------
    planned = set(plan)
    actual = set(by_id)
    for missing in sorted(planned - actual):
        errors.append(f"planned but not authored: {missing}")
    for extra in sorted(actual - planned):
        errors.append(f"authored but not in the plan: {extra} — either add it to "
                      f"the plan or delete it; an off-plan scenario breaks the "
                      f"balance guarantee")

    # --- every prompt rule is policed by at least one archetype or shape -----
    # Shapes count: R01 (remediation) and R04 (5E phase adaptation) are
    # session-level, and no single-turn scenario can legitimately exercise them.
    from evals.rule_registry import RULES
    claimed = (
        {r for a in ARCHETYPE_BY_KEY.values() for r in a.rules}
        | {r for s in SHAPE_BY_KEY.values() for r in s.rules}
    )
    for rule in RULES:
        if rule.id not in claimed:
            errors.append(f"rule {rule.id} ({rule.name}) is claimed by no "
                          f"archetype and no session shape — a rule without a "
                          f"scenario is a process bug")

    errors.extend(_lint_fixture_questions())

    return errors


def _lint_fixture_questions() -> list[str]:
    """Content-quality checks on the lesson/question fixtures the multi-turn
    scenarios run against (see the constants at the top for the two classes
    caught in the 2026-07-18 sweep)."""
    errors: list[str] = []
    if not FIXTURES_LESSONS.exists():
        return errors
    for obj in json.loads(FIXTURES_LESSONS.read_text()):
        if obj.get('model') != 'tutoring.exitticketquestion':
            continue
        f = obj.get('fields') or {}
        label = (f"fixtures/lessons.json: exit_ticket={f.get('exit_ticket')} "
                 f"order={f.get('order_index')}")
        for key in ('question_text', 'option_a', 'option_b', 'option_c',
                    'option_d', 'correct_answer', 'explanation'):
            val = f.get(key)
            if isinstance(val, str) and _FLOAT_NOISE_RE.search(val):
                errors.append(
                    f"{label}: float-noise in {key} "
                    f"({_FLOAT_NOISE_RE.search(val).group(0)}) — round "
                    f"template parameters before rendering")
        stem = f.get('question_text') or ''
        opts = [str(f.get(f'option_{letter}') or '').strip()
                for letter in 'abcd']
        if (all(opts) and all(re.fullmatch(r'\d+', o) for o in opts)
                and _STATEMENT_STEM_RE.search(stem)):
            errors.append(
                f"{label}: statement-asking MCQ with bare-integer options "
                f"{opts} — the option texts were lost at generation")
        # A probability stated as a bare number > 1 is an impossible
        # premise (cycle-10: "The probability of success is 5" — the
        # template dropped its /9 denominator; the tutor rightly
        # challenged the premise while the grader held the broken ref,
        # deadlocking the session).
        m = re.search(
            r'(?i)probability[^.?]*?\bis\s+(\d+(?:\.\d+)?)(?![\d%/])',
            stem,
        )
        if m and float(m.group(1)) > 1:
            errors.append(
                f"{label}: probability stated as {m.group(1)} (> 1) — "
                f"impossible premise; check the template's parameter "
                f"rendering (dropped denominator or percent sign)")
        params = (f.get('answer_data') or {}).get('parameters') or {}
        if (isinstance(params.get('p'), (int, float))
                and isinstance(params.get('p_denom'), (int, float))
                and f"{params['p']}/{params['p_denom']}" not in stem):
            errors.append(
                f"{label}: parameters carry p={params['p']} "
                f"p_denom={params['p_denom']} but the stem does not show "
                f"the fraction — the template dropped the denominator")
    return errors


def main() -> int:
    # rule_registry reaches into apps.llm, which needs the app registry loaded.
    # Under pytest, pytest-django has already done this.
    import os

    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()

    errors = lint()
    if not errors:
        n = len(_scenarios())
        print(f"✓ dataset clean — {n} scenarios, balance and structure hold")
        return 0
    print(f"✗ {len(errors)} problem(s):\n")
    for e in errors:
        print(f"  - {e}")
    return 1


if __name__ == '__main__':
    sys.exit(main())
