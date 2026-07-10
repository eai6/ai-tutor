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


# Tutoring-question allowlist. Controlled by the TUTORING_QUESTION_TYPES
# env var (default: 'mcq'). The 2026-05-28 staging E2E surfaced the
# failure mode this gates: short_answer questions accumulate `partial`
# verdicts that don't trigger competence advance, leaving the student
# stuck on one step for 5+ turns. MCQ verdicts are deterministic
# (`correct` / `incorrect` via letter match), so the advance trigger
# fires cleanly on the first correct attempt.
#
# Set TUTORING_QUESTION_TYPES='mcq,short_numeric,short_answer' to
# restore the old broader behaviour.
def _allowed_tutoring_types() -> tuple[str, ...]:
    import os
    raw = (os.environ.get('TUTORING_QUESTION_TYPES') or 'mcq').strip().lower()
    return tuple(
        t.strip() for t in raw.split(',')
        if t.strip() in {'mcq', 'short_numeric', 'short_answer'}
    ) or ('mcq',)

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

    # Drop questions whose text has already been graded in this session.
    # Without this, the LLM keeps seeing the just-answered question in
    # the pool and references it when hinting on a NEW question. Caught
    # 2026-05-26 in M11.3 E2E.
    graded_texts = _previously_graded_question_texts(session)

    def _is_already_graded(q) -> bool:
        qtext = (getattr(q, 'question_text', '') or '').strip().lower()
        return bool(qtext) and qtext in graded_texts

    allowed_types = _allowed_tutoring_types()
    pool: list = []

    # Source 1 — LessonStep.question (one entry max). StepQuestion
    # produces short_numeric or short_answer; skip when the allowlist
    # rejects those (e.g. MCQ-only mode).
    if step_has_question(step):
        sq = StepQuestion.from_step(step)
        if (
            getattr(sq, 'question_type', '') in allowed_types
            and not _is_already_graded(sq)
        ):
            pool.append(sq)

    # Source 2 — ETQs matching this step's enabling_objective.
    objective = (getattr(step, 'enabling_objective', '') or '').strip()
    if objective and len(pool) < max_questions:
        for q in (
            ExitTicketQuestion.objects
            .filter(
                exit_ticket__lesson=lesson,
                enabling_objective=objective,
                question_type__in=allowed_types,
            )
            .order_by('order_index', 'id')
        ):
            if _is_already_graded(q):
                continue
            pool.append(q)
            if len(pool) >= max_questions:
                break

    # Source 3 — ANY allowed ETQ on the lesson (fills the rest).
    if len(pool) < max_questions:
        for q in (
            ExitTicketQuestion.objects
            .filter(
                exit_ticket__lesson=lesson,
                question_type__in=allowed_types,
            )
            .exclude(pk__in=[
                getattr(p, 'pk', None) for p in pool
                if getattr(p, 'source', '') != 'lesson_step'
            ])
            .order_by('order_index', 'id')
        ):
            if _is_already_graded(q):
                continue
            pool.append(q)
            if len(pool) >= max_questions:
                break

    return pool


def _previously_graded_question_texts(session: 'TutorSession') -> set[str]:
    """Set of normalised question_text values that already have a
    recorded grader verdict on this session. Used by build_question_pool
    to drop questions the student has already worked through.
    """
    texts: set[str] = set()
    for jo in (
        session.turns
        .exclude(judge_outputs={})
        .values_list('judge_outputs', flat=True)
    ):
        if not isinstance(jo, dict):
            continue
        grader = jo.get('grader')
        if not isinstance(grader, dict):
            continue
        q = (grader.get('question_text') or '').strip().lower()
        if q:
            texts.add(q)
    return texts


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


# ============================================================================
# handle_pose_question — LLM-called: "I want to ask this question next"
# ============================================================================


