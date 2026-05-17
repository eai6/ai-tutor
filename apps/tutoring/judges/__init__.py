"""Per-domain post-response judges for tutor turns.

Replaces the monolithic combined_judge with focused judges that run
concurrently:

  - arithmetic    — verify arithmetic claims (mostly deterministic)
  - factual       — verify named/numeric claims against the curriculum KB
  - rule          — flag NO_AUTHORING / RULE_1 violations
  - step_eval     — answer_correct + step_complete (anchored on posed_question)
  - coherence     — flag self-contradictions inside the same response
  - figure_ref    — flag "looking at the diagram" with no figure attached
                    (deterministic; no LLM call)
  - figure_vision — when a figure IS attached, vision-check alignment
                    with the posed question (only fires when gated)

Each judge has a focused ~2-3KB system prompt (vs 9KB combined) and
returns a typed result. The orchestrator runs them in parallel via
ThreadPoolExecutor and merges deterministically into CombinedJudgeResult
— no final LLM call.

Why split: pilot transcripts showed the combined judge cross-polluting
verdicts (rule violation flipping answer_correct) and Sonnet drifting
on overloaded prompts. Smaller, focused calls are more reliable.

This package is the new home; combined_judge.py keeps a thin compat
wrapper so existing tests + callers continue to work without churn.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import List, Optional

from apps.tutoring.combined_judge import CombinedJudgeResult
from apps.tutoring.judges.arithmetic import (
    ArithmeticResult,
    run_arithmetic_judge,
)
from apps.tutoring.judges.history import format_history_window
from apps.tutoring.judges.coherence import (
    CoherenceResult,
    run_coherence_judge,
)
from apps.tutoring.judges.handoff import (
    HandoffResult,
    run_handoff_judge,
)
from apps.tutoring.judges.factual import FactualResult, run_factual_judge
from apps.tutoring.judges.figure_ref import (
    FigureRefResult,
    run_figure_ref_judge,
)
from apps.tutoring.judges.figure_vision import (
    FigureVisionResult,
    run_figure_vision_judge,
)
from apps.tutoring.judges.rule import RuleResult, run_rule_judge
from apps.tutoring.judges.safety import SafetyResult, run_safety_judge
from apps.tutoring.judges.step_eval import StepEvalResult, run_step_eval_judge

logger = logging.getLogger(__name__)

__all__ = [
    "run_all_judges",
    "ArithmeticResult",
    "CoherenceResult",
    "FactualResult",
    "FigureRefResult",
    "FigureVisionResult",
    "HandoffResult",
    "RuleResult",
    "SafetyResult",
    "StepEvalResult",
    "run_safety_judge",
]


def run_all_judges(
    response_text: str,
    *,
    lesson,
    llm_client=None,
    vision_client=None,
    image_reader=None,
    attached_media: Optional[List[dict]] = None,
    bank_stems: Optional[List[str]] = None,
    student_input: str = "",
    answer_was_bare: bool = False,
    answer_was_wrong: bool = False,
    step_context: Optional[dict] = None,
    subject_is_math: bool = True,
    bank_offered: bool = True,
    conversation_history: Optional[List[dict]] = None,
    history_turns: Optional[int] = None,
    max_workers: int = 8,
    bank_will_render: bool = False,
) -> CombinedJudgeResult:
    """Run all domain judges concurrently and merge into a
    CombinedJudgeResult. Each judge is fail-soft — if one errors, the
    others still produce verdicts.

    Args:
      vision_client: optional vision-capable LLM client for the
        figure_vision judge. Falls back to llm_client (which may or
        may not support vision; the judge itself will skip on error).
      image_reader: callable(url) → (b64_str, media_type). Pass
        `tutor._read_image_for_vision`.
      attached_media: list of media dicts the engine attached this
        turn. Empty/None → figure_vision skips, figure_ref counts the
        zero attachments and may flag.

    Returns an empty / skipped result on cheap pre-gates (empty
    response, no llm_client), matching combined_judge's behaviour.
    """
    result = CombinedJudgeResult(corrected_response=response_text or "")
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if llm_client is None:
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result

    bank_stems = bank_stems or []
    attached_media = attached_media or []
    # vision_client defaults to llm_client; figure_vision skips
    # gracefully if the model can't process images.
    vc = vision_client if vision_client is not None else llm_client

    # Populate prompt fingerprints — one entry per LLM-using judge,
    # plus '(deterministic)' markers for the two pure-Python judges.
    # SessionTurn.metadata['prompt_pack'] will pick this up via
    # CombinedJudgeResult.prompt_versions → to_judge_outputs(). Lets
    # the benchmark correlate behaviour shifts with specific prompt
    # revisions when prompts evolve.
    from apps.tutoring.judges import (
        coherence as _coh_mod, factual as _fact_mod,
        rule as _rule_mod, step_eval as _step_mod, safety as _safe_mod,
        figure_vision as _figvis_mod,
    )
    result.prompt_versions = {
        'arithmetic':    {'hash': '(deterministic)', 'chars': 0},
        'coherence':     {'hash': _coh_mod.PROMPT_HASH,    'chars': _coh_mod.PROMPT_CHARS},
        'factual':       {'hash': _fact_mod.PROMPT_HASH,   'chars': _fact_mod.PROMPT_CHARS},
        'rule':          {'hash': _rule_mod.PROMPT_HASH,   'chars': _rule_mod.PROMPT_CHARS},
        'step_eval':     {'hash': _step_mod.PROMPT_HASH,   'chars': _step_mod.PROMPT_CHARS},
        'safety':        {'hash': _safe_mod.PROMPT_HASH,   'chars': _safe_mod.PROMPT_CHARS},
        'figure_ref':    {'hash': '(deterministic)', 'chars': 0},
        'figure_vision': {'hash': _figvis_mod.PROMPT_HASH, 'chars': _figvis_mod.PROMPT_CHARS},
    }

    # Resolve history window. settings.JUDGE_HISTORY_TURNS is the
    # default (currently 12 — ~6 student + 6 tutor exchanges before
    # the current pair); callers can override per-call (e.g. tests).
    # The actual turn count used is recorded on the result so the
    # benchmark can slice agreement by history-aware vs not.
    if history_turns is None:
        try:
            from django.conf import settings
            history_turns = int(getattr(settings, 'JUDGE_HISTORY_TURNS', 12))
        except Exception:
            history_turns = 12
    prior_exchanges = format_history_window(
        conversation_history, turns=history_turns,
    )
    # Record the EFFECTIVE turn count — zero when history was empty
    # or the formatter produced nothing (e.g. all turns were blank).
    result.history_turns_used = (
        history_turns if prior_exchanges else 0
    )

    # Capture the current context so judge worker threads see the same
    # ContextVars as the orchestrator — specifically the tracing span
    # buffer. Without this, spans emitted from judge LLM calls would
    # not reach the buffer set up in ConversationalTutor.respond().
    # See apps.tutoring.tracing + Phase 1 of
    # memory/agentic_platform_architecture_plan.md.
    #
    # IMPORTANT: a single contextvars.Context CANNOT be entered
    # concurrently by multiple threads — the second entry raises
    # RuntimeError("cannot enter context: ... is already entered").
    # Each submission therefore takes its OWN copy of the parent
    # context. The copies share initial ContextVar values (so spans
    # still reach the orchestrator's buffer) but each can be entered
    # independently in its own worker thread.
    import contextvars

    def _submit(fn, *args, **kwargs):
        # New context copy per submission so parallel ctx.run() calls
        # don't trip the "already entered" RuntimeError.
        return ex.submit(
            contextvars.copy_context().run, fn, *args, **kwargs,
        )

    # Run all judges concurrently. Each has its own pre-gate so
    # submitting them all is cheap — most short-circuit without an
    # LLM call.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        f_arith = _submit(
            run_arithmetic_judge,
            response_text,
            llm_client=llm_client,
            subject_is_math=subject_is_math,
        )
        f_fact = _submit(
            run_factual_judge,
            response_text,
            lesson=lesson,
            llm_client=llm_client,
            prior_exchanges=prior_exchanges,
        )
        f_rule = _submit(
            run_rule_judge,
            response_text,
            bank_stems=bank_stems,
            student_input=student_input,
            answer_was_bare=answer_was_bare,
            answer_was_wrong=answer_was_wrong,
            subject_is_math=subject_is_math,
            bank_offered=bank_offered,
            llm_client=llm_client,
            prior_exchanges=prior_exchanges,
        )
        f_step = _submit(
            run_step_eval_judge,
            student_input=student_input,
            tutor_response=response_text,
            step_context=step_context or {},
            llm_client=llm_client,
        )
        f_coh = _submit(
            run_coherence_judge,
            response_text,
            llm_client=llm_client,
            prior_exchanges=prior_exchanges,
        )
        # figure_ref is deterministic — submit anyway so it runs in
        # the same dispatch loop and we collect its result the same way.
        f_figref = _submit(
            run_figure_ref_judge,
            response_text,
            attached_media_count=len(attached_media),
        )
        # figure_vision only fires when there's actually a figure to
        # check AND a question that depends on it; otherwise it
        # short-circuits without the (expensive) vision call.
        f_figvis = _submit(
            run_figure_vision_judge,
            response_text,
            attached_media=attached_media,
            image_reader=image_reader,
            llm_client=vc,
        )
        # Safety — runs on EVERY tutor turn (no gating). Child
        # protection is non-negotiable. role='tutor' so the judge
        # ignores MANIPULATION (student-only category).
        f_safety = _submit(
            run_safety_judge,
            response_text,
            role="tutor",
            llm_client=llm_client,
        )
        # Handoff — flags turns that don't hand the floor back to the
        # student (dangling promise, pure ack, no next-step). Runs
        # concurrently with the other judges, no extra wall-clock.
        # bank_will_render lets the judge know the engine has a
        # pose_question rendering OUTSIDE the text — when true, the
        # bank Q counts as the handoff.
        f_handoff = _submit(
            run_handoff_judge,
            response_text,
            llm_client=llm_client,
            bank_will_render=bool(bank_will_render),
        )

        arith = _safe_result(f_arith, "arithmetic", ArithmeticResult)
        fact = _safe_result(f_fact, "factual", FactualResult)
        rule = _safe_result(f_rule, "rule", RuleResult)
        step = _safe_result(f_step, "step_eval", StepEvalResult)
        coh = _safe_result(f_coh, "coherence", CoherenceResult)
        figref = _safe_result(f_figref, "figure_ref", FigureRefResult)
        figvis = _safe_result(f_figvis, "figure_vision", FigureVisionResult)
        safety = _safe_result(f_safety, "safety", SafetyResult)
        handoff = _safe_result(f_handoff, "handoff", HandoffResult)

    # Merge deterministically. No final LLM call — each judge's verdict
    # is independent and the merge is mechanical.
    result.corrected_response = (
        arith.corrected_response if arith and not arith.skipped
        else (response_text or "")
    )
    result.arithmetic_corrections = list(arith.corrections) if arith else []
    result.fact_claims = list(fact.claims) if fact else []
    result.rule_violations = list(rule.violations) if rule else []

    if step and not step.skipped:
        result.answer_correct = step.answer_correct
        result.step_complete = bool(step.step_complete)
        result.step_eval_reasoning = step.reasoning
        result.step_eval_skipped = False
        result.step_eval_source = getattr(step, "source", "") or ""
    else:
        result.step_eval_skipped = True
        result.step_eval_source = ""

    result.coherence_violations = list(coh.violations) if coh else []
    result.figure_ref_issues = list(figref.issues) if figref else []
    result.figure_ref_in_question = bool(
        figref and getattr(figref, "in_question", False)
    )
    if figvis and not figvis.skipped:
        result.figure_aligned = figvis.aligned
        result.figure_mismatch_reason = figvis.mismatch_reason
        result.figure_summary = figvis.figure_summary

    if safety and not safety.skipped:
        result.safety_severity = safety.severity
        result.safety_categories = list(safety.categories or [])
        result.safety_reasoning = safety.reasoning

    if handoff and not handoff.skipped:
        result.handed_off = bool(handoff.handed_off)
        result.handoff_reason = handoff.reason or ""

    # Sub-skip telemetry — same shape combined_judge produced so
    # validator + dashboard pickup keeps working.
    result.sub_skipped = {}
    if arith and arith.skipped:
        result.sub_skipped["arithmetic"] = arith.skip_reason
    if fact and fact.skipped:
        result.sub_skipped["fact_check"] = fact.skip_reason
    if rule and rule.skipped:
        result.sub_skipped["rule_check"] = rule.skip_reason
    if step and step.skipped:
        result.sub_skipped["step_eval"] = step.skip_reason
    if coh and coh.skipped:
        result.sub_skipped["coherence"] = coh.skip_reason
    if figref and figref.skipped:
        result.sub_skipped["figure_ref"] = figref.skip_reason
    if figvis and figvis.skipped:
        result.sub_skipped["figure_vision"] = figvis.skip_reason
    if safety and safety.skipped:
        result.sub_skipped["safety"] = safety.skip_reason
    if handoff and handoff.skipped:
        result.sub_skipped["handoff"] = handoff.skip_reason

    logger.info(
        "[Judges] arith=%s fact=%s rule=%s step=%s coh=%s figref=%s figvis=%s "
        "safety=%s answer_correct=%s step_complete=%s step_source=%s",
        "skipped" if (arith and arith.skipped) else f"corrections={len(result.arithmetic_corrections)}",
        "skipped" if (fact and fact.skipped) else f"claims={len(result.fact_claims)}",
        "skipped" if (rule and rule.skipped) else f"violations={len(result.rule_violations)}",
        "skipped" if (step and step.skipped) else "ran",
        "skipped" if (coh and coh.skipped) else f"violations={len(result.coherence_violations)}",
        "skipped" if (figref and figref.skipped) else f"issues={len(result.figure_ref_issues)}",
        "skipped" if (figvis and figvis.skipped) else f"aligned={result.figure_aligned}",
        ("skipped" if (safety and safety.skipped)
         else f"{result.safety_severity}"
              + (f"({','.join(result.safety_categories)})"
                 if result.safety_categories else "")),
        result.answer_correct,
        result.step_complete,
        getattr(step, "source", ""),
    )
    return result


def _safe_result(future, name: str, default_cls):
    """Return the future's result, or a skipped default on error.
    Keeps one judge's failure from poisoning the others.

    When the future raises, the exception class name + truncated
    message is preserved in ``skip_reason`` so per-judge breakdowns
    in SessionTurn.judge_outputs carry enough signal to root-cause
    a failure without diffing logs by hand.
    """
    try:
        return future.result()
    except Exception as e:
        # Full traceback to logs for forensic detail.
        logger.warning("[Judges] %s judge raised", name, exc_info=True)
        instance = default_cls()
        instance.skipped = True
        msg = str(e).strip().replace("\n", " ")[:200]
        instance.skip_reason = (
            f"exception: {type(e).__name__}: {msg}"
            if msg else f"exception: {type(e).__name__}"
        )
        return instance
