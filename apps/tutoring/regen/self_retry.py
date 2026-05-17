"""Tutor self-retry — tool-aware regen by re-invoking the tutor's own
generate path with judge feedback prepended.

Replaces the text-only regen ensemble (`apps/tutoring/regen/__init__.py::
run_regen_ensemble`) which produced engine-state drift when its prose
output didn't match the original tool call (task #176, pilot 2026-05-17
lesson 540 session 57 turn 937).

See memory/tutor_self_retry_plan.md for full design + rationale.

Flow per cycle:
  1. Build a focused feedback message from validator issues + judge
     verdicts (re-uses regen/prompt.py's translators).
  2. Append the feedback to the tutor's conversation history as a
     synthetic user turn with `[system_feedback]` prefix.
  3. Call `tutor.llm_client.generate_with_tools(...)` with the SAME
     system prompt + tools as the initial generation — the retry can
     pose questions via tools the same way the initial response did.
  4. Process the response through `tutor._handle_pose_question_message`
     so any tool calls update bank_question_ref + _awaiting_answer
     correctly. Engine state stays consistent with what's on screen.
  5. Re-run judges. If clean, return. Otherwise loop (cap at max_cycles).

Default model = tutor's own LLM client (Opus). Override via
`retry_client` kwarg if a different model is desired (e.g. Sonnet for
cheaper retry).
"""

from __future__ import annotations

import copy
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Same cap as the legacy ensemble (DEFAULT_MAX_CYCLES) so behaviour
# parity holds during the parallel-paths phase.
DEFAULT_MAX_CYCLES = 2


# Detects when the LLM, instead of emitting a real tool_use block,
# types the tool call as XML / function-call-style text in its prose
# response. Observed in lesson 540 session 58 turn 946 (2026-05-17):
# retry produced "<tool_use><invoke name='pose_inline_question'>
# <parameter name='answer_key'>to understand what the symbols mean
# </parameter>..." which rendered into the chat verbatim — leaked
# the answer key to the student.
#
# Stripping breaks coherence (the surrounding prose may reference
# the leaked question), so per pilot directive 2026-05-17 we
# detect-and-discard the cycle instead. The retry loop tries again
# (or falls back to stock CTA after the cycle cap).
_LEAKED_TOOL_XML_RE = re.compile(
    r'<\s*(?:tool_use|invoke|antml:function_calls|function_calls)\b',
    re.IGNORECASE,
)
# Also catch the function-style leak: `pose_inline_question(question="...", answer_key="...")`
# or `pose_question(slot=N)`. The engine strips these from the initial
# response via `_strip_leaked_tool_call_syntax`, but for self-retry we
# treat them as fatal-for-this-cycle so the loop tries again rather
# than shipping a leaky stripped turn.
_LEAKED_TOOL_FN_RE = re.compile(
    r'\bpose_(?:inline_)?question\s*\(',
    re.IGNORECASE,
)


def _detect_leaked_tool_call(text: str) -> Optional[str]:
    """Return a short reason string if `text` contains a leaked
    tool-call syntax (XML or function-call form). Otherwise None.
    """
    if not text:
        return None
    if _LEAKED_TOOL_XML_RE.search(text):
        return "xml_tool_use_block"
    if _LEAKED_TOOL_FN_RE.search(text):
        return "function_call_syntax"
    return None


@dataclass
class SelfRetryCycle:
    """Telemetry for one retry cycle."""
    cycle: int
    text: str = ""
    clean: bool = False
    score: float = 0.0
    judge_result: Any = None  # CombinedJudgeResult
    error: str = ""


@dataclass
class SelfRetryResult:
    """The outcome of a full self-retry loop.

    Mirrors `RegenResult` shape so the engine's downstream code (audit
    capture, metadata writes) can treat both interchangeably.
    """
    text: str = ""
    picked_model: str = ""
    clean: bool = False
    cycles_run: int = 0
    fallback_used: bool = False
    cycles: List[SelfRetryCycle] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    mechanism: str = "self_retry"  # discriminator vs "ensemble"


STOCK_FALLBACK = (
    "Let's slow down for a moment. Walk me through your thinking — "
    "what was your first step, and why?"
)


