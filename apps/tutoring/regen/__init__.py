"""Regen ensemble — multi-model concurrent rewrite of bad tutor turns.

Why this exists: the previous regen path appended a `<regeneration_required>`
block to the 30KB tutor system prompt and asked the tutor LLM to rewrite
itself. Production logs (Edward, 2026-05-07) showed the regenerated
text often had the SAME violations as the original — the LLM was
ignoring the regen clause buried in the tutoring prompt.

The new architecture:
  - Focused regen prompt (~1KB) with ONLY: previous response, violations
    to fix, bank stems, media catalog excerpt. No tutoring framing.
  - Run the prompt against N configurable models concurrently
    (default: the active TUTORING client + any active REGEN configs).
  - Run all judges on each candidate.
  - Score each candidate by violations and pick the best clean one.
  - If no candidate is clean, lower the temperature by 0.05 and retry.
  - Hard cap on cycles (default 3); on cap, send the highest-scoring
    candidate or a stock fallback.

Configurable via the `REGEN` ModelConfig purpose:
  - 0 active REGEN configs → single-model regen using the tutoring client
  - 1+ active REGEN configs → ensemble across exactly those models

Public API:
  run_regen_ensemble(...) → RegenResult

Usage from the engine:
  result = run_regen_ensemble(
      previous_response=clean_response,
      validation=validation,
      lesson=self.lesson,
      step_context=step_context,
      bank_stems=self._current_bank_stems(),
      media_catalog_text=self._build_media_catalog(),
      attached_media=attached_media_list,
      regen_clients=self.regen_clients,
      judge_client=self.judge_client,
      vision_client=self.judge_client,
      image_reader=self._read_image_for_vision,
      subject_is_math=subject_is_math,
      bank_offered=True,
  )
  clean_response = result.text
  turn_metadata['regen_cycles'] = result.cycles_run
  turn_metadata['regen_picked_model'] = result.picked_model
  turn_metadata['regen_clean'] = result.clean
  turn_metadata['regen_fallback'] = result.fallback_used
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from apps.tutoring.combined_judge import CombinedJudgeResult
from apps.tutoring.judges import run_all_judges
from apps.tutoring.regen.prompt import build_regen_prompt
from apps.tutoring.regen.score import score_candidate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------

# Hard cap on regen cycles. Each cycle = N concurrent model calls + N
# judge runs. With N models and 4 cycles worst case = 4N LLM calls + 4N
# judge orchestrator calls. Caps wall-clock to ~40s worst case.
#
# Spec (CLAUDE.md Architecture / Temperature controls):
# - max cycles = 4
# - temperature_start = 0.20, decay = 0.05 per cycle
# - cycle 1 → 0.20, cycle 2 → 0.15, cycle 3 → 0.10, cycle 4 → 0.05
# - early-exit as soon as any candidate is judge-clean (within a cycle)
# - after cycle 4 without a clean candidate, send the best-scoring
#   "least bad" candidate (or the stock fallback if nothing scored)
DEFAULT_MAX_CYCLES = 4

# Starting temperature for cycle 1. Each subsequent cycle drops by
# DEFAULT_TEMPERATURE_DECAY (so the model becomes more deterministic).
DEFAULT_TEMPERATURE_START = 0.20
DEFAULT_TEMPERATURE_DECAY = 0.05

# Max workers for the regen ensemble (one per model in the ensemble).
DEFAULT_MAX_WORKERS = 4

# Stock fallback when all regen cycles fail. Short, neutral, never
# wrong — keeps the conversation alive without exposing the student
# to a broken response.
STOCK_FALLBACK = (
    "Let's slow down for a moment. Walk me through your thinking — "
    "what was your first step, and why?"
)


# ---------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------

@dataclass
class RegenCandidate:
    """One model's output for one regen cycle."""
    model_name: str
    text: str
    judge_result: Optional[CombinedJudgeResult] = None
    score: float = 0.0
    clean: bool = False  # True when the candidate has no hard issues
    error: str = ""  # non-empty when the model call or judge failed


@dataclass
class RegenResult:
    """The outcome of a full ensemble run.

    `text` is the response to send to the student. `clean` indicates
    whether the picked candidate passed all hard issues; when False,
    we either picked the best-scoring candidate or fell back to the
    stock response.
    """
    text: str = ""
    picked_model: str = ""
    clean: bool = False
    cycles_run: int = 0
    fallback_used: bool = False
    candidates_per_cycle: List[List[RegenCandidate]] = field(default_factory=list)
    # Exact temperature used per cycle (parallel to candidates_per_cycle).
    # Captured rather than re-computed so summarise_regen_cycles is correct
    # when callers override temperature_start / temperature_decay.
    temperatures: List[float] = field(default_factory=list)
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------

