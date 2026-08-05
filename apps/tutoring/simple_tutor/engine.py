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
import time
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


def respond(
    session: 'TutorSession', user_input: str, *, _is_opening: bool = False,
    on_delta=None,
) -> dict[str, Any]:
    """Process one student turn and return the tutor's response.

    Args:
        session: TutorSession (with engine='simple').
        user_input: the student's latest message text.
        on_delta: optional ``callable(str)`` receiving progressively longer
            SAFE snapshots of the reply as Call 2 generates it. Advisory
            preview only — the full batch filter pipeline still runs on the
            complete text below, and that is what persists and what the
            returned ``content`` carries. When None (the default, and all of
            production) this function is byte-identical to before.

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
        if not _spec:
            # No sweep override: fall back to the model the DB actually selected,
            # so an admin picking Qwen from the browser gets the Qwen prompt
            # variant. Without this the family is None and a local Qwen runs on
            # the base XML template meant for Anthropic.
            from apps.llm.models import ModelConfig as _MC
            _cfg = _MC.get_for('tutoring')
            if _cfg is not None and _cfg.provider and _cfg.model_name:
                _spec = f'{_cfg.provider}/{_cfg.model_name}'
        if _spec:
            _prof = get_model_profile(_spec)
            if _prof is not None:
                _family = _prof.family
    except Exception:
        _family = None

    # ─── 3b. Server-side pre-grading (qwen_mt30 board, 2026-08-03) ───
    # For weak tool-callers the grade cannot depend on the model calling
    # record_answer: qwen3-4b-Instruct-2507 skipped the expected tool on
    # ~80% of Call 1s, FLAT from the very first turn — so grades landed
    # late (Call-2 repair) or never, the anti-repeat/reveal/polarity nets
    # all ran on a desynced slot, and every reply was written before the
    # verdict existed. The grader never needed the model (it already
    # grades raw messages in _auto_grade_fallback), so on strict answer
    # intents the server grades FIRST and the model narrates a verdict it
    # is handed in <last_grade>. Eval families only; production/Anthropic
    # keep the model-driven flow byte-identically.
    pre_grade = None
    if (_family and _family not in _FORCE_POSE_EXEMPT_FAMILIES
            and not _is_opening
            and in_flight is not None
            and student_intent == 'answer'):
        pre_grade = _pre_grade_answer(session, user_input)
        if pre_grade is not None:
            # The grade mutated the slot (cleared on correct; attempt
            # bumped on wrong) — refresh everything derived from it.
            in_flight = InFlightQuestion.objects.filter(session=session).first()
            question_pool = build_question_pool(session)
            if exit_ticket_review is not None:
                mode = 'REMEDIATION+GRADE' if in_flight else 'REMEDIATION'
            else:
                mode = 'GRADE' if in_flight else 'POSE'

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
        pre_grade=pre_grade,
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
    # A pre-graded turn needs no grade tool: the verdict already exists. On a
    # pre-graded CORRECT the slot is gone, mode is POSE, and want_pose above
    # forces the next question; on a pre-graded WRONG the reply is a hint on
    # the same open question and no tool is expected at all.
    want_grade = (pre_grade is None and not want_pose
                  and _should_force_grade(_family, mode, student_intent))
    gate_open = _adaptive_force_now(session, want_pose or want_grade)
    force_pose = want_pose and gate_open
    force_grade = want_grade and gate_open
    call1_tools, call1_tool_choice = _plan_call1(tools, force_pose, force_grade)

    # Expose this turn's student intent to the pose handler's anti-desync guard:
    # it blocks a premature pose only when the student ATTEMPTED an answer (the
    # model should grade it, not pose over it). When the student DECLINED ("idk" →
    # non_engagement) the tutor must be free to pivot to an easier question — else
    # it gets trapped re-posing into a dead slot (the cycle-3 anti-desync deadlock).
    _es_intent = getattr(session, 'engine_state', None)
    if isinstance(_es_intent, dict):
        _es_intent['_student_intent'] = student_intent
        # Per-turn flags — set by handle_record_answer on an incorrect grade
        # and by _pre_grade_answer, read by handle_pose_question's same-turn
        # premature-pose guard and handle_record_answer's double-grade guard.
        # Cleared here so stale values from the previous turn can never block
        # a legitimate pose or grade.
        _es_intent.pop('_graded_incorrect_this_turn', None)
        _es_intent.pop('_pre_graded_this_turn', None)

    # Nothing is appended to the student's message here, and a per-turn tool
    # directive is specifically the wrong fix for Call-1 tool skipping — it
    # drove the local model into a duplicate tool-call loop that ran to
    # num_predict every turn (median wall 17 s -> 164 s) while *lowering*
    # compliance. Deleted 2026-07-29; measurements in
    # memory/tool_compliance_root_cause.md. Ollama also ignores tool_choice
    # outright, so _plan_call1's forcing is inert on every local model.
    # Call-1 compliance remains open; the live leads are `presence_penalty`
    # (repetition control is off on this tag) and shrinking the 24k system
    # prompt, not more instruction text.
    messages: list = [{'role': 'user', 'content': user_input}]

    # Resolve the student's offline/online preference ONCE per turn and pass
    # the same config to both calls. Resolving separately would let a
    # connection that drops between call 1 and call 2 switch models mid-turn,
    # so the tool_use blocks from one model would be answered by another.
    # Returns None on the hosted platform (only one tutor configured), which
    # leaves the existing behaviour untouched.
    from apps.tutoring.simple_tutor.model_choice import resolve_for_session
    turn_config = resolve_for_session(session)

    response = _call_llm(
        system_blocks=system_blocks, tools=call1_tools, messages=messages,
        tool_choice=call1_tool_choice, config=turn_config,
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

    # Surface the pre-grade as this turn's verdict: _turn_verdict, the
    # polarity/reveal filters, the stream gate, and Call 2's tool-result
    # formatting all read tool_results, and the pre-grade IS the turn's
    # record_answer — just made by the server before Call 1 instead of by
    # the model during it.
    if pre_grade is not None:
        tool_results.insert(0, {'tool': 'record_answer', 'result': pre_grade})

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
    # Same-turn pose after a correct verdict (see _should_pose_next_after_
    # correct): ride the Call-2 repair machinery so the next question is
    # registered in the same turn it is asked.
    if missing_tool is None and _should_pose_next_after_correct(
            _family, mode, tool_results, step):
        missing_tool = 'pose_question_next'

    # ─── 6. Call 2 — feed tool_results back so the model writes
    #              the student-facing reply WITH the verdict in hand,
    #              and register whatever Call 1 skipped.
    # Stream gate — built here because the grader verdict in `tool_results`
    # is now known, and the reveal/polarity filters need it. Only construct
    # one when a transport actually asked for deltas.
    stream_gate = None
    if on_delta is not None:
        from apps.tutoring.simple_tutor.stream_filter import StreamGate
        stream_gate = StreamGate(
            session=session, tool_results=tool_results,
            family=_family, emit=on_delta,
        )
        # Flush Call 1's prose NOW, if it wrote any.
        #
        # Measured on the Jetson 2026-07-29 (qwen3-4b-jetson, 8 turns): the
        # student-visible reply came from CALL 1 on 4 of 5 turns. The local
        # model writes the whole reply as prose and skips the tool; Call 2
        # then exists only to register the repair (missing_tool=record_answer)
        # and emits no text at all, so _run_second_call falls back to
        # `text_reply_1`. Streaming only Call 2 therefore covered 2/8 turns.
        #
        # This text has been sitting complete since before _dispatch_tools —
        # it could not be shown earlier because the reveal and polarity
        # filters need the grader's verdict, which only exists now. Emitting
        # it here cuts the wait on exactly the turns that previously streamed
        # nothing: the student reads the reply after Call 1 + grading instead
        # of after Call 2 finishes.
        #
        # If Call 2 does write prose, _call_llm's begin_attempt() resets the
        # gate's buffer first, so Call 2's snapshots replace this text rather
        # than concatenating onto it (transports treat a non-continuation as
        # a replace — see cli/render.py::stream_delta).
        # ...but ONLY when the verdict-dependent filters can actually run.
        #
        # Measured on the Jetson 2026-07-29, and the reason this guard exists:
        # the student typed a correct answer, Call 1 wrote "Yes — 360° is the
        # total...", and Call 1 SKIPPED record_answer. The grade was recorded
        # by Call 2's repair, so at this point there is no verdict,
        # _align_reply_polarity has nothing to act on, and the affirmation
        # streamed. Call 2 then graded it incorrect (the slot wanted the MCQ
        # letter) and the batch pass rewrote the opener — the student watched
        # "Yes" turn into "Not this time." That flip is precisely what the
        # head rule exists to prevent, so a flush is only safe when:
        #
        #   * a verdict is already recorded — every filter has its inputs; or
        #   * no question was in flight when the turn began — no grade was
        #     expected, so _missing_forced_tool cannot ask Call 2 to record
        #     one, and both verdict-dependent filters are no-ops.
        #
        # Otherwise a grade is pending and Call 1's prose has to wait.
        verdict_known = _turn_verdict(tool_results) is not None
        if text_reply_1 and (verdict_known or in_flight is None):
            stream_gate.feed(text_reply_1)
            # Explicit handover: whatever Call 2 writes REPLACES this text,
            # it does not continue it. _call_llm also resets the gate before
            # each attempt, but relying on that would make correctness here
            # depend on a retry mechanism firing as a side effect.
            stream_gate.reset_buffer()

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
        # Same config as call 1 — see the comment at the turn_config assignment.
        config=turn_config,
        on_delta=stream_gate,
        family=_family,
    )

    # B2: latch a Call-1 skip (whether or not Call 2 repaired it). The skip
    # itself is the compliance signal — a model that needed the repair once is
    # pre-forced from here on, so it does not narrate a question into a dead slot
    # again. Compliant models never reach here and keep running free.
    # B3: an empty-slot grade is evidence of a missed pose on a prior turn, so it
    # feeds the same gate — force pose from here on to stop the recurrence.
    if missing_tool or _graded_empty_slot(tool_results):
        _record_forcing_miss(session)

    # Grade-side net: the student answered, a slot exists, and the model
    # declined record_answer through both calls (see _auto_grade_fallback).
    _auto_grade_fallback(
        session=session, family=_family, student_intent=student_intent,
        user_input=user_input, tool_results=tool_results,
    )

    # Deterministic backstop for leaked platform vocabulary ("POSE/TEACH
    # mode", "in flight") — see _scrub_engine_vocab.
    text_reply = _scrub_engine_vocab(text_reply or '')

    # OSS-sweep nets (eval families only): the reply's opening must agree
    # with the grader's verdict, and an open question's reference must not
    # leak into a wrong-answer hint.
    if _family and _family not in _FORCE_POSE_EXEMPT_FAMILIES:
        # Reuse the index the gate already consumed, so the ack the student
        # watched appear is the ack that gets persisted. None when nothing
        # streamed, which is the normal path and calls _rotation_index as
        # it always has.
        text_reply = _align_reply_polarity(
            session, text_reply, tool_results,
            rotation_index=(
                stream_gate.used_rotation_index if stream_gate else None
            ),
        )
        text_reply = _rotate_repeated_ack(session, text_reply, tool_results)
        text_reply = _filter_reveals(session, text_reply, tool_results)

    if not text_reply:
        # Last-resort: neither call produced usable text. Give a neutral
        # placeholder so the bubble isn't blank; it renders the in-flight
        # question when one exists.
        text_reply = _empty_reply_placeholder(tool_results, session)

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

    # Deterministic net: a correct verdict must never end the turn with no
    # question in flight (see _auto_pose_fallback).
    text_reply = _auto_pose_fallback(
        session=session, step=step, family=_family,
        tool_results=tool_results, text_reply=text_reply,
    )

    # Hard pivot: no single stuck question may consume a session (GB1).
    text_reply = _force_pivot_stuck_slot(
        session=session, step=step, family=_family,
        tool_results=tool_results, text_reply=text_reply,
    )

    # Last: never persist a reply identical to the previous tutor turn.
    text_reply = _dedupe_reply(session, text_reply)

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
        # "already recorded" means a verdict actually EXISTS this turn — a
        # record_answer call that recorded nothing (empty extracted_answer)
        # does not count, or a bare "a" the model waved off as a non-answer
        # would never reach this net (kiosk session 74).
        autograded = autograde_bare_answer_if_clear(
            session,
            student_answer=user_input,
            student_intent=student_intent,
            already_recorded=_turn_verdict(tool_results) is not None,
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


# Appended to every instruction-style tool result. Weaker models echo these
# blocks verbatim into student-facing replies ("we're in POSE/TEACH mode",
# "isn't currently in flight" — seen in the 2026-07-18 multi-turn sweep), so
# every block now states its private status and what the visible reply should
# talk about instead. _scrub_engine_vocab is the deterministic backstop.
_PRIVATE_NOTE = (
    "[Private platform notes — for you only. Write your reply in plain "
    "teaching language about the lesson and the student's answer — the "
    "words 'slot', 'mode', 'in flight', tool names, and grading mechanics "
    "belong to the platform, not the conversation.]"
)


def _format_tool_result_for_call2(tool_name: str, result: dict) -> str:
    """Render a tool result as an instruction-laden block for Call 2.

    For record_answer specifically: surface the question_text + verdict
    + reference + student answer prominently, and remind the LLM that
    its next reply must be ABOUT THIS QUESTION (not an older one in
    recent_turns).
    """
    if tool_name == 'pose_question' and result.get('repeat_of_correct'):
        return "\n".join([
            "QUESTION NOT POSED — the student already answered this exact "
            "question correctly earlier in the session, so asking it again "
            "wastes their time.",
            "Pose a different question from <question_pool> (or a fresh one "
            "you author) with pose_question, or continue teaching the next "
            "piece of content.",
            _PRIVATE_NOTE,
        ])

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
        parts.append(_PRIVATE_NOTE)
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
                "scaffolding, never reveal the reference). A hint sets "
                "up the next step the student should take; it performs "
                "no intermediate or final computation and never states "
                "the reference value or its option letter. If correct, "
                "briefly acknowledge and either continue teaching or "
                "call pose_question with the next question. Do NOT "
                "reference older questions from <recent_turns>."
            ),
        ]
        if just:
            parts.insert(4, f"Grader justification: {just}")
        parts.append(_PRIVATE_NOTE)
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
            "then re-anchor them to the question, which remains unanswered.\n"
            + _PRIVATE_NOTE
        )

    if (tool_name == 'record_answer'
            and not result.get('recorded')
            and result.get('error', '').startswith('no in-flight')):
        # 2026-07-18 sweep: the old wording ("treat their message as a
        # clarification") made models dismiss a correct answer to the
        # question they themselves wrote as prose one turn earlier —
        # students were told their answer didn't count. Engage with the
        # answer instead; the pose that follows restores a gradable slot.
        return (
            "NOTHING WAS GRADED — the platform has no registered question. "
            "The student's message is most likely an answer to the question "
            "you wrote in your previous turn (shown in <recent_turns>). "
            "Read that question: if their message answers it, tell them "
            "whether they got it right and why — you wrote the question, so "
            "judge it yourself. Then call pose_question to register the "
            "next question. If their message wasn't an answer, respond to "
            "what they actually said.\n"
            + _PRIVATE_NOTE
        )

    # Other tools / non-success results — JSON is fine.
    import json
    return json.dumps(result, default=str)


def _empty_reply_placeholder(tool_results: list, session) -> str:
    """When both LLM calls produce no text (very rare), surface a
    minimal acknowledgement so the chat bubble isn't blank.

    If we have a grader verdict, briefly reflect it. When a question is
    in flight, render it — the 2026-07-18 sweep showed the old
    "Here's the next one:" placeholder with no question attached costs
    two turns (student replies "ok im ready" to a promise of nothing).
    """
    verdict = None
    for tr in tool_results:
        if tr.get('tool') in ('record_answer', 'auto_grade_fallback'):
            r = tr.get('result') or {}
            if r.get('recorded'):
                verdict = r.get('verdict')
                break
    # Rotate phrasing deterministically on session length — cycle-7 judges
    # flagged the fixed "Got it — that's right. Here's the next one:"
    # repeated a dozen times per session as robotic/templated.
    from apps.tutoring.models import InFlightQuestion
    idx = _rotation_index(session, 'ack')
    if verdict == 'correct':
        base = _ACKS_CORRECT[idx % len(_ACKS_CORRECT)]
        lead = _LEADS_NEXT[idx % len(_LEADS_NEXT)]
    elif verdict == 'incorrect':
        base = _ACKS_INCORRECT[idx % len(_ACKS_INCORRECT)]
        lead = "Here's the question again:"
    else:
        base = _ACKS_NEUTRAL[idx % len(_ACKS_NEUTRAL)]
        lead = "Here's the question:"

    slot = InFlightQuestion.objects.filter(session=session).first()
    if slot is not None and (slot.question_text or '').strip():
        return f"{base} {lead}\n\n{_render_slot_question(slot)}"
    return base


_ACKS_CORRECT = (
    "Got it — that's right.",
    "Correct — nice work.",
    "Yes, exactly right.",
    "That's it — well done.",
)
_ACKS_INCORRECT = (
    "Not quite — let's walk through it together.",
    "Not this time — have another look.",
    "Close, but not quite — take it one step at a time.",
)
_LEADS_NEXT = (
    "Here's the next one:",
    "Try this one:",
    "Next up:",
    "Here's another:",
)
_ACKS_NEUTRAL = (
    "Let's keep going.",
    "Let's pick up where we left off.",
    "Right — let's carry on, one step at a time.",
)


_RETRY_FRAMES = (
    "Same question, fresh angle:",
    "Let's take another run at it:",
    "One more look at this one:",
)


def _render_slot_question(slot) -> str:
    """Render the in-flight slot as the student-visible question block:
    the stem, plus lettered options for MCQ (unless the stem already
    carries them). From the second attempt on, a rotated retry framing
    is prefixed so legal hint-ladder re-renders don't read as verbatim
    repetition (gemma v3: "recycled the compass question three times")."""
    stem = (slot.question_text or '').strip()
    lines = [stem]
    attempts = int(getattr(slot, 'attempt_count', 0) or 0)
    if attempts >= 2:
        lines = [f"{_RETRY_FRAMES[attempts % len(_RETRY_FRAMES)]} {stem}"]
    opt_lines = _render_slot_options(slot)
    if opt_lines and not _contains_lettered_options(stem):
        lines.extend(opt_lines)
    return "\n".join(lines)


def _render_slot_options(slot) -> list[str]:
    """Lettered option lines for an MCQ slot, [] otherwise. Strips any
    letter prefix already baked into the option text so options never
    render double-lettered ("A) A) 11" — cycle-7 sweeps)."""
    if (getattr(slot, 'question_type', '') or '') != 'mcq':
        return []
    from apps.tutoring.simple_tutor.grader import _OPT_PREFIX_RE
    options = getattr(slot, 'options', None) or []
    return [
        f"{letter}) {_OPT_PREFIX_RE.sub('', str(opt).strip())}"
        for letter, opt in zip(('A', 'B', 'C', 'D'), options)
    ]


# Lead-in phrases that mark the start of a posed question in prose.
# Used only to locate a *divergent* trailing question for removal —
# scoped to the last few paragraphs of a reply on turns where
# pose_question fired but the visible text doesn't contain the slot stem.
_QUESTION_LEADIN_RE = re.compile(
    r"(?i)\b(now try|try this|here'?s (?:the |your |one )?(?:next|last|first)|"
    r"next (?:question|one)|what'?s your answer|your turn|let'?s try|"
    r"answer this|one more)\b"
)

# How many trailing paragraphs may be treated as the prose question.
_PROSE_QUESTION_WINDOW = 4


def _looks_like_question_para(p: str) -> bool:
    return (
        '?' in p
        or _contains_lettered_options(p)
        or bool(_QUESTION_LEADIN_RE.search(p))
    )


def _strip_trailing_prose_question(text: str) -> str:
    """Remove the question the LLM wrote at the end of ``text``.

    Called only when the visible text is known to pose a question that
    DIVERGES from the graded slot (the 2026-07-18 sweep's dominant
    failure: student answers the question they read, gets graded
    against a different one). Scans the last _PROSE_QUESTION_WINDOW
    paragraphs for the earliest question-looking paragraph and cuts
    from there. When no question-looking paragraph is found the text
    is returned unchanged (caller appends the slot question after it).
    """
    paras = text.split('\n\n')
    start = max(0, len(paras) - _PROSE_QUESTION_WINDOW)
    for i in range(start, len(paras)):
        if _looks_like_question_para(paras[i]):
            return '\n\n'.join(paras[:i]).rstrip()
    return text


def _ensure_posed_question_in_text(
    text_reply: str, tool_results: list, session,
) -> str:
    """The slot is the single source of truth for the visible question.

    When pose_question fired this turn, the student must see exactly
    the question the platform will grade — the chat UI renders only
    the chat thread, not the slot. Two failure modes are repaired:

    - The LLM's reply omits the stem → append it (original behaviour,
      caught in M12.8 E2E).
    - The LLM's reply poses a DIFFERENT question than it registered →
      strip the divergent prose question, then append the slot's. The
      2026-07-18 multi-turn sweep showed this desync driving ignored
      correct answers, 21-30-turn sessions, and both dominant failed
      rubric items across three model families.

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
        # Stem is visible. For MCQ, the options must be too — cycle-7
        # kimi spent 8 turns asking for "the letter" of options the
        # student had never seen.
        opt_lines = _render_slot_options(slot)
        if opt_lines and not _contains_lettered_options(text_reply):
            return "\n".join([text_reply.rstrip(), ''] + opt_lines)
        return text_reply

    kept = _strip_trailing_prose_question(text_reply.rstrip())
    logger.info(
        "[simple_tutor] rendering slot question session=%s slot_id=%s "
        "stripped_divergent_prose=%s",
        session.pk, slot.pk, len(kept) != len(text_reply.rstrip()),
    )

    parts = [kept, '', _render_slot_question(slot)] if kept else \
        [_render_slot_question(slot)]
    return "\n".join(parts).strip()


