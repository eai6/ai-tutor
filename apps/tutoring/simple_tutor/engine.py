"""The simple-tutor engine main loop.

apps/tutoring/simple_tutor/engine.py — the orchestrator that glues
prompts.py + state.py + tools.py + the LLM call into a single
``respond(session, user_input)`` function.

Per-turn flow (server owns flow, LLM is the narrator):

  1. Server picks the current question via pick_current_question.
     Sets session.current_question_id so the LLM sees one focused
     question (no attribution ambiguity).
  2. Engine gathers context: current step, KB chunks (via
     query_with_global_fallback — the pgvector layer), figure
     catalog (from LessonStep.media.images), recent turns, step
     summaries.
  3. build_system_prompt → 3 cache-marked blocks + 4 tool schemas
     (or 3 when figures are disabled per course).
  4. LLM call (Anthropic Claude Opus 4.7 by default via ModelConfig).
  5. Dispatch each tool_use block to its handler. Collect text reply.
  6. Auto-fallback: if LLM skipped record_answer but student input
     looked like an answer, server auto-grades.
  7. Persist student + tutor SessionTurns with verdicts in judge_outputs.
  8. Server auto-advance (competence threshold OR turn cap).

Hard rules:
  - The engine NEVER raises. LLM exceptions → fallback reply.
  - Tool dispatch NEVER blocks. Every handler returns a dict.
  - All server flow primitives (pick / advance / auto-grade) are
    softly-fallible and log warnings instead of crashing.

Target: ≤ 600 lines including docstrings.
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

# The five server-side tools the tutor may call. Anything else is a parse
# artifact: the text-recovery parser in apps/llm/client.py has produced
# ' record_answer' (padded), 'requestFigure' (camelCase) and a literal
# 'tool_name' placeholder. Normalise what we can, reject the rest loudly.
_KNOWN_TOOLS = frozenset({
    'pose_question', 'record_answer', 'request_figure',
    'redirect_off_topic', 'advance_step',
})

# How many times one tool may be dispatched in a single turn.
#
# pose_question: handle_pose_question REPLACES the InFlightQuestion row on
#   every call, so an unbounded dispatch lets a model that emits parallel pose
#   calls desynchronise the slot from the question the student actually read:
#   they answer question #1 and the grader scores it against question #N.
#   gemini-3.1-pro emitted 139 pose calls in a single turn and graded only 33%
#   of answers correct, against Anthropic's 87% on identical scenarios.
# record_answer: each call grades and bumps attempt_count, and a correct
#   verdict clears the slot — so duplicates inflate the hint ladder and the
#   later calls land on an empty slot. Forcing a tool call (below) makes
#   parallel duplicates more likely, so cap it before turning forcing on.
# Tools not listed here are uncapped (request_figure, advance_step,
# redirect_off_topic are idempotent enough not to need it).
MAX_CALLS_PER_TURN = {'pose_question': 1, 'record_answer': 1}

# Families exempt from the forced-tool overrides. Anthropic complies natively
# (93% of GRADE turns, 100% of POSE turns on opus) and is the benchmark
# control — forcing it would move the control underneath the experiment.
# ``None`` (production, no model profile) is exempt by construction.
_FORCE_POSE_EXEMPT_FAMILIES = frozenset({'anthropic'})

# Student intents that are conversation, not answering. A forced tool call on
# these turns would talk over the student's question.
_NON_POSING_INTENTS = frozenset({'clarification', 'pushback', 'off_topic'})

# The only tools a forced GRADE turn may choose between. Forcing a *named*
# tool (tool_choice={'type':'tool','name':'record_answer'}) suppresses every
# other tool on that call, which would kill the combined turn — grade the old
# answer AND pose the next question — that best predicts pass rate (opus does
# it on 71% of turns). So we force "some tool" (tool_choice=any / required)
# and narrow the tool list instead, which is portable across providers:
# OpenAI's "required" cannot be restricted to a subset, but a short tools list
# achieves the same thing. Without this the model could satisfy "required" by
# calling advance_step and skip a lesson step.
#
# Narrowing costs nothing: Call 2 is issued with the FULL tool list, so a model
# that wants a figure can still request one after the verdict is in hand.
_GRADE_FORCED_TOOLS = ('record_answer', 'pose_question')

_DUPLICATE_SKIP_REASON = {
    'pose_question': (
        'duplicate pose_question in one turn — only the first question is '
        'registered; ask exactly one question per reply'
    ),
    'record_answer': (
        'duplicate record_answer in one turn — the first call already graded '
        "the student's answer; grade each answer once"
    ),
}


def _normalise_tool_name(raw: Any) -> str:
    """Map a model-emitted tool name onto a known tool, or return it unchanged
    so the caller can reject it. Handles whitespace padding and camelCase."""
    name = str(raw or '').strip()
    if name in _KNOWN_TOOLS:
        return name
    snake = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
    return snake if snake in _KNOWN_TOOLS else name


def _should_force_pose(family: str | None, mode: str, student_intent: str | None) -> bool:
    """Whether Call 1 should force the pose_question tool this turn.

    Eval-only: ``family`` is None in production (no TUTOR_MODEL_OVERRIDE →
    no model profile), so production behaviour is unchanged.

    Was hard-gated to ``family == 'gemini'``. The Eval-3 sweep showed every
    Qwen model has the identical pathology the Gemini gate was written for —
    questions narrated as prose create no gradable slot, so the student's next
    answer has nothing to grade and the session stalls. POSE compliance was
    85-100% for Gemini (forced) against 3-67% for Qwen (unforced). Extend the
    remedy to every non-Anthropic family (RC-2).
    """
    if not family or family in _FORCE_POSE_EXEMPT_FAMILIES:
        return False
    if mode != 'POSE':
        return False
    return (student_intent or '') not in _NON_POSING_INTENTS


def _plan_call1(tools: list, force_pose: bool, force_grade: bool) -> tuple[list, dict | None]:
    """Choose Call 1's tool list and tool_choice.

    - forced POSE  → full tool list, force the named pose_question.
    - forced GRADE → narrowed list, force "some tool" (``any``) so the model
      may emit record_answer AND pose_question in one reply. A named force
      would forbid the combined turn.
    - neither (production, Anthropic) → the call is byte-identical to before.
    """
    if force_pose:
        return tools, {'type': 'tool', 'name': 'pose_question'}
    if force_grade:
        narrowed = [t for t in (tools or []) if t.get('name') in _GRADE_FORCED_TOOLS]
        return (narrowed or tools), {'type': 'any'}
    return tools, None


def _should_force_grade(family: str | None, mode: str, student_intent: str | None) -> bool:
    """Whether Call 1 should force a tool call on a GRADE turn.

    Eval-only, same gating as _should_force_pose. GRADE mode means only that a
    question is in flight — NOT that the student answered it. So this forces
    "call one of {record_answer, pose_question}", never a specific tool, and
    the non-Anthropic prompts instruct the model to pass an empty
    extracted_answer when the student's message was not an answer.
    handle_record_answer returns early on an empty answer without touching the
    slot, the verdict or attempt_count, so the escape hatch is side-effect free.

    Why it matters: qwen2.5:72b entered GRADE mode 354 times with the question
    already registered and called record_answer on only 109 of them. The other
    245 turns recorded no verdict, so no step advanced and the tutor re-asked
    until the session deadlocked (Eval-3 bottleneck analysis, RC-1).
    """
    if not family or family in _FORCE_POSE_EXEMPT_FAMILIES:
        return False
    if 'GRADE' not in (mode or ''):     # 'GRADE' and 'REMEDIATION+GRADE'
        return False
    return (student_intent or '') not in _NON_POSING_INTENTS


# ── B2: adaptive forcing gate ────────────────────────────────────────────────
# _should_force_pose / _should_force_grade above answer "do we EXPECT a tool
# this turn?" — they do not, on their own, decide whether to constrain Call 1.
# Blanket forcing (force Call 1 whenever we expect a tool) is what made
# gemini-3.1-pro spray pose_question 139x in one turn and collapse to 3/15 in
# sweep 2: forcing guarantees a call, not a CORRECT one, and strong tool-users
# were never the ones missing calls. The gate below runs Call 1 UNFORCED for a
# model until it is actually seen to skip an expected tool; the existing Call-2
# repair catches that first miss in the same turn, and the miss is latched so
# every later turn pre-forces. Net effect: compliant models (the strong Geminis)
# run free and never spray; non-compliant models get forced from their second
# turn on. Toggle SIMPLE_TUTOR_ADAPTIVE_FORCING=0 restores blanket forcing so a
# sweep can isolate this variable (report §12/§13).
def _adaptive_forcing_enabled() -> bool:
    return os.getenv('SIMPLE_TUTOR_ADAPTIVE_FORCING', '1').strip() != '0'


def _session_forcing_misses(session) -> int:
    es = getattr(session, 'engine_state', None) or {}
    if not isinstance(es, dict):
        return 0
    forcing = es.get('forcing')
    if not isinstance(forcing, dict):
        return 0
    try:
        return int(forcing.get('misses') or 0)
    except (TypeError, ValueError):
        return 0


def _adaptive_force_now(session, expected: bool) -> bool:
    """Whether Call 1 should actually constrain tool_choice this turn.

    ``expected`` is the output of _should_force_pose/_should_force_grade. With
    adaptive forcing off, we force whenever a tool is expected (pre-B2 blanket
    behaviour). With it on, we only force once this session has been seen to
    skip an expected tool at least once.
    """
    if not expected:
        return False
    if not _adaptive_forcing_enabled():
        return True
    return _session_forcing_misses(session) >= 1


def _record_forcing_miss(session) -> None:
    """Latch that the model skipped an expected tool on an unforced Call 1, so
    subsequent turns pre-force. Mirrors the guarded read-copy-mutate-save idiom
    used elsewhere for engine_state (isinstance guard included)."""
    es = getattr(session, 'engine_state', None) or {}
    if not isinstance(es, dict):
        es = {}
    forcing = es.get('forcing')
    if not isinstance(forcing, dict):
        forcing = {}
    forcing['misses'] = int(forcing.get('misses') or 0) + 1
    es['forcing'] = forcing
    session.engine_state = es
    try:
        session.save(update_fields=['engine_state'])
    except Exception:
        # Never let a bookkeeping save break the turn (no-block design).
        logger.warning("[simple_tutor] could not persist forcing miss "
                       "session=%s", getattr(session, 'pk', None))


if TYPE_CHECKING:
    from apps.tutoring.models import TutorSession


# Fallback reply when the LLM call fails outright. Keeps the
# conversation flowing per the no-block design.
_FALLBACK_REPLY = (
    "Sorry — I had trouble responding just now. Could you tell me "
    "what you were thinking, or ask me to try again?"
)


# ============================================================================
# Public entry point
# ============================================================================


_OPENING_INSTRUCTION = (
    "Begin the lesson. Greet the student briefly, name what they'll "
    "learn in one short sentence, then immediately pose the first "
    "warm-up question via pose_question — include the full question "
    "stem (and A/B/C/D options for MCQ) in your visible text reply. "
    "Do not ask for permission to start; just open and pose."
)

_REMEDIATION_OPENING_INSTRUCTION = (
    "The student just submitted the exit ticket. The "
    "<exit_ticket_review> block in your system prompt shows their "
    "score and which enabling objectives they missed. Open the "
    "remediation tutor-driven: acknowledge their score in ONE short "
    "sentence, briefly re-explain the FIRST missed objective in "
    "1-3 sentences (fresh phrasing, not a re-read), then "
    "IMMEDIATELY call pose_question with a NEW question targeting "
    "that same enabling_objective — include the stem and options "
    "in your visible text reply. Do NOT ask for permission "
    "(\"would you like to review?\"); just open and pose. If "
    "<missed_objectives> is empty, briefly congratulate and call "
    "advance_step."
)


def start(session: 'TutorSession') -> dict[str, Any]:
    """Open a brand-new session via the simple-tutor engine.

    Behaves like ``respond()`` but with no student turn — the message
    is a synthetic "begin the lesson" instruction the LLM sees as the
    opening user turn. The platform persists only the resulting tutor
    turn (warm-up + optional pose_question), so the chat thread starts
    with the tutor greeting + the first question.

    Returns the same dict shape as ``respond()`` so the view adapter
    can project it uniformly.
    """
    payload = respond(session, _OPENING_INSTRUCTION, _is_opening=True)
    return payload


def start_remediation(session: 'TutorSession') -> dict[str, Any]:
    """Open the remediation phase tutor-driven, immediately after the
    student submits the exit ticket. No student turn — the message is
    a synthetic "begin remediation" instruction. The engine's
    ``<exit_ticket_review>`` block (built from the latest
    ExitTicketAttempt) drives the LLM toward acknowledging the score,
    re-explaining the first missed objective, and posing a targeted
    question — all without waiting for the student to ask.
    """
    payload = respond(session, _REMEDIATION_OPENING_INSTRUCTION, _is_opening=True)
    return payload


def _course_locale(session) -> str:
    """Return the locale of the session's course, defaulting to 'en-us'.

    Path is ``session.lesson.unit.course.locale``. Any missing link in
    that chain (legacy session without a lesson, lesson without a unit,
    etc.) falls back to the global default — never raises.
    """
    try:
        course = session.lesson.unit.course
        return (getattr(course, 'locale', '') or 'en-us').lower()
    except Exception:  # noqa: BLE001 — defensive, never break the tutor
        return 'en-us'


def respond(session: 'TutorSession', user_input: str, *, _is_opening: bool = False) -> dict[str, Any]:
    """Process one student turn and return the tutor's response.

    Args:
        session: TutorSession (with engine='simple').
        user_input: the student's latest message text.

    Returns:
        ``{'content': str, 'tool_calls': list[dict], ...}`` — the
        tutor's reply for the chat UI. Never raises; on any internal
        failure, returns ``_FALLBACK_REPLY`` content.
    """
    from apps.tutoring.simple_tutor.tools import (
        autograde_bare_answer_if_clear, build_question_pool, maybe_advance_step,
    )
    from apps.tutoring.simple_tutor.state import (
        build_recent_window, step_summary_log,
    )
    from apps.tutoring.models import TutorSession as _TS

    # ─── 0. Audit trail — mark the session as routed through this engine
    if session.engine != _TS.Engine.SIMPLE:
        session.engine = _TS.Engine.SIMPLE
        session.save(update_fields=['engine'])

    # ─── 1. Gather context + check for an in-flight question ─────
    # M12 pose_question architecture: when an InFlightQuestion row
    # exists for the session, the LLM is in GRADE mode (its job is
    # to interpret the student's input against the persisted slot).
    # When no slot exists, the LLM is in TEACH/POSE mode.
    #
    # M13 remediation: when an ExitTicketAttempt exists for this
    # session, the lesson is past the assessment and the LLM is in
    # REMEDIATION mode — its job is to re-teach the failed enabling
    # objectives surfaced in the attempt's per-question results.
    from apps.tutoring.models import InFlightQuestion
    in_flight = InFlightQuestion.objects.filter(session=session).first()
    step = _load_current_step(session)
    question_pool = build_question_pool(session)
    kb_chunks = _retrieve_kb(session, user_input)
    figure_catalog = _build_figure_catalog(step)
    figures_enabled = _figures_enabled(session)
    recent_window = build_recent_window(session)
    step_summaries = step_summary_log(session)
    exit_ticket_review = _build_exit_ticket_review(session)

    # Intent classification — added 2026-05-27 per user direction.
    # The engine no longer assumes "in_flight present == student is
    # answering". A deterministic pre-call labels the student's input
    # so the LLM can route conversationally on clarification / pushback
    # / off-topic / non-engagement instead of forcing record_answer.
    #
    # M4 (2026-06-01) made the patterns locale-aware so "não sei"
    # routes the same way in a pt-mz course as "i don't know" does
    # in an en-us course.
    from apps.tutoring.simple_tutor.intent import classify_student_message
    student_intent = classify_student_message(
        user_input,
        has_inflight_question=in_flight is not None,
        locale=_course_locale(session),
    )

    if exit_ticket_review is not None:
        mode = 'REMEDIATION+GRADE' if in_flight else 'REMEDIATION'
    else:
        mode = 'GRADE' if in_flight else 'POSE'
    logger.info(
        "[simple_tutor] mode=%s intent=%s session=%s step=%s pool_size=%s",
        mode, student_intent, session.pk,
        session.current_step_index, len(question_pool),
    )

    # ─── 2. Build system prompt + tool schemas ────────────────────
    # Locale drives both the per-turn instruction in the system prompt
    # (e.g. "respond in pt-mz") and the locale-aware intent classifier.
    # Sourced from the course rather than settings.LANGUAGE_CODE so the
    # same deployment can serve EN Seychelles and PT Mozambique
    # courses simultaneously. See memory/multi_locale_architecture_research.md.
    course_locale = _course_locale(session)

    from apps.tutoring.simple_tutor.prompts import build_system_prompt
    # Eval-only: the selected model's FAMILY picks its own Block-0 prompt
    # (Qwen → Markdown variant; Gemini → XML base + targeted rules; everything
    # else, incl. Anthropic/production, → the base XML template unchanged). The
    # family comes from the model profile keyed on TUTOR_MODEL_OVERRIDE; None in
    # production. Fail-soft so a lookup error never breaks tutoring.
    _family = None
    try:
        import os as _os
        from apps.llm.model_profiles import get_model_profile
        _spec = _os.getenv('TUTOR_MODEL_OVERRIDE', '').strip()
        if _spec:
            _prof = get_model_profile(_spec)
            if _prof is not None:
                _family = _prof.family
    except Exception:
        _family = None
    system_blocks, tools = build_system_prompt(
        session=session,
        step=step,
        question_pool=question_pool,
        in_flight_question=in_flight,
        kb_chunks=kb_chunks,
        figure_catalog=figure_catalog,
        figures_enabled=figures_enabled,
        recent_window=recent_window,
        step_summaries=step_summaries,
        exit_ticket_review=exit_ticket_review,
        student_intent=student_intent,
        locale=course_locale,
        family=_family,
    )

    # ─── 4. Tool-use loop: Call 1 → tools → (optional Call 2) ─────
    # Standard Anthropic agentic pattern (per claude-prompting-expert):
    # Call 1 — model decides which tool(s) to invoke. Often emits a
    #          short pre-text + tool_use blocks; sometimes ONLY tool_use.
    # Dispatch — server runs the grader / figure lookup / etc.
    # Call 2 — when any tool fired, we append the assistant's tool_use
    #          response + the tool_results and call again. The model
    #          now composes the student-facing reply knowing the verdict.
    #          This eliminates "tool-call-only" empty bubbles and stops
    #          the model from guess-confirming a verdict before grading.
    # Eval-only: force the pose_question tool on POSE turns where a question is
    # expected. A model under AUTO function-calling that writes its question as
    # prose creates no gradable slot → the student's next answer has nothing to
    # grade → the session deadlocks/stalls. Forcing tool_choice=pose_question
    # (→ Gemini mode=ANY, OpenAI/Vertex tool_choice=function) guarantees the
    # slot. See _should_force_pose: every non-Anthropic family, POSE mode only,
    # answering intents only. Production (family None) is untouched. Models can
    # emit teaching text alongside the forced call, so explanations survive.
    # B2 adaptive gate: _should_force_* answer "do we EXPECT a tool this turn?";
    # the gate decides whether to actually constrain Call 1. Compliant models run
    # Call 1 unforced until the session is seen to skip an expected tool once.
    want_pose = _should_force_pose(_family, mode, student_intent)
    want_grade = not want_pose and _should_force_grade(_family, mode, student_intent)
    gate_open = _adaptive_force_now(session, want_pose or want_grade)
    force_pose = want_pose and gate_open
    force_grade = want_grade and gate_open
    call1_tools, call1_tool_choice = _plan_call1(tools, force_pose, force_grade)

    messages: list = [{'role': 'user', 'content': user_input}]
    response = _call_llm(
        system_blocks=system_blocks, tools=call1_tools, messages=messages,
        tool_choice=call1_tool_choice,
    )
    if response is None:
        _persist_student_turn(session, user_input, step)
        return {
            'content': _FALLBACK_REPLY,
            'tool_calls': [],
            'fallback': True,
        }

    # ─── 5. Dispatch tools from Call 1 ────────────────────────────
    # NOTE: llm_called_record_answer is unused now — the auto-fallback
    # safety net was removed 2026-05-26 because its heuristic over-fired
    # on conversational continuations. If the LLM doesn't call
    # record_answer, no grade is recorded for that turn (trust the LLM).
    text_reply_1, tool_results, _llm_called_record_answer = _dispatch_tools(
        session=session,
        response=response,
        figure_catalog=figure_catalog,
    )

    # ─── 5b. Which forced tool did Call 1 skip? ───────────────────
    # tool_choice forcing only lands on providers whose client forwards it.
    # OllamaClient does not ("accepted for signature parity but NOT
    # forwarded"), so every local open-weight model narrated its questions as
    # prose: qwen3.5:4b called pose_question on 3% of its POSE turns and never
    # once reached an exit ticket. The repair rides on Call 2 rather than
    # costing a call of its own — see _run_second_call.
    # Detect a miss against the EXPECTATION (want_*), not merely when we forced —
    # so an unforced Call 1 that skipped an expected tool is still repaired by
    # Call 2, and the skip is latched below to pre-force the rest of the session.
    missing_tool = _missing_forced_tool(want_pose, want_grade, tool_results)

    # ─── 6. Call 2 — feed tool_results back so the model writes
    #              the student-facing reply WITH the verdict in hand,
    #              and register whatever Call 1 skipped.
    text_reply, used_two_call = _run_second_call(
        session=session,
        system_blocks=system_blocks,
        tools=tools,
        messages=messages,
        response=response,
        text_reply_1=text_reply_1,
        tool_results=tool_results,
        figure_catalog=figure_catalog,
        missing_tool=missing_tool,
        user_input=user_input,
    )

    # B2: latch a Call-1 skip (whether or not Call 2 repaired it). The skip
    # itself is the compliance signal — a model that needed the repair once is
    # pre-forced from here on, so it does not narrate a question into a dead slot
    # again. Compliant models never reach here and keep running free.
    # B3: an empty-slot grade is evidence of a missed pose on a prior turn, so it
    # feeds the same gate — force pose from here on to stop the recurrence.
    if missing_tool or _graded_empty_slot(tool_results):
        _record_forcing_miss(session)

    if not text_reply:
        # Last-resort: neither call produced text. Give a neutral
        # placeholder so the bubble isn't blank.
        text_reply = _empty_reply_placeholder(tool_results)

    # Defensive enforcement of the "include question stem in visible
    # text" contract. The prompt asks the LLM to render the stem +
    # options in its text reply when pose_question fires (since the
    # frontend renders the chat thread, not the InFlightQuestion
    # slot), but the LLM sometimes forgets — leaving the student
    # with a passive "Try this:" with no question visible. Detect
    # the miss and append from the slot. Caught in M12.8 E2E.
    text_reply = _ensure_posed_question_in_text(
        text_reply, tool_results, session,
    )

    logger.info(
        "[simple_tutor] two_call=%s text_chars=%s tools=%s",
        used_two_call, len(text_reply or ''),
        [tr.get('tool') for tr in tool_results],
    )

    # ─── 8. Persist turns + verdicts ──────────────────────────────
    # On the opening (warm-up) call, ``user_input`` is the synthetic
    # "begin the lesson" instruction — do NOT persist it as a student
    # turn so the chat thread starts with the tutor's greeting.
    if not _is_opening:
        _persist_student_turn(session, user_input, step)

    # ─── 8b. B1 bare-answer safety net ────────────────────────────
    # If the student clearly answered correctly but the model never called
    # record_answer, grade it server-side (deterministic tiers only) so the step
    # still advances. Appended before the tutor turn is persisted so the verdict
    # lands in judge_outputs['grader'] exactly like a model-issued grade. Skipped
    # on the synthetic opening turn (no student answer yet).
    if not _is_opening:
        autograded = autograde_bare_answer_if_clear(
            session,
            student_answer=user_input,
            student_intent=student_intent,
            already_recorded=_tool_was_called(tool_results, 'record_answer'),
        )
        if autograded is not None:
            tool_results.append(autograded)

    _persist_tutor_turn(session, text_reply, step, tool_results)

    # ─── 9. Server auto-advance (safety net) ──────────────────────
    advanced = maybe_advance_step(session)

    return {
        'content': text_reply or '',
        'tool_calls': tool_results,
        'fallback': False,
        'step_advanced': advanced,
    }


def _extract_tool_use_blocks(response) -> list:
    """Return the tool_use blocks from an Anthropic response, in order.
    Each block exposes ``.id``, ``.name``, ``.input``.
    """
    out = []
    for block in getattr(response, 'content', None) or []:
        if getattr(block, 'type', None) == 'tool_use':
            out.append(block)
    return out


def _build_tool_result_content(tool_use_blocks: list, tool_results: list) -> list:
    """Pair the LLM's tool_use blocks with our dispatched tool_results
    by ORDER (Anthropic's tool_use_id is the canonical pairing key, but
    our dispatch loop preserves order, so the i-th tool_result matches
    the i-th tool_use block).

    Returns the list of {'type': 'tool_result', 'tool_use_id': ..., 'content': ...}
    blocks ready to send back as a user-role message in the loop.
    Auto-fallback grading results are NOT included — they didn't come
    from an LLM tool_use block.
    """
    out = []
    # Pair tool_results to tool_use blocks by block identity (the
    # dispatch loop stores the source block on each result under
    # '_block'). This is robust to dispatch re-ordering (pose_question
    # is sorted first in M12).
    by_block_id = {}
    for tr in tool_results:
        blk = tr.get('_block')
        if blk is not None:
            by_block_id[id(blk)] = tr

    for block in tool_use_blocks:
        tr = by_block_id.get(id(block))
        if tr is None:
            continue
        result_obj = tr.get('result') or {}
        tool_name = tr.get('tool') or ''
        # Render the tool result as a human-readable summary instead of
        # raw JSON. This makes the "what was just graded" context highly
        # salient when the LLM composes its Call 2 text — otherwise it
        # tends to draw on older parts of <recent_turns> and reference
        # the wrong question in hints. Caught 2026-05-26 in M11.3 E2E.
        content_text = _format_tool_result_for_call2(tool_name, result_obj)
        out.append({
            'type': 'tool_result',
            'tool_use_id': getattr(block, 'id', ''),
            'content': content_text,
        })
    return out


def _format_tool_result_for_call2(tool_name: str, result: dict) -> str:
    """Render a tool result as an instruction-laden block for Call 2.

    For record_answer specifically: surface the question_text + verdict
    + reference + student answer prominently, and remind the LLM that
    its next reply must be ABOUT THIS QUESTION (not an older one in
    recent_turns).
    """
    if tool_name == 'pose_question' and result.get('posed'):
        # The platform has persisted the question to the slot. The
        # text reply must also CONTAIN the question stem (and A/B/C/D
        # options for MCQ) so the student sees it in the chat — the
        # slot is the grading anchor, not the student-visible surface.
        qtype = result.get('question_type', '?')
        src = result.get('source', '?')
        mismatch = result.get('catalog_mismatch')
        parts = [
            f"QUESTION POSED (type={qtype}, source={src}).",
            "Your text reply on this turn MUST include the question "
            "stem verbatim (and the four labelled options A/B/C/D if "
            "MCQ) so the student can read the question in the chat. "
            "A brief lead-in is fine (e.g. 'Try this:'), but the "
            "stem itself must appear in the visible text.",
        ]
        if mismatch:
            parts.append(
                "[advisory] Your reference_answer differs from the "
                "catalog's correct_answer for this catalog_question_id. "
                "The platform is using YOUR reference; double-check "
                "you picked the right option."
            )
        return "\n".join(parts)

    if tool_name == 'record_answer' and result.get('recorded'):
        verdict = (result.get('verdict') or '?').upper()
        ref = (result.get('reference_answer') or '').strip()
        just = (result.get('justification') or '').strip()
        qtext = (result.get('question_text') or '').strip()
        qtype = result.get('question_type') or 'short_answer'
        attempts_before = result.get('attempt_count_before', 0)
        # attempt_count for the hint ladder: when verdict=correct the
        # slot was cleared; otherwise attempts is attempts_before + 1.
        attempts_so_far = attempts_before if verdict == 'CORRECT' else attempts_before + 1
        parts = [
            f"VERDICT: {verdict}",
            f"Question I just graded (type={qtype}):",
            f'  "{qtext}"' if qtext else '  (no question_text recorded)',
            f"Reference (correct answer): {ref}" if ref else "",
            f"Wrong-attempt count on this question so far: {attempts_so_far}",
            "",
            (
                "Compose your next reply ABOUT THIS EXACT QUESTION. "
                "If incorrect, give a hint per the wrong-answer "
                "ladder (1st/2nd/3rd+ attempts → progressively deeper "
                "scaffolding, never reveal the reference). If correct, "
                "briefly acknowledge and either continue teaching or "
                "call pose_question with the next question. Do NOT "
                "reference older questions from <recent_turns>."
            ),
        ]
        if just:
            parts.insert(4, f"Grader justification: {just}")
        return "\n".join(p for p in parts if p)

    if (tool_name == 'record_answer'
            and not result.get('recorded')
            and result.get('error', '').startswith('extracted_answer is empty')):
        # The escape hatch for a forced GRADE turn: the model judged that the
        # student's message was not an answer. Nothing was graded, the slot and
        # attempt_count are untouched, and the question stays in flight.
        return (
            "NOT AN ANSWER. You reported that the student's message was not an "
            "answer to the question in flight, so nothing was graded and the "
            "question is still open. Respond to what they actually said — "
            "answer their clarification, or acknowledge their hesitation — and "
            "then re-anchor them to the question, which remains unanswered."
        )

    if (tool_name == 'record_answer'
            and not result.get('recorded')
            and result.get('error', '').startswith('no in-flight')):
        return (
            "NO IN-FLIGHT QUESTION. The student's message was not "
            "interpreted as an answer because there's no question "
            "currently posed. Treat their message as a clarification "
            "or off-topic input, and respond conversationally. If you "
            "want them to answer something, call pose_question first."
        )

    # Other tools / non-success results — JSON is fine.
    import json
    return json.dumps(result, default=str)


def _empty_reply_placeholder(tool_results: list) -> str:
    """When both LLM calls produce no text (very rare), surface a
    minimal acknowledgement so the chat bubble isn't blank.

    If we have a grader verdict, briefly reflect it; otherwise stall.
    """
    verdict = None
    for tr in tool_results:
        if tr.get('tool') in ('record_answer', 'auto_grade_fallback'):
            r = tr.get('result') or {}
            if r.get('recorded'):
                verdict = r.get('verdict')
                break
    if verdict == 'correct':
        return "Got it — that's right. Here's the next one:"
    if verdict == 'incorrect':
        return "Not quite — let's walk through it together."
    return "Let's keep going."


def _ensure_posed_question_in_text(
    text_reply: str, tool_results: list, session,
) -> str:
    """Defensive: when pose_question fired this turn but the LLM's
    text reply doesn't actually contain the question stem, append the
    stem (and options for MCQ) from the persisted InFlightQuestion
    slot. The chat UI renders only the chat thread (not the slot), so
    a missed stem leaves the student with nothing to answer.

    Matching is loose — if the first 30 chars of the stem appear in
    the text reply (case-insensitive, whitespace-collapsed), assume
    the LLM included it.
    """
    posed = any(
        tr.get('tool') == 'pose_question'
        and (tr.get('result') or {}).get('posed')
        for tr in tool_results
    )
    if not posed:
        return text_reply

    from apps.tutoring.models import InFlightQuestion
    slot = InFlightQuestion.objects.filter(session=session).first()
    if slot is None:
        # Slot was posed and then immediately graded clean this turn.
        # Nothing left to render.
        return text_reply

    stem = (slot.question_text or '').strip()
    if not stem:
        return text_reply

    def _norm(s: str) -> str:
        # Lowercase + collapse whitespace + strip common markdown
        # emphasis characters so "**1:25,000**" matches "1:25,000".
        s = (s or '').lower()
        for ch in ('*', '_', '`'):
            s = s.replace(ch, '')
        return ' '.join(s.split())

    needle = _norm(stem)[:30]
    if needle and needle in _norm(text_reply):
        return text_reply  # LLM included the stem — nothing to do.

    logger.info(
        "[simple_tutor] appending missing stem session=%s slot_id=%s",
        session.pk, slot.pk,
    )

    parts = [text_reply.rstrip(), '', stem]
    # Only append the options block when the stem doesn't already
    # have lettered options baked in AND the LLM's text reply doesn't
    # already list them (some LLMs render options without the stem,
    # which would cause double-render if we naively append).
    needs_options = (
        slot.question_type == InFlightQuestion.QuestionType.MCQ
        and slot.options
        and not _contains_lettered_options(stem)
        and not _contains_lettered_options(text_reply)
    )
    if needs_options:
        for letter, opt in zip(('A', 'B', 'C', 'D'), slot.options):
            parts.append(f"{letter}) {opt}")
    return "\n".join(parts).strip()


def _contains_lettered_options(text: str) -> bool:
    """True when ``text`` contains MCQ-style option lines (at least
    A and B with either ``)`` or ``.`` after the letter).
    """
    if not text:
        return False
    import re
    has_a = bool(re.search(r'(?mi)^\s*A[\)\.]', text))
    has_b = bool(re.search(r'(?mi)^\s*B[\)\.]', text))
    return has_a and has_b


# ============================================================================
# Context helpers
# ============================================================================


def _load_current_step(session):
    """Resolve the LessonStep at the session's current_step_index, or
    None if past the last step (exit-ticket / completion mode)."""
    from apps.curriculum.models import LessonStep
    current_idx = getattr(session, 'current_step_index', 0) or 0
    return (
        LessonStep.objects
        .filter(lesson=session.lesson, order_index=current_idx)
        .first()
    )


def _figures_enabled(session) -> bool:
    """Read course.tutoring_images_enabled. Default True."""
    lesson = getattr(session, 'lesson', None)
    if lesson is None:
        return True
    unit = getattr(lesson, 'unit', None)
    if unit is None:
        return True
    course = getattr(unit, 'course', None)
    if course is None:
        return True
    return bool(getattr(course, 'tutoring_images_enabled', True))


def _retrieve_kb(session, query_text: str) -> list[dict]:
    """Retrieve KB chunks via the pgvector layer
    (CurriculumKnowledgeBase.query_with_global_fallback). Fails soft —
    if KB is unavailable, returns []."""
    if not query_text or not query_text.strip():
        return []
    try:
        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
    except ImportError:
        return []
    lesson = getattr(session, 'lesson', None)
    course = getattr(getattr(lesson, 'unit', None), 'course', None)
    try:
        kb = CurriculumKnowledgeBase(
            institution_id=session.institution_id,
        )
        return kb.query_with_global_fallback(
            query_text=query_text,
            n_results=5,
            course=course,
        )
    except Exception as exc:
        logger.warning(
            "_retrieve_kb: failed (session=%s): %s",
            session.pk, exc,
        )
        return []


def _build_figure_catalog(step) -> list[dict]:
    """Synthesise stable per-turn figure ids from LessonStep.media.images.

    Each entry in step.media['images'] becomes
    ``{'id': i+1, 'description': alt, 'url': ..., 'alt_text': alt, 'caption': caption}``.
    The id is 1-based and stable within a step (matches the position
    in the JSON list).
    """
    if step is None:
        return []
    media = getattr(step, 'media', None) or {}
    if not isinstance(media, dict):
        return []
    images = media.get('images') or []
    if not isinstance(images, list):
        return []

    catalog = []
    for i, img in enumerate(images):
        if not isinstance(img, dict):
            continue
        url = (img.get('url') or '').strip()
        if not url:
            continue
        catalog.append({
            'id': i + 1,
            'description': (img.get('alt') or img.get('caption') or '').strip(),
            'url': url,
            'alt_text': (img.get('alt') or '').strip(),
            'caption': (img.get('caption') or '').strip(),
        })
    return catalog


# ============================================================================
# LLM call
# ============================================================================


def _system_blocks_to_text(system_blocks) -> str:
    """Flatten Anthropic system blocks (``[{'type':'text','text':..}]``,
    possibly carrying cache_control) into a single string for the
    provider-agnostic ``generate_with_tools()``, which re-applies any
    provider-specific prompt caching internally."""
    if isinstance(system_blocks, str):
        return system_blocks
    parts: list[str] = []
    for b in system_blocks or []:
        if isinstance(b, dict):
            t = b.get('text')
            if t:
                parts.append(t)
        elif isinstance(b, str):
            parts.append(b)
    return '\n\n'.join(parts)


def _call_llm(
    *,
    system_blocks: list,
    tools: list,
    messages: list,
    tool_choice: dict | None = None,
):
    """Call Anthropic with the simple-tutor prompt + tools + the messages
    array. Returns the raw Anthropic response, or None on any error.

    ``tool_choice`` is an optional Anthropic-shaped tool-choice value
    (e.g. ``{"type": "tool", "name": "pose_question"}``). It is None in
    production (the Anthropic call is byte-identical) and set only by the
    eval path for non-Anthropic families that need a tool call forced —
    the cross-provider clients translate it to their native shape
    (Gemini ``function_calling_config mode=ANY``, OpenAI ``tool_choice``).

    Uses ``ModelConfig.get_for('tutoring')`` so the model is configurable
    via the dashboard. Defaults to Claude Opus 4.7 per the prod config.
    Never raises — failures log a warning and return None; caller serves
    the fallback reply.

    ``messages`` is the full Anthropic messages list — the caller manages
    the user/assistant/tool_result alternation for the two-call loop.
    """
    try:
        from apps.llm.models import ModelConfig
    except ImportError:
        logger.warning("_call_llm: ModelConfig unavailable")
        return None

    try:
        config = ModelConfig.get_for('tutoring')
    except Exception as exc:
        logger.warning("_call_llm: ModelConfig.get_for raised: %s", exc)
        return None

    if config is None:
        logger.warning("_call_llm: no tutoring ModelConfig found")
        return None

    model_name = config.model_name
    if not model_name:
        logger.warning("_call_llm: no model_name on tutoring config")
        return None

    provider = str(getattr(config, 'provider', '') or '').lower()

    # ── Eval-only per-family profile (apps/llm/model_profiles) ──────────
    # Keyed on the sweep override spec; None in production (override unset)
    # → behaviour unchanged. Bypasses the [0.1,0.3] tutoring clamp the same
    # way the regen ensemble does — by passing explicit sampling/max_tokens
    # at the call site rather than mutating ModelConfig.effective_temperature.
    import os
    profile = None
    try:
        from apps.llm.model_profiles import get_model_profile
        spec = os.getenv('TUTOR_MODEL_OVERRIDE', '').strip()
        profile = get_model_profile(spec or model_name)
    except Exception as exc:
        logger.warning("_call_llm: model_profile lookup failed: %s", exc)
        profile = None

    max_tokens = profile.max_tokens if profile else 1024
    sampling = profile.sampling_dict() if profile else None
    # Eval-only temperature override, so the sampling A/B is a env-var flip
    # rather than a code edit. The Qwen profiles run at 0.7 (Qwen's recommended
    # chat sampling) while production clamps TUTORING to [0.1, 0.3]; that
    # confound is called out in the Eval-3 analysis (RC-6). Never applies in
    # production, where `profile` is None.
    if profile is not None and sampling is not None:
        _temp_override = os.getenv('TUTOR_EVAL_TEMPERATURE', '').strip()
        if _temp_override:
            try:
                sampling = {**sampling, 'temperature': float(_temp_override)}
            except ValueError:
                logger.warning(
                    "_call_llm: bad TUTOR_EVAL_TEMPERATURE=%r — ignoring",
                    _temp_override,
                )
    effective_blocks = system_blocks
    if profile is not None:
        try:
            from apps.tutoring.simple_tutor.prompts import family_prompt_delta
            delta = family_prompt_delta(profile.family, profile.prompt_strategy)
        except Exception:
            delta = ''
        if delta:
            # New list (idempotent across the 2-call loop) — append the delta as
            # an uncached trailing block so the production cache key is untouched.
            effective_blocks = list(system_blocks) + [{'type': 'text', 'text': delta}]
        logger.info(
            "_call_llm: profile family=%s mode=%s max_tokens=%d sampling=%s",
            profile.family, profile.mode, max_tokens, sampling or {},
        )

    # Anthropic: keep the native SDK path byte-for-byte so production
    # tutor behaviour is unchanged.
    if provider == 'anthropic':
        api_key = config.get_api_key()
        if not api_key:
            logger.warning("_call_llm: missing api_key for anthropic config")
            return None
        try:
            import anthropic
        except ImportError:
            logger.warning("_call_llm: anthropic SDK not installed")
            return None
        try:
            client = anthropic.Anthropic(api_key=api_key)
            # tool_choice omitted entirely when None so the production
            # Anthropic call is byte-identical (default is auto).
            extra = {'tool_choice': tool_choice} if tool_choice else {}
            return client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                system=effective_blocks,
                tools=tools,
                messages=messages,
                **extra,
            )
        except Exception as exc:
            msg = str(exc).strip().replace('\n', ' ')[:200]
            logger.warning(
                "_call_llm: Anthropic call failed: %s: %s",
                type(exc).__name__, msg,
            )
            return None

    # Any other provider (OpenAI / Gemini / local Ollama): route through
    # the pluggable client factory's generate_with_tools(), which returns
    # an Anthropic-Message-shaped object (AdaptedMessage) that
    # _dispatch_tools walks identically. This is what lets the tutor run
    # on an open-source local model.
    try:
        from apps.llm.client import get_llm_client
        client = get_llm_client(config)
    except Exception as exc:
        logger.warning("_call_llm: get_llm_client failed: %s", exc)
        return None
    if not hasattr(client, 'generate_with_tools'):
        logger.warning(
            "_call_llm: %s has no generate_with_tools — cannot run tool loop",
            type(client).__name__,
        )
        return None
    try:
        return client.generate_with_tools(
            messages=messages,
            system_prompt=_system_blocks_to_text(effective_blocks),
            tools=tools,
            max_tokens=max_tokens,
            sampling=sampling,
            tool_choice=tool_choice,
        )
    except Exception as exc:
        msg = str(exc).strip().replace('\n', ' ')[:200]
        logger.warning(
            "_call_llm: generate_with_tools(%s) failed: %s: %s",
            provider, type(exc).__name__, msg,
        )
        return None


# ============================================================================
# Tool dispatch
# ============================================================================


def _pose_was_registered(tool_results: list[dict]) -> bool:
    """True when this turn actually wrote the in-flight question slot."""
    return any(
        tr.get('tool') == 'pose_question' and (tr.get('result') or {}).get('posed')
        for tr in (tool_results or [])
    )


def _tool_was_called(tool_results: list[dict], name: str) -> bool:
    """True when the model called ``name`` at all this turn, whatever the
    result. A record_answer that deliberately passed an empty answer ("the
    student did not answer") counts as called — the model made the judgement,
    which is all the repair path is trying to elicit."""
    return any(tr.get('tool') == name for tr in (tool_results or []))


def _graded_empty_slot(tool_results: list[dict]) -> bool:
    """B3: True when the model called record_answer but no question was in
    flight. That only happens when it posed a question as prose on a PRIOR turn
    without calling pose_question — so there is no stored reference to grade
    against, and reconstructing one risks grading against the wrong question.
    The safe, root-cause fix is to treat it as a missed pose and let the B2
    adaptive gate force pose_question from here on (which guarantees a slot and
    stops the empty-slot grade recurring). We do NOT fabricate a reference."""
    for tr in tool_results or []:
        if tr.get('tool') != 'record_answer':
            continue
        result = tr.get('result') or {}
        if not result.get('recorded') and 'in-flight' in str(result.get('error', '')):
            return True
    return False


# Repair instruction. Positive framing throughout: Google documents that
# open-ended negatives ("do not…") make Gemini over-index and degrade its
# arithmetic and logic, and the same phrasing is what the Qwen markdown
# template already uses.
_POSE_REPAIR_INSTRUCTION = (
    "Register the question you just asked so the platform can grade the "
    "student's reply. Call pose_question once, with the exact question you "
    "wrote, its question_type, its options when it is multiple choice, and "
    "its reference_answer.\n\n"
    "The question you wrote was:\n{assistant_text}"
)


_RECORD_REPAIR_INSTRUCTION = (
    "Submit the student's answer for grading. Call record_answer once, with "
    "their answer exactly as they wrote it. If their message was not an "
    "answer to the question — a clarification, a request for help, or "
    "hesitation — call record_answer with an empty extracted_answer, and the "
    "platform will record nothing.\n\n"
    "The student wrote:\n{user_input}"
)


def _missing_forced_tool(force_pose: bool, force_grade: bool, tool_results: list) -> str | None:
    """The forced tool Call 1 was asked for but did not deliver, if any."""
    if force_pose and not _pose_was_registered(tool_results):
        return 'pose_question'
    if force_grade and not _tool_was_called(tool_results, 'record_answer'):
        return 'record_answer'
    return None


def _repair_instruction(missing_tool: str, user_input: str, assistant_text: str) -> str:
    if missing_tool == 'pose_question':
        return _POSE_REPAIR_INSTRUCTION.format(
            assistant_text=(assistant_text or '').strip()[:1500])
    return _RECORD_REPAIR_INSTRUCTION.format(
        user_input=str(user_input or '').strip()[:1500])


def _plan_call2(tools: list, missing_tool: str | None) -> tuple[list, dict | None]:
    """Choose Call 2's tool list and tool_choice.

    With nothing missing, Call 2 is exactly what it always was: full tool list,
    no forcing. When Call 1 skipped a forced tool, Call 2 does double duty — it
    composes the student-facing reply AND registers what was missed. Only the
    missing tool is exposed, so any tool call it makes is the one we need, which
    is what lets Ollama (which cannot honour tool_choice) be repaired at all.
    """
    if not missing_tool:
        return tools, None
    only = [t for t in (tools or []) if t.get('name') == missing_tool]
    return (only or tools), {'type': 'tool', 'name': missing_tool}


def _run_second_call(
    *, session, system_blocks, tools, messages, response, text_reply_1,
    tool_results, figure_catalog, missing_tool, user_input,
) -> tuple[str, bool]:
    """Issue Call 2, folding in the repair when Call 1 skipped a forced tool.

    Returns ``(text_reply, used_two_call)`` and extends ``tool_results`` with
    anything Call 2 dispatched.

    Cost note: a compliant turn already makes two calls (opus does so on 95% of
    turns). The models that skip the protocol make ONE call, so folding the
    repair into Call 2 means a repaired turn costs exactly what a correct turn
    always cost — never three. Previously the repair was a separate call.
    """
    tool_use_blocks = _extract_tool_use_blocks(response)
    tool_result_content = (
        _build_tool_result_content(tool_use_blocks, tool_results)
        if tool_use_blocks else []
    )
    if not tool_result_content and not missing_tool:
        # Nothing to feed back and nothing to repair — no second call, exactly
        # as before. This is the production/Anthropic path when Call 1 wrote a
        # plain conversational reply.
        return text_reply_1, False

    if tool_result_content:
        messages.append({'role': 'assistant', 'content': response.content})
        user_blocks = list(tool_result_content)
        if missing_tool:
            # Ride along in the SAME user message — Gemini requires strict
            # user/model alternation, so a second consecutive user turn would
            # be rejected.
            user_blocks.append({
                'type': 'text',
                'text': _repair_instruction(missing_tool, user_input, text_reply_1),
            })
        messages.append({'role': 'user', 'content': user_blocks})
    else:
        # Call 1 emitted no tool at all, so there would have been no Call 2.
        # This call IS the repair: the turn still costs exactly two calls.
        messages.append({'role': 'assistant', 'content': text_reply_1 or '(no reply)'})
        messages.append({
            'role': 'user',
            'content': _repair_instruction(missing_tool, user_input, text_reply_1),
        })

    if missing_tool:
        logger.info(
            "[simple_tutor] call2_repair: Call 1 skipped %s — folding the "
            "repair into Call 2 (no extra call)", missing_tool,
        )

    call2_tools, call2_tool_choice = _plan_call2(tools, missing_tool)
    response2 = _call_llm(
        system_blocks=system_blocks, tools=call2_tools, messages=messages,
        tool_choice=call2_tool_choice,
    )
    if response2 is None:
        return text_reply_1, False

    text_reply_2, extra_tool_results, _ = _dispatch_tools(
        session=session, response=response2, figure_catalog=figure_catalog,
    )
    # Call 2 is meant to produce text; if it also chose to call a tool, accept
    # the side effects but use only the accumulated text. (No third call —
    # keeps latency bounded.)
    tool_results.extend(extra_tool_results)
    if missing_tool and not _tool_was_called(extra_tool_results, missing_tool):
        logger.warning(
            "[simple_tutor] call2_repair: model still declined to call %s — "
            "this turn leaves no %s", missing_tool,
            'gradable slot' if missing_tool == 'pose_question' else 'verdict',
        )
    return (text_reply_2 or text_reply_1), True


def _dispatch_tools(*, session, response, figure_catalog):
    """Walk Anthropic response content. For each text block, accumulate
    the reply. For each tool_use block, dispatch to the right handler.

    M12 dispatch order: pose_question runs FIRST (it writes the
    in-flight slot), so a same-turn record_answer can read the freshly
    posed question. Anthropic returns content blocks in the order the
    model produced them, but the model can interleave — we re-order
    pose_question → record_answer → other to make the semantics
    deterministic.

    Returns:
        (text_reply, tool_results, llm_called_record_answer)
    """
    from apps.tutoring.simple_tutor.tools import (
        handle_pose_question, handle_record_answer, handle_request_figure,
        handle_redirect_off_topic, handle_advance_step,
    )

    text_reply = ''
    tool_use_blocks: list = []
    llm_called_record_answer = False

    # First pass — accumulate text + collect tool_use blocks separately.
    for block in getattr(response, 'content', None) or []:
        btype = getattr(block, 'type', None)
        if btype == 'text':
            text_reply += getattr(block, 'text', '')
        elif btype == 'tool_use':
            tool_use_blocks.append(block)

    # Sort: pose_question first, then record_answer, then everything
    # else preserving original order. This way handle_record_answer
    # always sees the slot the LLM just wrote in the same turn.
    def _priority(blk) -> int:
        n = getattr(blk, 'name', '')
        if n == 'pose_question':
            return 0
        if n == 'record_answer':
            return 1
        return 2
    sorted_blocks = sorted(
        enumerate(tool_use_blocks), key=lambda iblk: (_priority(iblk[1]), iblk[0]),
    )

    tool_results: list[dict] = []
    dispatched: dict[str, int] = {}
    for _idx, block in sorted_blocks:
        name = _normalise_tool_name(getattr(block, 'name', ''))
        params = getattr(block, 'input', None) or {}
        if not isinstance(params, dict):
            params = {}

        # Per-tool cap. A duplicate is NOT dispatched, but it still gets a
        # tool_result so every tool_use block stays paired (Anthropic rejects
        # an unpaired tool_use in the Call-2 message).
        cap = MAX_CALLS_PER_TURN.get(name)
        if cap is not None and dispatched.get(name, 0) >= cap:
            logger.warning(
                "_dispatch_tools: dropped duplicate %s (#%d this turn) — "
                "only the first call in a turn takes effect",
                name, dispatched[name] + 1,
            )
            dispatched[name] += 1
            tool_results.append({
                'tool': name,
                'result': {
                    'posed': False,
                    'recorded': False,
                    'skipped': True,
                    'skip_reason': _DUPLICATE_SKIP_REASON.get(
                        name, f'duplicate {name} in one turn — only the first takes effect'),
                },
                '_block': block,
            })
            continue

        try:
            if name == 'pose_question':
                result = handle_pose_question(
                    session,
                    question_text=str(params.get('question_text', '')),
                    question_type=str(params.get('question_type', '')),
                    reference_answer=str(params.get('reference_answer', '')),
                    source=str(params.get('source', '')),
                    options=params.get('options') or [],
                    catalog_question_id=(
                        params.get('catalog_question_id')
                        if isinstance(params.get('catalog_question_id'), int)
                        else None
                    ),
                )
            elif name == 'record_answer':
                result = handle_record_answer(
                    session,
                    extracted_answer=str(params.get('extracted_answer', '')),
                )
                llm_called_record_answer = True
            elif name == 'request_figure':
                fid = params.get('figure_id')
                try:
                    fid = int(fid) if fid is not None else None
                except (TypeError, ValueError):
                    fid = None
                if fid is None:
                    result = {'displayed': False, 'error': 'invalid figure_id'}
                else:
                    result = handle_request_figure(
                        session,
                        figure_id=fid,
                        figure_catalog=figure_catalog,
                    )
            elif name == 'redirect_off_topic':
                result = handle_redirect_off_topic(
                    session, reason=str(params.get('reason', '')),
                )
            elif name == 'advance_step':
                result = handle_advance_step(
                    session, reason=str(params.get('reason', '')),
                )
            else:
                # Never silently no-op: an unknown name means either the model
                # invented a tool or the text-recovery parser mis-extracted one
                # (e.g. the literal placeholder 'tool_name'). Both are bugs we
                # want counted in the sweep logs.
                logger.warning(
                    "_dispatch_tools: unknown tool %r (raw=%r) — not dispatched",
                    name, getattr(block, 'name', ''),
                )
                result = {'error': f'unknown tool {name!r}'}
        except Exception as exc:
            # Handlers should not raise, but if one does, log + continue
            msg = str(exc).strip().replace('\n', ' ')[:200]
            logger.warning(
                "_dispatch_tools: handler for %s raised %s: %s",
                name, type(exc).__name__, msg,
            )
            result = {'error': f'handler exception {type(exc).__name__}'}

        dispatched[name] = dispatched.get(name, 0) + 1
        tool_results.append({'tool': name, 'result': result, '_block': block})

    return text_reply, tool_results, llm_called_record_answer


# ============================================================================
# Persistence
# ============================================================================


def _persist_student_turn(session, user_input: str, step):
    """Create the student's SessionTurn row."""
    from apps.tutoring.models import SessionTurn
    SessionTurn.objects.create(
        session=session,
        role=SessionTurn.Role.STUDENT,
        content=user_input or '',
        step=step,
    )


def respond_for_view(session, user_input: str) -> dict:
    """Adapter for ``apps.tutoring.views.chat_respond``.

    Calls ``respond(...)`` (which returns the engine's internal dict),
    then projects the result into the same JSON shape the legacy
    v1 view returns — so the existing chat UI works without changes.

    Fields not produced by v1 of the simple engine (gamification,
    artifact_html, follow_up, etc.) default to safe values.
    """
    from apps.curriculum.models import LessonStep
    from apps.tutoring.models import ExitTicketAttempt

    out = respond(session, user_input)

    # Derive step display fields from session state (set by maybe_advance_step)
    session.refresh_from_db()
    current_idx = session.current_step_index or 0
    step = (
        LessonStep.objects
        .filter(lesson=session.lesson, order_index=current_idx)
        .first()
    )
    total_steps = LessonStep.objects.filter(lesson=session.lesson).count()

    # Extract is_correct from any record_answer verdict.
    is_correct = None
    media_url = None
    for entry in out.get('tool_calls') or []:
        tool = entry.get('tool')
        result = entry.get('result') or {}
        if tool == 'record_answer':
            verdict = result.get('verdict')
            if verdict == 'correct':
                is_correct = True
            elif verdict == 'incorrect':
                is_correct = False
        elif tool == 'request_figure' and result.get('displayed'):
            media_url = result.get('url')

    # Exit ticket transition: when all lesson steps are done, hand the
    # student off to the exit ticket instead of marking is_complete.
    # The session is only TRULY complete once the exit ticket itself
    # is scored (the legacy chat_complete_session endpoint handles that).
    #
    # Guard against the pose-and-complete race: if pose_question fired
    # this turn, the LLM has a fresh question in the slot the student
    # hasn't seen yet. Clear that slot before transitioning to the
    # exit ticket so the unanswered pose doesn't dangle as orphan state.
    steps_exhausted = step is None or current_idx >= total_steps
    exit_ticket_payload = None
    show_exit_ticket = False
    is_complete = False
    if steps_exhausted:
        # Only surface the exit-ticket modal on the FIRST transition.
        # Once the student has submitted (and we have an
        # ExitTicketAttempt for this session), the engine is in
        # REMEDIATION mode — re-rendering the modal would confuse the
        # student and bury the post-submit chat. M12.9 E2E found this:
        # the modal reopened on every remediation turn.
        from apps.tutoring.models import ExitTicketAttempt
        already_submitted = ExitTicketAttempt.objects.filter(
            session=session, completed_at__isnull=False,
        ).exists()
        # Remediation-complete trigger: handle_advance_step sets this
        # flag when the LLM calls advance_step after a failed attempt
        # ("the student has recovered all missed objectives, time to
        # re-take the quiz"). Bypasses the already_submitted guard so
        # the modal re-fires. We clear the flag on this transition so
        # the modal doesn't open in a loop after the next submit.
        session.refresh_from_db()
        es = session.engine_state or {}
        remediation_complete = bool(es.get('remediation_complete'))

        if remediation_complete:
            # Clear the flag so this only fires once per cycle.
            es.pop('remediation_complete', None)
            session.engine_state = es
            session.save(update_fields=['engine_state'])

        # Fire the exit-ticket modal when:
        #   - first time through (not already_submitted), OR
        #   - LLM signaled remediation complete (re-take)
        should_fire_modal = (not already_submitted) or remediation_complete

        if not should_fire_modal:
            # Remediation phase — let the chat continue normally.
            show_exit_ticket = False
        else:
            posed_this_turn = any(
                entry.get('tool') == 'pose_question'
                and (entry.get('result') or {}).get('posed')
                for entry in (out.get('tool_calls') or [])
            )
            if posed_this_turn:
                from apps.tutoring.models import InFlightQuestion
                cleared = InFlightQuestion.objects.filter(session=session).delete()
                logger.info(
                    "[simple_tutor] cleared orphan pose at exit-ticket "
                    "handoff session=%s deleted=%s",
                    session.pk, cleared,
                )

            exit_ticket_payload = _build_exit_ticket_payload(session)
            if exit_ticket_payload is not None:
                show_exit_ticket = True
                # is_complete stays False — the student still has to
                # submit the exit ticket. The chat_complete_session
                # endpoint flips the session to COMPLETED once the
                # ticket is scored.
                #
                # Replace the LLM's text with a clean transition
                # message. Without this, the last tutor turn's text
                # was often a freshly-posed question (the LLM didn't
                # know the exit ticket was about to fire), so the
                # student saw two prompts back-to-back: the LLM's
                # question + the exit-quiz modal. Also overwrite the
                # persisted tutor turn so resume doesn't surface the
                # dangling question on reload.
                from apps.tutoring.models import SessionTurn
                from django.utils.translation import gettext
                transition_msg = gettext(
                    "Great — you've worked through the lesson! Now "
                    "let's check what you've locked in with a short "
                    "quiz. Take your time on each question, then submit."
                )
                last_tutor_turn = (
                    SessionTurn.objects
                    .filter(session=session, role=SessionTurn.Role.TUTOR)
                    .order_by('-id').first()
                )
                if last_tutor_turn is not None:
                    last_tutor_turn.content = transition_msg
                    last_tutor_turn.save(update_fields=['content'])
                out = dict(out)
                out['content'] = transition_msg
            else:
                # No exit ticket attached → end the lesson here.
                is_complete = True

    # Phase + step number — match the rules in _project_start_payload
    # so resume + respond render the same chip label and the step
    # counter doesn't overshoot (the "Evaluate 6/5" bug).
    last_attempt = (
        ExitTicketAttempt.objects
        .filter(session=session, completed_at__isnull=False)
        .order_by('-completed_at')
        .first()
    )
    has_failed_attempt = bool(last_attempt and not last_attempt.passed)
    has_passed_attempt = bool(last_attempt and last_attempt.passed)
    if has_failed_attempt:
        phase = 'remediation'
    elif has_passed_attempt:
        phase = 'completed'
    elif show_exit_ticket:
        phase = 'exit_ticket'
    elif step is not None:
        phase = (getattr(step, 'phase', '') or '').lower() or 'evaluate'
    else:
        phase = 'evaluate'
    step_number = min(current_idx + 1, total_steps) if total_steps else 1

    return {
        'message': out.get('content', ''),
        'phase': phase,
        'media': [{'url': media_url}] if media_url else [],
        'show_exit_ticket': show_exit_ticket,
        'exit_ticket': exit_ticket_payload,
        'is_complete': is_complete,
        'step_number': step_number,
        'total_steps': total_steps,
        'is_correct': is_correct,
        'streak_count': None,                 # gamification not in v2 scope
        'practice_score': None,
        'milestone': None,
        'artifact_html': None,
        'probe': None,
        'pending_question': None,
        'follow_up_message': None,
    }


# Exit-ticket shape is env-tunable so we can flip filters during the
# pilot without a code deploy. Defaults per 2026-05-26 directive:
# 10 MCQ-only questions. Override via ``EXIT_TICKET_*`` env vars (the
# loader uses ``os.environ`` on every call so a Container App
# ``--set-env-vars`` flip takes effect without a process restart).
DEFAULT_EXIT_TICKET_CAP = 10
DEFAULT_EXIT_TICKET_TYPES = ('mcq',)


def _exit_ticket_cap() -> int:
    """Max number of questions on the exit ticket. Override via
    ``EXIT_TICKET_MAX_QUESTIONS``. Values <1 fall back to the default.
    """
    raw = (os.environ.get('EXIT_TICKET_MAX_QUESTIONS') or '').strip()
    if not raw:
        return DEFAULT_EXIT_TICKET_CAP
    try:
        n = int(raw)
        return n if n > 0 else DEFAULT_EXIT_TICKET_CAP
    except ValueError:
        return DEFAULT_EXIT_TICKET_CAP


def _exit_ticket_allowed_types() -> tuple[str, ...]:
    """Which ``ExitTicketQuestion.question_type`` values are eligible.
    Override via ``EXIT_TICKET_TYPES`` (comma-separated, e.g.
    ``mcq,short_answer``). Empty / unset → MCQ-only default.
    """
    raw = (os.environ.get('EXIT_TICKET_TYPES') or '').strip().lower()
    if not raw:
        return DEFAULT_EXIT_TICKET_TYPES
    parts = tuple(p.strip() for p in raw.split(',') if p.strip())
    return parts or DEFAULT_EXIT_TICKET_TYPES


def _build_exit_ticket_payload(session) -> dict | None:
    """Build the same exit-ticket payload the legacy engine emits, so
    the existing chat UI can render the ticket without changes.

    Returns the dict shape ``{'questions': [...], 'total': N,
    'passing_score': N}``, or ``None`` when no exit ticket is attached
    to this lesson.

    Filters by ``EXIT_TICKET_TYPES`` (default: MCQ-only) and caps at
    ``EXIT_TICKET_MAX_QUESTIONS`` (default: 10) — shorter, gradable,
    consistent format per pilot directive 2026-05-26.
    """
    import random
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion

    et = ExitTicket.objects.filter(lesson=session.lesson).first()
    if et is None:
        return None

    allowed_types = _exit_ticket_allowed_types()
    eligible = list(
        ExitTicketQuestion.objects.filter(
            exit_ticket=et, question_type__in=allowed_types,
        )
    )
    if not eligible:
        return None

    # Randomized sub-sample, deterministic per session for replay
    # consistency within a session (same student reload → same items).
    rng = random.Random(session.pk)
    rng.shuffle(eligible)
    selected = eligible[:_exit_ticket_cap()]

    # Persist the selected IDs (in render order) so the legacy submit
    # endpoint — which instantiates a fresh ConversationalTutor and
    # reads ``engine_state['selected_exit_ticket_ids']`` via
    # ``_load_exit_ticket_concepts`` — grades each answer against the
    # SAME question that was rendered. Without this, the submit-side
    # tutor would generate its own random selection and grade
    # answer[i] against the wrong question. (Caught by M12.9 E2E:
    # 1/10 score when answers were objectively correct.)
    selected_ids = [q.id for q in selected]
    es = dict(session.engine_state or {})
    if es.get('selected_exit_ticket_ids') != selected_ids:
        es['selected_exit_ticket_ids'] = selected_ids
        session.engine_state = es
        session.save(update_fields=['engine_state'])

    exit_questions = []
    for i, q in enumerate(selected):
        q_type = (q.question_type or 'mcq')
        q_data = {
            'index': i,
            'question_type': q_type,
            'question': q.question_text,
        }
        if q_type == 'mcq':
            q_data['options'] = [
                {'letter': 'A', 'text': q.option_a or ''},
                {'letter': 'B', 'text': q.option_b or ''},
                {'letter': 'C', 'text': q.option_c or ''},
                {'letter': 'D', 'text': q.option_d or ''},
            ]
            q_data['correct'] = q.correct_answer or ''
            if q.answer_data and isinstance(q.answer_data, dict) and q.answer_data.get('source'):
                q_data['source'] = q.answer_data['source']
        else:
            q_data['answer_data'] = q.answer_data or {}
        exit_questions.append(q_data)

    return {
        'questions': exit_questions,
        'total': len(exit_questions),
        'passing_score': min(et.passing_score or 8, len(exit_questions)),
    }


def _build_exit_ticket_review(session) -> dict | None:
    """Build a remediation-context payload from the most recent
    ``ExitTicketAttempt`` for this session.

    Returns ``None`` when the student hasn't submitted yet. When an
    attempt exists, returns the score, pass/fail, and the per-question
    breakdown grouped by enabling_objective so the system prompt can
    surface the failing objectives explicitly. M13 remediation.

    Schema (mirrors what the legacy engine persists at
    ``ExitTicketAttempt.answers``):
        {
            'score': int, 'total': int, 'passed': bool,
            'missed_objectives': [
                {
                    'enabling_objective': str,
                    'asked': int, 'correct': int,
                    'sample_question': str,        # one missed question stem
                    'student_answer': str,         # what they typed
                    'reference': str,              # correct letter/value
                },
                ...
            ],
            'mastered_objectives': [str, ...],   # EOs got 100% on
        }
    """
    from apps.tutoring.models import ExitTicketAttempt, ExitTicketQuestion

    attempt = (
        ExitTicketAttempt.objects
        .filter(session=session, completed_at__isnull=False)
        .order_by('-completed_at')
        .first()
    )
    if attempt is None:
        return None

    answers = attempt.answers or {}
    per_question = answers.get('per_question') or []
    eo_competency = answers.get('eo_competency') or {}

    # If the persisted shape isn't what we expect (e.g. attempt from
    # an older engine version), skip remediation context — better to
    # fall back to plain POSE/TEACH mode than render a broken block.
    if not per_question:
        return None

    # Group per_question by EO so we can name them in the prompt and
    # pull a sample missed question for each one.
    missed_objectives: list = []
    mastered_objectives: list = []
    for eo, bucket in eo_competency.items():
        if not eo:
            continue
        asked = int(bucket.get('asked') or 0)
        correct = int(bucket.get('correct') or 0)
        if asked == 0:
            continue
        if correct >= asked:
            mastered_objectives.append(eo)
            continue
        # Find a sample missed question for this EO.
        sample_q = None
        for entry in per_question:
            if entry.get('enabling_objective') == eo and not entry.get('correct'):
                sample_q = entry
                break
        sample_stem = ''
        sample_student = ''
        sample_ref = ''
        if sample_q:
            sample_student = str(sample_q.get('selected') or '')
            # We need to look up the question text + correct answer
            # from ExitTicketQuestion. failed_question_ids is in the
            # bucket; pick the first.
            failed_ids = bucket.get('failed_question_ids') or []
            if failed_ids:
                q = ExitTicketQuestion.objects.filter(pk=failed_ids[0]).first()
                if q is not None:
                    sample_stem = (q.question_text or '').strip()
                    sample_ref = (q.correct_answer or '').strip()
        missed_objectives.append({
            'enabling_objective': eo,
            'asked': asked,
            'correct': correct,
            'sample_question': sample_stem,
            'student_answer': sample_student,
            'reference': sample_ref,
        })

    return {
        'score': int(attempt.score or 0),
        'total': len(per_question),
        'passed': bool(attempt.passed),
        'missed_objectives': missed_objectives,
        'mastered_objectives': mastered_objectives,
    }


def start_for_view(session) -> dict:
    """Adapter for ``apps.tutoring.views.chat_start_session`` when the
    simple-tutor engine handles the warmup.

    Three branches:

    1. **Resume with in-flight question** — when an ``InFlightQuestion``
       slot exists AND the session has prior turns, the student is
       returning mid-question. Skip the LLM entirely and emit a
       deterministic "Welcome back — here's where we left off"
       message that re-displays the slot's stem + options. Prevents
       the LLM from orphaning the in-flight question with a fresh
       warmup pose. The slot itself is preserved so the next student
       answer routes through GRADE mode normally.

    2. **Fresh start** — no prior turns. Call ``start()`` which fires
       the warmup ``_OPENING_INSTRUCTION``.

    3. **Resume without in-flight slot** — there are prior turns but
       no slot (e.g. the last tutor turn was teaching, not posing, or
       the student finished and is now in remediation). Fall through
       to ``start()`` and let the engine decide via mode detection
       (POSE / TEACH / REMEDIATION).
    """
    from apps.curriculum.models import LessonStep
    from apps.tutoring.models import InFlightQuestion, SessionTurn

    in_flight = InFlightQuestion.objects.filter(session=session).first()
    has_prior_turns = SessionTurn.objects.filter(session=session).exists()

    if in_flight is not None and has_prior_turns:
        message = _build_resume_message(in_flight, _course_locale(session))
        step = _load_current_step(session)
        SessionTurn.objects.create(
            session=session,
            role=SessionTurn.Role.TUTOR,
            content=message,
            step=step,
        )
        logger.info(
            "[simple_tutor] resumed in-flight session=%s slot_id=%s type=%s",
            session.pk, in_flight.pk, in_flight.question_type,
        )
        return _project_start_payload(session, message)

    out = start(session)
    return _project_start_payload(session, out.get('content', ''))


def _build_resume_message(slot, locale: str = 'en-us') -> str:
    """Deterministic welcome-back text + re-display of the in-flight
    slot's question. Used by ``start_for_view`` on resume so we don't
    burn an LLM call (and risk orphaning the slot) just to render a
    question we already have. Localised to the course locale so a pt-mz
    student doesn't get an English banner above a Portuguese question.
    """
    from apps.tutoring.models import InFlightQuestion
    from django.utils import translation

    stem = (slot.question_text or '').strip()
    with translation.override(locale):
        welcome = translation.gettext(
            "👋 Welcome back! You were working on this question — "
            "let's pick up where we left off:"
        )
    parts = [
        welcome,
        '',
        stem,
    ]
    if slot.question_type == InFlightQuestion.QuestionType.MCQ and slot.options:
        for letter, opt in zip(('A', 'B', 'C', 'D'), slot.options):
            parts.append(f"{letter}) {opt}")
    return "\n".join(p for p in parts if p is not None).strip()


def _project_start_payload(session, message: str) -> dict:
    """Shared payload-shaping for ``start_for_view`` — keeps both the
    resume and fresh-start branches returning the exact same JSON
    shape ``chat_start_session`` expects.

    ``is_complete`` defaults to ``False`` whenever the chat is still
    interactive (in-flight question, exit ticket pending, OR
    remediation in progress). It's only True when the session is
    truly past everything (no lesson steps left AND no exit ticket
    attached OR the student already passed). Without this guard, the
    frontend showed the "Lesson Complete!" modal on every resume
    after a failed exit ticket — burying the remediation chat.
    """
    from apps.curriculum.models import LessonStep
    from apps.tutoring.models import (
        ExitTicket, ExitTicketAttempt, InFlightQuestion,
    )

    session.refresh_from_db()
    current_idx = session.current_step_index or 0
    step = (
        LessonStep.objects
        .filter(lesson=session.lesson, order_index=current_idx)
        .first()
    )
    total_steps = LessonStep.objects.filter(lesson=session.lesson).count()

    steps_exhausted = step is None or current_idx >= total_steps
    has_in_flight = InFlightQuestion.objects.filter(session=session).exists()
    last_attempt = (
        ExitTicketAttempt.objects
        .filter(session=session, completed_at__isnull=False)
        .order_by('-completed_at')
        .first()
    )
    has_passed_attempt = bool(last_attempt and last_attempt.passed)
    has_failed_attempt = bool(last_attempt and not last_attempt.passed)
    et_attached = ExitTicket.objects.filter(lesson=session.lesson).exists()

    # Phase + step display — frontend hides the step counter when phase
    # is exit_ticket / remediation / completed (templates/.../chat_tutor.html
    # ~line 1470). Pick the phase that matches the lesson state, and
    # clamp step_number so we never render "Evaluate 6/5" (the bug:
    # current_idx incremented past total_steps when the engine advanced
    # past the last lesson step).
    if has_failed_attempt:
        phase = 'remediation'
    elif has_passed_attempt:
        phase = 'completed'
    elif steps_exhausted and et_attached:
        phase = 'exit_ticket'
    elif step is not None:
        phase = (getattr(step, 'phase', '') or '').lower() or 'evaluate'
    else:
        phase = 'evaluate'
    step_number = min(current_idx + 1, total_steps) if total_steps else 1

    if not steps_exhausted:
        # Still in lesson steps — definitely not complete.
        is_complete = False
    elif has_in_flight:
        # An in-flight slot means there's an unanswered question — the
        # student should answer it before anything else. Not complete.
        is_complete = False
    elif has_failed_attempt:
        # Failed exit ticket → remediation chat is active. Not complete.
        is_complete = False
    elif has_passed_attempt:
        # Passed exit ticket — frontend showCompletion is fine.
        is_complete = True
    elif et_attached:
        # No attempt yet but an exit ticket is attached — student
        # still has to take the ticket.
        is_complete = False
    else:
        # Steps done, no exit ticket attached, no attempt → end here.
        is_complete = True

    return {
        'message': message,
        'phase': phase,
        'media': [],
        'show_exit_ticket': False,
        'exit_ticket': None,
        'is_complete': is_complete,
        'step_number': step_number,
        'total_steps': total_steps,
        'is_correct': None,
        'streak_count': None,
        'practice_score': None,
        'milestone': None,
        'artifact_html': None,
        'probe': None,
        'pending_question': None,
        'follow_up_message': None,
    }


def _persist_tutor_turn(session, text_reply: str, step, tool_results: list):
    """Create the tutor's SessionTurn row. If any tool call recorded
    a grader verdict (record_answer or auto_grade_fallback), embed it
    in ``judge_outputs['grader']`` so the dashboard + analytics +
    pick_current_question can read it next turn.
    """
    from apps.tutoring.models import SessionTurn

    judge_outputs: dict = {}
    # Strip non-serialisable fields before persisting. ``_block`` carries
    # the Anthropic ContentBlock object so Call 2 can pair tool_result
    # to tool_use by identity — it must not land in the DB JSON.
    persistable = [
        {k: v for k, v in entry.items() if k != '_block'}
        for entry in tool_results
    ]
    metadata: dict = {'tool_calls': persistable}

    # Surface the most recent grader verdict on the tutor turn's
    # judge_outputs['grader']. With the M11.3 tear-down the LLM provides
    # the reference + question text per tool call — those are preserved
    # for audit. There's no longer a question_id linking to a catalog
    # row (the LLM may have authored its own question).
    for entry in tool_results:
        tool = entry.get('tool')
        result = entry.get('result') or {}
        if tool == 'record_answer' and result.get('recorded'):
            judge_outputs['grader'] = {
                'verdict': result.get('verdict'),
                'confidence': result.get('confidence'),
                'tier': result.get('tier'),
                'per_criterion_scores': result.get('per_criterion_scores') or {},
                'justification': result.get('justification') or '',
                'needs_followup': result.get('needs_followup', False),
                'question_type': result.get('question_type'),
                'reference_answer': result.get('reference_answer'),
                'question_text': result.get('question_text'),
            }
            break

    SessionTurn.objects.create(
        session=session,
        role=SessionTurn.Role.TUTOR,
        content=text_reply or '',
        step=step,
        metadata=metadata,
        judge_outputs=judge_outputs,
    )
