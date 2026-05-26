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


def respond(session: 'TutorSession', user_input: str) -> dict[str, Any]:
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

    # ─── 1. Gather context (NO server-side anchor) ────────────────
    # The tutor LLM sees a pool of candidate questions and reference
    # answers as CONTEXT. It may pose any of them verbatim, adapt one,
    # or author its own. Grading happens via record_answer which
    # carries the LLM's chosen reference. See M11.3 in the milestones.
    step = _load_current_step(session)
    question_pool = build_question_pool(session)
    kb_chunks = _retrieve_kb(session, user_input)
    figure_catalog = _build_figure_catalog(step)
    figures_enabled = _figures_enabled(session)
    recent_window = build_recent_window(session)
    step_summaries = step_summary_log(session)

    logger.info(
        "[simple_tutor] pool session=%s step=%s pool_size=%s",
        session.pk, session.current_step_index, len(question_pool),
    )

    # ─── 2. Build system prompt + tool schemas ────────────────────
    from apps.tutoring.simple_tutor.prompts import build_system_prompt
    system_blocks, tools = build_system_prompt(
        session=session,
        step=step,
        question_pool=question_pool,
        kb_chunks=kb_chunks,
        figure_catalog=figure_catalog,
        figures_enabled=figures_enabled,
        recent_window=recent_window,
        step_summaries=step_summaries,
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

    logger.info(
        "[simple_tutor] two_call=%s text_chars=%s tools=%s",
        used_two_call, len(text_reply or ''),
        [tr.get('tool') for tr in tool_results],
    )

    # ─── 8. Persist turns + verdicts ──────────────────────────────
    _persist_student_turn(session, user_input, step)
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
    import json
    out = []
    # Filter tool_results to those that came from an LLM tool_use block
    # (i.e. names the LLM actually called this turn).
    llm_tool_names = {getattr(b, 'name', None) for b in tool_use_blocks}
    matched_results = [
        tr for tr in tool_results if tr.get('tool') in llm_tool_names
    ]
    if len(matched_results) != len(tool_use_blocks):
        # Defensive: pairing-by-order requires equal counts. If they
        # don't match (rare), skip the loop and let the single-call
        # text stand. Logged so we can diagnose.
        logger.warning(
            "tool_use_blocks=%s but matched_results=%s — skipping call 2",
            len(tool_use_blocks), len(matched_results),
        )
        return []
    for block, tr in zip(tool_use_blocks, matched_results):
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
    if tool_name == 'record_answer' and result.get('recorded'):
        verdict = (result.get('verdict') or '?').upper()
        ref = (result.get('reference_answer') or '').strip()
        ext = (result.get('justification') or '').strip()
        # The student's extracted answer isn't in the result dict (it
        # was an input). Fall back to a generic phrasing.
        qtext = (result.get('question_text') or '').strip()
        qtype = result.get('question_type') or 'short_answer'
        parts = [
            f"VERDICT: {verdict}",
            f"This was the question I just graded (question_type={qtype}):",
            f'  "{qtext}"' if qtext else '  (no question_text recorded)',
            f"Correct answer (reference): {ref}" if ref else "",
            "",
            "Compose your next reply ABOUT THIS QUESTION ONLY. Do NOT "
            "reference older questions from <recent_turns> in your hint "
            "or feedback. If incorrect, give a hint about THIS question "
            "and re-pose it. If correct, briefly acknowledge and move "
            "to the next teaching beat or new question.",
        ]
        if ext:
            parts.insert(3, f"Grader justification: {ext}")
        return "\n".join(p for p in parts if p)
    # Non-record_answer tools: keep JSON (cheaper context).
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
        return "Got it — that's right. Ready for the next one?"
    if verdict == 'incorrect':
        return "Not quite — want to walk through it together?"
    return "Let's keep going. What are you thinking?"


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

    Returns:
        (text_reply, tool_results, llm_called_record_answer)
    """
    from apps.tutoring.simple_tutor.tools import (
        handle_record_answer, handle_request_figure,
        handle_redirect_off_topic, handle_advance_step,
    )

    text_reply = ''
    tool_results: list[dict] = []
    llm_called_record_answer = False

    # Anthropic SDK returns response.content as a list of typed blocks
    for block in getattr(response, 'content', None) or []:
        btype = getattr(block, 'type', None)
        if btype == 'text':
            text_reply += getattr(block, 'text', '')
            continue
        if btype != 'tool_use':
            continue

        name = getattr(block, 'name', '')
        params = getattr(block, 'input', None) or {}
        if not isinstance(params, dict):
            params = {}

        try:
            if name == 'record_answer':
                result = handle_record_answer(
                    session,
                    extracted_answer=str(params.get('extracted_answer', '')),
                    reference_answer=str(params.get('reference_answer', '')),
                    question_type=str(params.get('question_type', '')),
                    question_text=str(params.get('question_text', '')),
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

        tool_results.append({'tool': name, 'result': result})

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
    phase = (
        (getattr(step, 'phase', '') or '').lower()
        if step else 'evaluate'
    )

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

    is_complete = step is None or current_idx >= total_steps

    return {
        'message': out.get('content', ''),
        'phase': phase,
        'media': [{'url': media_url}] if media_url else [],
        'show_exit_ticket': False,           # v2 hasn't wired exit ticket yet
        'exit_ticket': None,
        'is_complete': is_complete,
        'step_number': current_idx + 1,
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


def _persist_tutor_turn(session, text_reply: str, step, tool_results: list):
    """Create the tutor's SessionTurn row. If any tool call recorded
    a grader verdict (record_answer or auto_grade_fallback), embed it
    in ``judge_outputs['grader']`` so the dashboard + analytics +
    pick_current_question can read it next turn.
    """
    from apps.tutoring.models import SessionTurn

    judge_outputs: dict = {}
    metadata: dict = {'tool_calls': tool_results}

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