# ── Engine-vocabulary scrub ──────────────────────────────────────────────────
# The 2026-07-18 sweep showed mid-tier models echoing platform vocabulary
# into student-facing replies ("we're in POSE/TEACH mode", "isn't currently
# in flight", "(Keep the in-flight question live …)"). The prompt-side fix is
# _PRIVATE_NOTE; this is the deterministic backstop: drop any sentence,
# line, or parenthetical that names platform internals. Mirrors the media-
# signal strip: sanitize before persisting, never rely on the model.
_VOCAB_CI_RE = re.compile(
    r"(?i)\bin[-\s]?flight\b|\bpose_question\b|\brecord_answer\b|"
    r"pose\s*/\s*teach|\b(?:pose|grade|teach)\s+mode\b|\bquestion slot\b|"
    r"\btool[- ]calls?\b|\breference\s+(?:answer|value)s?\b|\bthe graders?\b"
)
_VOCAB_CS_RE = re.compile(r"\b(?:POSE|GRADE|TEACH)\b")
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')
_PAREN_RE = re.compile(r'\([^()]*\)')
# Narrated tool-call JSON: a key like "name": / "arguments": on a line, or a
# line of pure structural JSON punctuation. Kimi narrates tool calls as
# bracketed JSON blocks in its text; blocks naming advance_step etc. carry
# no vocab word, escaped the vocab pass, and left orphan '[' bubbles.
_TOOL_JSON_KEY_RE = re.compile(
    r'"(?:name|arguments|extracted_answer|question_text|question_type|'
    r'reference_answer|options|source)"\s*:'
)


