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
    - maybe_advance_step(session)       → soft auto-advance when current
                                         step's questions are all graded,
                                         OR after a soft turn cap

    NOTE: The auto_grade_if_missed safety net was REMOVED 2026-05-26.
    Its heuristic over-fired on conversational continuations like
    "yes let's go deeper", polluting the graded set and corrupting the
    next pick. We now trust the LLM — if it doesn't call record_answer,
    no grade is recorded for that turn.

Step ↔ question linkage uses ``enabling_objective`` (string field on both
LessonStep and ExitTicketQuestion) — see
``auto-memory/feedback_step_question_linkage.md``. No schema change needed.

All handlers return dicts (NEVER raise on bad input). The conversation
must always flow — failures get logged + return error dicts, never block.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from apps.tutoring.models import TutorSession  # noqa: F401  (typing only)


# Soft turn cap — server auto-advances after this many student turns
# on the same lesson step. Generous default: real Socratic discussion
# can run 6-10 turns before grading lands.
DEFAULT_STEP_TURN_CAP = 8


# ============================================================================
# build_question_pool — gather context questions for the system prompt
# ============================================================================


# ExitTicketQuestion types the tutor should NEVER see in the pool. Per
# user direction (2026-05-26): fill_in_blank and matching are too
# ambiguous to grade from free-form text answers. The tutor sticks to
# MCQ + short_answer + short_numeric (exclusion list keeps the filter
# forward-compatible with new question types).
EXCLUDED_QUESTION_TYPES = ('fill_in_blank', 'matching')

# Cap on the number of pool entries surfaced to the LLM each turn.
# Keeps the prompt focused + cache-friendly.
DEFAULT_POOL_SIZE = 6


def build_question_pool(
    session: 'TutorSession',
    *,
    max_questions: int = DEFAULT_POOL_SIZE,
) -> list:
    """Gather a small pool of CONTEXT questions for the current step.

    NO server-side anchor — the returned objects are for the LLM's
    grounding only. The LLM is free to pose any of them verbatim,
    adapt one, or write its own question entirely. ``record_answer``
    carries the reference answer the LLM chose.

    Source order:

      1. LessonStep.question (if non-empty) → StepQuestion adapter
      2. ExitTicketQuestion rows on the current step's enabling_objective
      3. ExitTicketQuestion rows from the same lesson (any objective)
         as additional context if pool is still short

    Returns at most ``max_questions`` entries. Empty list when the step
    is pure teaching (no questions anywhere — engine still teaches; LLM
    just doesn't grade this turn).
    """
    from apps.tutoring.models import ExitTicketQuestion
    from apps.curriculum.models import LessonStep
    from apps.tutoring.simple_tutor.step_question import (
        StepQuestion, has_question as step_has_question,
    )

    lesson = getattr(session, 'lesson', None)
    if lesson is None:
        return []

    current_step_index = getattr(session, 'current_step_index', 0) or 0
    step = (
        LessonStep.objects
        .filter(lesson=lesson, order_index=current_step_index)
        .first()
    )
    if step is None:
        return []

    pool: list = []

    # Source 1 — LessonStep.question (one entry max).
    if step_has_question(step):
        pool.append(StepQuestion.from_step(step))

    # Source 2 — ETQs matching this step's enabling_objective.
    objective = (getattr(step, 'enabling_objective', '') or '').strip()
    if objective and len(pool) < max_questions:
        for q in (
            ExitTicketQuestion.objects
            .filter(
                exit_ticket__lesson=lesson,
                enabling_objective=objective,
            )
            .exclude(question_type__in=EXCLUDED_QUESTION_TYPES)
            .order_by('order_index', 'id')[: max_questions - len(pool)]
        ):
            pool.append(q)

    # Source 3 — ANY un-excluded ETQ on the lesson (fills the rest).
    if len(pool) < max_questions:
        for q in (
            ExitTicketQuestion.objects
            .filter(exit_ticket__lesson=lesson)
            .exclude(question_type__in=EXCLUDED_QUESTION_TYPES)
            .exclude(pk__in=[
                getattr(p, 'pk', None) for p in pool
                if getattr(p, 'source', '') != 'lesson_step'
            ])
            .order_by('order_index', 'id')[: max_questions - len(pool)]
        ):
            pool.append(q)

    return pool


