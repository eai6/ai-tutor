# Unified judge rollout plan — keep prod safe, enable E2E (2026-05-18)

## Goal

Be able to run an end-to-end tutoring session with the unified judge (the v3 design from `memory/deepmind_unified_judge_v3_interpretation.md`) **without breaking the current 7-judge ensemble** that production depends on. Rollback = flip an env var.

## The seam already exists

We don't need a refactor. The current call chain is:

```
conversational_tutor.py:2691
  → run_combined_judge(...)               # apps/tutoring/combined_judge.py:445
    → run_all_judges(...)                 # apps/tutoring/judges/__init__.py:79
      ├─ run_arithmetic_judge   (concurrent)
      ├─ run_factual_judge      (concurrent)
      ├─ run_rule_judge         (concurrent)
      ├─ run_coherence_judge    (concurrent)
      ├─ run_handoff_judge      (concurrent)
      ├─ run_step_eval_judge    (concurrent)
      ├─ run_safety_judge       (concurrent)
      ├─ run_figure_ref_judge   (deterministic)
      ├─ run_figure_vision_judge(concurrent, gated)
      └─ _run_leak_inline       (concurrent, gated)
    returns CombinedJudgeResult
```

`run_combined_judge` is **already a compat shim** (added 2026-05-07 when the monolithic judge was split per-domain). It does nothing but forward arguments to `run_all_judges`. **That's where the swap lives** — no caller changes, no signature changes.

## The design

Add a third sibling under `apps/tutoring/judges/`: `unified.py`. It exposes one function with the same signature + return type as `run_all_judges`:

```python
def run_unified_judge(
    response_text: str,
    *,
    lesson, llm_client=None, vision_client=None, image_reader=None,
    attached_media=None, bank_stems=None, student_input="",
    answer_was_bare=False, answer_was_wrong=False, step_context=None,
    subject_is_math=True, bank_offered=True,
    conversation_history=None, history_turns=None,
    max_workers=8, bank_will_render=False,
    bank_question=None, chat_authored_q=None,
    wrong_attempts=0, reveal_threshold=3,
) -> CombinedJudgeResult:
    ...
```

Internally it:
1. Builds the v3 prompt (from `scripts/run_unified_judge_v3.py::UNIFIED_PROMPT`).
2. Calls Haiku 4.5 (or whatever ModelConfig says) once.
3. Parses the structured JSON output.
4. Maps the unified verdict to `CombinedJudgeResult` fields — including `arithmetic`, `factual`, `rule`, `coherence`, `safety`, `handoff`, `step_eval`, `figure_ref`, `figure_vision` (skipped — vision out of scope), `answer_leak`, and `prompt_versions`.
5. Returns the same `CombinedJudgeResult` shape the rest of the engine consumes.

`run_combined_judge` becomes a one-line dispatch:

```python
def run_combined_judge(response_text, **kwargs) -> CombinedJudgeResult:
    if _use_unified_judge():
        from apps.tutoring.judges.unified import run_unified_judge
        return run_unified_judge(response_text, **kwargs)
    from apps.tutoring.judges import run_all_judges
    return run_all_judges(response_text, **kwargs)


def _use_unified_judge() -> bool:
    """Feature flag — default OFF. Three precedence levels:
      1. env var UNIFIED_JUDGE=on
      2. (later) per-institution flag on Institution.settings
      3. (later) ModelConfig purpose=UNIFIED_JUDGE existence as implicit gate
    """
    return os.getenv('UNIFIED_JUDGE', '').lower() in ('on', '1', 'true')
```

**Zero changes to `conversational_tutor.py`. Zero changes to `judges/__init__.py`. Zero changes to any specialist.** Production behavior is identical when `UNIFIED_JUDGE` is unset.

## Concrete code changes (estimate: ~250 LOC, half a day)

| file | change | LOC |
|---|---|---|
| `apps/tutoring/judges/unified.py` | NEW. The v3 prompt + Haiku call + mapping to `CombinedJudgeResult`. | ~200 |
| `apps/tutoring/combined_judge.py:445` | Wrap `run_all_judges` call with `_use_unified_judge()` dispatch. Add the helper. | ~15 |
| `apps/tutoring/tests/test_unified_judge.py` | NEW. Smoke tests: prompt builds, parser handles malformed JSON, mapping populates every `CombinedJudgeResult` field, fail-soft on LLM error. | ~30 |
| `CLAUDE.md` | One line under "Critical rules" noting `UNIFIED_JUDGE=on` is dev-only opt-in, default OFF. | ~3 |

