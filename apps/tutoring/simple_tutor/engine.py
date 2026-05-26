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
        pick_current_question, auto_grade_if_missed, maybe_advance_step,
    )
    from apps.tutoring.simple_tutor.state import (
        build_recent_window, step_summary_log, set_current_question,
    )

    # ─── 1. Server picks the current question ─────────────────────
    current_q = pick_current_question(session)
    if current_q is not None:
        set_current_question(session, current_q.pk)

    # ─── 2. Gather context ────────────────────────────────────────
    step = _load_current_step(session)
    kb_chunks = _retrieve_kb(session, user_input)
    figure_catalog = _build_figure_catalog(step)
    figures_enabled = _figures_enabled(session)
    recent_window = build_recent_window(session)
    step_summaries = step_summary_log(session)

    # ─── 3. Build system prompt + tool schemas ────────────────────
    from apps.tutoring.simple_tutor.prompts import build_system_prompt
    system_blocks, tools = build_system_prompt(
        session=session,
        step=step,
        current_question=current_q,
        kb_chunks=kb_chunks,
        figure_catalog=figure_catalog,
        figures_enabled=figures_enabled,
        recent_window=recent_window,
        step_summaries=step_summaries,
    )

    # ─── 4. LLM call ──────────────────────────────────────────────
    response = _call_llm(
        system_blocks=system_blocks,
        tools=tools,
        user_input=user_input,
    )
    if response is None:
        # LLM call failed entirely → return fallback. Still persist
        # the student turn so the audit log is complete.
        _persist_student_turn(session, user_input, step)
        return {
            'content': _FALLBACK_REPLY,
            'tool_calls': [],
            'fallback': True,
        }

    # ─── 5. Dispatch tool calls ───────────────────────────────────
    text_reply, tool_results, llm_called_record_answer = _dispatch_tools(
        session=session,
        response=response,
        figure_catalog=figure_catalog,
    )

    # ─── 6. Auto-fallback grading ─────────────────────────────────
    fallback_verdict = auto_grade_if_missed(
        session, user_input, llm_called_record_answer,
    )
    if fallback_verdict is not None:
        tool_results.append({
            'tool': 'auto_grade_fallback',
            'result': {
                'recorded': True,
                'question_id': session.current_question_id,
                **fallback_verdict.to_dict(),
            },
        })
        # Auto-grade fired — clear the current question so next turn picks
        # a fresh one if appropriate.
        from apps.tutoring.simple_tutor.state import clear_current_question
        clear_current_question(session)

    # ─── 7. Persist turns + verdicts ──────────────────────────────
    _persist_student_turn(session, user_input, step)
    _persist_tutor_turn(session, text_reply, step, tool_results)

    # ─── 8. Server auto-advance (safety net) ──────────────────────
    advanced = maybe_advance_step(session)

    return {
        'content': text_reply or '',
        'tool_calls': tool_results,
        'fallback': False,
        'step_advanced': advanced,
    }


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


def _call_llm(*, system_blocks: list, tools: list, user_input: str):
    """Call Anthropic with the simple-tutor prompt + tools. Returns the
    raw Anthropic response object, or None on any error.

    Uses ``ModelConfig.get_for('tutoring')`` so the model is configurable
    via the dashboard. Defaults to Claude Opus 4.7 per the prod config.
    Never raises — failures log a warning and return None; caller serves
    the fallback reply.
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
            messages=[{'role': 'user', 'content': user_input}],
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

    # Extract is_correct from any record_answer / auto_grade verdict
    is_correct = None
    media_url = None
    for entry in out.get('tool_calls') or []:
        tool = entry.get('tool')
        result = entry.get('result') or {}
        if tool in ('record_answer', 'auto_grade_fallback'):
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

    # Surface the most recent grader verdict (from either record_answer
    # or auto_grade_fallback) on the tutor turn's judge_outputs['grader'].
    for entry in tool_results:
        tool = entry.get('tool')
        result = entry.get('result') or {}
        if tool in ('record_answer', 'auto_grade_fallback') and result.get('recorded'):
            judge_outputs['grader'] = {
                'verdict': result.get('verdict'),
                'confidence': result.get('confidence'),
                'tier': result.get('tier'),
                'per_criterion_scores': result.get('per_criterion_scores') or {},
                'justification': result.get('justification') or '',
                'needs_followup': result.get('needs_followup', False),
                'question_id': result.get('question_id'),
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