# Backwards-compat shim: callers that still import pick_current_question
# get a pool function that returns the first entry, or None. This is a
# stop-gap so we don't have to update every test file in this PR.
def pick_current_question(session: 'TutorSession'):
    """Deprecated 2026-05-26: returns the FIRST entry from
    ``build_question_pool`` to keep older call sites compiling. Real
    pickup logic has been replaced by context-only pool gathering.
    """
    pool = build_question_pool(session, max_questions=1)
    return pool[0] if pool else None


def _current_step_correct_verdict_count(session: 'TutorSession') -> int:
    """Count CORRECT grader verdicts recorded on the CURRENT step.

    Uses the ``SessionTurn.step`` FK (set by ``_persist_tutor_turn``)
    rather than tracking individual question ids — since the LLM is
    now free to author its own questions outside the catalog, there's
    no stable question-id mapping. A "correct verdict on this step"
    just means: some tutor turn on this step had a grader verdict of
    'correct' in its judge_outputs.

    Used by maybe_advance_step as the competence signal. One correct
    answer on the step is enough to advance (per user direction:
    competence isn't "answer everything").
    """
    from apps.curriculum.models import LessonStep

    lesson = getattr(session, 'lesson', None)
    if lesson is None:
        return 0

    current_idx = session.current_step_index or 0
    step = (
        LessonStep.objects
        .filter(lesson=lesson, order_index=current_idx)
        .first()
    )
    if step is None:
        return 0

    count = 0
    for jo in (
        session.turns
        .filter(step=step)
        .exclude(judge_outputs={})
        .values_list('judge_outputs', flat=True)
    ):
        if not isinstance(jo, dict):
            continue
        grader = jo.get('grader')
        if isinstance(grader, dict) and grader.get('verdict') == 'correct':
            count += 1
    return count
    return graded


# ============================================================================
# handle_record_answer — LLM-called: "the student just answered"
# ============================================================================