# XML-convention tool markup (gemma_probe5: the okamototk template emits
# <tool_call>…</tool_call>, and fragments leaked into student text). The tag
# regex tolerates a missing closing '>' — the observed leaks were truncated.
_XML_TOOL_TAG_RE = re.compile(r'</?tool_(?:call|response)>?', re.I)
_FENCE_LINE_RE = re.compile(r'^\s*```[\w-]*\s*$')


def _is_tool_json_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _TOOL_JSON_KEY_RE.search(s):
        return True
    if _FENCE_LINE_RE.match(s):
        return True
    return bool(re.fullmatch(r'[\[\]{}",:\s]+', s))


def _has_engine_vocab(s: str) -> bool:
    return bool(_VOCAB_CI_RE.search(s) or _VOCAB_CS_RE.search(s))


def _scrub_engine_vocab(text: str) -> str:
    """Remove engine-internal vocabulary and narrated tool-JSON from a
    student-facing reply.

    Drops tool-JSON lines and offending parentheticals first, then
    offending sentences line-by-line. Returns '' when nothing survives
    (the caller falls back to the placeholder). Clean text passes
    through byte-identical.
    """
    if not text:
        return text
    if _XML_TOOL_TAG_RE.search(text):
        # Strip the tags inline (prose on the same line survives); the
        # call payload between them is caught by the vocab/JSON passes.
        text = _XML_TOOL_TAG_RE.sub('', text)
    if any(_is_tool_json_line(ln) for ln in text.split('\n')):
        text = '\n'.join(
            ln for ln in text.split('\n') if not _is_tool_json_line(ln))
        text = re.sub(r'\n{3,}', '\n\n', text).strip('\n')
    if not _has_engine_vocab(text):
        return text
    text = _PAREN_RE.sub(
        lambda m: '' if _has_engine_vocab(m.group(0)) else m.group(0), text,
    )
    out_lines = []
    for line in text.split('\n'):
        if not _has_engine_vocab(line):
            out_lines.append(line)
            continue
        kept = [s for s in _SENTENCE_SPLIT_RE.split(line)
                if not _has_engine_vocab(s)]
        out_lines.append(' '.join(kept).strip())
    # Drop punctuation-only residue. A narrated tool-call block like
    # "[\n{...pose_question...}\n]" loses its content lines to the vocab
    # filter but keeps the bare brackets — which then repeat as the whole
    # bubble and deadlock the session (kimi, cycle-7 sweep). A blank result
    # falls through to the slot-aware placeholder instead.
    out_lines = [ln for ln in out_lines
                 if not ln.strip() or re.search(r'[A-Za-z0-9]', ln)]
    result = '\n'.join(out_lines)
    if not re.search(r'[A-Za-z0-9]', result):
        result = ''
    result = re.sub(r'[ \t]+\n', '\n', result)
    result = re.sub(r'\n{3,}', '\n\n', result).strip('\n')
    if result != text:
        logger.info(
            "[simple_tutor] scrubbed engine vocabulary from reply "
            "(%d -> %d chars)", len(text), len(result),
        )
    return result


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


def _turn_verdict(tool_results) -> str | None:
    """The grading verdict recorded this turn, if any."""
    for tr in tool_results or []:
        if tr.get('tool') in ('record_answer', 'auto_grade_fallback'):
            r = tr.get('result') or {}
            if r.get('recorded'):
                return r.get('verdict')
    return None


# ── OSS-sweep nets (2026-07-22 oss13_mt analysis) ────────────────────────────

def _rotation_index(session, key: str) -> int:
    """Dedicated persisted counter for phrase rotations. The old
    SessionTurn-count index advanced by 2 per exchange, so 4-entry
    tuples cycled only half their variants and 'One more time, from a
    different angle' repeated verbatim (gemma20_mt, phrase-window
    assert failures)."""
    es = getattr(session, 'engine_state', None)
    if not isinstance(es, dict):
        return 0
    rot = es.get('_rot')
    if not isinstance(rot, dict):
        rot = {}
    idx = int(rot.get(key, -1)) + 1
    rot[key] = idx
    es['_rot'] = rot
    session.engine_state = es
    try:
        session.save(update_fields=['engine_state'])
    except Exception:
        pass
    return idx


_VARIATION_LINES = (
    "Let me put it another way.",
    "One more time, from a different angle.",
    "Let's take this a step at a time.",
    "Here it is again — take your time.",
)


def _dedupe_reply(session, text_reply: str) -> str:
    """Never persist a tutor reply identical (normalised) to the previous
    tutor turn — qwen3:14b lost 6 sessions to verbatim repeats, which is
    the student-sim's deadlock trigger and equally dead-endy for a real
    student. A rotated re-engagement line varies the reply."""
    if not text_reply:
        return text_reply
    from apps.tutoring.models import SessionTurn
    prev = (
        SessionTurn.objects
        .filter(session=session, role=SessionTurn.Role.TUTOR)
        .order_by('-created_at', '-pk')
        .values_list('content', flat=True)
        .first()
    )
    if not prev:
        return text_reply

    def _norm(s: str) -> str:
        return ' '.join((s or '').lower().split()).rstrip('.!?')

    if _norm(prev) != _norm(text_reply):
        return text_reply
    line = _VARIATION_LINES[
        _rotation_index(session, 'dedupe') % len(_VARIATION_LINES)]
    logger.info(
        "[simple_tutor] dedupe_reply: varied a verbatim repeat session=%s",
        session.pk,
    )
    return f"{line}\n\n{text_reply}"


