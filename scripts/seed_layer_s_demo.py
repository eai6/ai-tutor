"""Seed a math tutoring session containing the production transcript
so we can click through the chat-history view and confirm Layer S
chips render. Idempotent — re-running replaces the demo session.

Run via:
    python scripts/seed_layer_s_demo.py

Then visit:
    /dashboard/session/<session_id>/chat-history/

(URL printed at end.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_tutor.config.settings")

import django  # noqa: E402
django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from ai_tutor.apps.accounts.models import Institution, Membership  # noqa: E402
from ai_tutor.apps.curriculum.models import Course, Unit, Lesson, LessonStep  # noqa: E402
from ai_tutor.apps.tutoring.models import (  # noqa: E402
    SessionTurn,
    TutorSession,
)
from ai_tutor.apps.tutoring.student_working_analyzer import analyze_working  # noqa: E402


DEMO_SCHOOL_SLUG = "layer-s-demo-school"
DEMO_STUDENT_USERNAME = "layer-s-demo-student"
DEMO_COURSE_TITLE = "Layer S Demo — Math S3 (angles around a point)"


# Each entry: (student_input, tutor_reply, expected_answer_for_step,
#              note shown above the analyzer chips in the demo view)
TURNS = [
    # Step 1: warm-up
    ("180", "Not quite — 180° is a half-turn. A full rotation is 360°.", "360",
     "warm-up: bare wrong answer"),
    # Step 1: warm-up complete
    ("Got it — 360", "Right. Let's apply that rule.", "360",
     "warm-up: prose acceptance"),
    # Step 2: practice — the bug case
    ("95 + 70 + 110 = 275",
     "Good — 95 + 70 + 110 = 275. What do you do with that to find x?",
     "85",
     "★ THE PRODUCTION BUG: PARTIAL_CORRECT. Tutor must NOT finish."),
    # Student writes second step
    ("360 - 275 = 85", "Yes — 85° is correct. Why did you subtract from 360?",
     "85",
     "★ COMPLETE_CORRECT — but the tutor asks WHY, not just 'great'"),
    # Step 3: a wrong-arithmetic case
    ("95 + 70 + 110 = 285",
     "Let me check that addition — what do you get when you add 95 + 70?",
     "85",
     "★ PARTIAL_WRONG: addition error in step 1"),
    # Student fixes
    ("oh you're right, 95+70=165, then 165+110=275",
     "Exactly. Now finish the problem.",
     "85",
     "self-correction in prose form"),
    # Bare answer
    ("85",
     "Show me your working — write out each step that got you to 85.",
     "85",
     "bare answer — Layer S falls through to bare-answer signal"),
    # Wrong intermediate, right final
    ("100 + 100 = 250\n250 - 165 = 85",
     "Hmm — let me check 100 + 100. What does that equal?",
     "85",
     "★ Wrong intermediate, right final → still PARTIAL_WRONG"),
    # Multi-separator input
    ("First, 95+70=165. Then 165+110=275; finally 360-275=85",
     "Excellent — and you walked me through it cleanly. Why subtract from 360?",
     "85",
     "real-world multi-separator → COMPLETE_CORRECT"),
    # NO_WORKING case
    ("I think x is 85 because of the angle rule",
     "I want to walk through this with you step by step. "
     "Can you write each calculation on its own line, like:\n"
     "    95 + 70 = 165\n"
     "    165 + 110 = 275\n"
     "    360 - 275 = 85\n"
     "Then I can check each step.",
     "85",
     "★ NO_WORKING — tutor requests step-per-line format"),
]


def _get_or_create_demo_fixtures():
    institution, _ = Institution.objects.get_or_create(
        slug=DEMO_SCHOOL_SLUG,
        defaults={"name": "Layer S Demo School"},
    )
    student, _ = User.objects.get_or_create(
        username=DEMO_STUDENT_USERNAME,
        defaults={"first_name": "Layer", "last_name": "S Demo"},
    )
    Membership.objects.get_or_create(
        user=student, institution=institution,
        defaults={"role": "student"},
    )

    course, _ = Course.objects.get_or_create(
        institution=institution,
        title=DEMO_COURSE_TITLE,
        defaults={"grade_level": "S3", "is_published": True},
    )
    unit, _ = Unit.objects.get_or_create(
        course=course, title="Geometry — angles", order_index=0,
    )
    lesson, _ = Lesson.objects.get_or_create(
        unit=unit,
        title="Angles around a point",
        defaults={
            "objective": "Find a missing angle x given that all angles sum to 360°.",
            "order_index": 0,
            "is_published": True,
        },
    )
    step, _ = LessonStep.objects.get_or_create(
        lesson=lesson,
        order_index=0,
        defaults={
            "step_type": "practice",
            "teacher_script": "Find x in: 95° + 70° + 110° + x = 360°.",
            "question": "Find x in: 95° + 70° + 110° + x = 360°.",
            "answer_type": "free_text",
            "expected_answer": "85",
        },
    )
    return institution, student, lesson, step


def main():
    institution, student, lesson, step = _get_or_create_demo_fixtures()

    # Replace any existing demo session.
    TutorSession.objects.filter(
        student=student, lesson=lesson,
    ).delete()

    session = TutorSession.objects.create(
        institution=institution,
        student=student,
        lesson=lesson,
        status="completed",
        engine_state={},
        started_at=timezone.now(),
        ended_at=timezone.now(),
    )

    print(f"\nCreated session id={session.id}")
    print(f"  student:  {student.username}")
    print(f"  lesson:   {lesson.title}")
    print(f"  step:     {step.question!r}")
    print(f"  expected: {step.expected_answer!r}")
    print()

    # Build alternating student/tutor turns. Student turn metadata is
    # empty (the engine writes it on tutor turns); tutor turn metadata
    # carries the Layer S analysis of the *preceding* student input.
    for i, (student_input, tutor_reply, expected, note) in enumerate(TURNS, 1):
        SessionTurn.objects.create(
            session=session,
            role="student",
            content=student_input,
            step=step,
            metadata={"demo_note": note},
        )
        analysis = analyze_working(student_input, expected_answer=expected)

        # Match the metadata shape the runtime engine writes (S3 wiring).
        tutor_metadata = {
            "is_correct": analysis.state.value == "complete_correct",
            "eval_layer": "deterministic_numeric",
            "step_index": 0,
            "step_type": "practice",
            "working_state": analysis.state.value,
            "working_steps_count": len(analysis.steps),
            "working_first_error_idx": analysis.first_error_idx,
            "working_propagated_idxs": sorted(analysis.propagated_idxs),
        }
        if analysis.final_claim is not None:
            tutor_metadata["working_final_claim"] = analysis.final_claim
        if analysis.expected_answer is not None:
            tutor_metadata["working_expected"] = analysis.expected_answer

        SessionTurn.objects.create(
            session=session,
            role="tutor",
            content=tutor_reply,
            step=step,
            metadata=tutor_metadata,
        )
        print(f"  turn {i:>2}: {analysis.state.value:<18} "
              f"({len(analysis.steps)} step(s)) — {note}")

    print()
    print("=" * 78)
    print("Open the chat-history view to see the chips render:")
    print(f"  /dashboard/session/{session.id}/chat-history/")
    print("=" * 78)
    print()


if __name__ == "__main__":
    main()