Nothing in `apps/tutoring/judges/` (the specialist modules) gets touched. They remain the prod default.

## Sketched `unified.py` shape

```python
# apps/tutoring/judges/unified.py
"""Single multi-axis judge — drop-in replacement for run_all_judges.

Returns the SAME CombinedJudgeResult shape so the engine doesn't care
which orchestrator produced it. Gated behind UNIFIED_JUDGE env var in
combined_judge.run_combined_judge — default OFF, production unaffected.

Eval: memory/deepmind_unified_judge_v3_interpretation.md
"""

from apps.tutoring.combined_judge import CombinedJudgeResult
from apps.tutoring.judges.history import format_history_window
from apps.tutoring.judges._prompt_meta import prompt_fingerprint


_UNIFIED_SYSTEM = """<role>
YOUR JOB IS TO CATCH PROBLEMS.
... <all 10 dimension definitions, pasted from v3> ...
"""
PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_UNIFIED_SYSTEM)


def run_unified_judge(response_text, *, lesson, llm_client=None, **kwargs):
    result = CombinedJudgeResult(corrected_response=response_text or "")
    if not response_text or not response_text.strip():
        result.skipped = True; result.skip_reason = "empty_response"
        return result
    if llm_client is None:
        result.skipped = True; result.skip_reason = "no_llm_client"
        return result

    user_prompt = _build_user_prompt(response_text, **kwargs)
    try:
        resp = llm_client.generate(
            messages=[{'role': 'user', 'content': user_prompt}],
            system_prompt=_UNIFIED_SYSTEM,
            max_tokens=4000, temperature=0,
        )
        parsed = _parse_unified_json(resp.content)
    except Exception as e:
        result.skipped = True
        result.skip_reason = f"unified_judge_error: {e}"
        return result

    _map_to_combined(parsed, result)
    result.prompt_versions = {'unified': {'hash': PROMPT_HASH, 'chars': PROMPT_CHARS}}
    return result


def _build_user_prompt(response_text, *, conversation_history, student_input,
                       step_context, subject_is_math, bank_offered, ...):
    # Format the same context the v3 experiment used — but from real
    # engine inputs instead of derived heuristics.
    ...


def _parse_unified_json(text):
    # Tolerant JSON parse, strip code fences, find {...}.
    ...


def _map_to_combined(parsed, result):
    # parsed['factual']['contradicted_claims'] → result.factual.contradicted
    # parsed['rule']['violations'] → result.rule.violations
    # parsed['coherence']['violations'] → result.coherence.violations
    # parsed['safety']['severity'] / ['categories'] → result.safety.*
    # parsed['handoff']['handed_off'] → result.handoff.handed_off
    # parsed['step_complete']['value'] → result.step_eval.step_complete
    # parsed['answer_correct']['value'] → result.step_eval.answer_correct
    # parsed['figure_ref']['issues'] → result.figure_ref.issues
    # parsed['arithmetic']['corrections'] → result.arithmetic.corrections
    # parsed['answer_leak']['leaked'] → result.answer_leaked
    ...
```

## E2E test recipe (manual, ~20 minutes per run)

After the unified module is built:

```bash
# In a new shell — production stays untouched without this var
export UNIFIED_JUDGE=on

# Local dev server
python manage.py runserver

# In another shell — drive a lesson via chrome-devtools-mcp or by hand
# 1. log in as admin / benchmark-temp-2026 (per auto-memory)
# 2. start lesson 540 (geography) or 638 (math)
# 3. play 8-12 turns as a struggling student
# 4. watch logs for [UnifiedJudge] entries (we'll add them) — confirm
#    every turn went through the unified path
# 5. check SessionTurn.judge_outputs in shell — confirm same fields populated
```

Then run the same lesson again with `UNIFIED_JUDGE` unset → compare regen-trigger rate, turn count, exit-ticket outcome. If unified matches or beats specialists on a real lesson, that's the green light to widen.

## Phased rollout