# Mid-reply verdict contradictions (gemma_probe5_v2): strong assertions of
# the WRONG polarity anywhere in the reply — "That's right – 50 is correct!"
# on an incorrect verdict; "you selected A instead of B" on a correct one.
_MID_AFFIRM_RE = re.compile(
    r"(?i)\b(?:that[’']?s right\b|(?:is|was|are) correct[.!]|"
    r"exactly right\b|you[’']?re (?:right|correct)\b|spot[- ]on\b)")
_MID_DENY_RE = re.compile(
    r"(?i)\b(?:you (?:picked|selected|chose)\s+\S{1,16}\s+instead of\b|"
    r"that[’']?s (?:not right|wrong|incorrect)\b|"
    r"not quite\b|not this time\b|"
    r"(?:isn[’']?t|is not|wasn[’']?t) (?:right|correct)\b)")

_NEG_OPENER_RE = re.compile(
    r"^(?:not quite|that'?s not|not this time|incorrect|close, but|almost\b|"
    r"sorry|hmm, not|no[,—: ])", re.I)
_POS_OPENER_RE = re.compile(
    r"^(?:exactly|correct|that'?s (?:it|right)|great|well done|got it|"
    r"perfect|spot on|right\b|nice\b|brilliant|bang on|good "
    r"(?:job|work|thinking)|you(?:'ve| have)? got it|yes[,!— ])", re.I)
_FIRST_SENTENCE_END_RE = re.compile(r'[.!?](?:\s+|$)|\n')


def _align_reply_polarity(
    session, text_reply: str, tool_results, *, rotation_index: int | None = None,
) -> str:
    """The reply's opening must agree with the grader's verdict.

    The oss13_mt sweep's top rubric killer: models opened with "Not
    quite" on graded-CORRECT answers and "Exactly!" on graded-WRONG ones,
    then argued with their own verdicts for whole sessions. The verdict
    is authoritative; when the opening contradicts it, the opening
    sentence is replaced with a rotated verdict-consistent
    acknowledgement.

    ``rotation_index`` exists for the streaming path. _rotation_index()
    INCREMENTS a persisted counter and saves the session on every call,
    so re-running this per streamed chunk would rotate the ack between
    snapshots — the student would watch "Exactly!" turn into "Nice work!"
    — and issue a DB write per chunk. The stream gate resolves the index
    once and passes it here and to the final batch pass, so both agree.
    None (the default, and every non-streaming call) preserves today's
    behaviour exactly."""
    verdict = _turn_verdict(tool_results)
    if verdict not in ('correct', 'incorrect') or not text_reply:
        return text_reply
    out = text_reply
    stripped = out.lstrip()
    if verdict == 'correct' and _NEG_OPENER_RE.match(stripped):
        acks = _ACKS_CORRECT
    elif verdict == 'incorrect' and _POS_OPENER_RE.match(stripped):
        acks = _ACKS_INCORRECT
    else:
        acks = None
    if acks is not None:
        m = _FIRST_SENTENCE_END_RE.search(stripped)
        rest = stripped[m.end():].lstrip() if m else ''
        idx = (
            rotation_index if rotation_index is not None
            else _rotation_index(session, 'polarity')
        )
        ack = acks[idx % len(acks)]
        logger.info(
            "[simple_tutor] polarity_align: %s opener replaced on %s "
            "verdict session=%s",
            'negative' if verdict == 'correct' else 'positive', verdict,
            session.pk,
        )
        out = f"{ack} {rest}".strip()

    # Sentence-level pass: contradictions that sit mid-reply.
    pat = _MID_AFFIRM_RE if verdict == 'incorrect' else _MID_DENY_RE
    if pat.search(out):
        lines = []
        for line in out.split('\n'):
            if not pat.search(line):
                lines.append(line)
                continue
            kept = [s for s in _SENTENCE_SPLIT_RE.split(line)
                    if not pat.search(s)]
            lines.append(' '.join(kept).strip())
        out = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip('\n')
        logger.info(
            "[simple_tutor] polarity_align: mid-reply %s dropped on %s "
            "verdict session=%s",
            'affirmation' if verdict == 'incorrect' else 'denial', verdict,
            session.pk,
        )

    # Ack-prepend (mt50 qwen3-4b): 62 turns acknowledged nothing — the reply
    # opened straight onto the NEXT question ("A bearing is always measured
    # clockwise from which direction?") with the just-graded answer passed
    # over in silence, which the judges scored as "ignored what the student
    # said". When a verdict exists but the reply's head carries neither
    # polarity, prepend a rotated verdict-consistent acknowledgement. The
    # mid-pattern check on the head keeps this from stacking a second ack
    # onto phrasings the opener regexes don't know ("You're right — ...").
    stripped = out.lstrip()
    if stripped and not _POS_OPENER_RE.match(stripped) \
            and not _NEG_OPENER_RE.match(stripped):
        head = stripped[:140]
        prepend = None
        if verdict == 'correct' and not _MID_AFFIRM_RE.search(head):
            prepend = _ACKS_CORRECT
        elif verdict == 'incorrect' and not _MID_DENY_RE.search(head):
            prepend = _ACKS_INCORRECT_SHORT
        if prepend is not None:
            idx = (
                rotation_index if rotation_index is not None
                else _rotation_index(session, 'polarity')
            )
            out = f"{prepend[idx % len(prepend)]} {stripped}"
            logger.info(
                "[simple_tutor] polarity_align: prepended missing %s ack "
                "session=%s", verdict, session.pk,
            )
    return out


# Prepend-only incorrect acks: unlike _ACKS_INCORRECT these carry no guidance
# clause, because the model's own hint follows immediately after.
_ACKS_INCORRECT_SHORT = (
    "Not quite.",
    "Not this time.",
    "Close — not quite.",
)

# Leading positive-ack lexeme + trailing punctuation, for the repeated-opener
# rotation below. Matches only the ack PHRASE, not the sentence.
_ACK_LEXEME_RE = re.compile(
    r"^(exactly|correct|that'?s (?:it|right)|great(?:\s+(?:job|work))?|"
    r"nice(?:\s+work)?|got it|spot on|perfect|well done|brilliant|"
    r"you(?:'ve| have)? got it|yes)\b[\s,!.—:–-]*", re.I)

# Short openers the rotation swaps in. Each reads naturally in front of
# whatever followed the original ack ("Nice — 140°. Alternate interior…").
_ACK_OPENERS = (
    "Nice —", "Good —", "That's it —", "Well done —", "Spot on —",
    "You've got it —",
)


def _rotate_repeated_ack(session, text_reply: str, tool_results) -> str:
    """Break "Exactly — … / Exactly — …" opener chains on correct verdicts.

    mt50 qwen3-4b opened with the same "Exactly —" on up to 10 of 13 turns in
    a session; every judge flagged it as templated/robotic, and the prompt's
    "vary your affirmations" rule is read straight past by 4B models. When
    this turn's reply opens with the same ack lexeme as either of the last
    two persisted tutor turns, swap the leading phrase for a rotated opener.
    Deterministic, verdict-safe (correct verdicts only), and a no-op for
    every reply whose opener is already fresh."""
    if _turn_verdict(tool_results) != 'correct' or not text_reply:
        return text_reply
    stripped = text_reply.lstrip()
    m = _ACK_LEXEME_RE.match(stripped)
    if not m:
        return text_reply
    lexeme = m.group(1).lower()
    from apps.tutoring.models import SessionTurn
    prev_contents = list(
        SessionTurn.objects
        .filter(session=session, role=SessionTurn.Role.TUTOR)
        .order_by('-created_at', '-pk')
        .values_list('content', flat=True)[:2]
    )
    repeated = False
    for prev in prev_contents:
        pm = _ACK_LEXEME_RE.match((prev or '').lstrip())
        if pm and pm.group(1).lower() == lexeme:
            repeated = True
            break
    if not repeated:
        return text_reply
    idx = _rotation_index(session, 'ack_open')
    for i in range(len(_ACK_OPENERS)):
        opener = _ACK_OPENERS[(idx + i) % len(_ACK_OPENERS)]
        if not opener.lower().startswith(lexeme[:4]):
            break
    rest = stripped[m.end():].lstrip()
    logger.info(
        "[simple_tutor] ack_rotate: repeated opener %r varied session=%s",
        lexeme, session.pk,
    )
    return f"{opener} {rest}" if rest else opener.rstrip(' —')


_ANSWER_PAREN_RE = re.compile(r'\(\s*answer\s*:[^)]*\)', re.I)


