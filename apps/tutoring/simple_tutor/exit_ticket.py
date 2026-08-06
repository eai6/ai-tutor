"""Lightweight exit-ticket submit for the simple-tutor engine.

Bypasses ``ConversationalTutor`` entirely — no sentence-transformer
load, no skills graph init, no conversation hydration, no
gamification cascade. Just:

  1. Pull the selected questions (the same set the engine surfaced
     to the frontend, via ``engine_state['selected_exit_ticket_ids']``)
  2. Deterministic MCQ compare in a single loop
  3. Optional batched LLM call for non-MCQ items (defaults to MCQ-only
     per ``EXIT_TICKET_TYPES`` — most tickets skip step 3 entirely)
  4. ONE ``ExitTicketAttempt`` write with the per_question +
     eo_competency shape M13 remediation reads
  5. ``StudentLessonProgress`` update (best_score, attempts, mastery)
  6. Return the response shape ``chat_exit_ticket`` view expects

Designed for sub-second latency on all-MCQ tickets against staging
Postgres. The legacy CT path was 5-8s of cold-init + ORM serial
writes — this lives at <500ms for the typical 10-MCQ case.

Reuses ``apps.tutoring.exit_ticket_grader`` for the non-MCQ batch
path so we don't duplicate grading logic.
"""
from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)


