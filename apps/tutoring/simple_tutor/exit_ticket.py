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

    if passed:
        message = (
            f"🎉 You scored {correct_count}/{total} — that's a pass. "
            "Well done!"
        )
    else:
        message = (
            f"📋 **Exit ticket review**\n\nYou scored {correct_count} "
            f"out of {total}. Let's revisit the concepts you missed."
        )

    return {
        'message': message,
        'phase': 'exit_ticket',
        'is_complete': passed,
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


def _empty_payload(message: str) -> dict:
    return {
        'message': message,
        'phase': 'exit_ticket',
        'is_complete': False,
        'exit_ticket': {
            'results': [], 'score': 0, 'passed': False,
            'total': 0, 'passing_score': 0,
        },
    }