def handle_record_answer(
    session: 'TutorSession',
    *,
    extracted_answer: str,
    reference_answer: str = '',
    question_type: str = 'short_answer',
    question_text: str = '',
) -> dict[str, Any]:
    """Grade the student's extracted answer against the LLM-provided
    reference. No server-side question anchor — the LLM is the source
    of truth for what was posed (text + correct answer + type).

    Tear-down 2026-05-26 (M11.3 of memory/simple_tutor_engine_milestones.md):
    the deterministic ``current_question_id`` was retired because the
    LLM frequently authored its own questions outside the catalog,
    leaving the server-side anchor out of sync. The grader now operates
    on a transient ``_TransientQuestion`` built from the LLM's tool
    args.

    Args:
        session: TutorSession (used only for audit logging).
        extracted_answer: the student's answer in canonical form
            (LLM-extracted).
        reference_answer: the correct answer the LLM was checking
            against. For MCQ: 'A'/'B'/'C'/'D'. For short_numeric: the
            numeric value (e.g. '150'). For short_answer: one canonical
            phrasing.
        question_type: 'mcq', 'short_numeric', or 'short_answer'.
            Selects the grader tier.
        question_text: the question stem the LLM posed (verbatim) —
            audit-only; the verifier LLM (short_answer Tier 2) reads it
            for context.

    Returns:
        ``{'recorded': bool, 'verdict': ..., 'confidence': ..., 'tier': ...,
           'justification': ..., 'reference_answer': ..., 'question_type': ...}``
    """
    from apps.tutoring.simple_tutor.grader import grade_answer

    extracted = (extracted_answer or '').strip()
    reference = (reference_answer or '').strip()
    qtype = (question_type or '').strip().lower() or 'short_answer'

    if qtype not in ('mcq', 'short_numeric', 'short_answer'):
        # Unknown type — fall back to short_answer (the verifier LLM
        # handles arbitrary text). Logged for analytics.
        logger.warning(
            "handle_record_answer: unsupported question_type=%r — "
            "falling back to short_answer",
            qtype,
        )
        qtype = 'short_answer'

    if not extracted:
        return {
            'recorded': False,
            'error': 'extracted_answer is empty',
        }

    # The grader expects an ExitTicketQuestion-shaped object. Build a
    # transient namespace carrying the LLM-provided fields in the same
    # surface the grader reads.
    question = _TransientQuestion(
        question_text=(question_text or '').strip(),
        question_type=qtype,
        reference_answer=reference,
    )

    try:
        result = grade_answer(question=question, student_answer=extracted)
    except Exception as exc:
        msg = str(exc).strip().replace('\n', ' ')[:200]
        logger.warning(
            "handle_record_answer: grader raised %s: %s "
            "(qtype=%s, session=%s)",
            type(exc).__name__, msg, qtype, session.pk,
        )
        return {
            'recorded': False,
            'error': f'grader exception {type(exc).__name__}: {msg}',
        }

    logger.info(
        "[simple_tutor] graded session=%s qtype=%s verdict=%s "
        "ref=%r ext=%r",
        session.pk, qtype, result.verdict.value,
        reference[:40], extracted[:40],
    )

    return {
        'recorded': True,
        'question_type': qtype,
        'reference_answer': reference,
        'question_text': (question_text or '').strip()[:300],
        **result.to_dict(),
    }


class _TransientQuestion:
    """ExitTicketQuestion-shaped duck for the grader. Built per tool
    call from the LLM's record_answer args — no DB row, no persistence.

    Exposes the attribute surface the grader functions read:
    ``question_type``, ``question_text``, ``correct_answer``,
    ``answer_data``, ``option_a..d``, ``pk``.
    """
    __slots__ = (
        'question_text', 'question_type', 'correct_answer',
        'answer_data', 'option_a', 'option_b', 'option_c', 'option_d',
        'pk',
    )

    def __init__(
        self,
        *,
        question_text: str,
        question_type: str,
        reference_answer: str,
    ):
        self.pk = None
        self.question_text = question_text
        self.question_type = question_type
        self.correct_answer = reference_answer
        # answer_data shape mirrors what the math grader expects:
        # {'model_answer': str, 'computed': float | None}
        ad: dict[str, Any] = {'model_answer': reference_answer}
        if question_type == 'short_numeric':
            try:
                # Strip optional unit suffix (e.g. "150°", "1000 cm")
                import re as _re
                m = _re.match(r'\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)', reference_answer)
                if m:
                    ad['computed'] = float(m.group(1))
            except (ValueError, TypeError):
                pass
        self.answer_data = ad
        # MCQ options aren't passed via the tool args — the grader's
        # _grade_mcq only reads correct_answer (a letter), so empty
        # options are fine.
        self.option_a = self.option_b = self.option_c = self.option_d = ''


# ============================================================================
# handle_request_figure — LLM-called: "show this figure inline"
# ============================================================================


