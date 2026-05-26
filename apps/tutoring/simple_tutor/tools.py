"""Server-side tool handlers + flow primitives for the simple-tutor engine.

4-tool design (revised 2026-05-26 per
auto-memory/feedback_server_owns_question_state.md):

  LLM-called tools (all soft / advisory — engine never blocks on them):
    - handle_record_answer  — student gave an answer; grade it
    - handle_request_figure — display a figure inline
    - handle_redirect_off_topic — soft moderation flag
    - handle_advance_step — LLM hints "ready for the next step"

  Server-driven primitives the engine calls automatically:
    - pick_current_question(session)  → picks the next un-graded
                                         question for the CURRENT step's
                                         enabling_objective, BEFORE the
                                         LLM call
    - auto_grade_if_missed(session, user_input, llm_called_record_answer)
                                        → safety net AFTER the LLM call
    - maybe_advance_step(session)       → soft auto-advance when current
                                         step's questions are all graded,
                                         OR after a soft turn cap

Step ↔ question linkage uses ``enabling_objective`` (string field on both
LessonStep and ExitTicketQuestion) — see
``auto-memory/feedback_step_question_linkage.md``. No schema change needed.

All handlers return dicts (NEVER raise on bad input). The conversation
must always flow — failures get logged + return error dicts, never block.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from apps.tutoring.models import TutorSession  # noqa: F401  (typing only)


# Soft turn cap — server auto-advances after this many student turns
# on the same lesson step. Generous default: real Socratic discussion
# can run 6-10 turns before grading lands.
DEFAULT_STEP_TURN_CAP = 8


# ============================================================================
# pick_current_question — server picks the next un-graded question
# ============================================================================


def pick_current_question(session: 'TutorSession'):
    """Return the next un-graded ExitTicketQuestion for the CURRENT
    lesson step's ``enabling_objective``, or None when the current
    step's questions are all graded (signals "ready to advance step").

    See ``auto-memory/feedback_step_question_linkage.md`` — questions
    are linked to steps via matching ``enabling_objective`` strings.

    Selection order: ``order_index`` then ``pk`` ascending — matches how
    questions are authored within an objective.
    """
    from apps.tutoring.models import ExitTicketQuestion
    from apps.curriculum.models import LessonStep

    lesson = getattr(session, 'lesson', None)
    if lesson is None:
        return None

    current_step_index = getattr(session, 'current_step_index', 0) or 0
    step = (
        LessonStep.objects
        .filter(lesson=lesson, order_index=current_step_index)
        .first()
    )
    if step is None:
        return None

    objective = (getattr(step, 'enabling_objective', '') or '').strip()
    graded_qids = _graded_question_ids(session)

    # Pass 1 — prefer questions matching the current step's objective.
    if objective:
        match = (
            ExitTicketQuestion.objects
            .filter(
                exit_ticket__lesson=lesson,
                enabling_objective=objective,
            )
            .exclude(pk__in=graded_qids)
            .order_by('order_index', 'id')
            .first()
        )
        if match is not None:
            return match

    # Pass 2 — fall back to ANY un-graded lesson question. Handles:
    #  - Step has empty enabling_objective (legacy / sparse data)
    #  - Step has objective but no questions match (content generator
    #    didn't author one for it)
    # Per user direction: better to pick any question than block.
    return (
        ExitTicketQuestion.objects
        .filter(exit_ticket__lesson=lesson)
        .exclude(pk__in=graded_qids)
        .order_by('order_index', 'id')
        .first()
    )


def _current_step_questions_remaining(session: 'TutorSession') -> bool:
    """STRICT check used by maybe_advance_step: are there any un-graded
    questions matching the CURRENT step's enabling_objective?

    Distinct from ``pick_current_question`` (which falls back to
    lesson-wide pool). This function returns True ONLY when there's
    an objective-matching question still un-graded. When the current
    step's objective is empty / has no questions at all, returns False
    (treat as "step has no evaluation; ready to advance").
    """
    from apps.tutoring.models import ExitTicketQuestion
    from apps.curriculum.models import LessonStep

    lesson = getattr(session, 'lesson', None)
    if lesson is None:
        return False

    current_idx = session.current_step_index or 0
    step = (
        LessonStep.objects
        .filter(lesson=lesson, order_index=current_idx)
        .first()
    )
    if step is None:
        return False

    objective = (getattr(step, 'enabling_objective', '') or '').strip()
    if not objective:
        # No objective → no questions strictly tied to this step
        return False

    graded_qids = _graded_question_ids(session)
    return (
        ExitTicketQuestion.objects
        .filter(
            exit_ticket__lesson=lesson,
            enabling_objective=objective,
        )
        .exclude(pk__in=graded_qids)
        .exists()
    )


def _graded_question_ids(session: 'TutorSession') -> set:
    """Set of question pks that already have a recorded verdict on
    this session.
    """
    graded: set = set()
    # iterate only turns with a non-empty judge_outputs JSON
    turns = (
        session.turns
        .exclude(judge_outputs={})
        .values_list('judge_outputs', flat=True)
    )
    for jo in turns:
        if not isinstance(jo, dict):
            continue
        grader = jo.get('grader')
        if isinstance(grader, dict):
            qid = grader.get('question_id')
            if qid:
                graded.add(qid)
    return graded


# ============================================================================
# handle_record_answer — LLM-called: "the student just answered"
# ============================================================================


def handle_record_answer(
    session: 'TutorSession',
    *,
    extracted_answer: str,
) -> dict[str, Any]:
    """Grade the student's extracted answer against
    ``session.current_question_id`` and persist the verdict.

    The LLM does NOT pass question_id — the server already knows from
    current_question_id (set by pick_current_question before the LLM
    call).

    Returns a dict the engine passes back to the LLM as the tool_result:
        {
            'verdict': 'correct' | 'partial' | 'incorrect',
            'confidence': 0.0-1.0,
            'tier': 'mcq' | 'math' | ...,
            'justification': '...',
            'question_id': int | None,
            'recorded': bool,
        }
    """
    from apps.tutoring.simple_tutor.grader import grade_answer, Verdict
    from apps.tutoring.models import ExitTicketQuestion

    qid = getattr(session, 'current_question_id', None)
    if qid is None:
        # Server didn't set a current question — LLM is calling
        # record_answer in the wrong context. Don't crash; return a
        # benign error result so the LLM can react.
        logger.warning(
            "handle_record_answer called with no current_question_id "
            "set (session=%s, extracted=%r)",
            session.pk, str(extracted_answer)[:60],
        )
        return {
            'recorded': False,
            'error': 'no current question in play; nothing to grade',
        }

    try:
        question = ExitTicketQuestion.objects.get(pk=qid)
    except ExitTicketQuestion.DoesNotExist:
        logger.warning(
            "handle_record_answer: current_question_id=%s no longer "
            "exists (session=%s)",
            qid, session.pk,
        )
        # Clear the stale pointer so the next turn doesn't repeat
        session.current_question_id = None
        session.save(update_fields=['current_question_id'])
        return {
            'recorded': False,
            'error': f'question_id {qid} not found',
        }

    try:
        result = grade_answer(question=question, student_answer=extracted_answer)
    except Exception as exc:
        msg = str(exc).strip().replace('\n', ' ')[:200]
        logger.warning(
            "handle_record_answer: grader raised %s: %s (qid=%s, session=%s)",
            type(exc).__name__, msg, qid, session.pk,
        )
        return {
            'recorded': False,
            'error': f'grader exception {type(exc).__name__}: {msg}',
        }

    # Stamp the verdict + the question_id it applies to. The verdict
    # gets persisted on the CURRENT tutor turn (the one being built by
    # the engine main loop). Persistence happens in M9 — here we just
    # return the data.
    return {
        'recorded': True,
        'question_id': qid,
        **result.to_dict(),
    }


# ============================================================================
# handle_request_figure — LLM-called: "show this figure inline"
# ============================================================================


def handle_request_figure(
    session: 'TutorSession',
    *,
    figure_id: int,
) -> dict[str, Any]:
    """Look up a pre-generated figure by id, validate it belongs to the
    current step, and return the URL + alt text for inline rendering.

    Invalid id (not in the step's StepMedia catalog) returns an error
    dict — never raises. The engine still renders the LLM's text
    response, just without the figure.
    """
    from apps.curriculum.models import LessonStep
    try:
        from apps.media_library.models import StepMedia
    except ImportError:
        # Some test envs may not have media_library — return a
        # placeholder error rather than crashing.
        return {
            'displayed': False,
            'error': 'StepMedia model unavailable',
        }

    # Validate id is in the CURRENT step's media catalog
    current_step_index = getattr(session, 'current_step_index', 0) or 0
    lesson = getattr(session, 'lesson', None)
    step = None
    if lesson is not None:
        step = (
            LessonStep.objects
            .filter(lesson=lesson, order_index=current_step_index)
            .first()
        )

    try:
        media = StepMedia.objects.get(pk=figure_id)
    except (StepMedia.DoesNotExist, ValueError):
        logger.warning(
            "handle_request_figure: figure_id=%s not found (session=%s)",
            figure_id, session.pk,
        )
        return {
            'displayed': False,
            'error': f'figure_id {figure_id} not in catalog',
        }

    if step is not None and getattr(media, 'lesson_step_id', None) != step.pk:
        # LLM picked a figure from a different step
        logger.warning(
            "handle_request_figure: figure_id=%s belongs to step %s "
            "but current step is %s (session=%s)",
            figure_id, getattr(media, 'lesson_step_id', None),
            step.pk, session.pk,
        )
        return {
            'displayed': False,
            'error': (
                f'figure_id {figure_id} not on current step '
                f'(belongs to step {getattr(media, "lesson_step_id", None)})'
            ),
        }

    return {
        'displayed': True,
        'figure_id': figure_id,
        'url': getattr(media, 'url', '') or '',
        'alt_text': getattr(media, 'alt_text', '') or '',
        'caption': getattr(media, 'caption', '') or '',
    }


# ============================================================================
# handle_redirect_off_topic — soft moderation flag
# ============================================================================


def handle_redirect_off_topic(
    session: 'TutorSession',
    *,
    reason: str = '',
) -> dict[str, Any]:
    """Increment the off-topic counter on the session's engine_state.

    Purely a signal for analytics; does not block the conversation.
    """
    es = getattr(session, 'engine_state', None) or {}
    if not isinstance(es, dict):
        es = {}
    count = int(es.get('off_topic_count') or 0) + 1
    es['off_topic_count'] = count
    es['last_off_topic_reason'] = (reason or '')[:200]

    session.engine_state = es
    session.save(update_fields=['engine_state'])

    logger.info(
        "off_topic count=%s session=%s reason=%r",
        count, session.pk, reason[:80],
    )
    return {
        'recorded': True,
        'off_topic_count': count,
    }


# ============================================================================
# handle_advance_step — soft hint from LLM ("ready to move on")
# ============================================================================


def handle_advance_step(
    session: 'TutorSession',
    *,
    reason: str = '',
) -> dict[str, Any]:
    """Soft-hint handler: the LLM thinks the student is ready for the
    next step. We honour the hint by moving ``current_step_index`` and
    clearing ``current_question_id`` — but the engine ALSO calls
    ``maybe_advance_step`` automatically after every turn, so the LLM
    forgetting this tool isn't fatal.

    Records the LLM's reason on the session's engine_state for analytics.
    """
    from apps.curriculum.models import LessonStep

    lesson = getattr(session, 'lesson', None)
    if lesson is None:
        return {'advanced': False, 'error': 'no lesson on session'}

    # Find next step's order_index
    next_idx = (session.current_step_index or 0) + 1
    next_step = (
        LessonStep.objects
        .filter(lesson=lesson, order_index=next_idx)
        .first()
    )

    es = getattr(session, 'engine_state', None) or {}
    if not isinstance(es, dict):
        es = {}
    hints = es.get('advance_step_hints') or []
    hints.append({
        'from_step_index': session.current_step_index or 0,
        'reason': (reason or '')[:200],
    })
    es['advance_step_hints'] = hints[-20:]   # keep last 20

    session.current_step_index = next_idx
    session.current_question_id = None
    session.engine_state = es
    session.save(update_fields=[
        'current_step_index', 'current_question_id', 'engine_state',
    ])

    logger.info(
        "handle_advance_step: session=%s advanced to step_index=%s "
        "next_step=%s reason=%r",
        session.pk, next_idx,
        getattr(next_step, 'pk', None), (reason or '')[:80],
    )
    return {
        'advanced': True,
        'new_step_index': next_idx,
        'has_next_step': next_step is not None,
    }


# ============================================================================
# auto_grade_if_missed — safety net for LLM that skipped record_answer
# ============================================================================


_LIKELY_QUESTION_PATTERNS = (
    re.compile(r'^\s*what\b', re.IGNORECASE),
    re.compile(r'^\s*why\b', re.IGNORECASE),
    re.compile(r'^\s*how\b', re.IGNORECASE),
    re.compile(r'^\s*can you\b', re.IGNORECASE),
    re.compile(r'^\s*explain\b', re.IGNORECASE),
    re.compile(r'^\s*tell me\b', re.IGNORECASE),
    re.compile(r'\?\s*$'),
)


def _looks_like_answer_attempt(text: str) -> bool:
    """Heuristic: is the student's text an attempt at an answer (vs a
    clarifying question)?

    Conservative — false-positives auto-grade something the student
    didn't intend as an answer. We err on the side of NOT auto-grading
    (return False) when uncertain.

    Returns True when:
      - Text is short (< 200 chars) AND doesn't end in '?'
      - Text doesn't start with what/why/how/explain/tell-me/can-you
    Returns False when:
      - Empty / whitespace-only
      - Starts with question-word pattern
      - Ends with '?'
    """
    if not text or not text.strip():
        return False
    if len(text) > 500:
        # Long student responses are more likely clarifications or
        # discussion — defer to LLM judgement (via record_answer).
        return False
    for pat in _LIKELY_QUESTION_PATTERNS:
        if pat.search(text):
            return False
    return True


def auto_grade_if_missed(
    session: 'TutorSession',
    student_input: str,
    llm_called_record_answer: bool,
):
    """Auto-grade the student's input when the LLM skipped
    ``record_answer`` but there's still a question in play.

    Returns:
        ``GradeResult`` when auto-grading fired. Caller persists this
            with ``tier='auto_fallback'`` so analytics can distinguish.
        ``None`` when no grading was triggered (either the LLM handled
            it, or the input doesn't look like an answer, or no
            current question is set).
    """
    if llm_called_record_answer:
        return None

    if session.current_question_id is None:
        # No question in play — nothing to grade against
        return None

    if not _looks_like_answer_attempt(student_input):
        return None

    from apps.tutoring.models import ExitTicketQuestion
    from apps.tutoring.simple_tutor.grader import grade_answer

    try:
        question = ExitTicketQuestion.objects.get(pk=session.current_question_id)
    except ExitTicketQuestion.DoesNotExist:
        return None

    try:
        result = grade_answer(question=question, student_answer=student_input)
    except Exception as exc:
        logger.warning(
            "auto_grade_if_missed: grader raised %s: %s",
            type(exc).__name__, str(exc)[:120],
        )
        return None

    # Mark this result as auto-fallback (engine persists; we just
    # return the result with a tier override in justification).
    logger.info(
        "auto_grade_if_missed: graded session=%s qid=%s verdict=%s "
        "(tier=auto_fallback)",
        session.pk, session.current_question_id, result.verdict.value,
    )

    # Return a new GradeResult with tier='auto_fallback' so the engine
    # persists it correctly. dataclasses are frozen → replace.
    from dataclasses import replace
    return replace(
        result,
        tier='auto_fallback',
        justification=(
            f'[auto-fallback: LLM did not call record_answer] '
            f'{result.justification}'
        )[:300],
    )


# ============================================================================
# maybe_advance_step — server auto-advances the lesson step
# ============================================================================


def maybe_advance_step(
    session: 'TutorSession',
    *,
    turn_cap: int = DEFAULT_STEP_TURN_CAP,
) -> bool:
    """Soft, automatic step advancement called after each engine turn.

    Two triggers (in order):

    1. **Objective complete** — all questions for the current step's
       ``enabling_objective`` have a recorded verdict. The step's
       evaluation is done, so move on.

    2. **Soft turn cap** — student has had ``turn_cap`` turns on the
       same step without the LLM hinting via ``advance_step``. Move on
       anyway with ``forced=True`` logged. Prevents the tutor from
       getting stuck on a step forever.

    The LLM's own ``advance_step`` tool call (M8 ``handle_advance_step``)
    is the FAST path — this function is the SAFETY NET for when the
    LLM doesn't call it.

    Returns True if the session moved.
    """
    from apps.curriculum.models import LessonStep

    lesson = getattr(session, 'lesson', None)
    if lesson is None:
        return False

    current_idx = session.current_step_index or 0

    # Idempotency guard — if there's no current LessonStep at this
    # index (we're already past the end), don't advance further.
    current_step = (
        LessonStep.objects
        .filter(lesson=lesson, order_index=current_idx)
        .first()
    )
    if current_step is None:
        return False

    # Trigger 1 — current step's objective-matching questions are
    # all graded (STRICT check via _current_step_questions_remaining,
    # which does NOT fall back to lesson-wide pool).
    forced = False
    if _current_step_questions_remaining(session):
        # Still have ungraded objective-matching questions — check the
        # soft turn cap. Count student turns on the CURRENT step using
        # the SessionTurn.step FK.
        try:
            this_step_turns = session.turns.filter(
                step=current_step, role='student',
            ).count()
            if this_step_turns < turn_cap:
                return False
            forced = True
        except Exception:
            return False

    # Compute next step index
    next_idx = current_idx + 1
    next_step = (
        LessonStep.objects
        .filter(lesson=lesson, order_index=next_idx)
        .first()
    )

    session.current_step_index = next_idx
    session.current_question_id = None
    if forced:
        # Record on engine_state so analytics can spot stuck steps.
        es = getattr(session, 'engine_state', None) or {}
        if not isinstance(es, dict):
            es = {}
        forced_log = es.get('forced_advances') or []
        forced_log.append({
            'from_step_index': current_idx,
            'reason': f'turn_cap={turn_cap} exceeded',
        })
        es['forced_advances'] = forced_log[-20:]
        session.engine_state = es
        session.save(update_fields=[
            'current_step_index', 'current_question_id', 'engine_state',
        ])
    else:
        session.save(
            update_fields=['current_step_index', 'current_question_id'],
        )

    logger.info(
        "maybe_advance_step: session=%s advanced to step_index=%s "
        "(next_step=%s forced=%s)",
        session.pk, next_idx,
        getattr(next_step, 'pk', None), forced,
    )
    return True