def handle_pose_question(
    session: 'TutorSession',
    *,
    question_text: str,
    question_type: str,
    reference_answer: str,
    source: str,
    options: list | None = None,
    catalog_question_id: int | None = None,
) -> dict[str, Any]:
    """Persist the LLM's question as the session's in-flight question.

    Writes (or replaces) the InFlightQuestion row. When source=catalog
    and catalog_question_id is provided, cross-checks the LLM's
    reference_answer against the catalog's correct_answer and logs a
    warning on mismatch (but still uses the LLM's reference — catalog
    content has had errors in pilot data).

    Returns a dict the engine passes back as the tool_result:
        {
            'posed': True,
            'question_type': str,
            'catalog_mismatch': bool,   # only when source=catalog
        }

    Never raises — bad input returns a posed=False error dict so the
    conversation flow doesn't break.
    """
    from apps.tutoring.models import InFlightQuestion, ExitTicketQuestion

    qtext = (question_text or '').strip()
    ref = (reference_answer or '').strip()
    qtype = (question_type or '').strip().lower()
    src = (source or '').strip().lower()

    if not qtext or not ref:
        return {
            'posed': False,
            'error': 'question_text and reference_answer are required',
        }
    if qtype not in ('mcq', 'short_numeric', 'short_answer'):
        logger.warning(
            "handle_pose_question: unsupported question_type=%r — "
            "falling back to short_answer",
            qtype,
        )
        qtype = 'short_answer'
    if src not in ('catalog', 'inline_authored'):
        logger.warning(
            "handle_pose_question: unsupported source=%r — "
            "falling back to inline_authored",
            src,
        )
        src = 'inline_authored'

    # Normalise options — must be a list of strings.
    opts = options or []
    if not isinstance(opts, list):
        opts = []
    opts = [str(o)[:300] for o in opts]

    # Replace any prior in-flight question (analytics-log the orphan).
    prior = InFlightQuestion.objects.filter(session=session).first()
    if prior is not None:
        logger.info(
            "[simple_tutor] orphan_in_flight session=%s prior_type=%s "
            "attempts=%s — replaced before grading",
            session.pk, prior.question_type, prior.attempt_count,
        )
        # Mark on engine_state for downstream analytics.
        es = getattr(session, 'engine_state', None) or {}
        if isinstance(es, dict):
            orphans = es.get('orphan_questions') or []
            orphans.append({
                'question_text': prior.question_text[:200],
                'question_type': prior.question_type,
                'attempt_count': prior.attempt_count,
            })
            es['orphan_questions'] = orphans[-20:]   # keep last 20
            session.engine_state = es
            session.save(update_fields=['engine_state'])
        prior.delete()

    # Catalog cross-check (advisory — does NOT override LLM's reference).
    catalog_mismatch = False
    if src == 'catalog' and isinstance(catalog_question_id, int):
        try:
            cat_q = ExitTicketQuestion.objects.filter(
                pk=catalog_question_id,
            ).first()
            if cat_q is not None:
                cat_correct = (cat_q.correct_answer or '').strip()
                if cat_correct and cat_correct.lower() != ref.lower():
                    catalog_mismatch = True
                    logger.warning(
                        "[simple_tutor] catalog_mismatch session=%s "
                        "catalog_q=%s catalog_correct=%r llm_ref=%r "
                        "— using LLM ref; flag for content review",
                        session.pk, catalog_question_id,
                        cat_correct, ref,
                    )
        except Exception as exc:
            logger.warning(
                "handle_pose_question: catalog lookup failed for "
                "id=%s: %s",
                catalog_question_id, exc,
            )

    # Resolve the tutor turn the pose was made on — engine sets this
    # via session.engine_state['_pose_turn_id'] if it knows the turn,
    # but we tolerate the slot being null on first-write.
    es = getattr(session, 'engine_state', None) or {}
    posed_at_turn_id = (
        es.get('_current_turn_id') if isinstance(es, dict) else None
    )

    new = InFlightQuestion.objects.create(
        session=session,
        question_text=qtext,
        question_type=qtype,
        options=opts,
        reference_answer=ref,
        source=src,
        catalog_question_id=catalog_question_id if isinstance(catalog_question_id, int) else None,
        posed_at_turn_id=posed_at_turn_id,
    )

    logger.info(
        "[simple_tutor] posed session=%s type=%s source=%s "
        "catalog_id=%s mismatch=%s",
        session.pk, qtype, src,
        catalog_question_id, catalog_mismatch,
    )

    return {
        'posed': True,
        'question_type': qtype,
        'source': src,
        'catalog_mismatch': catalog_mismatch,
    }


# ============================================================================
# handle_record_answer — LLM-called: "the student just answered"
# ============================================================================