def submit_exit_ticket(session, answers: list) -> dict:
    """Grade + persist an exit-ticket submission for the simple-tutor
    engine. Returns the JSON payload chat_exit_ticket should send.

    Args:
        session: TutorSession the student is submitting on.
        answers: list of student answers in render order. For MCQ:
            single-letter strings ('A'..'D'). For other types: any
            shape the existing exit_ticket_grader handles.

    Response shape (mirrors what the legacy CT.submit_exit_ticket
    returns so the frontend renders identically):
        {
            'message': str,             # "Scored X/10" / passing notice
            'phase': 'exit_ticket',
            'is_complete': bool,
            'exit_ticket': {
                'results': [...],       # per-question correctness + hint
                'score': int,
                'passed': bool,
                'total': int,
                'passing_score': int,
                'competency': {...},    # added by the view's enrich step
            }
        }
    """
    from apps.tutoring.models import (
        ExitTicket, ExitTicketQuestion, ExitTicketAttempt,
        StudentLessonProgress,
    )

    et = ExitTicket.objects.filter(lesson=session.lesson).first()
    if et is None:
        return _empty_payload("This lesson has no exit ticket.")

    # Question order MUST match what the engine surfaced to the
    # frontend (the shuffled selection persisted in engine_state by
    # _build_exit_ticket_payload). Without this the answers index
    # against the wrong questions — the M12.9 grading bug.
    state = session.engine_state or {}
    selected_ids = state.get('selected_exit_ticket_ids') or []
    if selected_ids:
        q_map = {
            q.id: q for q in ExitTicketQuestion.objects.filter(
                id__in=selected_ids,
            )
        }
        questions = [q_map[qid] for qid in selected_ids if qid in q_map]
    else:
        # Fallback (resume / legacy session without the persisted IDs):
        # use the first N questions in canonical order. Same shape the
        # frontend would have rendered if no shuffle happened.
        questions = list(
            ExitTicketQuestion.objects.filter(exit_ticket=et)[:len(answers)]
        )

    if not questions:
        return _empty_payload("No exit-ticket questions to grade.")

    # ── Grade per-question ──────────────────────────────────────────
    # MCQ is deterministic letter-compare. Non-MCQ items get bundled
    # into a single batched LLM call via the existing grader.
    from apps.tutoring.exit_ticket_grader import (
        build_batch_grade_item, grade_written_responses_batch,
    )
    batch_items = []
    deterministic_verdicts: dict[int, bool] = {}

    for i, q in enumerate(questions):
        student_answer = answers[i] if i < len(answers) else ''
        q_type = (q.question_type or 'mcq') or 'mcq'

        if q_type == 'mcq':
            picked = _normalize_letter(student_answer)
            correct_letter = _normalize_letter(q.correct_answer or '')
            deterministic_verdicts[i] = (
                bool(picked) and picked == correct_letter
            )
            continue

        # Numeric fast-path: short_numeric where the student typed a
        # number that matches the reference. Avoids an LLM call on
        # the common "just a number" answer.
        if q_type == 'short_numeric':
            ref = _try_number(q.correct_answer or '')
            given = _try_number(student_answer)
            if ref is not None and given is not None:
                deterministic_verdicts[i] = (abs(ref - given) < 1e-6)
                continue

        # Anything else (short_answer, fill_in_blank, matching, etc.)
        # → batch LLM call.
        batch_items.append(build_batch_grade_item(i, q, student_answer))

    if batch_items:
        # One LLM round-trip for ALL non-deterministic items.
        from apps.llm.models import ModelConfig
        from apps.llm.client import get_llm_client
        judge_client = None
        try:
            cfg = ModelConfig.get_for('judge')
            if cfg:
                judge_client = get_llm_client(cfg)
        except Exception as e:
            logger.warning(
                "[simple_tutor.exit_ticket] judge client load failed: %s", e,
            )
        batch_results = grade_written_responses_batch(
            batch_items, llm_client=judge_client,
        )
        for br in batch_results:
            deterministic_verdicts[br.index] = bool(br.correct)

    # ── Build per-question results list (what frontend renders) ─────
    per_question_for_attempt: list = []
    eo_competency_map: dict = {}
    results_list: list = []
    correct_count = 0

    for i, q in enumerate(questions):
        is_correct = bool(deterministic_verdicts.get(i, False))
        if is_correct:
            correct_count += 1
        student_answer = answers[i] if i < len(answers) else ''
        eo = (q.enabling_objective or q.concept_tag or '').strip()
        q_type = q.question_type or 'mcq'

        # Frontend results shape — matches what legacy CT emits.
        result = {
            'index': i,
            'question_id': q.id,
            'question_type': q_type,
            'question': q.question_text,
            'selected': student_answer,
            'is_correct': is_correct,
            'concept_tag': q.concept_tag or '',
            'enabling_objective': eo,
        }
        if q_type == 'mcq':
            result['options'] = [
                {'letter': 'A', 'text': q.option_a or ''},
                {'letter': 'B', 'text': q.option_b or ''},
                {'letter': 'C', 'text': q.option_c or ''},
                {'letter': 'D', 'text': q.option_d or ''},
            ]
            result['correct'] = q.correct_answer or ''
        if not is_correct:
            result['hint'] = (q.explanation or '').strip()
        results_list.append(result)

        # M13 remediation shape — per_question + eo_competency on
        # ExitTicketAttempt.answers. Without this the engine's
        # _build_exit_ticket_review block can't surface the missed
        # objectives in REMEDIATION mode.
        per_question_for_attempt.append({
            'concept_tag': q.concept_tag or '',
            'enabling_objective': eo,
            'correct': is_correct,
            'selected': student_answer,
            'question_type': q_type,
        })
        if eo:
            bucket = eo_competency_map.setdefault(eo, {
                'asked': 0, 'correct': 0,
                'failed_question_ids': [], 'is_mastered': False,
            })
            bucket['asked'] += 1
            if is_correct:
                bucket['correct'] += 1
            else:
                bucket['failed_question_ids'].append(q.id)

    for eo, bucket in eo_competency_map.items():
        bucket['is_mastered'] = bucket['correct'] >= bucket['asked']

    total = len(questions)
    passing_score = et.passing_score or 8
    passed = correct_count >= passing_score

    # ── Persist a single ExitTicketAttempt ──────────────────────────
    ExitTicketAttempt.objects.create(
        exit_ticket=et,
        student=session.student,
        session=session,
        score=correct_count,
        passed=passed,
        answers={
            'per_question': per_question_for_attempt,
            'eo_competency': eo_competency_map,
        },
        completed_at=timezone.now(),
    )

    # ── StudentLessonProgress (best_score / mastery / attempts) ─────
    score_pct = round(correct_count / total, 4) if total else 0.0
    progress, _ = StudentLessonProgress.objects.get_or_create(
        student=session.student,
        lesson=session.lesson,
        defaults={'institution': session.institution},
    )
    progress.best_score = score_pct
    progress.attempts_count = (progress.attempts_count or 0) + 1
    progress.last_attempt_at = timezone.now()
    progress.last_completion_session = session
    if passed:
        if progress.mastery_level != 'mastered':
            progress.mastery_level = 'mastered'
    else:
        # Mirror legacy: only DEMOTE if not already mastered (don't
        # erase a prior passing attempt's mastery).
        if progress.mastery_level == 'mastered':
            pass
        elif progress.mastery_level != 'in_progress':
            progress.mastery_level = 'in_progress'
    progress.save()

    # ── Session-level state ─────────────────────────────────────────
    from apps.tutoring.models import TutorSession
    if passed:
        session.status = TutorSession.Status.COMPLETED
        session.ended_at = timezone.now()
        session.completed_lesson_at = timezone.now()
        session.mastery_achieved = True
    es = session.engine_state or {}
    es['exit_ticket_score'] = correct_count
    es['exit_ticket_total'] = total
    es['exit_ticket_passed'] = passed
    session.engine_state = es
    session.save()

    logger.info(
        "[simple_tutor] exit_ticket submitted session=%s score=%s/%s "
        "passed=%s mcq=%s batch=%s",
        session.pk, correct_count, total, passed,
        sum(1 for q in questions if (q.question_type or 'mcq') == 'mcq'),
        len(batch_items),
    )

    from django.utils import translation
    # Render in the COURSE locale, not the ambient request locale: the
    # exit-ticket submit endpoint (chat_exit_ticket) doesn't activate the
    # course locale the way the page view does, so without this override a
    # pt-mz student could get an English result message.
    try:
        _loc = (session.lesson.unit.course.locale or 'en-us').lower()
    except Exception:
        _loc = 'en-us'
    with translation.override(_loc):
        if passed:
            message = translation.gettext(
                "🎉 You scored %(score)d/%(total)d — that's a pass. Well done!"
            ) % {'score': correct_count, 'total': total}
        else:
            message = translation.gettext(
                "📋 **Exit ticket review**\n\nYou scored %(score)d out of %(total)d. "
                "Let's revisit the concepts you missed."
            ) % {'score': correct_count, 'total': total}
            # Open remediation with an actual question. Without this the
            # student is told to "revisit the concepts" and handed nothing to
            # do: no in-flight slot, no prompt, and remediation only starts if
            # they happen to type something unprompted.
            #
            # This does NOT call an LLM. Proactive remediation was removed on
            # 2026-05-26 (views.py::chat_exit_ticket) because it added a
            # synchronous 5-15s model call on top of deterministic MCQ grading
            # and made the "Grading..." spinner look hung. Since the tutor
            # became catalog-only the server can pick the question itself, so
            # the opener costs a DB read — the objection no longer applies.
            opener = _remediation_opening_question(session, eo_competency_map)
            if opener:
                message = f"{message}\n\n{opener}"

    # The third payload site. respond_for_view and _project_start_payload both
    # carry answer_choices; this one did not, and the remediation opener is
    # exactly where it matters — it deletes the lesson's stale slot and poses a
    # fresh question, so the buttons on screen belong to a question that no
    # longer exists. Device session 81: the review text asked about two
    # villages while the picker still offered "Locate northing 29 and mark
    # where the lines intersect" from the pre-quiz question. The slot was
    # correct the whole time; nothing told the frontend to repaint it.
    from apps.tutoring.simple_tutor.engine import (
        _answer_choices_payload, _remediation_progress_payload,
    )

    return {
        'message': message,
        'phase': 'exit_ticket',
        'is_complete': passed,
        'answer_choices': _answer_choices_payload(session),
        'remediation_progress': _remediation_progress_payload(session),
        'exit_ticket': {
            'results': results_list,
            'score': correct_count,
            'passed': passed,
            'total': total,
            'passing_score': passing_score,
        },
    }


