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
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

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
        build_question_pool, maybe_advance_step,
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

    if exit_ticket_review is not None:
        mode = 'REMEDIATION+GRADE' if in_flight else 'REMEDIATION'
    else:
        mode = 'GRADE' if in_flight else 'POSE'
    logger.info(
        "[simple_tutor] mode=%s session=%s step=%s pool_size=%s",
        mode, session.pk, session.current_step_index, len(question_pool),
    )

    # ─── 2. Build system prompt + tool schemas ────────────────────
    from apps.tutoring.simple_tutor.prompts import build_system_prompt
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
    messages: list = [{'role': 'user', 'content': user_input}]
    response = _call_llm(
        system_blocks=system_blocks, tools=tools, messages=messages,
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

    # ─── 6. Call 2 — feed tool_results back so the model writes
    #              the student-facing reply WITH the verdict in hand.
    text_reply = text_reply_1
    used_two_call = False
    tool_use_blocks = _extract_tool_use_blocks(response)
    if tool_use_blocks:
        tool_result_content = _build_tool_result_content(
            tool_use_blocks, tool_results,
        )
        if tool_result_content:
            messages.append({'role': 'assistant', 'content': response.content})
            messages.append({'role': 'user', 'content': tool_result_content})
            response2 = _call_llm(
                system_blocks=system_blocks, tools=tools, messages=messages,
            )
            if response2 is not None:
                used_two_call = True
                text_reply_2, extra_tool_results, _ = _dispatch_tools(
                    session=session,
                    response=response2,
                    figure_catalog=figure_catalog,
                )
                # Call 2 is meant to produce text; if it also chose to
                # call a tool, accept the side effects but use only the
                # accumulated text. (No third call — keeps latency
                # bounded; tool calls in call 2 are uncommon.)
                tool_results.extend(extra_tool_results)
                if text_reply_2:
                    text_reply = text_reply_2

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

    # ─── 8. Collapse multi-paragraph responses to one block ──────
    # The system prompt asks for concise turns but the LLM frequently
    # emits 2–4 paragraphs with blank lines between them. The mobile
    # chat UX wants one block of text per turn. ``collapse_paragraphs``
    # replaces ``\n\s*\n+`` with single ``\n`` so the response becomes
    # one paragraph; single newlines (bullet/list items) are preserved
    # and a trailing ``|||MEDIA:N|||`` signal stays on its own line for
    # the media parser. Applied BEFORE persist so DB rows match the
    # student view exactly — unlike the legacy ConversationalTutor where
    # collapse runs post-impl to keep the coherence judge from
    # mis-flagging the collapsed text. simple_tutor's grader operates
    # on student input, not tutor text, so the early-collapse is safe.
    # Today's baseline (2026-05-27) showed all 6 representative
    # scenarios failing solely on max_paragraphs while rubric scored
    # 0.93–0.99 — pedagogy is sound; this is purely format compliance.
    from apps.tutoring.validator import collapse_paragraphs
    text_reply = collapse_paragraphs(text_reply or '')

    # ─── 9. Persist turns + verdicts ──────────────────────────────
    # On the opening (warm-up) call, ``user_input`` is the synthetic
    # "begin the lesson" instruction — do NOT persist it as a student
    # turn so the chat thread starts with the tutor's greeting.
    if not _is_opening:
        _persist_student_turn(session, user_input, step)
    _persist_tutor_turn(session, text_reply, step, tool_results)

    # ─── 10. Server auto-advance (safety net) ─────────────────────
    advanced = maybe_advance_step(session)

    return {
        'content': text_reply,
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


def _call_llm(
    *,
    system_blocks: list,
    tools: list,
    messages: list,
):
    """Call Anthropic with the simple-tutor prompt + tools + the messages
    array. Returns the raw Anthropic response, or None on any error.

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

    api_key = config.get_api_key()
    model_name = config.model_name
    if not api_key or not model_name:
        logger.warning("_call_llm: missing api_key or model_name")
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning("_call_llm: anthropic SDK not installed")
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model_name,
            max_tokens=1024,
            system=system_blocks,
            tools=tools,
            messages=messages,
        )
        return response
    except Exception as exc:
        msg = str(exc).strip().replace('\n', ' ')[:200]
        logger.warning(
            "_call_llm: Anthropic call failed: %s: %s",
            type(exc).__name__, msg,
        )
        return None


# ============================================================================
# Tool dispatch
# ============================================================================


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
    for _idx, block in sorted_blocks:
        name = getattr(block, 'name', '')
        params = getattr(block, 'input', None) or {}
        if not isinstance(params, dict):
            params = {}

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
                result = {'error': f'unknown tool {name!r}'}
        except Exception as exc:
            # Handlers should not raise, but if one does, log + continue
            msg = str(exc).strip().replace('\n', ' ')[:200]
            logger.warning(
                "_dispatch_tools: handler for %s raised %s: %s",
                name, type(exc).__name__, msg,
            )
            result = {'error': f'handler exception {type(exc).__name__}'}

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
                transition_msg = (
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
        message = _build_resume_message(in_flight)
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


def _build_resume_message(slot) -> str:
    """Deterministic welcome-back text + re-display of the in-flight
    slot's question. Used by ``start_for_view`` on resume so we don't
    burn an LLM call (and risk orphaning the slot) just to render a
    question we already have.
    """
    from apps.tutoring.models import InFlightQuestion

    stem = (slot.question_text or '').strip()
    parts = [
        "👋 Welcome back! You were working on this question — let's pick "
        "up where we left off:",
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
