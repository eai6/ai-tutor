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


# Previous-turn dedup penalty — task surfaced in
# ab-test-reports-v5/FINAL_REPORT.md engine recs #1 + #10. Without
# this, the regen loop can pick a candidate that duplicates the
# previously-emitted tutor turn (the "regen_did_not_clean" failure
# mode). Each candidate's trigram-Jaccard similarity vs the previous
# emitted tutor turn produces a continuous penalty so near-duplicates
# lose the candidate race even when their judge score is otherwise
# clean.
#
# Re-tuned post-v7 mid-tier (ab-test-reports-v7/): the original
# (5.0 / 0.4) parameters caused a regen storm on Sonnet L1425
# struggler under v7's tighter <valid_turn_contract> -- the more
# uniformly-shaped v7 candidates triggered moderate-similarity
# penalties (~50-70%) that flipped otherwise-clean candidates into
# negative scores, which the loop kept trying to escape until
# max_turns. Re-tuned to:
#   - WEIGHT 5.0 -> 3.5    (less crushing penalty on near-dupes)
#   - FLOOR  0.4 -> 0.55   (more tolerant of moderate similarity)
# Net effect at 70% similarity: -1.17 (was -2.5). At 100% similarity:
# -3.5 (was -5.0) -- still strongly disqualifying for outright dupes.
DEDUP_PENALTY_WEIGHT = 3.5
DEDUP_PENALTY_FLOOR = 0.55  # similarities below this get zero penalty


def _trigram_set(text: str) -> set:
    text = ' '.join((text or '').lower().split())
    if len(text) < 3:
        return set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def _jaccard_similarity(a: str, b: str) -> float:
    A, B = _trigram_set(a), _trigram_set(b)
    if not A or not B:
        return 0.0
    union = A | B
    if not union:
        return 0.0
    return len(A & B) / len(union)


def _dedup_penalty(candidate_text: str, prior_turn_text: str) -> float:
    """Returns a non-positive penalty for candidate-vs-prior overlap.

    0.0 when similarity < DEDUP_PENALTY_FLOOR (40%). Scales linearly
    from 0 at the floor up to -DEDUP_PENALTY_WEIGHT (5.0) at 100%
    overlap. The penalty is added directly to the candidate's judge
    score so it competes alongside other quality signals.
    """
    if not prior_turn_text or not candidate_text:
        return 0.0
    sim = _jaccard_similarity(candidate_text, prior_turn_text)
    if sim < DEDUP_PENALTY_FLOOR:
        return 0.0
    # Linear scale from (floor, 0.0) to (1.0, -WEIGHT).
    span = 1.0 - DEDUP_PENALTY_FLOOR
    return -DEDUP_PENALTY_WEIGHT * (sim - DEDUP_PENALTY_FLOOR) / span


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
# Browser-e2e variants surfaced 2026-05-20 with the Gemini Flash
# family — protocol markup the LLM invents in prose instead of
# emitting a real tool_use block:
#   - "|||tool_call:pose_question{slot: 1}|||"   (3.5 Flash)
#   - "tool_use: pose_question(slot=4)"          (Flash Lite Preview)
# Detect-and-discard the cycle so the retry loop tries again rather
# than shipping the markup verbatim.
_LEAKED_TOOL_MARKUP_RE = re.compile(
    r'\|{2,}\s*tool[_ ]?(?:call|use|code)\s*:'
    r'|\btool[_ ]?(?:call|use|code)\s*:\s*pose_(?:inline_)?question\b',
    re.IGNORECASE,
)


