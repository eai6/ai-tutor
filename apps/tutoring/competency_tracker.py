"""Longitudinal competency tracking — per (student, teaching objective).

A teaching objective is the atomic competency unit. Every assessment
question is tagged with `concept_tag` = the teaching objective it
assesses. This module aggregates *all* student attempts (per-lesson
exit tickets + course summatives) and produces:

  - Per-student, per-objective: latest score, baseline score, final score,
    delta, total attempts.
  - Per-class (per-course): roll-up by objective showing how many students
    have it at each measurement point.

Used by the teacher dashboard's class-competency view and the
per-student competency view. See `memory/summative_assessments_plan.md`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional


def _normalize_tag(tag: str) -> str:
    return ' '.join((tag or '').split()).strip()


def _per_question_rows(attempt) -> Iterable[dict]:
    """Yield per-question rows from an attempt's stored result blob."""
    res = (attempt.answers or {}).get('result') or {}
    for row in res.get('per_question') or []:
        yield row


def collect_objective_signals_for_course(course, students=None) -> dict:
    """Walk every relevant attempt for the given course and return the
    longitudinal signal grouped by (student_id, normalized_concept_tag).

    Sources scanned:
      - All `ExitTicketAttempt` rows on summative ExitTickets where
        `course == course`.
      - All `ExitTicketAttempt` rows on per-lesson exit tickets where
        the lesson's course == course.

    Returns:
        {
            (student_id, concept_tag): {
                'baseline':   {correct, total, attempt_id, completed_at} | None,
                'final':      {...} | None,
                'latest':     {...} | None,   # most recent attempt regardless of purpose
                'practice':   [list of practice rows],   # exit-ticket attempts
                'all_attempts': int,
            }, ...
        }
    """
    from apps.tutoring.models import ExitTicket, ExitTicketAttempt

    summative_q = ExitTicket.objects.filter(
        course=course, assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
    )
    lesson_q = ExitTicket.objects.filter(
        lesson__unit__course=course,
        assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
    )

    attempts_qs = ExitTicketAttempt.objects.filter(
        exit_ticket__in=list(summative_q) + list(lesson_q),
        completed_at__isnull=False,
    )
    if students is not None:
        attempts_qs = attempts_qs.filter(student__in=students)
    attempts_qs = attempts_qs.select_related('exit_ticket', 'student').order_by(
        'student_id', 'completed_at'
    )

    out: Dict[tuple, dict] = {}

    for attempt in attempts_qs:
        sid = attempt.student_id
        purpose = attempt.purpose

        # Walk per-question rows so a single attempt fans out per objective.
        per_obj_correct: Dict[str, int] = defaultdict(int)
        per_obj_total: Dict[str, int] = defaultdict(int)
        for row in _per_question_rows(attempt):
            tag = _normalize_tag(row.get('concept_tag') or '')
            if not tag:
                continue
            per_obj_total[tag] += 1
            if row.get('is_correct'):
                per_obj_correct[tag] += 1

        for tag, total in per_obj_total.items():
            key = (sid, tag)
            bucket = out.setdefault(key, {
                'baseline': None, 'final': None, 'latest': None,
                'practice': [], 'all_attempts': 0,
            })
            bucket['all_attempts'] += 1
            row = {
                'correct': per_obj_correct[tag],
                'total': total,
                'attempt_id': attempt.id,
                'completed_at': attempt.completed_at,
                'is_summative': bool(attempt.exit_ticket.course_id),
                'purpose': purpose,
            }
            # Always update 'latest' since attempts come in chronological order.
            bucket['latest'] = row
            if purpose == 'baseline' and bucket['baseline'] is None:
                bucket['baseline'] = row
            elif purpose == 'final':
                bucket['final'] = row
            elif purpose == 'practice':
                bucket['practice'].append(row)

    return out


