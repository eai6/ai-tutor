"""Audit ALL generated curriculum math content against Layers 1+2.

Walks four sources in the database and runs the same checks the new
content-generation pipeline runs:

  1. LessonStep.teacher_script / hints / educational_content / etc.
     → Layer 1 (auto-correct prose, detect-only on stems)
  2. LessonStep.question / expected_answer / choices
     → Layer 1 (detect-only — answer-key bound)
  3. ExitTicketQuestion (lesson exit tickets, where ExitTicket.lesson_id is set)
     → Layer 1 (explanation auto-correct, stem/options detect-only)
     → Layer 2 (answer-key cross-check)
  4. ExitTicketQuestion (summative exams, where ExitTicket.course_id is set)
     → same checks; aggregated per-course, not per-lesson

Produces:
  - Per-lesson summary (steps + question audit counts)
  - Per-summative-exam summary (course-level)
  - Aggregate distribution (math vs non-math, auto-correct vs
    detect-only, mismatch counts, summative vs lesson exit-ticket)
  - CSV output at /tmp/content_audit.csv for spreadsheet review

DOES NOT MUTATE anything in the database — read-only audit. Teacher
manually triggers regeneration on flagged lessons / summatives.

Run locally:
    python scripts/audit_existing_content.py
    python scripts/audit_existing_content.py --math-only
    python scripts/audit_existing_content.py --course-id 5

Run against production (Azure PostgreSQL):
    DATABASE_URL='postgresql://...' \\
    python scripts/audit_existing_content.py --math-only --csv /tmp/prod_audit.csv

`DATABASE_URL` overrides the dev `db.sqlite3` via dj-database-url.
Confirm you're hitting prod by checking the first line of output:
"Auditing N course(s)" — prod has many more courses than local.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_tutor.config.settings")

import django  # noqa: E402
django.setup()

from ai_tutor.apps.curriculum.content_verifier import (  # noqa: E402
    verify_exit_ticket_question,
    verify_lesson_step,
)
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep  # noqa: E402
from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketQuestion  # noqa: E402
from ai_tutor.apps.tutoring.question_validator import cross_check_question  # noqa: E402


_RESET = "\033[0m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_GREY = "\033[90m"


def _step_to_dict(step: LessonStep) -> dict:
    """Project a LessonStep ORM row onto the dict shape the verifier
    expects (mirroring what `_save_steps_to_db` receives from the
    LLM)."""
    return {
        "order_index": step.order_index,
        "teacher_script": step.teacher_script or "",
        "question": step.question or "",
        "expected_answer": step.expected_answer or "",
        "hints": [
            step.hint_1 or "",
            step.hint_2 or "",
            step.hint_3 or "",
        ],
        "educational_content": step.educational_content,
        "choices": step.choices,
    }


def _question_to_dict(q: ExitTicketQuestion) -> dict:
    return {
        "question_type": q.question_type,
        "question_text": q.question_text,
        "question": q.question_text,
        "explanation": q.explanation or "",
        "option_a": q.option_a or "",
        "option_b": q.option_b or "",
        "option_c": q.option_c or "",
        "option_d": q.option_d or "",
        "correct": q.correct_answer or "",
        "correct_answer": q.correct_answer or "",
        "answer_data": q.answer_data or {},
    }


def audit_questions(
    questions, csv_writer: csv.writer, *,
    course_title: str, lesson_title: str, kind_prefix: str,
) -> tuple:
    """Run Layer 1 + Layer 2 over a queryset of ExitTicketQuestion
    rows. `kind_prefix` is "exit_ticket" or "summative" — appears in
    the CSV `kind` column so prod analysis can filter on it.

    Returns (question_audit_list, answer_key_mismatches_list).
    """
    question_audit: list = []
    answer_key_mismatches: list = []
    for q in questions.order_by("order_index"):
        qd = _question_to_dict(q)
        verify_exit_ticket_question(qd, question_index=q.order_index, audit=question_audit)
        ck = cross_check_question(qd, question_index=q.order_index)
        if ck is not None:
            answer_key_mismatches.append(ck)

    # Write findings to CSV.
    for e in question_audit:
        for fix in e.get("corrections", []):
            csv_writer.writerow([
                course_title, lesson_title,
                kind_prefix, e.get("question_index"), e.get("field"),
                "auto-correct" if e.get("auto_corrected") else "detect-only",
                fix.get("expression"), fix.get("claimed"), fix.get("correct"),
                "", "", "",
            ])
    for c in answer_key_mismatches:
        csv_writer.writerow([
            course_title, lesson_title,
            f"{kind_prefix}_answer_key", c.get("question_index"),
            c.get("pattern"), "mismatch", "", "", "",
            c.get("computed"), c.get("claimed"), c.get("reason"),
        ])
    return question_audit, answer_key_mismatches


def audit_lesson(lesson: Lesson, csv_writer: csv.writer) -> dict:
    """Run Layers 1+2 over one lesson's steps + lesson-level exit
    ticket. Returns aggregate counts. Skip summative coverage —
    that's audited per-course by audit_summative."""
    is_math = lesson.unit.course.is_math

    step_audit: list = []
    for step in LessonStep.objects.filter(lesson=lesson).order_by("order_index"):
        sd = _step_to_dict(step)
        # We work on a COPY so we don't mutate stored content.
        sd_copy = {k: v for k, v in sd.items()}
        verify_lesson_step(sd_copy, audit=step_audit)

    # Lesson-level exit ticket questions only (not summative).
    et_questions = ExitTicketQuestion.objects.filter(
        exit_ticket__lesson=lesson,
    )
    question_audit, answer_key_mismatches = audit_questions(
        et_questions, csv_writer,
        course_title=lesson.unit.course.title,
        lesson_title=lesson.title,
        kind_prefix="exit_ticket",
    )

    auto_step = sum(1 for e in step_audit if e.get("auto_corrected"))
    detect_step = len(step_audit) - auto_step
    auto_q = sum(1 for e in question_audit if e.get("auto_corrected"))
    detect_q = len(question_audit) - auto_q

    # Step-audit findings still need CSV rows (audit_questions
    # already wrote the question + answer-key rows).
    for e in step_audit:
        for fix in e.get("corrections", []):
            csv_writer.writerow([
                lesson.unit.course.title, lesson.title,
                "step", e.get("step_order"), e.get("field"),
                "auto-correct" if e.get("auto_corrected") else "detect-only",
                fix.get("expression"), fix.get("claimed"), fix.get("correct"),
                "", "", "",
            ])

    return {
        "is_math": is_math,
        "auto_step": auto_step,
        "detect_step": detect_step,
        "auto_q": auto_q,
        "detect_q": detect_q,
        "answer_key_mismatches": len(answer_key_mismatches),
    }