def _filter_reveals(
    session, text_reply: str, tool_results, *, reference: str | None = None,
) -> str:
    """While a question is open after a wrong answer, the reply must not
    state the reference (qwen3:14b printed "(Answer: A)" verbatim; others
    said "option C is correct" mid-hint). The engine knows the reference,
    so this is deterministically enforceable.

    ``reference`` lets the streaming path supply the answer it already
    looked up, so this does not re-query InFlightQuestion once per
    streamed chunk. None (the default) keeps the query.

    Runs on incorrect/partial verdicts AND on no-verdict turns where a
    question is still in flight. The mt50 qwen3-4b reveals ("Let's
    calculate: 1 − 0.8 = 0.2", "180 − 113 = 67°") mostly landed on
    clarification/help turns — no record_answer, so no verdict, and the
    old incorrect-only gate let them straight through. A correct verdict
    still skips: the slot then holds the NEXT question, freshly posed,
    and its stem is appended after this filter runs."""
    verdict = _turn_verdict(tool_results)
    if verdict == 'correct' or not text_reply:
        return text_reply
    slot_options = None
    if reference is None:
        from apps.tutoring.models import InFlightQuestion
        slot = InFlightQuestion.objects.filter(session=session).first()
        if slot is None:
            return text_reply
        reference = slot.reference_answer or ''
        if (slot.question_type or '') == 'mcq':
            slot_options = slot.options or None
    ref = (reference or '').strip()
    if not ref:
        return text_reply
    out = _ANSWER_PAREN_RE.sub('', text_reply)
    is_letter = len(ref) == 1 and ref.upper() in 'ABCD'
    # Paraphrase net for MCQ reveals (kiosk session 74): "A small scale map
    # has a large ratio (like 1:1,000,000 or bigger), meaning it covers a
    # vast area but shows less detail" restates the correct option's content
    # without ever naming its letter, so the letter patterns below can't see
    # it. When a sentence covers ≥70% of the correct option's distinctive
    # tokens (prefix-matched, so shows≈showing), it has told the student
    # which option is right — drop it. Batch path only: the streaming gate
    # supplies `reference` without options, and the batch pass is what
    # persists.
    opt_tokens = None
    if is_letter and slot_options:
        try:
            opt_text = str(slot_options['ABCD'.index(ref.upper())])
        except (IndexError, ValueError):
            opt_text = ''
        toks = _distinctive_tokens(opt_text)
        if len(toks) >= 4:
            opt_tokens = toks

    def _paraphrases_option(sentence: str) -> bool:
        if not opt_tokens:
            return False
        sent = _distinctive_tokens(sentence)
        if not sent:
            return False
        sent_prefixes = {t[:4] for t in sent}
        covered = sum(1 for t in opt_tokens if t[:4] in sent_prefixes)
        return covered / len(opt_tokens) >= 0.7
    if is_letter:
        pat = re.compile(
            rf'(?i)\b(?:option|answer|letter)\s*(?:is\s*)?{ref}\b'
            rf'|\b{ref}\s*(?:is|was)\s*(?:the\s*)?(?:correct|right)')
    else:
        ref_esc = re.escape(ref)
        pat = re.compile(
            rf'(?i)(?:answer|correct|equals|=|\bis)\W{{0,15}}{ref_esc}\b'
            rf'|\b{ref_esc}\s*(?:is|was)\s*(?:the\s*)?'
            rf'(?:correct|right|answer)')
    lines_out = []
    for line in out.split('\n'):
        if not pat.search(line) and not (opt_tokens and _paraphrases_option(line)):
            lines_out.append(line)
            continue
        kept = [s for s in _SENTENCE_SPLIT_RE.split(line)
                if not pat.search(s) and not _paraphrases_option(s)]
        lines_out.append(' '.join(kept).strip())
    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines_out)).strip('\n')
    if result != text_reply:
        logger.info(
            "[simple_tutor] reveal_filter: redacted reference leak "
            "session=%s", session.pk,
        )
    return result


def _distinctive_tokens(s: str) -> set[str]:
    """Content-bearing tokens for the paraphrase net: numbers/ratios
    (``1:1,000,000``) plus words of 4+ letters, lowercased."""
    s = (s or '').lower()
    return set(re.findall(r'\d[\d,.:]*', s)) | set(re.findall(r'[a-z]{4,}', s))


def _pre_grade_answer(session, user_input: str) -> dict | None:
    """Grade the student's strict-shaped answer BEFORE Call 1.

    Returns the record_answer-shaped result dict (with ``pre_graded`` and the
    raw ``student_answer`` attached for the <last_grade> block), or None when
    nothing was recorded — the caller then proceeds on the model-driven flow.
    Sets the ``_pre_graded_this_turn`` engine_state flag so a model-issued
    record_answer later in the turn cannot double-grade (it would bump
    attempt_count a second time for one answer).

    Stale-slot guard (it2, tightened it3): during a hint ladder the tutor
    sometimes asks a micro-step in PROSE ("what is 360 − 175?") while the
    slot still holds the main question — the student's correct micro-answer
    must not be graded against the main question's reference (it2 graded a
    correct '185' against ref '175' twice this way). The it2 version skipped
    whenever the last tutor turn didn't restate the stem, which threw out
    the COMMON case — a plain hint ("check the subtraction") followed by the
    student re-answering the main question — with the rare bad one: 50
    skipped pre-grades on the it3 board, each falling back into the chaotic
    model-flow. Now the skip requires an actual competing question: the last
    tutor turn ends with a question-looking paragraph that does NOT match
    the slot's stem. No trailing question, or a trailing question that IS
    the slot → pre-grade."""
    from apps.tutoring.models import InFlightQuestion, SessionTurn
    from apps.tutoring.simple_tutor.tools import handle_record_answer
    slot = InFlightQuestion.objects.filter(session=session).first()
    if slot is not None and (slot.question_text or '').strip():
        last_tutor = (
            SessionTurn.objects
            .filter(session=session, role=SessionTurn.Role.TUTOR)
            .order_by('-created_at', '-pk')
            .values_list('content', flat=True)
            .first()
        ) or ''
        stripped = _strip_trailing_prose_question(last_tutor)
        trailing = last_tutor[len(stripped):].strip()
        needle = _norm_loose(slot.question_text)[:30]
        if trailing and needle and needle not in _norm_loose(trailing) \
                and _norm_loose(trailing)[:30] not in _norm_loose(slot.question_text):
            logger.info(
                "[simple_tutor] pre_grade skipped: last tutor turn asks a "
                "different question than the slot session=%s", session.pk,
            )
            return None
    try:
        result = handle_record_answer(
            session, extracted_answer=(user_input or '').strip()[:300])
    except Exception:
        logger.warning("[simple_tutor] pre_grade failed session=%s",
                       getattr(session, 'pk', None), exc_info=True)
        return None
    if not result.get('recorded'):
        return None
    result['pre_graded'] = True
    result['student_answer'] = (user_input or '').strip()[:200]
    es = getattr(session, 'engine_state', None)
    if isinstance(es, dict):
        es['_pre_graded_this_turn'] = True
        session.engine_state = es
    logger.info(
        "[simple_tutor] pre_graded session=%s verdict=%s tier=%s",
        session.pk, result.get('verdict'), result.get('tier'),
    )
    return result


def _auto_grade_fallback(
    *, session, family, student_intent, user_input, tool_results,
) -> None:
    """Grade-side net mirroring _auto_pose_fallback: the student clearly
    answered (intent='answer', strict), a slot exists, and the model
    declined record_answer through Call 1 AND the forced Call-2 repair
    (Ollama cannot honour tool_choice; qwen3.5:4b lost 12% of its answer
    turns this way). Grade the raw student message server-side.

    The pre-intent-classifier version of this fallback was removed in
    2026-05 for over-firing on conversational continuations; the strict
    intent gate is what makes it safe now. Eval families only."""
    if not family or family in _FORCE_POSE_EXEMPT_FAMILIES:
        return
    if student_intent != 'answer':
        return
    for tr in tool_results or []:
        if tr.get('tool') == 'record_answer' \
                and (tr.get('result') or {}).get('recorded'):
            return  # the model made a grading judgement — trust it
    # A record_answer call with an EMPTY extracted_answer is NOT a judgement
    # to trust here: intent='answer' means the message deterministically looks
    # like an answer (bare MCQ letter, bare number). Kiosk session 74
    # (2026-08-03): the model called record_answer('') on a bare "a" twice in
    # a row — "that was not an answer" about a message that plainly was — and
    # the old any-call-counts guard let the answer vanish; the tutor re-asked
    # the same question verbatim with zero feedback, twice.
    from apps.tutoring.models import InFlightQuestion
    if not InFlightQuestion.objects.filter(session=session).exists():
        return
    from apps.tutoring.simple_tutor.tools import handle_record_answer
    result = handle_record_answer(
        session, extracted_answer=(user_input or '').strip()[:300])
    if result.get('recorded'):
        tool_results.append({'tool': 'auto_grade_fallback', 'result': result})
        logger.info(
            "[simple_tutor] auto_grade_fallback: graded raw answer "
            "session=%s verdict=%s", session.pk, result.get('verdict'),
        )


def _auto_pose_fallback(
    *, session, step, family, tool_results, text_reply,
) -> str:
    """Deterministic net for dangling correct-verdict turns (cycle 9).

    When the verdict was CORRECT but the turn is ending with no in-flight
    question — the model declined both the forced Call-2 pose and the
    prose question — the student gets a dead acknowledgement bubble
    ("That's it — well done.") and burns a turn answering "yeah". Pose
    the next unused pool question server-side instead: catalog authority
    for options and correct letter, rendered into the reply.

    Eval-only gating mirrors the other repairs: production (family None)
    and Anthropic are untouched.
    """
    if not family or family in _FORCE_POSE_EXEMPT_FAMILIES:
        return text_reply
    if step is None:
        return text_reply
    verdict_correct = any(
        tr.get('tool') in ('record_answer', 'auto_grade_fallback')
        and (tr.get('result') or {}).get('recorded')
        and (tr.get('result') or {}).get('verdict') == 'correct'
        for tr in (tool_results or [])
    )
    # ANY rejected pose, not just repeat_of_correct. The optionless-MCQ
    # rejection (tools.py:504) leaves exactly the same dangling turn: small
    # models routinely write "A) … B) … C) …" into the prose while calling
    # pose_question without `options`, handle_pose_question refuses it because
    # a letter reference with no option list cannot be graded, and the turn ends
    # with no InFlightQuestion. The student then answers "B" against nothing —
    # so the letter never grades, the step never advances, and the question is
    # re-asked. Observed on qwen3-4b, geography lesson 1463, 2026-07-27:
    # unanswerable by letter, only full option text worked.
    #
    # The catalog fallback below is the right repair for it — it supplies
    # authoritative options AND the correct letter, which is precisely what the
    # rejected pose lacked.
    pose_rejected = any(
        tr.get('tool') == 'pose_question'
        and not (tr.get('result') or {}).get('posed')
        for tr in (tool_results or [])
    )
    if not (verdict_correct or pose_rejected):
        return text_reply
    from apps.tutoring.models import InFlightQuestion
    if InFlightQuestion.objects.filter(session=session).exists():
        return text_reply
    from apps.tutoring.simple_tutor.tools import (
        build_question_pool, handle_pose_question,
    )
    pool = build_question_pool(session, max_questions=2)
    for q in pool:
        opts = [
            str(getattr(q, f'option_{letter}', '') or '').strip()
            for letter in 'abcd'
        ]
        opts = [o for o in opts if o]
        result = handle_pose_question(
            session,
            question_text=getattr(q, 'question_text', '') or '',
            question_type=getattr(q, 'question_type', '') or 'short_answer',
            reference_answer=str(getattr(q, 'correct_answer', '') or ''),
            source='catalog',
            options=opts or None,
            engine_initiated=True,
        )
        if result.get('posed'):
            tool_results.append({'tool': 'auto_pose_fallback', 'result': result})
            slot = InFlightQuestion.objects.filter(session=session).first()
            logger.info(
                "[simple_tutor] auto_pose_fallback posed pool question "
                "session=%s type=%s", session.pk, result.get('question_type'),
            )
            rendered = _render_slot_question(slot) if slot else ''
            if not rendered:
                return text_reply
            # Strip the model's own prose question first. It diverges from the
            # catalog question now in the slot, and the slot is what grading
            # uses — leaving both asks the student two questions and grades only
            # one. That desync is exactly what _strip_trailing_prose_question
            # exists to repair (the 2026-07-18 sweep's dominant failure).
            base = _strip_trailing_prose_question(
                (text_reply or '').rstrip()
            ).rstrip()
            return f"{base}\n\n{rendered}" if base else rendered
    return text_reply