def handle_request_figure(
    session: 'TutorSession',
    *,
    figure_id: int,
    figure_catalog: list[dict] | None = None,
) -> dict[str, Any]:
    """Validate a figure id against the catalog the engine built for
    this turn and return the URL + alt text for inline rendering.

    The figure_catalog is the same list the engine passes to
    ``build_system_prompt`` — entries shaped as
    ``{'id': int, 'description': str, 'url': str, 'alt_text': str, 'caption': str}``.
    IDs are synthesised by the engine from the index of each image in
    ``LessonStep.media['images']`` (1-based; stable within a step).

    Invalid id (not in the catalog) returns an error dict — never raises.
    When figures are disabled on the course
    (``course.tutoring_images_enabled=False``), returns an error dict
    even if the LLM somehow has the tool available (cached prompt race).
    """
    # First defense: figures disabled on the course
    lesson = getattr(session, 'lesson', None)
    course = (
        getattr(getattr(lesson, 'unit', None), 'course', None)
        if lesson is not None else None
    )
    images_enabled = getattr(course, 'tutoring_images_enabled', True)
    if not images_enabled:
        logger.warning(
            "handle_request_figure: figures disabled on course "
            "(session=%s figure_id=%s) — refusing",
            session.pk, figure_id,
        )
        return {
            'displayed': False,
            'error': 'figures are disabled for this lesson',
        }

    if not figure_catalog:
        return {
            'displayed': False,
            'error': f'figure_id {figure_id} not in catalog '
                     f'(no figures available on this step)',
        }

    for fig in figure_catalog:
        if fig.get('id') == figure_id:
            return {
                'displayed': True,
                'figure_id': figure_id,
                'url': fig.get('url') or '',
                'alt_text': fig.get('alt_text') or fig.get('alt') or '',
                'caption': fig.get('caption') or '',
            }

    logger.warning(
        "handle_request_figure: figure_id=%s not in catalog "
        "(session=%s, catalog_size=%s)",
        figure_id, session.pk, len(figure_catalog),
    )
    return {
        'displayed': False,
        'error': f'figure_id {figure_id} not in catalog',
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
    session.engine_state = es
    session.save(update_fields=[
        'current_step_index', 'engine_state',
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
# maybe_advance_step — server auto-advances the lesson step
# ============================================================================


DEFAULT_COMPETENCE_THRESHOLD = 1   # number of CORRECT verdicts to call it


def maybe_advance_step(
    session: 'TutorSession',
    *,
    turn_cap: int = DEFAULT_STEP_TURN_CAP,
    competence_threshold: int = DEFAULT_COMPETENCE_THRESHOLD,
) -> bool:
    """Soft, automatic step advancement called after each engine turn.

    The PRIMARY signal is the LLM's ``advance_step(reason)`` tool call
    (handled by ``handle_advance_step``). This function only fires the
    SAFETY NETS for when the LLM never calls it:

    1. **Competence demonstrated** — the student has at least
       ``competence_threshold`` CORRECT verdicts on the current step's
       objective (default 1). Per user direction: a step doesn't require
       ALL questions answered — 1-2 correct attempts can be enough.
       The engine advances regardless.

    2. **Soft turn cap** — student has had ``turn_cap`` turns on the
       current step regardless of verdicts. Force-advance with
       ``forced=True`` logged. Prevents the tutor from getting stuck on
       a step forever.

    Both triggers respect idempotency (won't advance past the last
    step).

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

    forced = False

    # Trigger 1 — competence demonstrated. Threshold is per-call (LOW
    # default of 1 correct verdict; engine/caller can raise per-step).
    correct_count = _current_step_correct_verdict_count(session)
    if correct_count >= competence_threshold:
        pass   # advance
    else:
        # Trigger 2 — soft turn cap. Count student turns on the CURRENT
        # step via the SessionTurn.step FK.
        try:
            this_step_turns = session.turns.filter(
                step=current_step, role='student',
            ).count()
        except Exception:
            return False
        if this_step_turns < turn_cap:
            return False
        forced = True

    # Compute next step index
    next_idx = current_idx + 1
    next_step = (
        LessonStep.objects
        .filter(lesson=lesson, order_index=next_idx)
        .first()
    )

    session.current_step_index = next_idx
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
        session.save(update_fields=['current_step_index', 'engine_state'])
    else:
        session.save(update_fields=['current_step_index'])

    logger.info(
        "maybe_advance_step: session=%s advanced to step_index=%s "
        "(next_step=%s forced=%s)",
        session.pk, next_idx,
        getattr(next_step, 'pk', None), forced,
    )
    return True