def audit_summative(course: Course, csv_writer: csv.writer) -> dict:
    """Run Layers 1+2 over a course's summative-exam questions.
    Returns counts. Skips courses without a summative exam."""
    summative = ExitTicket.objects.filter(
        course=course,
        assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
    ).first()
    if not summative:
        return None

    question_audit, answer_key_mismatches = audit_questions(
        ExitTicketQuestion.objects.filter(exit_ticket=summative),
        csv_writer,
        course_title=course.title,
        lesson_title="(summative exam)",
        kind_prefix="summative",
    )

    auto_q = sum(1 for e in question_audit if e.get("auto_corrected"))
    detect_q = len(question_audit) - auto_q

    return {
        "is_math": course.is_math,
        "question_count": ExitTicketQuestion.objects.filter(
            exit_ticket=summative,
        ).count(),
        "auto_q": auto_q,
        "detect_q": detect_q,
        "answer_key_mismatches": len(answer_key_mismatches),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--math-only", action="store_true")
    parser.add_argument("--course-id", type=int)
    parser.add_argument(
        "--csv", default="/tmp/content_audit.csv",
        help="CSV output path",
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N lessons (0 = all)")
    args = parser.parse_args()

    courses = Course.objects.all()
    if args.course_id:
        courses = courses.filter(id=args.course_id)
    if args.math_only:
        courses = [c for c in courses if c.is_math]
    else:
        courses = list(courses)

    print(f"\n{'='*78}")
    print(f"Auditing {len(courses)} course(s)")
    print(f"{'='*78}\n")

    total = defaultdict(int)
    by_course: dict = defaultdict(lambda: defaultdict(int))
    flagged_lessons: list = []

    with open(args.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "course", "lesson", "kind", "index", "field",
            "category", "expression", "claimed", "correct",
            "computed", "stored_answer", "reason",
        ])

        seen = 0
        for course in courses:
            # Per-course summative exam audit (course-level, not
            # per-lesson — runs once per course before walking lessons).
            sum_stats = audit_summative(course, w)
            if sum_stats:
                total["summatives"] += 1
                total["summative_questions"] += sum_stats["question_count"]
                total["sum_auto_q"] += sum_stats["auto_q"]
                total["sum_detect_q"] += sum_stats["detect_q"]
                total["sum_answer_key_mismatches"] += sum_stats[
                    "answer_key_mismatches"
                ]
                hits = (
                    sum_stats["auto_q"] + sum_stats["detect_q"]
                    + sum_stats["answer_key_mismatches"]
                )
                if hits:
                    color = _GREEN if sum_stats["is_math"] else _GREY
                    print(
                        f"  {color}[{course.title[:18]:<18}]{_RESET} "
                        f"{'(summative exam)':<40} "
                        f"q {sum_stats['auto_q']}+{sum_stats['detect_q']} "
                        f"key {_RED if sum_stats['answer_key_mismatches'] else _GREY}"
                        f"{sum_stats['answer_key_mismatches']}{_RESET}"
                    )

            lessons = Lesson.objects.filter(unit__course=course).order_by(
                "unit__order_index", "order_index",
            )
            for lesson in lessons:
                if args.limit and seen >= args.limit:
                    break
                seen += 1
                stats = audit_lesson(lesson, w)
                total["lessons"] += 1
                if stats["is_math"]:
                    total["math_lessons"] += 1
                for k in (
                    "auto_step", "detect_step",
                    "auto_q", "detect_q",
                    "answer_key_mismatches",
                ):
                    total[k] += stats[k]
                    by_course[course.title][k] += stats[k]

                # Print per-lesson row when something fired.
                hits = (
                    stats["auto_step"] + stats["detect_step"]
                    + stats["auto_q"] + stats["detect_q"]
                    + stats["answer_key_mismatches"]
                )
                if hits:
                    flagged_lessons.append((course.title, lesson.title, stats))
                    color = _GREEN if stats["is_math"] else _GREY
                    print(
                        f"  {color}[{course.title[:18]:<18}]{_RESET} "
                        f"{lesson.title[:40]:<40} "
                        f"steps {stats['auto_step']}+{stats['detect_step']} "
                        f"q {stats['auto_q']}+{stats['detect_q']} "
                        f"key {_RED if stats['answer_key_mismatches'] else _GREY}"
                        f"{stats['answer_key_mismatches']}{_RESET}"
                    )
            if args.limit and seen >= args.limit:
                break

    print(f"\n{'='*78}")
    print("AGGREGATE")
    print(f"{'='*78}")
    print(f"  Lessons audited:           {total['lessons']}")
    print(f"  Math lessons:              {total['math_lessons']}")
    print(f"  Summative exams audited:   {total['summatives']}")
    print(f"  Summative questions:       {total['summative_questions']}")
    print(f"  Lessons with findings:     {len(flagged_lessons)}")
    print()
    print(f"  Layer 1 — lesson steps:")
    print(f"    auto-corrected:          {total['auto_step']}")
    print(f"    detect-only:             {total['detect_step']}")
    print(f"  Layer 1 — exit-ticket Qs:")
    print(f"    auto-corrected:          {total['auto_q']}")
    print(f"    detect-only:             {total['detect_q']}")
    print(f"  Layer 1 — summative-exam Qs:")
    print(f"    auto-corrected:          {total['sum_auto_q']}")
    print(f"    detect-only:             {total['sum_detect_q']}")
    print(f"  Layer 2 — answer-key mismatches:")
    print(
        f"    exit-ticket questions:   {_RED if total['answer_key_mismatches'] else ''}"
        f"{total['answer_key_mismatches']}{_RESET}"
    )
    print(
        f"    summative questions:     {_RED if total['sum_answer_key_mismatches'] else ''}"
        f"{total['sum_answer_key_mismatches']}{_RESET}"
    )
    print()
    print(f"  CSV written:               {args.csv}")

    # False-positive check: non-math lessons should ideally produce 0
    # findings. Any hits on geography/etc. are noise we should
    # investigate (the regex caught a date or population number).
    non_math_findings = sum(
        s["auto_step"] + s["detect_step"] + s["auto_q"]
        + s["detect_q"] + s["answer_key_mismatches"]
        for course_title, lesson_title, s in flagged_lessons
        if not s["is_math"]
    )
    if non_math_findings:
        print()
        print(
            f"  {_YELLOW}⚠ {non_math_findings} finding(s) on non-math "
            f"lessons{_RESET} — review for false positives "
            f"(regex caught a number used incidentally)"
        )


if __name__ == "__main__":
    main()
