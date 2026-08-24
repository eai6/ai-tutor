"""Server-side tool handlers + flow primitives for the simple-tutor engine.

4-tool design (revised 2026-05-26 per
auto-memory/feedback_server_owns_question_state.md):

  LLM-called tools (all soft / advisory — engine never blocks on them):
    - handle_record_answer  — student gave an answer; grade it
    - handle_request_figure — display a figure inline (only offered when the
                              course enables figures)

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
import random
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ai_tutor.apps.tutoring.models import TutorSession  # noqa: F401  (typing only)


# Soft turn cap — server auto-advances after this many student turns
# on the same lesson step. Generous default: real Socratic discussion
# can run 6-10 turns before grading lands.
DEFAULT_STEP_TURN_CAP = 8


# ── Anti-repetition (breaks the content-repetition deadlock) ─────────────────
# The multi-turn fix-check surfaced a failure mode the protocol fixes don't
# touch: a model resolves a question, then re-poses the SAME question — the
# student answers it again ("wait, that's the same question, right?"), the tutor
# re-poses again, and the session deadlocks without the lesson advancing
# (qwen3-next-80b-instruct deadlocked 5/5 this way). This is the REPEATS failure
# mode, not a tool-protocol one. Guard: remember questions graded CORRECT, and
# when the model poses an EXACT re-ask of one, force the lesson forward after two
# in a row so the loop cannot persist. Exact-normalised match only — a genuinely
# new-but-similar question ("now try FOUR angles") must NOT trip this.
# Toggle SIMPLE_TUTOR_ANTIREPEAT=0 disables it (sweep isolation).
_ANSWERED_CORRECT_CAP = 40      # remember at most this many resolved questions
_REPEAT_STREAK_TO_ADVANCE = 2   # force-advance after this many exact re-asks in a row


def _norm_q(text: str) -> str:
    """Normalise a question stem for exact-repeat comparison: lowercase, drop
    non-alphanumerics, collapse whitespace. Deliberately strict — it must match
    a verbatim re-ask but not a reworded or numerically-different question."""
    import re
    return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()


def _antirepeat_enabled() -> bool:
    import os
    return os.getenv('SIMPLE_TUTOR_ANTIREPEAT', '1').strip() != '0'


def _norm_optset(options) -> str | None:
    """Order-insensitive signature of an MCQ's option set. A re-ask with a
    reworded stem but the SAME options is the same question (gemma_probe5_v2:
    12b re-asked an answered MCQ three times, reworded past the verbatim
    stem guard); a variant with changed numbers has different options and
    stays allowed."""
    from ai_tutor.apps.tutoring.simple_tutor.grader import _norm_option
    opts = [_norm_option(str(o)) for o in (options or []) if str(o).strip()]
    if len(opts) < 2:
        return None
    return 'optset:' + '|'.join(sorted(opts))


def _record_answered_correct(
    session: 'TutorSession', question_text: str, options=None,
) -> None:
    """Remember a question the student just answered correctly, so a later
    re-ask can be detected — by exact stem, and for MCQs by option set.
    Guarded read-copy-mutate-save on engine_state."""
    key = _norm_q(question_text)
    if not key:
        return
    es = getattr(session, 'engine_state', None) or {}
    if not isinstance(es, dict):
        es = {}
    answered = es.get('answered_correct')
    if not isinstance(answered, list):
        answered = []
    if key not in answered:
        answered.append(key)
    okey = _norm_optset(options)
    if okey and okey not in answered:
        answered.append(okey)
    es['answered_correct'] = answered[-_ANSWERED_CORRECT_CAP:]
    # A fresh correct answer ends any repeat streak.
    es['repeat_pose_streak'] = 0
    session.engine_state = es
    try:
        session.save(update_fields=['engine_state'])
    except Exception:
        logger.warning("[simple_tutor] could not persist answered_correct "
                       "session=%s", getattr(session, 'pk', None))


# How many recent poses to remember when detecting a loop. Must be > 2: the
# observed failure was the model ALTERNATING between two questions, so
# comparing only against the immediately previous pose never matches.
_POSE_HISTORY_WINDOW = 6


def _note_pose_repetition(
    session: 'TutorSession', question_text: str, options=None,
    question_type: str = '',
) -> bool:
    """Called on every pose. Returns True and sets a pending-advance flag when
    the model is looping — the signal to force the lesson forward.

    Two independent repeat signals, because they fail in different ways:

    1. **Already answered correctly** — the pose matches something in
       ``answered_correct``. The stronger signal, but it requires a grader
       verdict, which requires the model to have called ``record_answer``.
    2. **Recently posed** — the pose matches something in the last
       ``_POSE_HISTORY_WINDOW`` poses, regardless of any verdict.

    Signal 2 exists because signal 1 shares a single point of failure with the
    competence trigger in ``maybe_advance_step``: both are downstream of
    ``record_answer``. Observed 2026-08-05 on session 6 — the model confirmed
    three correct answers in prose, never called ``record_answer``, so
    ``engine_state`` stayed empty ({}), ``answered_correct`` was None, and BOTH
    of those nets were dead. The only remaining escape was the turn cap at 8
    student turns, by which point the student had answered correctly repeatedly
    and been credited for none of it. One of them said "we already did this".

    Signal 2 needs no verdict, no tool call, and no cooperation from the model
    — only what the server itself already knows it asked.
    """
    if not _antirepeat_enabled():
        return False
    es = getattr(session, 'engine_state', None) or {}
    if not isinstance(es, dict):
        return False

    qkey = _norm_q(question_text)
    okey = _norm_optset(options) if question_type == 'mcq' else None

    # Signal 1 — re-asking something already answered correctly.
    answered = es.get('answered_correct')
    already_correct = isinstance(answered, list) and (
        qkey in answered or (okey and okey in answered))

    # Signal 2 — re-asking something posed recently, verdict or not.
    history = es.get('recent_poses')
    history = history if isinstance(history, list) else []
    recently_posed = qkey in history or bool(okey and okey in history)

    is_repeat = already_correct or recently_posed

    # Record this pose BEFORE returning, so the next call can see it. Keys are
    # normalised question text plus, for MCQ, the option set — the observed
    # loop re-used identical options under a reworded stem, which the text
    # fingerprint alone would miss.
    for key in (qkey, okey):
        if key:
            history.append(key)
    es['recent_poses'] = history[-_POSE_HISTORY_WINDOW * 2:]

    streak = int(es.get('repeat_pose_streak') or 0)
    force = False
    if is_repeat:
        streak += 1
        logger.info(
            "[simple_tutor] repeat_pose session=%s streak=%d question=%r "
            "(already_correct=%s recently_posed=%s)",
            session.pk, streak, (question_text or '')[:60],
            already_correct, recently_posed,
        )
        if streak >= _REPEAT_STREAK_TO_ADVANCE:
            force = True
            es['_repeat_force_advance'] = True
            streak = 0   # consumed
    else:
        streak = 0
    es['repeat_pose_streak'] = streak
    session.engine_state = es
    try:
        session.save(update_fields=['engine_state'])
    except Exception:
        pass
    return force


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
    from ai_tutor.apps.tutoring.models import ExitTicketQuestion
    from ai_tutor.apps.curriculum.models import LessonStep
    from ai_tutor.apps.tutoring.simple_tutor.step_question import (
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
    if step is not None and step.step_type == LessonStep.StepType.WARM_UP:
        # The warm-up step's question comes from a lesson the student already
        # did, not from this lesson's bank. It is a real ExitTicketQuestion, so
        # pose-by-index, the slot, the grader and the letter picker all work
        # exactly as they do for any question.
        #
        # The NEXT step's questions come along behind it. Block 0 tells the
        # model to pose the next question in the same turn as a correct
        # verdict, and with the warm-up alone in the pool the only thing it
        # could pose was the question just answered — the student got the same
        # item twice. Following it with today's first questions makes that
        # instruction do the right thing: answer the recall question, hand
        # straight over to the lesson.
        from ai_tutor.apps.tutoring.simple_tutor.warm_up import (
            select_warm_up_question,
        )
        chosen = select_warm_up_question(session)
        pool = [chosen] if chosen is not None else []
        next_step = (
            LessonStep.objects
            .filter(lesson=lesson, order_index=current_step_index + 1)
            .first()
        )
        if next_step is not None and len(pool) < max_questions:
            pool.extend(_pool_for_step(
                session, lesson, next_step, max_questions - len(pool),
            ))
        return pool

    return _pool_for_step(session, lesson, step, max_questions)


def _pool_for_step(session, lesson, step, max_questions: int) -> list:
    """The three-tier pool for one step. Split out of build_question_pool so
    the warm-up step can borrow the NEXT step's questions without duplicating
    the tier logic."""
    from ai_tutor.apps.tutoring.models import ExitTicketQuestion
    from ai_tutor.apps.curriculum.models import LessonStep
    from ai_tutor.apps.tutoring.simple_tutor.step_question import (
        StepQuestion, has_question as step_has_question,
    )

    current_step_index = getattr(step, 'order_index', 0) or 0

    if step is None:
        # Past the last step. That is remediation: the exit ticket has been
        # submitted and failed, and there is no LessonStep to key a pool off.
        #
        # This returned [] and the consequences were invisible from here.
        # pose_question(question_index=N) had nothing to select, so the only
        # way the tutor could ask anything at all was to write a question in
        # prose — which creates no slot, so nothing grades, and offline the
        # student gets no letter buttons either. The prompt licensed exactly
        # that ("or author your own"), so the instruction and the empty pool
        # were the same bug arriving from two directions.
        return _remediation_question_pool(session, lesson, max_questions)

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

    # Per-session question order.
    #
    # The DB order (order_index, id) is the authoring order and it never
    # varied, so every student on a given step met the same question first,
    # and a student retaking a lesson met it again. That was survivable while
    # the tutor authored and adapted its own questions; since catalog-only
    # (f59bdb7) it selects a pool INDEX, so the authoring order became the
    # teaching order verbatim.
    #
    # Seeded on session.pk, exactly like the exit-ticket sub-sample in
    # engine._build_exit_ticket_payload. Different session → different pk →
    # different order, which is the variety the student sees; a retake is a
    # new session, so a retake gets a new order.
    #
    # Seeded rather than free rng deliberately. One turn builds the pool once
    # and threads the same list to both the prompt and the dispatcher
    # (engine.py:405), so pose_question(question_index=N) is safe either way —
    # but a free shuffle would reorder the pool the model reads on EVERY turn,
    # so the question it saw at index 2 last turn is somewhere else now. That
    # churn is exactly what makes a 4B lose track of what it already asked.
    rng = random.Random(getattr(session, 'pk', None) or 0)

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
        # Shuffled WITHIN the tier, never across tiers. The tiers encode
        # pedagogy — this step's objective outranks the rest of the lesson —
        # and a global shuffle would let an off-objective question take the
        # last slot from an on-objective one. What varies is which of the
        # objective's own questions the student meets first.
        candidates = list(
            ExitTicketQuestion.objects
            .filter(
                exit_ticket__lesson=lesson,
                enabling_objective=objective,
                question_type__in=allowed_types,
            )
            .order_by('order_index', 'id')
        )
        rng.shuffle(candidates)
        for q in candidates:
            if _is_already_graded(q):
                continue
            pool.append(q)
            if len(pool) >= max_questions:
                break

    # Source 3 — ANY allowed ETQ on the lesson (fills the rest).
    if len(pool) < max_questions:
        candidates = list(
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
        )
        rng.shuffle(candidates)
        for q in candidates:
            if _is_already_graded(q):
                continue
            pool.append(q)
            if len(pool) >= max_questions:
                break

    return pool


def _remediation_question_pool(session, lesson, max_questions: int) -> list:
    """Pool for a failed-exit-ticket session: the questions they actually missed.

    Remediation is meant to work back through every question the exit ticket
    marked wrong, so the pool IS that list, minus the ones already re-answered
    correctly. Ordered worst-objective-first, since the model takes index 1 and
    index 1 should be the objective they understood least.

    This reverses the earlier "prefer a sibling they did NOT fail" preference.
    That was written to avoid re-asking an identical item, but it meant a
    student could finish remediation without ever revisiting a question they
    got wrong, and it left the completion check — which counts missed questions
    — unable to ever reach zero.

    Falls back to any question on the missed objectives when the failed items
    are exhausted, so the tutor is never handed an empty pool mid-session.
    Returns [] when there is no completed attempt or nothing was missed, which
    puts the tutor back in plain TEACH mode rather than posing at random.
    """
    from ai_tutor.apps.tutoring.models import ExitTicketAttempt, ExitTicketQuestion

    attempt = (
        ExitTicketAttempt.objects
        .filter(session=session, completed_at__isnull=False)
        .order_by('-completed_at')
        .first()
    )
    if attempt is None:
        return []
    eo_competency = (attempt.answers or {}).get('eo_competency') or {}

    missed = [
        (eo, b) for eo, b in eo_competency.items()
        if eo and isinstance(b, dict)
        and int(b.get('asked') or 0) > 0
        and int(b.get('correct') or 0) < int(b.get('asked') or 0)
    ]
    if not missed:
        return []
    missed.sort(key=lambda kv: (
        int(kv[1].get('correct') or 0) / max(int(kv[1].get('asked') or 1), 1),
        kv[0],
    ))

    _, covered_ids = _remediation_question_sets(session)
    allowed_types = _allowed_tutoring_types()
    rng = random.Random(getattr(session, 'pk', None) or 0)

    pool: list = []
    seen: set[int] = set()

    def _take(queryset):
        for q in queryset:
            if len(pool) >= max_questions:
                return
            if q.pk in seen or q.pk in covered_ids:
                continue
            seen.add(q.pk)
            pool.append(q)

    # Pass 1 — the failed questions themselves, worst objective first.
    for objective, bucket in missed:
        if len(pool) >= max_questions:
            break
        ids = [q for q in (bucket.get('failed_question_ids') or [])
               if isinstance(q, int)]
        if not ids:
            continue
        rows = list(
            ExitTicketQuestion.objects
            .filter(pk__in=ids, question_type__in=allowed_types)
            .order_by('order_index', 'id')
        )
        rng.shuffle(rows)
        _take(rows)

    # Pass 2 — anything else on the missed objectives, once the failures run
    # out. Without this the pool empties as the student recovers items and the
    # tutor is left with nothing to pose.
    for objective, _bucket in missed:
        if len(pool) >= max_questions:
            break
        rows = list(
            ExitTicketQuestion.objects
            .filter(
                exit_ticket__lesson=lesson,
                enabling_objective=objective,
                question_type__in=allowed_types,
            )
            .order_by('order_index', 'id')
        )
        rng.shuffle(rows)
        _take(rows)
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
    from ai_tutor.apps.curriculum.models import LessonStep

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


def _catalog_match_for_stem(session, question_text: str):
    """(options, correct_letter) of the catalog question whose stem exactly
    matches ``question_text`` (normalised), within this session's lesson.
    None when no unambiguous match. Used to salvage optionless MCQ poses
    and to keep the reference letter coherent with the displayed options."""
    from ai_tutor.apps.tutoring.models import ExitTicketQuestion
    key = _norm_q(question_text)
    if not key:
        return None
    lesson = getattr(session, 'lesson', None)
    if lesson is None:
        return None
    matches = []
    qs = ExitTicketQuestion.objects.filter(
        exit_ticket__lesson=lesson, question_type='mcq',
    )
    for q in qs:
        if _norm_q(q.question_text or '') == key:
            opts = [
                (getattr(q, f'option_{letter}', '') or '').strip()
                for letter in 'abcd'
            ]
            letter = (q.correct_answer or '').strip().upper()
            if len([o for o in opts if o]) >= 2 and letter in 'ABCD':
                matches.append((opts, letter))
    return matches[0] if len(matches) == 1 else None


# ============================================================================
# handle_pose_question — LLM-called: "I want to ask this question next"
# ============================================================================


def handle_pose_question_by_index(
    session: 'TutorSession',
    *,
    question_index: int,
    question_pool: list | None = None,
) -> dict[str, Any]:
    """Pose the pool question the model selected. THE ONLY tool-reachable pose.

    The model passes an index into ``<question_pool>``; the server reads the
    stem, options, type and correct answer off the catalog row and writes the
    slot from those. The model never transmits question text, so it cannot
    corrupt it — which is the point.

    This replaces the old six-parameter pose (2026-08-06). A 4B tutor
    re-authoring its own stems mangled numeric notation while keeping the
    original reference answer: ``1:200,000`` became ``-200,000``, ``1/6``
    became ``-1/6``, ``0.5`` became ``5``. The student saw a corrupted
    question and was graded against a reference for the uncorrupted one, so a
    correct answer was marked wrong. No amount of validation on a
    model-supplied stem fixes that; not accepting one does.
    See memory/catalog_only_questions_plan.md.

    ``question_pool`` is the list the SYSTEM PROMPT was rendered from, threaded
    through by the engine. Resolving against a rebuilt pool would risk the
    indices shifting under the model between prompt and dispatch.
    """
    pool = question_pool
    if pool is None:
        # Only reachable from a direct call (tests, tooling). The engine always
        # threads the rendered pool.
        pool = build_question_pool(session)

    try:
        idx = int(question_index)
    except (TypeError, ValueError):
        return {'posed': False, 'error': f'question_index must be an integer, got {question_index!r}'}

    if not pool:
        logger.info(
            "handle_pose_question_by_index: session=%s empty pool — nothing to "
            "ask on this step", getattr(session, 'pk', None),
        )
        return {'posed': False, 'error': 'question_pool is empty'}

    if idx < 1 or idx > len(pool):
        # Rejected poses already trigger _auto_pose_fallback, which picks a
        # pool question server-side — so an out-of-range index degrades to the
        # right behaviour rather than a dead turn.
        logger.warning(
            "handle_pose_question_by_index: session=%s index=%s out of range "
            "1..%d", getattr(session, 'pk', None), idx, len(pool),
        )
        return {'posed': False, 'error': f'question_index {idx} out of range 1..{len(pool)}'}

    q = pool[idx - 1]
    qtype = str(getattr(q, 'question_type', '') or '').strip().lower()
    options = [
        str(getattr(q, f'option_{letter}', '') or '').strip()
        for letter in 'abcd'
    ]
    options = [o for o in options if o]

    # Derive the reference EXACTLY as prompts._render_question_pool does, or the
    # answer the model was shown is not the answer we grade against. Only MCQ
    # keeps its answer in `correct_answer` (the letter); short_numeric / math /
    # short_answer carry it in `answer_data`, and reading the wrong field yields
    # an empty reference, which handle_pose_question rejects outright.
    #
    # Caught by the eval: math lesson 1144 rejected 8 of 8 poses with
    # "question_text and reference_answer are required" and the session ran to
    # the turn cap having asked nothing.
    if qtype == 'mcq':
        reference = str(getattr(q, 'correct_answer', '') or '').strip()
    else:
        ad = getattr(q, 'answer_data', None) or {}
        ref = None
        if isinstance(ad, dict):
            # Precedence flips by type. For a NUMERIC question the grader
            # needs the bare value: `model_answer` carries the unit ('165°',
            # '39 SCR', '8.94427 m') and handle_pose_question rejects it with
            # "short_numeric requires a numeric reference_answer", while
            # `computed` holds 165.0. For short_answer the opposite is true —
            # `model_answer` is the canonical phrasing and `computed` is
            # meaningless. The eval caught the numeric half as 4 rejected
            # poses after the earlier reference fix.
            if qtype in ('short_numeric', 'math', 'numeric'):
                ref = ad.get('computed')
                if ref is None:
                    ref = ad.get('model_answer')
            else:
                ref = ad.get('model_answer')
                if ref is None:
                    ref = ad.get('computed')
        if ref is None:
            ref = (getattr(q, 'correct_answer', '') or '').strip() or None
        reference = '' if ref is None else str(ref).strip()

    if not reference:
        logger.warning(
            "handle_pose_question_by_index: session=%s pool entry %d (type=%r) "
            "has no usable reference answer — skipping rather than posing an "
            "ungradable question",
            getattr(session, 'pk', None), idx, qtype,
        )
        return {'posed': False, 'error': f'pool entry {idx} has no reference answer'}

    return handle_pose_question(
        session,
        question_text=str(getattr(q, 'question_text', '') or ''),
        question_type=qtype,
        reference_answer=reference,
        source='catalog',
        options=options or None,
        catalog_question_id=idx,
    )


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
    from ai_tutor.apps.tutoring.models import InFlightQuestion, ExitTicketQuestion

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

    # Normalise options — must be a list of strings. Strip any leading
    # letter label the model baked into the option text ("A) 11" → "11");
    # the platform adds letters at render time, and stored prefixes caused
    # double-lettered options ("A) A) 11") throughout the cycle-7 sweeps.
    from ai_tutor.apps.tutoring.simple_tutor.grader import _OPT_PREFIX_RE
    opts = options or []
    if not isinstance(opts, list):
        opts = []
    opts = [_OPT_PREFIX_RE.sub('', str(o).strip())[:300] for o in opts]

    # ── Malformed-pose salvage (cycle 8) ─────────────────────────────
    # Two malformed shapes recur: an MCQ posed without its options (can
    # neither render choices nor grade a typed value — the "you picked D"
    # hallucination), and a short_numeric slot with a letter reference
    # (the correct value can never grade correct). Salvage them into the
    # nearest valid slot instead of rejecting: models that cannot act on
    # corrective feedback (kimi's Call-2 text scrubs to empty) turned
    # every rejection into a slotless placeholder deadlock — 12 deadlocks
    # in the first cycle-8 kimi leg. Reject only when nothing gradable
    # can be built.
    from ai_tutor.apps.tutoring.simple_tutor.grader import _option_number
    non_empty_opts = [o for o in opts if o.strip()]
    is_letter_ref = len(ref) == 1 and ref.upper() in ('A', 'B', 'C', 'D')
    if qtype == 'mcq' and len(non_empty_opts) < 2:
        if is_letter_ref:
            # Most common malformed shape: a catalog MCQ posed by stem with
            # its options dropped. The options AND the correct letter live
            # in the catalog — adopt both by exact-normalised stem match
            # (adopting only the options while keeping the model's letter
            # re-creates the letter/order mismatch).
            cat = _catalog_match_for_stem(session, qtext)
            if cat:
                opts, ref = cat
                logger.info(
                    "[simple_tutor] pose salvaged: optionless mcq options+"
                    "letter adopted from catalog session=%s", session.pk,
                )
            else:
                logger.info(
                    "[simple_tutor] pose rejected: optionless mcq with "
                    "letter ref session=%s", session.pk,
                )
                return {
                    'posed': False,
                    'error': 'mcq requires its options — pass all four '
                             'option texts in `options`, or pose as '
                             'short_numeric/short_answer with the answer '
                             'value as reference_answer',
                    'question_type': qtype,
                }
        else:
            qtype = ('short_numeric' if _option_number(ref) is not None
                     else 'short_answer')
            opts = []
            logger.info(
                "[simple_tutor] pose salvaged: optionless mcq -> %s "
                "session=%s", qtype, session.pk,
            )
    elif qtype == 'mcq' and is_letter_ref and len(non_empty_opts) >= 2:
        # Letter/text coherence (cycle 9): the prompt's letter-rotation
        # discipline made models re-letter CATALOG questions whose option
        # order is fixed — the slot's reference letter then pointed at the
        # wrong option and correct answers were graded wrong all session
        # (Beau Vallon: correct B rejected six times, kimi+qwen cycle 8b).
        # For a catalog-stem match, the catalog's correct TEXT is the
        # authority; the letter is derived from where that text sits in
        # the options actually being shown.
        cat = _catalog_match_for_stem(session, qtext)
        if cat is not None:
            from ai_tutor.apps.tutoring.simple_tutor.grader import _norm_option
            cat_opts, cat_letter = cat
            correct_text = _norm_option(cat_opts['ABCD'.index(cat_letter)])
            posed_norm = [_norm_option(o) for o in opts]
            if correct_text and correct_text in posed_norm:
                derived = 'ABCD'[posed_norm.index(correct_text)]
                if derived != ref.upper():
                    logger.info(
                        "[simple_tutor] ref letter overridden %s -> %s "
                        "(catalog text authority) session=%s",
                        ref, derived, session.pk,
                    )
                    ref = derived
            else:
                # Posed options don't contain the catalog's correct answer
                # at all — the option set is untrustworthy; use the
                # catalog's options and letter wholesale.
                logger.info(
                    "[simple_tutor] posed options missing catalog answer — "
                    "catalog options+letter adopted session=%s", session.pk,
                )
                opts, ref = cat_opts, cat_letter
    elif qtype == 'short_numeric' and _option_number(ref) is None:
        if is_letter_ref and len(non_empty_opts) >= 2:
            qtype = 'mcq'
            logger.info(
                "[simple_tutor] pose salvaged: short_numeric with letter "
                "ref + options -> mcq session=%s", session.pk,
            )
        else:
            logger.info(
                "[simple_tutor] pose rejected: short_numeric with "
                "non-numeric reference %r session=%s", ref[:40], session.pk,
            )
            return {
                'posed': False,
                'error': 'short_numeric requires a numeric reference_answer '
                         '— pass the numeric value itself (e.g. "0.3" or '
                         '"3/4"), or pose the question as mcq with its '
                         'options',
                'question_type': qtype,
            }

    # Anti-repetition, stage 1 (2026-07-18 sweep): posing a question the
    # student already answered correctly wastes a turn — the sweep showed the
    # same stem re-asked up to 4x per session with correct answers each time.
    # Reject the FIRST such pose with corrective feedback so Call 2 can pick a
    # fresh question. A re-pose of the same stem after a rejection is accepted
    # (the turn must never be left without a gradable slot when the model has
    # nothing else to ask) — stage 2, _note_pose_repetition below, then
    # force-advances the step out of the loop.
    if _antirepeat_enabled():
        _key = _norm_q(qtext)
        _okey = _norm_optset(opts) if qtype == 'mcq' else None
        _es = getattr(session, 'engine_state', None) or {}
        _answered = _es.get('answered_correct') if isinstance(_es, dict) else None
        _hit = (isinstance(_answered, list)
                and (_key in _answered or (_okey and _okey in _answered)))
        if _hit:
            _hitkey = _key if _key in _answered else _okey
            _rejected = _es.get('repeat_rejected_stems')
            if not isinstance(_rejected, list):
                _rejected = []
            if _hitkey not in _rejected:
                _es['repeat_rejected_stems'] = (
                    _rejected + [_hitkey])[-_ANSWERED_CORRECT_CAP:]
                session.engine_state = _es
                try:
                    session.save(update_fields=['engine_state'])
                except Exception:
                    pass
                logger.info(
                    "[simple_tutor] repeat_pose rejected session=%s "
                    "question=%r (already answered correctly)",
                    session.pk, qtext[:60],
                )
                return {
                    'posed': False,
                    'error': 'repeat_question: the student already answered '
                             'this exact question correctly earlier in the '
                             'session — pose a different question from the '
                             'pool, or continue to the next piece of content',
                    'repeat_of_correct': True,
                    'question_type': qtype,
                }

    # Replace any prior in-flight question (analytics-log the orphan).
    prior = InFlightQuestion.objects.filter(session=session).first()
    if prior is not None:
        import os
        # Anti-desync (cycle 3): a prior question with attempt_count==0 was shown
        # to the student and NOT yet answered. Posing a new one now swaps the
        # question out from under them — they answer what they read, the platform
        # grades the swap, and the lesson stalls. gemini did this 161x in one
        # cycle-2 sweep (55% of its poses), driving 8 of its timeouts. A prior
        # with attempt_count>=1 means the student tried and a pivot to an easier
        # item is legitimate, so only the never-attempted case is blocked.
        # Toggle SIMPLE_TUTOR_ANTIDESYNC=0 disables it.
        _es_i = getattr(session, 'engine_state', None) or {}
        _intent = _es_i.get('_student_intent') if isinstance(_es_i, dict) else None
        # Only block when the student ATTEMPTED an answer this turn (intent
        # 'answer' / 'answer_or_other') — then the model should grade it, not pose
        # over it. If they DECLINED ('idk' → non_engagement / clarification /
        # off_topic), allow the tutor to pivot to a new question; blocking there
        # traps it re-posing into a dead slot (the cycle-3 anti-desync deadlock).
        if os.getenv('SIMPLE_TUTOR_ANTIDESYNC', '1').strip() != '0' \
           and (prior.attempt_count or 0) == 0 \
           and _intent in ('answer', 'answer_or_other'):
            logger.info(
                "[simple_tutor] premature_pose blocked session=%s — student "
                "answered an in-flight question; grade it first", session.pk,
            )
            return {
                'posed': False,
                'error': 'premature_pose: the student answered a question already '
                         'in flight — grade their answer to it before posing a new '
                         'one',
                'premature': True,
                'question_type': prior.question_type,
            }
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

    # Anti-repetition: register the slot as normal (so the turn always has a
    # gradable question — never a dangling turn), but if this is a repeat re-ask
    # of an already-correct question, flag the lesson to move forward. The slot
    # stays valid; the step just advances underneath, which walks the session out
    # of the loop instead of letting it deadlock.
    repeat_force_advance = _note_pose_repetition(
        session, qtext, options=opts, question_type=qtype)

    logger.info(
        "[simple_tutor] posed session=%s type=%s source=%s "
        "catalog_id=%s mismatch=%s repeat_advance=%s",
        session.pk, qtype, src,
        catalog_question_id, catalog_mismatch, repeat_force_advance,
    )

    return {
        'posed': True,
        'question_type': qtype,
        'source': src,
        'catalog_mismatch': catalog_mismatch,
        'repeat_force_advance': repeat_force_advance,
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
    from ai_tutor.apps.tutoring.models import InFlightQuestion
    from ai_tutor.apps.tutoring.simple_tutor.grader import grade_answer

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
        # Captured here, with the rest of the snapshot, because the
        # correct-verdict branch below DELETES the slot — read it afterwards
        # and it is gone. The engine uses it to fetch the bank's authored
        # explanation so the tutor can say why an answer was right.
        'catalog_question_id': in_flight.catalog_question_id,
    }
    # Read off the row before the correct-verdict branch deletes it.
    in_flight_options = list(in_flight.options or [])
    in_flight_type = in_flight.question_type

    if verdict == 'correct':
        # Remember this stem BEFORE deleting the slot, so an exact re-ask on a
        # later turn is detectable (anti-repetition).
        _record_answered_correct(
            session, in_flight.question_text,
            options=(in_flight.options
                     if in_flight.question_type == 'mcq' else None))
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

    payload = {
        'recorded': True,
        **snapshot,
        **result.to_dict(),
    }
    choice = _resolve_student_choice(in_flight_options, in_flight_type, extracted)
    if choice is not None:
        payload['student_choice'] = choice
    return payload


def _resolve_student_choice(
    options: list, question_type: str, extracted_answer: str,
) -> dict | None:
    """``{'letter': 'D', 'text': '...'}`` for the option the student picked.

    The tutor already knows the letter — it passed it in — and has the option
    list in its prompt, so on paper this is redundant. In practice the lookup
    is ~7,000 tokens up in a 30k-char prompt and the 4B gets it wrong. Device
    session 30: the student clicked D ("It shows the compass direction between
    the two points") and the tutor answered "it doesn't help pick grid
    squares", which refutes option A. The student was told why an option they
    did not choose was wrong.

    Resolving it here costs nothing and removes the lookup rather than asking
    the model to be more careful about it. Returns None for a non-MCQ or an
    answer that isn't a letter (someone typing prose online), where there is
    no option to name and inventing one would be worse than silence.
    """
    if (question_type or '').strip().lower() != 'mcq':
        return None
    opts = [str(o).strip() for o in (options or [])]
    if len(opts) < 2:
        return None
    try:
        from ai_tutor.apps.tutoring.simple_tutor.grader import _extract_letter_forms
        letter = _extract_letter_forms(extracted_answer or '')
    except Exception:                              # noqa: BLE001
        return None
    if not letter:
        return None
    idx = 'ABCD'.find(letter)
    if idx < 0 or idx >= len(opts) or not opts[idx]:
        return None
    return {'letter': letter, 'text': opts[idx]}


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


def remediation_progress(session: 'TutorSession') -> dict | None:
    """``{'recovered': int, 'total': int}`` in QUESTIONS, while remediating.

    Counts the questions the student got wrong on the exit ticket, not the
    objectives behind them. Score 0/10 means ten questions to work back
    through, and that is the denominator the student should see — an objective
    count reads as 1/6 when there are ten items left, which understates the
    work rather than orienting them.

    During remediation the header's step chip is blanked (there is no lesson
    step to count), so without this a student has no idea whether they are one
    item from the retake or nine.

    None outside remediation, so the caller leaves the chip alone.
    """
    try:
        missed, covered = _remediation_question_sets(session)
        if missed is None:
            return None
        return {'recovered': len(missed & covered), 'total': len(missed)}
    except Exception:  # noqa: BLE001
        return None


def _remediation_question_sets(session) -> tuple[set | None, set]:
    """(missed_question_ids, covered_question_ids) for the latest failed attempt.

    Remediation goes over every question the student missed, so both sets are
    ExitTicketQuestion PKs. ``failed_question_ids`` on each eo_competency
    bucket is the authoritative list of what was wrong.

    ``covered`` is matched by normalised stem against the grader payload,
    because SessionTurn.step is NULL on every remediation turn — remediation
    runs past the last step, so there is no step to attach. Reading the
    objective off the step FK is what made the retake unreachable: `covered`
    was empty on every session and the completion check never fired.

    ``missed`` is None when there is nothing to remediate — no completed
    attempt, a passed one, or a fail with no recorded failures.
    """
    from ai_tutor.apps.tutoring.models import (
        ExitTicketAttempt, ExitTicketQuestion, SessionTurn,
    )

    attempt = (
        ExitTicketAttempt.objects
        .filter(session=session, completed_at__isnull=False)
        .order_by('-completed_at')
        .first()
    )
    if attempt is None or attempt.passed:
        return None, set()

    missed: set[int] = set()
    for bucket in ((attempt.answers or {}).get('eo_competency') or {}).values():
        if not isinstance(bucket, dict):
            continue
        for qid in (bucket.get('failed_question_ids') or []):
            if isinstance(qid, int):
                missed.add(qid)
    if not missed:
        return None, set()

    stem_to_id = {
        _norm_q(q.question_text or ''): q.pk
        for q in ExitTicketQuestion.objects.filter(
            exit_ticket__lesson=session.lesson)
        if (q.question_text or '').strip()
    }

    covered: set[int] = set()
    turns = (
        SessionTurn.objects
        .filter(session=session, role='tutor',
                created_at__gt=attempt.completed_at)
        .exclude(judge_outputs={})
    )
    for turn in turns:
        grader = (turn.judge_outputs or {}).get('grader') or {}
        if grader.get('verdict') != 'correct':
            continue
        qid = stem_to_id.get(_norm_q(grader.get('question_text') or ''))
        if qid is not None:
            covered.add(qid)
    return missed, covered


PIVOT_AFTER_ATTEMPTS = 3


def maybe_pivot_stalled_question(session: 'TutorSession'):
    """Replace a question the student has failed twice, server-side.

    The prompt asks for this at rung 2+ of the hint ladder and
    <pivot_guidance> repeats it inside the slot. Neither reliably fires: a 4B
    does not act on a conditional instruction, which is the same finding that
    took remediation posing from 0/4 on prompt alone to 4/4 once the server
    did it. Device transcript: three wrong answers on one question, three
    hints, no pivot.

    So the server pivots. Prefers a strictly easier question on the same
    objective; falls back to any unasked pool entry, because a third hint on
    an item they have failed twice is worse than a sideways move.

    Returns the ExitTicketQuestion posed, or None when nothing needed doing —
    fewer than two attempts, no slot, or a pool with nothing else in it.
    """
    from ai_tutor.apps.tutoring.models import ExitTicketQuestion, InFlightQuestion

    _RANK = {'easy': 0, 'medium': 1, 'hard': 2}
    try:
        slot = InFlightQuestion.objects.filter(session=session).first()
        if slot is None:
            return None
        if (slot.attempt_count or 0) < PIVOT_AFTER_ATTEMPTS:
            return None

        current_rank = _RANK.get(
            (ExitTicketQuestion.objects
             .filter(pk=slot.catalog_question_id)
             .values_list('difficulty', flat=True).first() or 'medium'), 1)

        pool = [q for q in build_question_pool(session)
                if _norm_q(q.question_text or '') != _norm_q(slot.question_text or '')]
        if not pool:
            return None
        easier = [q for q in pool
                  if _RANK.get((q.difficulty or 'medium'), 1) < current_rank]
        easier.sort(key=lambda q: _RANK.get((q.difficulty or 'medium'), 1))
        chosen = (easier or pool)[0]

        # Retire the stalled slot first: handle_pose_question_by_index refuses
        # to pose over a live one, which is the guard that stops the model
        # swapping a question out from under the student mid-answer.
        InFlightQuestion.objects.filter(session=session).delete()
        result = handle_pose_question_by_index(
            session, question_index=1, question_pool=[chosen])
        if not result.get('posed'):
            logger.warning(
                "[simple_tutor] pivot could not pose session=%s: %s",
                session.pk, result.get('error'))
            return None
        logger.info(
            "[simple_tutor] pivoted after %d attempts session=%s "
            "%s -> %s (difficulty %s)",
            slot.attempt_count, session.pk, slot.catalog_question_id,
            chosen.pk, chosen.difficulty)
        return chosen
    except Exception:  # noqa: BLE001 — a missed pivot must not lose the turn
        logger.warning(
            "[simple_tutor] pivot failed session=%s",
            getattr(session, 'pk', None), exc_info=True)
        return None


def maybe_complete_remediation(session: 'TutorSession') -> bool:
    """Set ``engine_state['remediation_complete']`` once the student has
    recovered every objective they failed on their last exit-ticket attempt.

    This is the server-side replacement for the signal that used to arrive via
    the LLM calling ``advance_step`` after a failed attempt (removed
    2026-08-05). That made the exit-ticket RETAKE depend on a tool the model
    called once in 1,443 production turns — the retake path was effectively
    dead. ``engine.respond_for_view`` reads the flag unchanged; only the
    producer moved.

    Criterion, deterministic and verdict-based so it mirrors
    ``maybe_advance_step`` rather than inventing a new mechanism: every
    enabling objective the student got wrong has since been answered CORRECTLY
    in a turn recorded after the attempt was submitted.

    The step↔objective link is ``SessionTurn.step.enabling_objective`` — no
    schema change needed (``InFlightQuestion`` carries no objective, but the
    turn's step FK is populated on 1,114 of the graded turns in prod).

    Returns True when it flipped the flag this call. Never raises — a failure
    here must not break the turn.
    """
    try:
        es = getattr(session, 'engine_state', None) or {}
        if not isinstance(es, dict):
            es = {}
        if es.get('remediation_complete'):
            return False        # already signalled; consumer clears it

        missed, covered = _remediation_question_sets(session)
        if missed is None:
            return False

        if not missed.issubset(covered):
            logger.info(
                "maybe_complete_remediation: session=%s %d/%d questions "
                "recovered — not yet complete",
                session.pk, len(missed & covered), len(missed),
            )
            return False

        es['remediation_complete'] = True
        session.engine_state = es
        session.save(update_fields=['engine_state'])
        logger.info(
            "maybe_complete_remediation: session=%s all %d missed questions "
            "recovered — exit ticket re-opened",
            session.pk, len(missed),
        )
        return True
    except Exception:  # noqa: BLE001 — never break the turn
        logger.warning(
            "maybe_complete_remediation failed session=%s",
            getattr(session, 'pk', None), exc_info=True,
        )
        return False


# handle_redirect_off_topic was REMOVED 2026-08-05. It incremented an
# `off_topic_count` on engine_state that nothing ever read, and the model called
# it once in 1,443 production turns. Off-topic handling is fully covered by
# intent.classify_student_message plus the CONVERSATIONAL branch of Block-0,
# neither of which needs a tool. See memory/tool_surface_reduction_plan.md.
#
# Existing `off_topic_count` / `last_off_topic_reason` keys are left in place on
# old sessions — inert JSON on a handful of rows; migrating them costs more than
# it saves. Nothing writes them any more.


# handle_advance_step was REMOVED 2026-08-05. Called once in 1,443 production
# turns; maybe_advance_step below already advanced every measured session
# through all its steps without it. Its one non-redundant job — setting
# remediation_complete so the exit ticket re-opens — moved to
# maybe_complete_remediation above, which is verdict-based rather than
# dependent on the model remembering a tool call.
# The `advance_step_hints` analytics list went with it (1 record ever).


# ============================================================================
# maybe_advance_step — server auto-advances the lesson step
# ============================================================================


# B1 — server-authoritative "clearly-correct bare answer" safety net.
#
# The old auto_grade_if_missed net (removed 2026-05-26) recorded a verdict for
# EVERY missed record_answer, so conversational continuations ("yes let's go
# deeper") got graded as answers and polluted the graded set. This replacement
# is deliberately narrow: it records ONLY when the student's own message
# DETERMINISTICALLY matches the in-flight reference (mcq letter / numeric value).
# A non-answer cannot match an MCQ letter or a numeric reference, so the failure
# mode that got the old net pulled cannot recur. This is what makes "one missed
# tool call non-fatal" (report B1): the student answered correctly, the model
# forgot to call record_answer, and the step still advances.
#
# Deterministic tiers only — never an LLM verifier call in a safety net (cost +
# the "clearly correct" bar). Toggle SIMPLE_TUTOR_AUTOGRADE_BARE=0 disables it so
# a sweep can isolate this variable.
# 'short_answer' added 2026-08-05. Free text was excluded by construction, which
# is exactly the reported failure: A/B/C/D graded fine (deterministic letter
# match, no LLM) while a written answer was never graded at all, so the step
# never advanced.
_AUTOGRADE_QTYPES = frozenset(
    {'mcq', 'math', 'numeric', 'short_numeric', 'short_answer'})

# Tiers whose CORRECT verdict is trustworthy enough to record without the model
# asking. Deliberately TWO sets rather than widening one: this name promises
# determinism and free text cannot deliver it.
_AUTOGRADE_DET_TIERS = frozenset({'mcq', 'math'})

# Non-deterministic tiers also allowed to salvage a CORRECT verdict.
#
# 'embed_gate' only resolves above the high-similarity threshold, so a positive
# is already conservative. 'verifier_llm' is a judged verdict — cross-family in
# production, and offline it falls back to the local model
# (grader._local_verifier_chain), which is weaker but was measured discriminating
# correctly, including marking a plainly wrong answer INCORRECT at 0.99.
#
# Still CORRECT-only, like the deterministic path. Recording an INCORRECT the
# model never asked for would pre-empt the tutor's hint ladder and tell a student
# they are wrong with none of the framing that makes that useful. Salvage credit,
# never blame.
_AUTOGRADE_LLM_TIERS = frozenset({'embed_gate', 'verifier_llm'})
_AUTOGRADE_SKIP_INTENTS = frozenset(
    {'clarification', 'pushback', 'off_topic', 'non_engagement'}
)


def autograde_bare_answer_if_clear(
    session: 'TutorSession',
    *,
    student_answer: str,
    student_intent: str | None,
    already_recorded: bool,
) -> dict | None:
    """Grade a clearly-correct bare answer the model forgot to submit.

    Returns a ``record_answer``-shaped tool_result entry (so the caller can
    append it to ``tool_results`` before persistence, and the verdict lands in
    ``judge_outputs['grader']`` exactly like a real record_answer) — or ``None``
    when nothing should be recorded.
    """
    import os

    if os.getenv('SIMPLE_TUTOR_AUTOGRADE_BARE', '1').strip() == '0':
        return None
    if already_recorded:
        return None
    if (student_intent or '') in _AUTOGRADE_SKIP_INTENTS:
        return None

    answer = (student_answer or '').strip()
    if not answer:
        return None

    from ai_tutor.apps.tutoring.models import InFlightQuestion
    from ai_tutor.apps.tutoring.simple_tutor.grader import grade_answer

    in_flight = InFlightQuestion.objects.filter(session=session).first()
    if in_flight is None or in_flight.question_type not in _AUTOGRADE_QTYPES:
        return None

    # Pre-grade against the reference. Only a clean deterministic CORRECT
    # verdict is allowed to record — INCORRECT / uncertain is left to the LLM's
    # hint ladder, unchanged.
    question = _TransientQuestion(
        question_text=in_flight.question_text,
        question_type=in_flight.question_type,
        reference_answer=in_flight.reference_answer,
        options=in_flight.options or [],
    )
    try:
        result = grade_answer(question=question, student_answer=answer)
    except Exception as exc:
        logger.warning(
            "autograde_bare_answer: grader raised %s (session=%s) — skipping",
            type(exc).__name__, session.pk,
        )
        return None

    if result.verdict.value != 'correct':
        return None
    if result.tier not in (_AUTOGRADE_DET_TIERS | _AUTOGRADE_LLM_TIERS):
        return None
    if result.tier in _AUTOGRADE_LLM_TIERS:
        # Log every non-deterministic salvage. Each one makes the model look
        # more compliant than it is, and the compliance harness must be able to
        # tell "the model called record_answer" from "the server rescued it".
        logger.info(
            "[simple_tutor] autograde salvaged a %s verdict via tier=%s "
            "(session=%s) — model did not call record_answer",
            result.verdict.value, result.tier, session.pk,
        )

    # It IS clearly correct. Route through the real handler so the slot clears
    # and the result dict is byte-identical to a model-issued record_answer.
    recorded = handle_record_answer(session, extracted_answer=answer)
    if not recorded.get('recorded'):
        return None

    logger.info(
        "[simple_tutor] autograded a clearly-correct bare answer session=%s "
        "qtype=%s tier=%s ext=%r (model skipped record_answer)",
        session.pk, in_flight.question_type, result.tier, answer[:40],
    )
    return {'tool': 'record_answer', 'result': recorded, 'autograded': True}


DEFAULT_COMPETENCE_THRESHOLD = 1   # number of CORRECT verdicts to call it


def maybe_advance_step(
    session: 'TutorSession',
    *,
    turn_cap: int = DEFAULT_STEP_TURN_CAP,
    competence_threshold: int = DEFAULT_COMPETENCE_THRESHOLD,
) -> bool:
    """Automatic step advancement, called after each engine turn.

    Since advance_step was removed (2026-08-05) this is the ONLY advancement
    path — it was already doing the work, since the model called advance_step
    once in 1,443 production turns while every measured session still reached
    its last step. The two triggers:

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
    from ai_tutor.apps.curriculum.models import LessonStep

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
    forced_reason = None

    # Trigger 0 — repetition. The model re-asked an already-correct question
    # _REPEAT_STREAK_TO_ADVANCE times in a row (flag set by _note_pose_repetition).
    # Move the lesson forward regardless of competence/turn-cap so the session
    # walks out of the loop instead of deadlocking. Pop the flag here so it fires
    # once per streak.
    es0 = getattr(session, 'engine_state', None) or {}
    repeat_force = isinstance(es0, dict) and es0.pop('_repeat_force_advance', False)
    if repeat_force:
        session.engine_state = es0   # keep the popped flag out of the DB
        forced = True
        forced_reason = 'repetition (re-asked an already-correct question)'
    else:
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
            forced_reason = f'turn_cap={turn_cap} exceeded'

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
            'reason': forced_reason,
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