_PIVOT_ATTEMPTS = 4
# Tutor turns one slot may stay in flight before the engine pivots it, however
# few wrong ATTEMPTS it has collected. The attempt-count trigger alone never
# fires on the mt50 help-intensive burners (23-27 turns, budget 15-20):
# clarification/help turns don't increment attempt_count, so a student who
# asks for help every turn can idle one question forever while the session
# runs out of budget. Age counts tutor REPLIES while the same slot is live.
_PIVOT_SLOT_AGE = 6
_PIVOT_BRIDGE = (
    "Let's park that one for now and come at the skill from a different "
    "problem:"
)


def _bump_slot_age(session, slot) -> int:
    """Track how many tutor turns the current slot has been in flight.

    ``InFlightQuestion.posed_at_turn`` would be the natural source, but
    nothing ever sets ``engine_state['_current_turn_id']`` so the column is
    always NULL — count in engine_state instead, keyed by slot pk so a
    pivot/re-pose resets the clock."""
    es = getattr(session, 'engine_state', None) or {}
    if not isinstance(es, dict):
        return 0
    rec = es.get('_slot_age')
    if isinstance(rec, dict) and rec.get('id') == slot.pk:
        age = int(rec.get('age') or 0) + 1
    else:
        age = 1
    es['_slot_age'] = {'id': slot.pk, 'age': age}
    session.engine_state = es
    try:
        session.save(update_fields=['engine_state'])
    except Exception:
        pass
    return age


def _force_pivot_stuck_slot(
    *, session, step, family, tool_results, text_reply,
) -> str:
    """Hard pivot for stuck slots (gemma20_mt GB1). The attempt>=3 pivot
    GUIDANCE in the in-flight block is prompt-level and gemma ignored it —
    a mis-authored slot (ref '100' vs answers in probability form)
    collected five straight incorrect verdicts and burned sessions to
    max_turns. At attempt >= _PIVOT_ATTEMPTS the engine replaces the
    stuck slot with the next unused pool question and renders it, so no
    single question can consume a session. Eval families only."""
    if not family or family in _FORCE_POSE_EXEMPT_FAMILIES:
        return text_reply
    if step is None:
        return text_reply
    from apps.tutoring.models import InFlightQuestion
    slot = InFlightQuestion.objects.filter(session=session).first()
    if slot is None:
        return text_reply
    age = _bump_slot_age(session, slot)
    if int(slot.attempt_count or 0) < _PIVOT_ATTEMPTS \
            and age < _PIVOT_SLOT_AGE:
        return text_reply
    from apps.tutoring.simple_tutor.tools import (
        build_question_pool, handle_pose_question,
    )
    stuck_stem = (slot.question_text or '')[:60]
    for q in build_question_pool(session, max_questions=2):
        opts = [
            str(getattr(q, f'option_{letter}', '') or '').strip()
            for letter in 'abcd'
        ]
        opts = [o for o in opts if o]
        result = handle_pose_question(
            session,
            question_text=getattr(q, 'question_text', '') or '',
            question_type=getattr(q, 'question_type', '') or 'short_answer',
            reference_answer=str(getattr(q, 'correct_answer', '') or ''),
            source='catalog',
            options=opts or None,
            engine_initiated=True,
        )
        if result.get('posed'):
            tool_results.append({'tool': 'auto_pivot', 'result': result})
            new_slot = InFlightQuestion.objects.filter(session=session).first()
            logger.info(
                "[simple_tutor] auto_pivot: replaced stuck slot (%r, "
                "attempts=%s age=%s) session=%s", stuck_stem,
                slot.attempt_count, age, session.pk,
            )
            rendered = _render_slot_question(new_slot) if new_slot else ''
            if not rendered:
                return text_reply
            # Strip a trailing prose question first: it2 produced replies that
            # asked "What's 360 − 245?" and then immediately bridged to the
            # pivot question — two questions in one bubble, and only the pivot
            # is gradable.
            base = _strip_trailing_prose_question(
                (text_reply or '').rstrip()
            ).rstrip()
            return (f"{base}\n\n{_PIVOT_BRIDGE}\n\n{rendered}" if base
                    else f"{_PIVOT_BRIDGE}\n\n{rendered}")
    return text_reply


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
        chunks = kb.query_with_global_fallback(
            query_text=query_text,
            n_results=5,
            course=course,
        )
        if not chunks:
            # An empty retrieval is legitimate (a KB with nothing relevant),
            # but it is also exactly what a broken backend looks like — the
            # SQLite path returned [] unconditionally from the pgvector
            # migration until 2026-07-30 and nobody noticed, because the tutor
            # keeps answering, just ungrounded. Say so once per turn.
            from apps.curriculum import kb_storage
            stats = kb_storage.collection_stats(session.institution_id)
            logger.warning(
                "_retrieve_kb: no chunks for session=%s institution=%s "
                "(backend=%s, chunks_in_kb=%s) — tutoring turn will be ungrounded",
                session.pk, session.institution_id,
                stats.get('backend'), stats.get('total_chunks'),
            )
        return chunks
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


# ── Transient-error retry for the tutor call ─────────────────────────────────
# _call_llm returns None on ANY exception, and the engine then serves
# _FALLBACK_REPLY — which, repeated, deadlocks the session. In the multi-turn
# fix-check that turned transient cloud rate-limits into failures: kimi-k2-thinking
# deadlocked 12/20 purely on Vertex `429 Resource exhausted` (and a `503`), not on
# any tutoring flaw. A 429/503/529 is transient — retry it with backoff instead of
# collapsing the turn. Also hardens production against Anthropic overloads.
# Toggle SIMPLE_TUTOR_TRANSIENT_RETRY=0 disables it.
# Extended past 12s on 2026-07-22: the oss13_mt sweep lost 10 scenarios to an
# Anthropic overload window that outlasted the short ladder.
_TRANSIENT_BACKOFF = [2, 5, 12, 30, 60]

# The local ladder is one short retry, and the difference is not timidity — the
# two failure modes have nothing in common beyond the status code.
#
# A cloud 429/503/529 means "capacity, come back later": the wait IS the fix,
# and 109s of laddered sleep is cheap next to deadlocking a session.
#
# A local Ollama 5xx is a different animal. There is no queue to drain — one
# process, OLLAMA_NUM_PARALLEL=1 — so the usual cause is that the request
# itself cannot succeed: a generation that ran to num_predict and got cut off
# mid-`<tool_call>`, which Ollama then fails to parse and reports as a 500.
# That is DETERMINISTIC in the request, so every retry reproduces it, and each
# reproduction costs a full generation. Measured on the Jetson 2026-07-29:
# ~92 s per attempt, so the cloud ladder turned one bad turn into ~11 minutes
# (6 x 92 s decode + 109 s sleeping) before serving the placeholder anyway.
# See memory/tool_compliance_root_cause.md.
#
# One retry is still worth having: a local 5xx CAN be a genuine blip — a model
# reload, or an allocation failure while the box is under memory pressure, both
# real on an 8 GB Jetson — and those clear on the next attempt. What is never
# worth having is attempts 3-6, which have no failure mode they can fix.
_LOCAL_TRANSIENT_BACKOFF = [2]

# Providers running on the same box as Django. Not "open-weight" — a
# Qwen served by a cloud host has cloud queueing behaviour and wants the
# cloud ladder.
_LOCAL_PROVIDERS = frozenset({'local_ollama'})


def _backoff_for(provider: str | None) -> list[int]:
    """Retry ladder for ``provider`` — short for local, laddered for cloud."""
    if (provider or '').strip().lower() in _LOCAL_PROVIDERS:
        return _LOCAL_TRANSIENT_BACKOFF
    return _TRANSIENT_BACKOFF


# A 5xx whose body names the GENERATION as malformed, not the server as busy.
# Captured verbatim from the Jetson, 2026-07-29, Ollama 0.30.7:
#
#   {"error":"llama-server returned invalid tool call arguments for
#             \"pose_question\": unexpected end of JSON input"}
#
# "unexpected end of JSON input" is the tell: the tool call was cut off
# mid-arguments because decoding hit num_predict. Nothing about resending the
# same request changes that, so this is the one 5xx worth zero retries rather
# than one — it saves a guaranteed-wasted full generation (~92-103 s measured).
#
# Layered UNDER the local ladder on purpose: if a future Ollama reworded this,
# the match lapses and the failure falls back to _LOCAL_TRANSIENT_BACKOFF's
# single retry, which is still bounded. Sniffing the message can therefore only
# improve on the fallback, never regress past it.
_MALFORMED_GENERATION_MARKERS = (
    'invalid tool call arguments',
    'unexpected end of json input',
)