def _build_feedback_message(
    *,
    previous_response: str,
    issues: List[str],
    validation_metadata: Dict,
    combined_judge_result: Any = None,
) -> str:
    """Construct the synthetic `[system_feedback]` user-turn content.

    Re-uses `regen/prompt.py::_violation_line` for each issue so the
    repair directives are identical to those the legacy ensemble used.
    Wrapped with a clear preamble + closing call-to-action.
    """
    from apps.tutoring.regen.prompt import _violation_line

    issue_lines: List[str] = []
    for issue in issues or []:
        line = _violation_line(issue, validation_metadata or {})
        if line:
            issue_lines.append(line)

    _tool_use_warning = (
        "TOOL-USE PROTOCOL (CRITICAL):\n"
        "  - If you need to pose a question, EMIT A REAL tool_use "
        "block via the native tool-calling API — do NOT type the "
        "call as text in your response. The student literally sees "
        "raw text characters.\n"
        "  - NEVER write '<tool_use>', '<invoke name=...>', "
        "'<parameter>', or 'pose_question(slot=N)' in your prose. "
        "Those are leaked tool-call syntax — the engine cannot "
        "process them and they expose internal fields (including "
        "answer keys) to the student.\n"
        "  - If you can't or shouldn't pose a new question this turn "
        "(e.g. scaffolding gate active, or no fitting bank slot), "
        "just write your prose response with a probing question or "
        "next-action directive — no tool call needed.\n"
    )

    if not issue_lines:
        return (
            "[system_feedback]\n"
            "Your previous response was flagged by the post-response "
            "validators. Please revise.\n"
            "\n"
            + _tool_use_warning
            + f"\n<your_previous_response>\n{previous_response.strip()[:2000]}\n"
            "</your_previous_response>\n"
        )

    body = (
        "[system_feedback]\n"
        "Your previous response was flagged by the post-response "
        "validators. REVISE the response to fix every issue below. "
        "Keep what was good; fix what was flagged.\n"
        "\n"
        + _tool_use_warning
        + "\n"
        "REVISION RULES:\n"
        "  - End with one focused question or clear next-action.\n"
        "  - Do not contradict yourself within the revised response.\n"
        "\n"
        f"<your_previous_response>\n{previous_response.strip()[:2000]}\n"
        "</your_previous_response>\n"
        "\n"
        "<issues_to_fix>\n"
        + "\n\n".join(issue_lines)
        + "\n</issues_to_fix>\n"
        "\n"
        "Now produce the REVISED response. Output the response only — "
        "no preamble, no meta-commentary about the revision, no "
        "leaked tool-call syntax."
    )
    return body


