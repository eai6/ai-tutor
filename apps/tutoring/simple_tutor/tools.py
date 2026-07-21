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


def _record_answered_correct(session: 'TutorSession', question_text: str) -> None:
    """Remember a question the student just answered correctly, so a later exact
    re-ask can be detected. Guarded read-copy-mutate-save on engine_state."""
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
    es['answered_correct'] = answered[-_ANSWERED_CORRECT_CAP:]
    # A fresh correct answer ends any repeat streak.
    es['repeat_pose_streak'] = 0
    session.engine_state = es
    try:
        session.save(update_fields=['engine_state'])
    except Exception:
        logger.warning("[simple_tutor] could not persist answered_correct "
                       "session=%s", getattr(session, 'pk', None))


def _note_pose_repetition(session: 'TutorSession', question_text: str) -> bool:
    """Called on every pose. Returns True and sets a pending-advance flag when the
    model has now re-asked an already-correct question _REPEAT_STREAK_TO_ADVANCE
    times in a row — the signal to force the lesson forward. Returns False for a
    normal (non-repeat) pose, and resets the streak."""
    if not _antirepeat_enabled():
        return False
    es = getattr(session, 'engine_state', None) or {}
    if not isinstance(es, dict):
        return False
    answered = es.get('answered_correct')
    is_repeat = isinstance(answered, list) and _norm_q(question_text) in answered
    streak = int(es.get('repeat_pose_streak') or 0)
    force = False
    if is_repeat:
        streak += 1
        logger.info(
            "[simple_tutor] repeat_pose session=%s streak=%d question=%r "
            "(already answered correctly)",
            session.pk, streak, (question_text or '')[:60],
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


def _catalog_match_for_stem(session, question_text: str):
    """(options, correct_letter) of the catalog question whose stem exactly
    matches ``question_text`` (normalised), within this session's lesson.
    None when no unambiguous match. Used to salvage optionless MCQ poses
    and to keep the reference letter coherent with the displayed options."""
    from apps.tutoring.models import ExitTicketQuestion
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

    # Normalise options — must be a list of strings. Strip any leading
    # letter label the model baked into the option text ("A) 11" → "11");
    # the platform adds letters at render time, and stored prefixes caused
    # double-lettered options ("A) A) 11") throughout the cycle-7 sweeps.
    from apps.tutoring.simple_tutor.grader import _OPT_PREFIX_RE
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
    from apps.tutoring.simple_tutor.grader import _option_number
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
            from apps.tutoring.simple_tutor.grader import _norm_option
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
        _es = getattr(session, 'engine_state', None) or {}
        _answered = _es.get('answered_correct') if isinstance(_es, dict) else None
        if isinstance(_answered, list) and _key in _answered:
            _rejected = _es.get('repeat_rejected_stems')
            if not isinstance(_rejected, list):
                _rejected = []
            if _key not in _rejected:
                _es['repeat_rejected_stems'] = (
                    _rejected + [_key])[-_ANSWERED_CORRECT_CAP:]
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
    repeat_force_advance = _note_pose_repetition(session, qtext)

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
        # Remember this stem BEFORE deleting the slot, so an exact re-ask on a
        # later turn is detectable (anti-repetition).
        _record_answered_correct(session, in_flight.question_text)
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
_AUTOGRADE_QTYPES = frozenset({'mcq', 'math', 'numeric', 'short_numeric'})
_AUTOGRADE_DET_TIERS = frozenset({'mcq', 'math'})
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

    from apps.tutoring.models import InFlightQuestion
    from apps.tutoring.simple_tutor.grader import grade_answer

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

    if result.verdict.value != 'correct' or result.tier not in _AUTOGRADE_DET_TIERS:
        return None

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