def _detect_leaked_tool_call(text: str) -> Optional[str]:
    """Return a short reason string if `text` contains a leaked
    tool-call syntax (XML / function-call / Gemini markup forms).
    Otherwise None.
    """
    if not text:
        return None
    if _LEAKED_TOOL_XML_RE.search(text):
        return "xml_tool_use_block"
    if _LEAKED_TOOL_MARKUP_RE.search(text):
        return "gemini_tool_markup"
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
    from ai_tutor.apps.tutoring.regen.prompt import _violation_line

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

    # Capture the LAST-EMITTED tutor turn so candidates that duplicate
    # it can be penalised. The current turn is NOT yet persisted when
    # self-retry runs, so .first() returns the prior turn. Fail-soft:
    # if the lookup blows up, dedup is just disabled for this cycle.
    _prior_tutor_text = ""
    try:
        from ai_tutor.apps.tutoring.models import SessionTurn as _ST
        _prev = (_ST.objects
                 .filter(session=tutor.session, role='tutor')
                 .order_by('-created_at').first())
        if _prev is not None:
            _prior_tutor_text = (_prev.content or '').strip()
    except Exception as _e:
        logger.debug("[SelfRetry] prior-turn lookup failed: %s", _e)

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

    # Dry-run dispatch (task #200, pilot directive 2026-05-17):
    # engine state stays untouched throughout retry cycles. Each
    # cycle calls `tutor._pose_dry_run()` which returns (text, delta)
    # without mutating engine state. The winning candidate's delta is
    # committed once at the end via `_apply_pose_delta()`. This
    # eliminates the snapshot-and-restore dance from the previous
    # implementation and structurally guarantees that engine state
    # always matches the shipped text (fixes task #176 drift bug).
    #
    # previous_response is treated as a candidate too: its delta is
    # whatever turn_metadata + engine state already reflect from the
    # original generation (captured below as `prev_delta`).

    import copy as _copy

    # Capture the previous_response's current delta (what's already
    # committed from the initial generation). If previous wins the
    # score race, we re-apply this to ensure state consistency.
    prev_delta = {
        'aa': _copy.deepcopy(getattr(tutor, '_awaiting_answer', None)),
        'shown_added': set(),  # initial gen already added these; don't re-add
        'turn_q': _copy.deepcopy(getattr(tutor, '_turn_questions', {}) or {}),
        'bank_used': bool(getattr(tutor, '_bank_signal_used_this_turn', False)),
        'last_bank': str(getattr(tutor, '_last_bank_rendered_text', '') or ''),
        'meta': _copy.deepcopy(turn_metadata),
    }

    # Score the previous_response so it competes with the retry
    # cycles. Without this, we always preferred a retry candidate
    # (since previous_response defaulted to score=-inf).
    prev_score = float('-inf')
    prev_clean = False
    prev_leak = _detect_leaked_tool_call(previous_response)
    if prev_leak and (prev_delta['meta'].get('tool_use_count', 0) or 0) == 0:
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

    # Dedup penalty: the original response can itself be a near-dup
    # of the prior tutor turn. Apply the same penalty rule so a
    # genuinely different retry candidate can beat it.
    _prev_dedup = _dedup_penalty(previous_response, _prior_tutor_text)
    if _prev_dedup < 0:
        logger.info(
            "[SelfRetry] previous_response dedup penalty %.2f vs prior turn",
            _prev_dedup,
        )
    prev_score = prev_score + _prev_dedup if prev_score > float('-inf') else prev_score

    # Candidates list. Each entry: dict with text/score/clean/delta/label.
    # Pre-seeded with previous_response. Retry cycles append.
    candidates: List[Dict[str, Any]] = [{
        'text': previous_response,
        'score': prev_score,
        'clean': prev_clean,
        'delta': prev_delta,
        'label': 'previous',
    }]

    # Roll back the original generation's engine-state mutations
    # BEFORE running retry cycles, so each cycle's dry-run sees a
    # clean slate. We've already captured prev_delta so we can
    # restore it if previous wins.
    tutor._apply_pose_delta({
        'aa': None,
        'shown_added': set(),
        'turn_q': {},
        'bank_used': False,
        'last_bank': '',
        'meta': {
            k: v for k, v in turn_metadata.items()
            if k not in ('bank_question_ref', 'inline_authored_question',
                         'tool_use_count', 'bank_rendered')
        },
    }, turn_metadata)

    for cycle_idx in range(1, max_cycles + 1):
        cycle = SelfRetryCycle(cycle=cycle_idx)
        result.cycles.append(cycle)

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
            # DRY-RUN: returns (text, delta) without mutating engine
            # state. Caller commits delta only if this candidate wins.
            candidate_text, cycle_delta = tutor._pose_dry_run(
                message, turn_metadata,
            )
            candidate_text = candidate_text.strip()
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

        # PRE-JUDGE leak check (XML or function-call syntax in prose).
        # The delta carries this cycle's tool_use_count — read from
        # there since engine state is still at pre-retry baseline.
        leak_reason = _detect_leaked_tool_call(candidate_text)
        _cycle_tool_count = int(
            cycle_delta.get('meta', {}).get('tool_use_count', 0) or 0
        )
        if leak_reason and _cycle_tool_count == 0:
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
        # Dedup penalty vs previously-emitted tutor turn. A near-clone
        # of the prior turn is bad even if judges say it's clean.
        dedup_pen = _dedup_penalty(candidate_text, _prior_tutor_text)
        if dedup_pen < 0:
            logger.info(
                "[SelfRetry] cycle=%d dedup penalty %.2f vs prior turn",
                cycle_idx, dedup_pen,
            )
            score = score + dedup_pen
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
            'delta': cycle_delta,
            'label': f'cycle_{cycle_idx}',
        })

        # Don't early-exit on dedup-penalised candidates: they're judge-
        # clean but structurally a dupe, so let another cycle try.
        if clean and dedup_pen >= 0:
            break

    elapsed = time.monotonic() - started
    result.cycles_run = sum(1 for c in result.cycles if c.text or c.error)
    result.elapsed_seconds = elapsed

    # Pick the highest-scoring candidate. Ties broken by insertion
    # order (earliest cycle wins — slight bias toward previous).
    best = max(candidates, key=lambda c: c['score'])
    result.text = best['text']
    result.clean = bool(best['clean'])
    result.fallback_used = False  # stock fallback dropped 2026-05-17

    # Commit ONLY the winning candidate's delta. Engine state +
    # turn_metadata now reflect EXACTLY the shipped text — no drift
    # possible because we never committed losing candidates' state.
    tutor._apply_pose_delta(best['delta'], turn_metadata)

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