# ============================================================================
# Helpers
# ============================================================================


def _normalize_letter(s: str) -> str:
    """Map a free-form student answer to an MCQ letter ('A'..'D'). Returns
    empty string when the input doesn't look like a letter answer.
    """
    if not s:
        return ''
    s = str(s).strip().upper()
    if not s:
        return ''
    first = s[0]
    return first if first in ('A', 'B', 'C', 'D') else ''


def _try_number(s) -> float | None:
    """Best-effort numeric coercion for the short_numeric fast-path."""
    if s is None or s == '':
        return None
    try:
        return float(str(s).strip().replace(',', ''))
    except (TypeError, ValueError):
        return None


def maybe_pose_remediation_next(session) -> str:
    """Pose the next remediation question server-side, or '' if not needed.

    Remediation ends the moment a turn leaves no question in flight: there is
    no step to advance to and no warm-up to fall back on, so the student is
    handed a compliment and a dead end.

    That is what happens. Measured over 4 offline turns answering the opener
    correctly, the tutor posed a follow-up 0 times — it wrote "Well done, you
    correctly identified..." and stopped. Neither an empty pool (fixed), nor
    the prompt licensing prose (fixed), nor moving the remediation rules into
    Block 0 as a mode (tried) changed it: a 4B does not reliably make a second
    tool call in the same turn, and remediation is the one mode where missing
    it terminates the session rather than costing a beat.

    So the server poses instead. This is the codebase's existing position —
    tool calls are hints, the server owns question state
    (auto-memory/feedback_server_owns_question_state.md) — and the opener
    already works this way; this is the same move for every turn after it.

    Returns the rendered stem + options to append to the reply, or '' when a
    question is already in flight, remediation is over, or the pool is dry.
    """
    from apps.tutoring.models import InFlightQuestion
    from apps.tutoring.simple_tutor.tools import (
        build_question_pool, handle_pose_question_by_index,
    )

    try:
        es = getattr(session, 'engine_state', None) or {}
        if isinstance(es, dict) and es.get('remediation_complete'):
            return ''
        if InFlightQuestion.objects.filter(session=session).exists():
            return ''      # the model posed one itself — leave it alone
        from apps.tutoring.simple_tutor.engine import _build_exit_ticket_review
        review = _build_exit_ticket_review(session)
        if not review or review.get('passed') or not review.get('missed_objectives'):
            return ''

        pool = build_question_pool(session)
        if not pool:
            return ''
        chosen = pool[0]
        result = handle_pose_question_by_index(
            session, question_index=1, question_pool=[chosen])
        if not result.get('posed'):
            logger.warning(
                "[simple_tutor] remediation follow-up could not pose "
                "session=%s: %s", session.pk, result.get('error'))
            return ''

        lines = [(chosen.question_text or '').strip()]
        for letter in ('A', 'B', 'C', 'D'):
            opt = (getattr(chosen, f'option_{letter.lower()}', '') or '').strip()
            if opt:
                lines.append(f'{letter}) {opt}')
        logger.info(
            "[simple_tutor] remediation follow-up posed server-side "
            "session=%s q=%s", session.pk, chosen.pk)
        return '\n'.join(lines)
    except Exception:  # noqa: BLE001 — a missing follow-up must not lose the turn
        logger.warning(
            "[simple_tutor] remediation follow-up failed session=%s",
            getattr(session, 'pk', None), exc_info=True)
        return ''