def run_tutor_self_retry(
    tutor,
    *,
    previous_response: str,
    validation,
    combined_judge_result,
    turn_metadata: Dict,
    student_input: str,
    system_prompt: str,
    messages: List[Dict],
    tools: List[Dict],
    judge_runner: Callable,
    score_fn: Callable,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    max_tokens: int = 2048,
) -> SelfRetryResult:
    """Re-invoke the tutor's generate path with judge feedback until
    the response is judge-clean or `max_cycles` is exhausted.

    Args:
      tutor: the ConversationalTutor instance. We mutate
        `tutor._awaiting_answer` etc. by calling
        `_handle_pose_question_message`.
      previous_response: the original (dirty) tutor response text.
      validation: the ValidationResult from the initial pass.
      combined_judge_result: judges' verdict on the initial response.
      turn_metadata: dict the tool-handler will mutate. Caller is
        responsible for surfacing `regen_*` keys.
      student_input: the student's last message (for judge re-runs).
      system_prompt: same system prompt the initial call used.
      messages: the conversation history that drove the initial call
        (already includes the student's latest input as the trailing
        user turn).
      tools: the tools offered to the initial call (None entries
        already filtered).
      judge_runner: callable that takes the candidate text + supporting
        kwargs and returns a CombinedJudgeResult. Lets the caller
        decide which judges to re-run (concurrent fan-out, etc.) so
        this module doesn't import the entire judge stack.
      score_fn: callable that takes a CombinedJudgeResult and returns
        `(score, clean)`. Same signature as `regen.score.score_candidate`.
      max_cycles: cap on retry attempts.
      max_tokens: per-call max_tokens (matches initial generation).

    Returns:
      SelfRetryResult — `.text` is the response to send to the
      student. `.clean` is True iff a clean candidate was found.
      `.fallback_used` is True iff we returned STOCK_FALLBACK.
    """
    started = time.monotonic()
    _model_name = "unknown"
    try:
        _model_name = str(getattr(tutor.llm_client.config, 'model_name', 'unknown'))
    except Exception:
        pass
    result = SelfRetryResult(
        text=previous_response,
        picked_model=_model_name,
    )

    if not hasattr(tutor.llm_client, 'generate_with_tools'):
        logger.warning(
            "[SelfRetry] llm_client does not support generate_with_tools — "
            "skipping retry, returning previous response unchanged"
        )
        result.elapsed_seconds = time.monotonic() - started
        return result

    # Build retry messages with feedback appended.
    feedback = _build_feedback_message(
        previous_response=previous_response,
        issues=list(getattr(validation, 'issues', []) or []),
        validation_metadata=dict(getattr(validation, 'metadata', None) or {}),
        combined_judge_result=combined_judge_result,
    )

    # Engine-state snapshot helpers. We compete previous_response
    # against each retry cycle and restore the winning candidate's
    # tool-effects on exit. Pilot directive 2026-05-17: drop the
    # STOCK_FALLBACK path. Always ship the best-scoring candidate
    # from {previous, cycle_1, cycle_2} — even a flagged original
    # is better UX than the generic "walk me through your thinking"
    # CTA appearing after a correct answer.
    _META_TOOL_KEYS = (
        'bank_question_ref', 'inline_authored_question',
        'tool_use_count', 'bank_rendered',
    )

    def _snap_state():
        return {
            'aa': copy.deepcopy(getattr(tutor, '_awaiting_answer', None)),
            'meta': {
                k: copy.deepcopy(turn_metadata[k])
                for k in _META_TOOL_KEYS
                if k in turn_metadata
            },
        }

    def _restore_state(snap):
        tutor._awaiting_answer = snap['aa']
        for k in _META_TOOL_KEYS:
            turn_metadata.pop(k, None)
        for k, v in snap['meta'].items():
            turn_metadata[k] = v

    # Snapshot the pre-retry state — that's the "previous response"
    # candidate's engine state. If previous wins the score race, we
    # restore this snapshot.
    prev_snap = _snap_state()

    # Score the previous_response so it competes with the retry
    # cycles. Without this, we always preferred a retry candidate
    # (since previous_response defaulted to score=-inf).
    prev_score = float('-inf')
    prev_clean = False
    prev_leak = _detect_leaked_tool_call(previous_response)
    if prev_leak and (prev_snap['meta'].get('tool_use_count', 0) or 0) == 0:
        # Original itself leaked tool-call syntax. Don't favour it.
        prev_score = -10.0
        logger.warning(
            "[SelfRetry] previous_response itself contains leaked tool "
            "syntax (%s) — scoring at -10.0",
            prev_leak,
        )
    else:
        try:
            if combined_judge_result is not None:
                prev_score, prev_clean = score_fn(combined_judge_result)
        except Exception as exc:
            logger.debug("[SelfRetry] previous score fn failed: %s", exc)

    # Candidates list. Each entry: dict with text/score/clean/snap/label.
    # Pre-seeded with previous_response. Retry cycles append.
    candidates: List[Dict[str, Any]] = [{
        'text': previous_response,
        'score': prev_score,
        'clean': prev_clean,
        'snap': prev_snap,
        'label': 'previous',
    }]

    for cycle_idx in range(1, max_cycles + 1):
        cycle = SelfRetryCycle(cycle=cycle_idx)
        result.cycles.append(cycle)

        # Reset state for this cycle so _handle_pose_question_message
        # rebuilds it from THIS cycle's tool calls.
        _restore_state({'aa': None, 'meta': {}})

        # Build retry-cycle messages = original messages + synthetic
        # feedback user turn.
        retry_messages = list(messages) + [
            {"role": "user", "content": feedback},
        ]

        try:
            logger.info(
                "[SelfRetry] cycle=%d session=%s issues=%s",
                cycle_idx, getattr(tutor.session, 'id', '?'),
                list(getattr(validation, 'issues', []) or []),
            )
            message = tutor.llm_client.generate_with_tools(
                messages=retry_messages,
                system_prompt=system_prompt,
                tools=tools,
                max_tokens=max_tokens,
                tool_choice=None,
            )
            if not hasattr(message, 'content') or not isinstance(
                getattr(message, 'content', None), list
            ):
                raise TypeError(
                    f"generate_with_tools returned non-Message: "
                    f"{type(message).__name__}"
                )
            candidate_text = tutor._handle_pose_question_message(
                message, turn_metadata,
            ).strip()
        except Exception as exc:
            cycle.error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[SelfRetry] cycle=%d FAILED %s",
                cycle_idx, cycle.error,
            )
            continue

        if not candidate_text:
            cycle.error = "empty_candidate"
            logger.warning(
                "[SelfRetry] cycle=%d produced empty candidate", cycle_idx,
            )
            continue

        cycle.text = candidate_text
        cycle_snap = _snap_state()  # capture this cycle's tool effects

        # PRE-JUDGE leak check: if the LLM typed the tool call as
        # text (XML or function-call form) instead of emitting a real
        # tool_use block, mark the cycle dirty with a fatal score so
        # it loses the race. We do NOT strip (breaks coherence) and
        # we do NOT include it in the candidates list (would risk
        # shipping leaked answer keys to the student).
        leak_reason = _detect_leaked_tool_call(candidate_text)
        if leak_reason and (cycle_snap['meta'].get('tool_use_count', 0) or 0) == 0:
            cycle.error = f"leaked_tool_call:{leak_reason}"
            cycle.clean = False
            cycle.score = -10.0
            logger.warning(
                "[SelfRetry] cycle=%d LEAKED_TOOL_CALL_AS_TEXT (%s) — "
                "excluded from candidate pool",
                cycle_idx, leak_reason,
            )
            continue

        # Re-judge the candidate.
        try:
            judges = judge_runner(candidate_text)
        except Exception as exc:
            cycle.error = f"judge_failed: {type(exc).__name__}: {exc}"
            logger.warning(
                "[SelfRetry] cycle=%d judge run failed: %s",
                cycle_idx, cycle.error,
            )
            continue

        score, clean = score_fn(judges)
        cycle.judge_result = judges
        cycle.score = score
        cycle.clean = clean
        logger.info(
            "[SelfRetry] cycle=%d score=%.2f clean=%s",
            cycle_idx, score, clean,
        )

        candidates.append({
            'text': candidate_text,
            'score': score,
            'clean': clean,
            'snap': cycle_snap,
            'label': f'cycle_{cycle_idx}',
        })

        if clean:
            break

    elapsed = time.monotonic() - started
    result.cycles_run = sum(1 for c in result.cycles if c.text or c.error)
    result.elapsed_seconds = elapsed

    # Pick the highest-scoring candidate. Ties broken by insertion
    # order (earliest cycle wins — slight bias toward previous,
    # which is fine: it's the only one with engine state already
    # processed via the initial generation path).
    best = max(candidates, key=lambda c: c['score'])
    result.text = best['text']
    result.clean = bool(best['clean'])
    result.fallback_used = False  # stock fallback dropped 2026-05-17
    # Restore engine state to match the winning candidate's tool
    # effects. Critical when a non-last candidate wins — without
    # this, engine state would reflect cycle_N (last cycle) but the
    # shipped text would be from cycle_M (some earlier cycle) or
    # previous_response.
    _restore_state(best['snap'])

    logger.info(
        "[SelfRetry] DONE cycles=%d winner=%s score=%.2f clean=%s "
        "elapsed=%.2fs",
        result.cycles_run, best['label'], best['score'],
        best['clean'], elapsed,
    )

    return result


def summarise_self_retry(result: SelfRetryResult) -> Dict:
    """Compact audit dict mirroring `summarise_regen_cycles` shape so
    callers can stash it in turn_metadata['regen_audit'] without
    branching."""
    return {
        'mechanism': 'self_retry',
        'picked_model': result.picked_model,
        'cycles_run': result.cycles_run,
        'fallback_used': result.fallback_used,
        'clean': result.clean,
        'elapsed_seconds': round(result.elapsed_seconds, 2),
        'cycles': [
            {
                'cycle': c.cycle,
                'text_preview': (c.text or '')[:400],
                'clean': c.clean,
                'score': c.score,
                'error': c.error,
            }
            for c in result.cycles
        ],
    }