def _is_malformed_generation_error(exc: Exception) -> bool:
    """True when the server rejected its own truncated output — a failure that
    is deterministic in the request and so cannot be retried away."""
    haystack = str(exc).lower()
    try:
        haystack += ' ' + (
            getattr(getattr(exc, 'response', None), 'text', '') or '').lower()
    except Exception:
        pass
    return any(m in haystack for m in _MALFORMED_GENERATION_MARKERS)


def _error_detail(exc: Exception) -> str:
    """One-line error text, including the HTTP body when there is one.

    `requests.HTTPError.__str__` is only "500 Server Error: Internal Server
    Error for url: …" — the provider's own explanation lives in
    `exc.response.text` and was being dropped. Six Ollama 500s in one session
    logged nothing about *why* (2026-07-27), which is the whole reason that
    incident needed a probe script to diagnose.
    """
    msg = str(exc).strip().replace('\n', ' ')[:160]
    body = ''
    try:
        raw = getattr(getattr(exc, 'response', None), 'text', '') or ''
        body = raw.strip().replace('\n', ' ')[:240]
    except Exception:
        body = ''
    return f'{msg} | body: {body}' if body else msg


def _is_transient_error(exc: Exception) -> bool:
    """True for retryable cloud failures — rate limits, overloads, 5xx, connection
    blips — as opposed to permanent errors (400 / auth / schema) which must fail
    fast so we don't burn backoff on something that will never succeed."""
    name = type(exc).__name__.lower()
    if any(k in name for k in (
        'ratelimit', 'internalserver', 'serviceunavailable', 'apiconnection',
        'apitimeout', 'timeout', 'overloaded',
    )):
        return True
    # `requests.HTTPError` — the shape the Ollama adapter raises — carries the
    # status on exc.response.status_code, NOT exc.status_code, so the local
    # provider fell through every check here and its 500s were treated as
    # permanent. Measured on the Jetson 2026-07-27: six 500s in one session,
    # none retried, each silently degrading a turn to the placeholder reply.
    for attr in ('status_code', 'code'):
        try:
            if int(getattr(exc, attr, None)) in (429, 500, 502, 503, 504, 529):
                return True
        except (TypeError, ValueError):
            pass
    try:
        if int(getattr(getattr(exc, 'response', None), 'status_code', None)) in (
            429, 500, 502, 503, 504, 529,
        ):
            return True
    except (TypeError, ValueError):
        pass
    msg = str(exc).lower()
    return any(s in msg for s in (
        '429', '500 server error', '502', '503', '529', 'resource exhausted',
        'overloaded', 'unavailable', 'please try again', 'rate limit',
        'timed out', 'connection error',
    ))