def run_regen_ensemble(
    *,
    previous_response: str,
    validation,                # ValidationResult — used for issues + metadata
    lesson,
    step_context: Optional[dict] = None,
    bank_stems: Optional[List[str]] = None,
    media_catalog_text: str = "",
    attached_media: Optional[List[dict]] = None,
    regen_clients: Optional[List] = None,  # list of BaseLLMClient
    judge_client=None,
    vision_client=None,
    image_reader: Optional[Callable] = None,
    subject_is_math: bool = True,
    bank_offered: bool = True,
    student_input: str = "",
    answer_was_bare: bool = False,
    answer_was_wrong: bool = False,
    conversation_history: Optional[List[dict]] = None,
    history_turns: Optional[int] = None,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    temperature_start: float = DEFAULT_TEMPERATURE_START,
    temperature_decay: float = DEFAULT_TEMPERATURE_DECAY,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> RegenResult:
    """Rewrite a bad tutor turn until clean (or the cycle cap is hit).

    Returns a RegenResult with:
      text — what to send to the student
      clean — whether the picked candidate had zero hard issues
      cycles_run — how many cycles fired
      fallback_used — whether we sent the stock fallback
      candidates_per_cycle — full audit trail (every model's output
        + judge findings for every cycle)
    """
    started = time.monotonic()
    result = RegenResult()
    issues = list(getattr(validation, "issues", []) or [])
    meta = dict(getattr(validation, "metadata", {}) or {})

    if not regen_clients:
        # No regen ensemble configured — fall back to "no regen". The
        # caller should already have generated a tutor response; we
        # just hand it back unchanged with a warning.
        logger.warning(
            "[Regen] no regen_clients configured — skipping regen "
            "(issues=%s)", issues,
        )
        result.text = previous_response or ""
        result.clean = False
        result.cycles_run = 0
        result.elapsed_seconds = time.monotonic() - started
        return result

    prompt_text, system_prompt = build_regen_prompt(
        previous_response=previous_response,
        issues=issues,
        validation_metadata=meta,
        bank_stems=bank_stems or [],
        media_catalog_text=media_catalog_text or "",
        student_input=student_input,
    )

    logger.info(
        "[Regen] starting ensemble session=%s issues=%s models=%d max_cycles=%d "
        "temp_start=%.2f temp_decay=%.2f",
        meta.get("session_id", "?"),
        issues,
        len(regen_clients),
        max_cycles,
        temperature_start,
        temperature_decay,
    )

    best_overall: Optional[RegenCandidate] = None

    for cycle in range(1, max_cycles + 1):
        temperature = max(0.0, temperature_start - (cycle - 1) * temperature_decay)
        result.temperatures.append(temperature)
        candidates = _run_one_cycle(
            cycle=cycle,
            temperature=temperature,
            prompt_text=prompt_text,
            system_prompt=system_prompt,
            regen_clients=regen_clients,
            judge_client=judge_client,
            vision_client=vision_client,
            image_reader=image_reader,
            attached_media=attached_media,
            bank_stems=bank_stems,
            student_input=student_input,
            answer_was_bare=answer_was_bare,
            answer_was_wrong=answer_was_wrong,
            step_context=step_context,
            subject_is_math=subject_is_math,
            bank_offered=bank_offered,
            conversation_history=conversation_history,
            history_turns=history_turns,
            lesson=lesson,
            max_workers=max_workers,
        )
        result.candidates_per_cycle.append(candidates)
        result.cycles_run = cycle

        # Pick the best clean candidate; fall back to highest-scoring.
        clean_candidates = [c for c in candidates if c.clean and not c.error]
        scored_candidates = [c for c in candidates if not c.error]
        cycle_best = (
            max(clean_candidates, key=lambda c: c.score)
            if clean_candidates
            else (max(scored_candidates, key=lambda c: c.score) if scored_candidates else None)
        )

        # Track the all-time best across cycles so we can fall back
        # to it if every cycle fails.
        if cycle_best is not None and (
            best_overall is None or cycle_best.score > best_overall.score
        ):
            best_overall = cycle_best

        if clean_candidates:
            picked = cycle_best
            logger.info(
                "[Regen] cycle=%d temp=%.2f clean candidates=%d picked=%s "
                "score=%.2f",
                cycle, temperature, len(clean_candidates),
                picked.model_name, picked.score,
            )
            result.text = picked.text
            result.picked_model = picked.model_name
            result.clean = True
            result.elapsed_seconds = time.monotonic() - started
            return result

        logger.info(
            "[Regen] cycle=%d temp=%.2f no clean candidates "
            "(scored=%d, errored=%d) — retrying",
            cycle, temperature,
            len(scored_candidates),
            len([c for c in candidates if c.error]),
        )

    # Exhausted all cycles without a clean candidate. Send the
    # best-scoring one (still has issues but is the least bad), OR
    # fall back to a stock response when nothing scored.
    result.elapsed_seconds = time.monotonic() - started
    if best_overall is not None and best_overall.text.strip():
        logger.warning(
            "[Regen] cycles exhausted — sending best-effort candidate "
            "model=%s score=%.2f issues_remaining=%s",
            best_overall.model_name, best_overall.score,
            _judge_issue_summary(best_overall.judge_result),
        )
        result.text = best_overall.text
        result.picked_model = best_overall.model_name
        result.clean = False
        return result

    logger.warning(
        "[Regen] cycles exhausted with no usable candidate — "
        "sending stock fallback"
    )
    result.text = STOCK_FALLBACK
    result.picked_model = "stock_fallback"
    result.clean = False
    result.fallback_used = True
    return result


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------

def _run_one_cycle(
    *,
    cycle: int,
    temperature: float,
    prompt_text: str,
    system_prompt: str,
    regen_clients: List,
    judge_client,
    vision_client,
    image_reader,
    attached_media,
    bank_stems,
    student_input: str,
    answer_was_bare: bool,
    answer_was_wrong: bool,
    step_context,
    subject_is_math: bool,
    bank_offered: bool,
    conversation_history: Optional[List[dict]],
    history_turns: Optional[int],
    lesson,
    max_workers: int,
) -> List[RegenCandidate]:
    """Generate one candidate per model concurrently, then judge each."""
    futures: List[concurrent.futures.Future] = []
    candidates: List[RegenCandidate] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for client in regen_clients:
            futures.append(ex.submit(
                _generate_one_candidate,
                client=client,
                prompt_text=prompt_text,
                system_prompt=system_prompt,
                temperature=temperature,
            ))

        # Collect raw text first, then run judges on each in parallel.
        raw_results = [f.result() for f in futures]

    # Judge each candidate concurrently. We do this in a SECOND phase
    # so we don't block on slow models holding up faster judge runs.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        judge_futures = []
        for cand in raw_results:
            if cand.error or not cand.text.strip():
                judge_futures.append(None)
                continue
            judge_futures.append(ex.submit(
                run_all_judges,
                cand.text,
                lesson=lesson,
                llm_client=judge_client,
                vision_client=vision_client,
                image_reader=image_reader,
                attached_media=attached_media,
                bank_stems=bank_stems,
                student_input=student_input,
                answer_was_bare=answer_was_bare,
                answer_was_wrong=answer_was_wrong,
                step_context=step_context,
                subject_is_math=subject_is_math,
                bank_offered=bank_offered,
                conversation_history=conversation_history,
                history_turns=history_turns,
            ))

        for cand, jf in zip(raw_results, judge_futures):
            if jf is None:
                candidates.append(cand)
                continue
            try:
                cand.judge_result = jf.result()
            except Exception as e:
                logger.warning(
                    "[Regen] cycle=%d model=%s judge failed: %s",
                    cycle, cand.model_name, e,
                )
                cand.error = f"judge_error: {type(e).__name__}"
                candidates.append(cand)
                continue
            cand.score, cand.clean = score_candidate(cand.judge_result)
            logger.info(
                "[Regen] cycle=%d model=%s temp=%.2f score=%.2f clean=%s "
                "issues=%s",
                cycle, cand.model_name, temperature, cand.score, cand.clean,
                _judge_issue_summary(cand.judge_result),
            )
            candidates.append(cand)

    return candidates


def _generate_one_candidate(
    *, client, prompt_text: str, system_prompt: str, temperature: float,
) -> RegenCandidate:
    """Single LLM call for one regen candidate. Captures errors so a
    failing model doesn't take down the cycle."""
    raw_name = getattr(getattr(client, "config", None), "model_name", "?")
    # Force-coerce to str — tests using MagicMock clients return
    # MagicMock attributes here, which would later poison
    # JSON-serialization of turn_metadata.
    try:
        model_name = str(raw_name) if raw_name is not None else "?"
    except Exception:
        model_name = "?"
    if "MagicMock" in model_name:
        model_name = "mock"
    cand = RegenCandidate(model_name=model_name, text="")
    try:
        # Override temperature on this single call. The clients honour
        # ModelConfig.temperature by default; we pass it explicitly here
        # so the cycle-level decay actually takes effect.
        response = client.generate(
            messages=[{"role": "user", "content": prompt_text}],
            system_prompt=system_prompt,
            max_tokens=900,
            temperature=temperature,
        )
        cand.text = (response.content or "").strip()
        if not cand.text:
            cand.error = "empty_response"
    except TypeError:
        # Older clients don't accept `temperature` kwarg — fall back.
        try:
            response = client.generate(
                messages=[{"role": "user", "content": prompt_text}],
                system_prompt=system_prompt,
                max_tokens=900,
            )
            cand.text = (response.content or "").strip()
            if not cand.text:
                cand.error = "empty_response"
        except Exception as e:
            logger.warning(
                "[Regen] model=%s call failed (no-temp fallback): %s",
                model_name, e,
            )
            cand.error = f"llm_error: {type(e).__name__}"
    except Exception as e:
        logger.warning("[Regen] model=%s call failed: %s", model_name, e)
        cand.error = f"llm_error: {type(e).__name__}"
    return cand


def summarise_regen_cycles(result: RegenResult,
                           *, text_preview_chars: int = 400) -> dict:
    """JSON-safe per-cycle / per-candidate audit of a regen ensemble run.

    Persisted on SessionTurn.metadata['regen_audit'] (and surfaced in
    BenchmarkItem.snapshot) so annotators can answer "what did the
    first attempt look like, and why did each judge flag it?" — the
    in-memory candidates_per_cycle is dropped once respond() returns,
    so without this summary the audit trail is gone.

    Shape:
        {
          'picked_model', 'cycles_run', 'fallback_used', 'clean': bool,
          'cycles': [
            {
              'cycle': 1, 'temperature': 0.20,
              'candidates': [
                {
                  'model', 'score', 'clean', 'error',
                  'text_preview': str (first ~400 chars),
                  'judge_outputs': dict (per-judge breakdown, same
                      shape as SessionTurn.judge_outputs — empty when
                      the judge_result is None or the candidate errored)
                },
                ...
              ]
            },
            ...
          ]
        }

    Pure function — no DB access. Safe to call from any context.
    """
    if result is None:
        return {}

    cycles_out = []
    for idx, cycle_candidates in enumerate(result.candidates_per_cycle or []):
        # Temperatures list is parallel to candidates_per_cycle; fall
        # back to None if (legacy) result wasn't built with temps.
        temps = result.temperatures or []
        temperature = temps[idx] if idx < len(temps) else None

        candidates_out = []
        for cand in cycle_candidates or []:
            text = (cand.text or '')[:text_preview_chars]
            ellipsis = (
                '…' if cand.text and len(cand.text) > text_preview_chars
                else ''
            )
            jr = cand.judge_result
            if jr is not None and hasattr(jr, 'to_judge_outputs'):
                try:
                    judge_outputs = jr.to_judge_outputs()
                except Exception as e:
                    logger.warning(
                        "[Regen] summary: to_judge_outputs failed for "
                        "model=%s: %s", cand.model_name, e,
                    )
                    judge_outputs = {}
            else:
                judge_outputs = {}

            candidates_out.append({
                'model': cand.model_name or '',
                'score': float(cand.score or 0.0),
                'clean': bool(cand.clean),
                'error': cand.error or '',
                'text_preview': text + ellipsis,
                'judge_outputs': judge_outputs,
            })

        cycles_out.append({
            'cycle': idx + 1,
            'temperature': temperature,
            'candidates': candidates_out,
        })

    return {
        'picked_model': result.picked_model or '',
        'cycles_run': int(result.cycles_run or 0),
        'fallback_used': bool(result.fallback_used),
        'clean': bool(result.clean),
        'cycles': cycles_out,
    }


def _judge_issue_summary(jr: Optional[CombinedJudgeResult]) -> str:
    """Compact one-line summary of a judge result for log lines."""
    if jr is None:
        return "(no judge result)"
    parts = []
    if jr.arithmetic_corrections:
        parts.append(f"arith={len(jr.arithmetic_corrections)}")
    if jr.fact_claims:
        contradicted = sum(
            1 for c in jr.fact_claims if c.status == "contradicted"
        )
        if contradicted:
            parts.append(f"fact_contradicted={contradicted}")
    if jr.rule_violations:
        parts.append(f"rules={[v.rule for v in jr.rule_violations]}")
    if jr.coherence_violations:
        parts.append(f"coh={len(jr.coherence_violations)}")
    if jr.figure_ref_issues:
        parts.append(f"figref={len(jr.figure_ref_issues)}")
    if jr.figure_aligned is False:
        parts.append("figure_misaligned")
    return ",".join(parts) or "(clean)"


__all__ = [
    "run_regen_ensemble",
    "RegenResult",
    "RegenCandidate",
    "STOCK_FALLBACK",
    "DEFAULT_MAX_CYCLES",
    "DEFAULT_TEMPERATURE_START",
    "DEFAULT_TEMPERATURE_DECAY",
]