| phase | what | safety |
|---|---|---|
| **0. Build** | Add `unified.py` + dispatch in `combined_judge.py`. Tests pass. Default OFF. | Zero risk — feature flag default OFF. |
| **1. Local E2E** | Run 2-4 full lessons with `UNIFIED_JUDGE=on` on local dev. Inspect SessionTurn.judge_outputs. | Local only. |
| **2. Shadow on local** | Optional: add a `UNIFIED_JUDGE=shadow` mode that runs BOTH and logs disagreements to a JSONL, but uses specialists for engine decisions. Catches the "unified misses something specialist catches" cases. | Same cost as today + unified cost. Specialists still authoritative. |
| **3. Staging E2E** | If we have a staging env (Azure Container App revision split), enable for 10% of sessions for 24h. | Easy revision rollback. |
| **4. Per-institution rollout** | Add `Institution.use_unified_judge` bool. Flip for one test institution first. | Per-tenant gradual ramp. |
| **5. Default ON** | Once stable across 2-3 institutions for a week, flip default to unified. Specialists still available via `UNIFIED_JUDGE=off`. | Easy rollback. |
| **6. Remove specialists** | When confident, delete `judges/*.py` and `run_all_judges`. Far future. | Don't rush this — the specialists are great fallback. |

## Risks + mitigations

| risk | mitigation |
|---|---|
| Unified judge errors on a turn → engine has no verdict | `unified.py` sets `result.skipped = True` on any exception, same as current specialists do on pre-gate. Engine already handles `skipped=True` (no regen trigger, no flags surfaced). |
| Unified output schema drift breaks `_map_to_combined` | Pydantic validation in the parser — any field missing defaults to "clean / not flagged". Worst case: skipped result. |
| Unified misses a violation the specialist would have caught (recall regression) | The v3 audit already showed this happens on cross-turn coherence. Phase 2 shadow mode catches this without shipping. |
| Unified is slower than specialists (single call ~3.75s vs max(judges) ~5-10s — actually FASTER but might regress under load) | Same `max_workers` budget. No code path changes. |
| Cost spike if unified prompt is bigger than expected | Anthropic prompt caching: the unified system prompt is ~20K tokens, 95% cacheable. Real per-call cost is the user-portion deltas, ~$0.008/turn measured. |
| Engine fields the unified judge doesn't populate (figure_vision needs vision) | Set `result.figure_vision.skipped = True` with reason "not_implemented_in_unified_v3". Engine already handles skipped. Future: add a vision dimension when we move to Haiku-vision. |

## What this plan DELIBERATELY doesn't do

- **No refactor of `run_all_judges`.** It stays exactly as-is.
- **No swap of the tutor model.** Tutor stays Opus 4.7. This is purely a judge-stack change.
- **No regex removal in the specialist judges.** That's a separate cleanup. The unified judge happens to be all-LLM (per `auto-memory/feedback_unified_judge_design.md`) but the specialists keep their regex helpers until specifically cleaned up.
- **No RULE_1 cleanup in `judges/rule.py`** — that's task #222, orthogonal. Unified judge already drops RULE_1 internally.
- **No prompt-caching changes** — Reduction 1 from the cost analysis is its own work; this plan composes with it cleanly.

## Open questions

1. **What model should the unified judge use by default?** Recommend: `ModelConfig.get_for(JUDGE)` — same as today's specialists, so admins can swap via dashboard. For E2E test, point JUDGE at Haiku 4.5.
2. **Should shadow mode write to SessionTurn.judge_outputs or a separate field?** Recommend: separate JSONField `unified_judge_outputs` on SessionTurn — keeps comparison clean, avoids polluting the existing field that callers + dashboards already read.
3. **Vision (figure_vision) — defer or include?** Recommend: defer. Haiku 4.5 is vision-capable but the v3 prompt isn't tuned for it. Add a separate `unified_vision.py` later if needed.

## Concrete next step

Build phase 0:
1. Create `apps/tutoring/judges/unified.py` with the v3 prompt and the mapping
2. Add the feature-flag dispatch to `combined_judge.py:445`
3. Add 4 smoke tests (default-off behavior, on-behavior happy path, parse-error fallback, missing-field defaults)
4. Run `pytest apps/tutoring/tests/` to confirm zero regression with flag OFF
5. Manual E2E on one lesson with `UNIFIED_JUDGE=on`

Approve this and I'll do it in one focused session.

Refs: `memory/deepmind_unified_judge_v3_interpretation.md`, `auto-memory/feedback_unified_judge_design.md`, `scripts/run_unified_judge_v3.py`, `auto-memory/feedback_test_locally_before_deploy.md`.