def _remediation_opening_question(session, eo_competency_map: dict) -> str:
    """Pose the first question of remediation and return it rendered for the
    student, or '' when there is nothing sensible to ask.

    Every tutor turn owes the student something to do. A failed exit ticket
    that ends on "let's revisit the concepts you missed" is a dead end: no
    in-flight slot exists, so even if the student replies there is nothing to
    grade, and the lesson stalls.

    Deterministic by design — no LLM call:
      * pick the missed objective with the worst correct/asked ratio,
      * prefer a bank question on that objective the student did NOT just get
        wrong (re-asking the identical item teaches nothing; a sibling item on
        the same objective is the point of remediation),
      * fall back to a failed one if that objective has no sibling.

    Returns '' on any problem — a missing opener is a worse turn, but a raised
    exception here would lose the student's whole submission.
    """
    from apps.tutoring.models import ExitTicketQuestion
    from apps.tutoring.simple_tutor.tools import (
        _allowed_tutoring_types, handle_pose_question_by_index,
    )

    try:
        missed = [
            (eo, b) for eo, b in (eo_competency_map or {}).items()
            if eo and isinstance(b, dict)
            and int(b.get('asked') or 0) > 0
            and int(b.get('correct') or 0) < int(b.get('asked') or 0)
        ]
        if not missed:
            return ''
        # Worst-first: the objective they understood least.
        missed.sort(key=lambda kv: (
            int(kv[1].get('correct') or 0) / max(int(kv[1].get('asked') or 1), 1),
            kv[0],
        ))
        objective, bucket = missed[0]
        failed_ids = {
            q for q in (bucket.get('failed_question_ids') or [])
            if isinstance(q, int)
        }

        candidates = list(
            ExitTicketQuestion.objects
            .filter(
                exit_ticket__lesson=session.lesson,
                enabling_objective=objective,
                question_type__in=_allowed_tutoring_types(),
            )
            .order_by('order_index', 'id')
        )
        if not candidates:
            return ''

        # Never re-open on something they already got right. The anti-repeat
        # guard logs these as `already_correct=True` and forces the lesson
        # onward — opening remediation with one wastes the turn and reads as
        # the tutor not having noticed.
        from apps.tutoring.simple_tutor.tools import _norm_q
        es = getattr(session, 'engine_state', None) or {}
        answered = set(es.get('answered_correct') or []) if isinstance(es, dict) else set()
        unseen = [
            q for q in candidates
            if _norm_q(q.question_text or '') not in answered
        ]
        pool_for_pick = unseen or candidates

        # Open on a question they actually got wrong. Remediation is meant to
        # work back through the missed items, and the completion check counts
        # them — so opening on a sibling they never failed leaves the item that
        # tripped them up unaddressed and the counter unable to move.
        # Reverses the earlier "prefer a fresh sibling" preference.
        failed_first = [q for q in pool_for_pick if q.pk in failed_ids]
        chosen = (failed_first or pool_for_pick)[0]

        # Retire the lesson's leftover in-flight question first. Submitting the
        # exit ticket ends the lesson phase, so a slot still open from it is
        # stale by definition — and leaving it there silently blocks this pose:
        # handle_pose_question's anti-desync guard refuses to pose over a slot
        # with attempt_count 0 while `_student_intent` is still 'answer', which
        # is exactly the state the final lesson turn leaves behind.
        #
        # That is why remediation only started after the student typed "okay":
        # the filler flipped the intent to non_engagement, the guard stopped
        # firing, and the question the submit response should have carried
        # finally appeared a turn late.
        from apps.tutoring.models import InFlightQuestion as _IFQ
        _IFQ.objects.filter(session=session).delete()

        result = handle_pose_question_by_index(
            session, question_index=1, question_pool=[chosen])
        if not result.get('posed'):
            logger.warning(
                "[simple_tutor] remediation opener could not pose session=%s "
                "objective=%r: %s",
                session.pk, objective[:60], result.get('error'),
            )
            return ''

        lines = [
            f"Let's start with **{objective}**.",
            '',
            (chosen.question_text or '').strip(),
        ]
        for letter in ('A', 'B', 'C', 'D'):
            opt = (getattr(chosen, f'option_{letter.lower()}', '') or '').strip()
            if opt:
                lines.append(f'{letter}) {opt}')
        logger.info(
            "[simple_tutor] remediation opened session=%s objective=%r q=%s",
            session.pk, objective[:60], chosen.pk,
        )
        return '\n'.join(lines)
    except Exception:  # noqa: BLE001 — never lose the submission
        logger.warning(
            "[simple_tutor] remediation opener failed session=%s",
            getattr(session, 'pk', None), exc_info=True,
        )
        return ''


def _empty_payload(message: str) -> dict:
    """Bail-out payload for a lesson with no exit ticket or no questions.

    ``answer_choices`` is explicitly None rather than absent. Both callers
    return before anything is posed, so there is genuinely nothing in flight —
    but the frontend clears the picker on a null and leaves it alone on an
    undefined, and 'happens to be falsy' is how the stale picker in device
    session 81 survived. Say it, don't imply it.
    """
    return {
        'message': message,
        'phase': 'exit_ticket',
        'is_complete': False,
        'answer_choices': None,
        'exit_ticket': {
            'results': [], 'score': 0, 'passed': False,
            'total': 0, 'passing_score': 0,
        },
    }