def _invoke_with_transient_retry(fn, *, label: str, on_attempt=None,
                                 provider: str | None = None):
    """Call ``fn()``; on a transient error retry with backoff, else return None.
    Never raises (preserves _call_llm's no-block contract).

    ``on_attempt`` fires before every attempt, including the first. It exists
    for the streaming path: a retry re-runs the generation from scratch, so
    without a reset the stream gate would append the second attempt's tokens
    to the first attempt's partial text and emit the concatenation.

    ``provider`` selects the retry ladder — see `_backoff_for`. A local
    provider gets one short retry because its 5xx is usually deterministic in
    the request and each attempt costs a whole generation; omitting it keeps
    the cloud ladder, which is the safe default for anything remote.
    """
    backoff = _backoff_for(provider)
    retries = len(backoff) if (
        os.getenv('SIMPLE_TUTOR_TRANSIENT_RETRY', '1').strip() != '0') else 0
    for attempt in range(retries + 1):
        try:
            if on_attempt is not None:
                on_attempt()
            return fn()
        except Exception as exc:
            detail = _error_detail(exc)
            if _is_malformed_generation_error(exc):
                # Retrying cannot help: the server rejected its own truncated
                # generation. Fail straight to the caller's fallback.
                logger.warning(
                    "_call_llm: %s malformed generation, not retrying: %s: %s",
                    label, type(exc).__name__, detail,
                )
                return None
            if _is_transient_error(exc) and attempt < retries:
                delay = backoff[attempt]
                logger.warning(
                    "_call_llm: %s transient %s (%s) — retry %d/%d in %ds",
                    label, type(exc).__name__, detail, attempt + 1, retries,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.warning("_call_llm: %s failed: %s: %s",
                           label, type(exc).__name__, detail)
            return None
    return None


def _call_llm(
    *,
    system_blocks: list,
    tools: list,
    messages: list,
    tool_choice: dict | None = None,
    on_delta=None,
    config=None,
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

    ``on_delta`` is forwarded only to clients whose ``generate_with_tools``
    accepts it (today: Ollama). It is deliberately NOT plumbed into the
    native Anthropic branch below — production must stay byte-identical,
    and streaming is an offline-kiosk feature. A client that does not
    support it simply runs buffered; the caller's gate then emits nothing
    and the turn behaves exactly as it does today.
    """
    try:
        from apps.llm.models import ModelConfig
    except ImportError:
        logger.warning("_call_llm: ModelConfig unavailable")
        return None

    # `config` is passed in when the student's tutor_mode preference selected a
    # specific model (offline vs online on the desktop build). None means no
    # preference applies — the hosted platform's normal path — so fall through
    # to the active tutoring config exactly as before.
    if config is None:
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
        # Fall back to the RESOLVED config's provider/model_name, not the bare
        # model_name. MODEL_PROFILES is keyed by full spec
        # ("local_ollama/qwen3.5:2b"), so a bare name never matches and drops
        # through to a cloud family pattern sized at num_ctx=24192.
        #
        # That path is now reachable in production, not just in sweeps: the
        # kiosk deliberately does NOT set TUTOR_MODEL_OVERRIDE so that an admin
        # can pick the model from the browser. Measured on the Jetson
        # 2026-07-28 before this fix — selecting qwen3.5:2b from the DB gave
        # num_ctx=24192, 34%/66% CPU/GPU instead of full offload, and 204 s per
        # turn against the ~79 s the same model does on its own profile.
        profile = get_model_profile(
            spec or (f'{provider}/{model_name}' if provider else model_name)
        )
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
        client = anthropic.Anthropic(api_key=api_key)
        # tool_choice omitted entirely when None so the production
        # Anthropic call is byte-identical (default is auto).
        extra = {'tool_choice': tool_choice} if tool_choice else {}
        return _invoke_with_transient_retry(
            lambda: client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                system=effective_blocks,
                tools=tools,
                messages=messages,
                **extra,
            ),
            label='Anthropic',
            provider=provider,
        )

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
    extra_stream = {}
    reset_stream = None
    if on_delta is not None:
        try:
            import inspect
            if 'on_delta' in inspect.signature(
                    client.generate_with_tools).parameters:
                extra_stream['on_delta'] = on_delta
                reset_stream = getattr(on_delta, 'begin_attempt', None)
            else:
                logger.info(
                    "_call_llm: %s does not support on_delta — running "
                    "buffered", type(client).__name__,
                )
        except (TypeError, ValueError):
            pass
    return _invoke_with_transient_retry(
        lambda: client.generate_with_tools(
            messages=messages,
            system_prompt=_system_blocks_to_text(effective_blocks),
            tools=tools,
            max_tokens=max_tokens,
            sampling=sampling,
            tool_choice=tool_choice,
            **extra_stream,
        ),
        label=f'generate_with_tools({provider})',
        on_attempt=reset_stream,
        provider=provider,
    )


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
_POSE_NEXT_INSTRUCTION = (
    "The answer was CORRECT and the graded question is now closed. Keep the "
    "session moving in this same reply: briefly acknowledge, then ask the "
    "next question — call pose_question to register it (question_text, "
    "question_type, options for MCQ, reference_answer) and include the same "
    "question in your visible text. Prefer an unused question from "
    "<question_pool>."
)


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
    if missing_tool == 'pose_question_next':
        return _POSE_NEXT_INSTRUCTION
    if missing_tool == 'pose_question':
        return _POSE_REPAIR_INSTRUCTION.format(
            assistant_text=(assistant_text or '').strip()[:1500])
    return _RECORD_REPAIR_INSTRUCTION.format(
        user_input=str(user_input or '').strip()[:1500])


def _missing_tool_name(missing_tool: str) -> str:
    """The actual tool behind a repair token ('pose_question_next' rides on
    pose_question)."""
    return ('pose_question' if missing_tool.startswith('pose_question')
            else missing_tool)


def _should_pose_next_after_correct(
    family: str | None, mode: str, tool_results: list, step,
) -> bool:
    """Whether Call 2 must register the next question this turn.

    2026-07-20 smoke run after the desync fixes: on correct-verdict GRADE
    turns qwen wrote the next question in prose with no pose_question call
    (tools=['record_answer'] only). The next student answer then met an
    empty slot, and the forced pose on THAT turn grabbed a different pool
    question — the model then fabricated a re-grade of the student's answer
    against it ("You just answered '60' to [the 75° question]"). Requiring
    the pose in the same turn as the correct verdict kills the desync at its
    source; _ensure_posed_question_in_text then aligns the visible text.

    Eval-only gating mirrors _should_force_pose: production (family None)
    and Anthropic are untouched. Skipped on the lesson's last step, where a
    forced question could collide with the exit-ticket handoff.
    """
    if not family or family in _FORCE_POSE_EXEMPT_FAMILIES:
        return False
    if mode != 'GRADE':
        return False
    if _pose_was_registered(tool_results):
        return False
    verdict_correct = any(
        tr.get('tool') == 'record_answer'
        and (tr.get('result') or {}).get('recorded')
        and (tr.get('result') or {}).get('verdict') == 'correct'
        for tr in (tool_results or [])
    )
    if not verdict_correct:
        return False
    if step is None:
        return False
    from apps.curriculum.models import LessonStep
    return LessonStep.objects.filter(
        lesson=step.lesson, order_index__gt=step.order_index,
    ).exists()


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
    name = _missing_tool_name(missing_tool)
    only = [t for t in (tools or []) if t.get('name') == name]
    return (only or tools), {'type': 'tool', 'name': name}


def _call1_contradicts_verdict(text_reply_1: str, tool_results) -> bool:
    """Whether Call-1 prose asserts the OPPOSITE of the grader's verdict.

    In one-call mode the prose was written before grading; when the model
    guessed its own verdict wrong, the reply's hint content is built on a
    false premise that no deterministic rewrite can fix — those turns spend
    Call 2 after all (see the call site in _run_second_call)."""
    verdict = _turn_verdict(tool_results)
    if verdict not in ('correct', 'incorrect') or not text_reply_1:
        return False
    head = text_reply_1.lstrip()
    if verdict == 'correct':
        return bool(_NEG_OPENER_RE.match(head)
                    or _MID_DENY_RE.search(text_reply_1))
    return bool(_POS_OPENER_RE.match(head)
                or _MID_AFFIRM_RE.search(text_reply_1))


def _call_mode(family: str | None) -> str:
    """'one' or 'two' — how many LLM calls a compliant turn may cost.

    TWO-CALL is the original design and stays the default for Anthropic:
    Call 1 picks tools, the platform grades, and Call 2 writes the reply
    *knowing the verdict*. That ordering is why the model cannot
    guess-confirm a grade it has not seen.

    ONE-CALL accepts Call 1's prose and skips Call 2 whenever Call 1
    already produced everything the turn needs (the expected tool AND
    usable text). It halves the turn on a box where each call is 8-10s.

    The trade is real and worth stating plainly: in one-call mode the
    reply is written BEFORE the platform grades, so the model is
    predicting its own verdict. `_align_reply_polarity` is the
    deterministic net that catches the contradictions, and it is why
    one-call is defensible rather than reckless.

    'auto' (the default) resolves to one-call for local/open-weight
    families and two-call for Anthropic — offline is where latency hurts
    and where Call 2 was usually a silent tool-repair anyway. Override
    with TUTOR_CALL_MODE=one|two|auto.
    """
    raw = os.getenv('TUTOR_CALL_MODE', 'auto').strip().lower()
    if raw in ('one', 'two'):
        return raw
    if not family or family in _FORCE_POSE_EXEMPT_FAMILIES:
        return 'two'
    return 'one'


def _run_second_call(
    *, session, system_blocks, tools, messages, response, text_reply_1,
    tool_results, figure_catalog, missing_tool, user_input, on_delta=None,
    family=None, config=None,
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

    # One-call mode: Call 1 already produced the tool AND the prose, so the
    # only thing Call 2 would add is a reply rewritten with the verdict in
    # hand. Skip it and keep Call 1's text.
    #
    # Measured on the Jetson 2026-07-29: Call 2 emitted no student-visible
    # text on 4 of 5 turns — it existed purely to register the tool Call 1
    # had skipped, while `text_reply_1` was what the student actually read.
    # With the per-turn directive making Call 1 compliant, that second call
    # buys nothing on most turns and costs 8-10s.
    #
    # A MISSING tool still falls through to the repair below, so this only
    # ever skips a call whose work is already done.
    #
    # Escalation guard: in one-call mode the prose was written BEFORE the
    # platform graded, so the model predicted its own verdict. When the
    # prediction was WRONG — a "Not quite" opener on a graded-correct answer,
    # or an affirmation on a graded-wrong one — _align_reply_polarity can fix
    # the opener but not the hint content built on the wrong premise ("you
    # used 0.65 instead of 0.60" about an answer that was right). Those turns
    # spend Call 2 after all: the model rewrites with the verdict in hand.
    # Contradiction turns are the minority, so the latency win of one-call
    # mode survives; the quality floor comes back up to two-call on exactly
    # the turns that need it.
    if missing_tool is None and text_reply_1 and _call_mode(family) == 'one':
        # A REJECTED pose also escalates: the platform refused the question
        # (premature over an ungraded/just-missed slot, or a repeat), but
        # Call-1's prose was written assuming it registered — it announces a
        # question the student can never answer. Call 2 sees the rejection
        # feedback and rewrites coherently against the surviving slot.
        pose_rejected = any(
            tr.get('tool') == 'pose_question'
            and not (tr.get('result') or {}).get('posed')
            for tr in (tool_results or [])
        )
        if not pose_rejected \
                and not _call1_contradicts_verdict(text_reply_1, tool_results):
            logger.info(
                "[simple_tutor] one_call: Call 1 delivered tool+prose — "
                "skipping Call 2 (mode=%s)", _call_mode(family),
            )
            return text_reply_1, False
        logger.info(
            "[simple_tutor] one_call_escalated: %s — spending Call 2 to "
            "rewrite",
            'Call-1 pose was rejected' if pose_rejected
            else f'Call-1 prose contradicts verdict={_turn_verdict(tool_results)}',
        )

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
    # Call 2 is the ONLY streamable call. Call 1's text is pre-text the
    # repair path may discard, and the grader verdict the reveal/polarity
    # filters need is not known until _dispatch_tools has run — which
    # happens between the two calls.
    response2 = _call_llm(
        system_blocks=system_blocks, tools=call2_tools, messages=messages,
        tool_choice=call2_tool_choice, on_delta=on_delta, config=config,
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
    if missing_tool and not _tool_was_called(
            extra_tool_results, _missing_tool_name(missing_tool)):
        logger.warning(
            "[simple_tutor] call2_repair: model still declined to call %s — "
            "this turn leaves no %s", missing_tool,
            'verdict' if missing_tool == 'record_answer' else 'gradable slot',
        )
    return (text_reply_2 or text_reply_1), True


def _norm_loose(s: str) -> str:
    """Lowercase + collapse whitespace + strip markdown emphasis — the loose
    matching used for stem comparisons."""
    s = (s or '').lower()
    for ch in ('*', '_', '`'):
        s = s.replace(ch, '')
    return ' '.join(s.split())


def _pose_before_record(session, tool_use_blocks) -> bool:
    """Whether pose_question should dispatch before record_answer.

    True only for the late-registration case: no slot exists, and a posed
    stem appears in the previous tutor turn's visible text — i.e. the model
    is registering the question the student actually answered. See
    _dispatch_tools' docstring for the full decision table.
    """
    has_pose = any(getattr(b, 'name', '') == 'pose_question'
                   for b in tool_use_blocks)
    has_record = any(getattr(b, 'name', '') == 'record_answer'
                     for b in tool_use_blocks)
    if not (has_pose and has_record):
        return not has_record  # order is irrelevant with ≤1 of the pair
    if getattr(session, 'pk', None) is None:
        return True  # detached session (mocked-handler tests): legacy order
    from apps.tutoring.models import InFlightQuestion, SessionTurn
    if InFlightQuestion.objects.filter(session=session).exists():
        return False  # grade the question the student saw, then re-pose
    prev = (
        SessionTurn.objects
        .filter(session=session, role=SessionTurn.Role.TUTOR)
        .order_by('-created_at', '-pk')
        .values_list('content', flat=True)
        .first()
    ) or ''
    prev_norm = _norm_loose(prev)
    for b in tool_use_blocks:
        if getattr(b, 'name', '') != 'pose_question':
            continue
        params = getattr(b, 'input', None) or {}
        stem = _norm_loose(str(params.get('question_text') or ''))[:30]
        if stem and stem in prev_norm:
            return True  # late registration — grade against this pose
    return False


def _dispatch_tools(*, session, response, figure_catalog):
    """Walk Anthropic response content. For each text block, accumulate
    the reply. For each tool_use block, dispatch to the right handler.

    Dispatch order encodes WHICH question the student's message answers
    (cycle-7 fix — the old unconditional pose-first order graded the
    student's answer to the PREVIOUS visible question against a freshly
    posed NEXT question they had never seen):

    - A slot exists at dispatch time → the student saw that question.
      record_answer runs FIRST (grades the existing slot); a same-turn
      pose then replaces the slot with the next question.
    - No slot, and a posed stem matches the previous tutor turn's text →
      late registration of the question the model narrated as prose
      (M12 repair flow). pose runs first so record grades it.
    - No slot and the posed stem is fresh → the pose is the NEXT
      question. record runs first, finds no slot, and returns the
      no-in-flight result; the model self-judges in Call 2 instead of
      grading against a question the student never saw.

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

    pose_first = _pose_before_record(session, tool_use_blocks)

    def _priority(blk) -> int:
        n = getattr(blk, 'name', '')
        if n == 'pose_question':
            return 0 if pose_first else 1
        if n == 'record_answer':
            return 1 if pose_first else 0
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


def respond_for_view(session, user_input: str, *, on_delta=None) -> dict:
    """Adapter for ``apps.tutoring.views.chat_respond``.

    Calls ``respond(...)`` (which returns the engine's internal dict),
    then projects the result into the same JSON shape the legacy
    v1 view returns — so the existing chat UI works without changes.

    Fields not produced by v1 of the simple engine (gamification,
    artifact_html, follow_up, etc.) default to safe values.

    ``on_delta`` is forwarded to ``respond`` for the streaming transports
    (the SSE view and the terminal CLI). Note that the returned
    ``message`` can still differ from the last streamed snapshot — the
    exit-ticket branch below deliberately REPLACES the reply text — which
    is why transports must render the final payload rather than keeping
    whatever they streamed.
    """
    from apps.curriculum.models import LessonStep
    from apps.tutoring.models import ExitTicketAttempt

    out = respond(session, user_input, on_delta=on_delta)

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