def class_competency_matrix(course, *, students=None, objectives=None) -> dict:
    """Class-level competency roll-up for a course.

    Returns:
        {
            'objectives': [{
                'tag': str,
                'students_with_baseline': int,
                'students_with_final': int,
                'students_with_any': int,
                'avg_baseline_pct': float | None,
                'avg_latest_pct': float | None,
                'avg_final_pct': float | None,
                'delta_pct': float | None,    # final - baseline, average
                'mastered_latest': int,       # students with latest >= 70%
            }, ...],
            'total_students': int,
            'students_attempted': int,
        }
    """
    from apps.curriculum.content_generator import combined_objectives_for_lesson

    # Build the canonical objective list from the curriculum (so we show
    # gaps where no student has touched an objective yet).
    if objectives is None:
        seen = set()
        canonical = []
        for unit in course.units.prefetch_related('lessons').order_by('order_index'):
            for lesson in unit.lessons.order_by('order_index'):
                for obj in combined_objectives_for_lesson(lesson):
                    norm = _normalize_tag(obj)
                    if norm in seen:
                        continue
                    seen.add(norm)
                    canonical.append(obj)
        objectives = canonical

    # Roster
    if students is None:
        from apps.accounts.models import Membership
        students = list(
            Membership.objects.filter(
                role='student', is_active=True,
                institution=course.institution,
            ).values_list('user_id', flat=True)
        ) if course.institution_id else []

    signals = collect_objective_signals_for_course(course, students=students or None)

    # Per-objective accumulator
    rows = []
    students_attempted = set()
    for obj in objectives:
        tag = _normalize_tag(obj)
        baseline_pcts = []
        latest_pcts = []
        final_pcts = []
        deltas = []
        students_with_baseline = set()
        students_with_final = set()
        students_with_any = set()
        mastered_latest = 0

        for sid in (students or []):
            bucket = signals.get((sid, tag))
            if not bucket:
                continue
            students_with_any.add(sid)
            students_attempted.add(sid)
            if bucket['baseline']:
                pct = (bucket['baseline']['correct'] / bucket['baseline']['total']) * 100
                baseline_pcts.append(pct)
                students_with_baseline.add(sid)
            if bucket['final']:
                pct = (bucket['final']['correct'] / bucket['final']['total']) * 100
                final_pcts.append(pct)
                students_with_final.add(sid)
            if bucket['latest']:
                pct = (bucket['latest']['correct'] / bucket['latest']['total']) * 100
                latest_pcts.append(pct)
                if pct >= 70:
                    mastered_latest += 1
            if bucket['baseline'] and bucket['final']:
                b = (bucket['baseline']['correct'] / bucket['baseline']['total']) * 100
                f = (bucket['final']['correct'] / bucket['final']['total']) * 100
                deltas.append(f - b)

        rows.append({
            'tag': obj,
            'students_with_baseline': len(students_with_baseline),
            'students_with_final': len(students_with_final),
            'students_with_any': len(students_with_any),
            'avg_baseline_pct': (sum(baseline_pcts) / len(baseline_pcts)) if baseline_pcts else None,
            'avg_latest_pct': (sum(latest_pcts) / len(latest_pcts)) if latest_pcts else None,
            'avg_final_pct': (sum(final_pcts) / len(final_pcts)) if final_pcts else None,
            'delta_pct': (sum(deltas) / len(deltas)) if deltas else None,
            'mastered_latest': mastered_latest,
        })

    return {
        'objectives': rows,
        'total_students': len(students or []),
        'students_attempted': len(students_attempted),
    }


def student_competency_table(course, student) -> dict:
    """Per-student per-objective table.

    Returns:
        {
            'objectives': [{
                'tag': str,
                'baseline_pct': float | None,
                'latest_pct': float | None,
                'final_pct': float | None,
                'delta_pct': float | None,
                'attempts': int,
            }, ...],
        }
    """
    matrix = class_competency_matrix(course, students=[student.id])
    # Re-shape: extract the single-student values from each objective row.
    signals = collect_objective_signals_for_course(course, students=[student])

    rows = []
    for obj_row in matrix['objectives']:
        tag = _normalize_tag(obj_row['tag'])
        bucket = signals.get((student.id, tag))
        if bucket:
            def pct(b):
                return (b['correct'] / b['total'] * 100) if b else None
            base = pct(bucket['baseline'])
            late = pct(bucket['latest'])
            fin = pct(bucket['final'])
            delta = (fin - base) if (base is not None and fin is not None) else None
            attempts = bucket['all_attempts']
        else:
            base = late = fin = delta = None
            attempts = 0
        rows.append({
            'tag': obj_row['tag'],
            'baseline_pct': base,
            'latest_pct': late,
            'final_pct': fin,
            'delta_pct': delta,
            'attempts': attempts,
        })
    return {'objectives': rows}