def handle_record_answer(
    session: 'TutorSession',
    *,
    extracted_answer: str,
) -> dict[str, Any]:
    """Grade the student's extracted answer against the persisted
    in-flight question.

    M12 architecture: the tutor LLM no longer supplies the reference
    on every grade call. The reference + question_type + question_text
    were written to ``InFlightQuestion`` at the moment of posing (via
    ``handle_pose_question``). The grader reads from that slot.

    This eliminates the M11.3 failure mode where the LLM mis-identified
    which question was in flight when one turn mixed praise + new
    question, causing it to pass the wrong reference.

    Args:
        session: TutorSession.
        extracted_answer: the student's literal answer text
            (LLM-extracted from the conversation).

    Returns:
        ``{'recorded': bool, 'verdict': ..., 'confidence': ..., 'tier': ...,
           'justification': ..., 'question_type': ..., 'reference_answer': ...,
           'question_text': ...}``

        When no in-flight question exists, ``recorded=False`` and the
        caller should treat the student's message as a clarification
        rather than an answer.
    """
    from apps.tutoring.models import InFlightQuestion
    from apps.tutoring.simple_tutor.grader import grade_answer

    extracted = (extracted_answer or '').strip()
    if not extracted:
        return {
            'recorded': False,
            'error': 'extracted_answer is empty',
        }

    in_flight = InFlightQuestion.objects.filter(session=session).first()
    if in_flight is None:
        # No question in flight — LLM called record_answer in the
        # wrong context. Return a soft error so the conversation
        # continues; the LLM can react in Call 2.
        logger.warning(
            "[simple_tutor] record_answer without in-flight question "
            "session=%s extracted=%r",
            session.pk, extracted[:60],
        )
        return {
            'recorded': False,
            'error': 'no in-flight question — student input was not an answer',
        }

    # Instrumentation only (no behaviour change): a STALE slot is one the tutor
    # has spoken past — it posed the question, then produced one or more further
    # replies without the student answering it. Grading the student's answer
    # against a question two tutor turns old is how a correct answer gets marked
    # wrong, and it is the one branch of the desync cascade the logs could not
    # previously distinguish from a healthy grade. Measure it before changing
    # anything: `grep 'grading a STALE slot'` over the next sweep tells us
    # whether this is real or rare.
    if in_flight.posed_at_turn_id:
        try:
            tutor_turns_since = session.turns.filter(
                role='tutor', id__gt=in_flight.posed_at_turn_id,
            ).count()
        except Exception:
            tutor_turns_since = 0
        if tutor_turns_since >= 1:
            logger.warning(
                "[simple_tutor] grading a STALE slot session=%s "
                "tutor_turns_since_posed=%d question=%r extracted=%r",
                session.pk, tutor_turns_since,
                (in_flight.question_text or '')[:60], extracted[:40],
            )

    # Build the transient question surface the grader expects.
    question = _TransientQuestion(
        question_text=in_flight.question_text,
        question_type=in_flight.question_type,
        reference_answer=in_flight.reference_answer,
        options=in_flight.options or [],
    )

    try:
        result = grade_answer(question=question, student_answer=extracted)
    except Exception as exc:
        msg = str(exc).strip().replace('\n', ' ')[:200]
        logger.warning(
            "handle_record_answer: grader raised %s: %s "
            "(qtype=%s, session=%s)",
            type(exc).__name__, msg, in_flight.question_type, session.pk,
        )
        return {
            'recorded': False,
            'error': f'grader exception {type(exc).__name__}: {msg}',
        }

    verdict = result.verdict.value

    # Snapshot the in-flight question state BEFORE we mutate it; the
    # result payload echoes question_text + reference for audit.
    snapshot = {
        'question_text': in_flight.question_text[:300],
        'reference_answer': in_flight.reference_answer,
        'question_type': in_flight.question_type,
        'attempt_count_before': in_flight.attempt_count,
    }

    if verdict == 'correct':
        # Slot is resolved — clear it. The hint ladder + analytics
        # state is preserved in the grader result + session turns.
        in_flight.delete()
    else:
        # Increment attempt counter for the hint ladder.
        in_flight.attempt_count = (in_flight.attempt_count or 0) + 1
        in_flight.save(update_fields=['attempt_count'])

    logger.info(
        "[simple_tutor] graded session=%s qtype=%s verdict=%s "
        "ref=%r ext=%r attempts=%s",
        session.pk, snapshot['question_type'], verdict,
        snapshot['reference_answer'][:40], extracted[:40],
        snapshot['attempt_count_before'] + (0 if verdict == 'correct' else 1),
    )

    return {
        'recorded': True,
        **snapshot,
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
        options: list | None = None,
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
        # MCQ options: passed in by pose_question (M12). The grader's
        # _grade_mcq does letter match against correct_answer; the
        # option text is useful when full-option-text matching is
        # needed (e.g. student typed "alpha" instead of "A").
        opts = options or []
        self.option_a = opts[0] if len(opts) > 0 else ''
        self.option_b = opts[1] if len(opts) > 1 else ''
        self.option_c = opts[2] if len(opts) > 2 else ''
        self.option_d = opts[3] if len(opts) > 3 else ''


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

    # Remediation completion signal: when the LLM calls advance_step
    # AFTER the student has submitted (and failed) the exit ticket,
    # treat it as "remediation is done — re-fire the exit ticket so
    # the student can re-take it." respond_for_view reads this flag
    # to bypass the already_submitted guard and re-open the modal.
    from apps.tutoring.models import ExitTicketAttempt
    has_failed_attempt = ExitTicketAttempt.objects.filter(
        session=session, completed_at__isnull=False, passed=False,
    ).exists()
    if has_failed_attempt:
        es['remediation_complete'] = True

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
