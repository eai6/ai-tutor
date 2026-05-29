"""ContextManager — single owner of the SessionRuntimeState boundary.

Per Phase 2 §2.7, the ContextManager:
  - Assembles ``TutoringContext`` for each service call (transcript +
    profile snapshot + objective + KB chunks + verdict).
  - Owns load/save of ``TutorSession.runtime_state`` via the typed
    Pydantic model.
  - Implements Phase B commit of ``PendingPose`` objects — the only
    code path that mutates the posed-question ledger or writes
    ``open_question``.

Service calls receive **frozen snapshots**, not live state — this is
what the "stateless services" claim means in practice. Mutation
happens through this manager's save / commit methods only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from apps.tutoring.tracing import emit_span

logger = logging.getLogger(__name__)
from apps.tutoring.v2.contracts import (
    OpenQuestion,
    PendingPose,
    SessionRuntimeState,
    TutoringContext,
)


class ContextManager:
    """Owns the typed-state boundary for a single TutorSession."""

    def __init__(self, session) -> None:
        """``session`` is a ``apps.tutoring.models.TutorSession`` instance."""
        self.session = session
        self._state: Optional[SessionRuntimeState] = None

    # ------------------------------------------------------------------
    # Load / save boundary
    # ------------------------------------------------------------------

    def load_runtime_state(self) -> SessionRuntimeState:
        """Hydrate ``SessionRuntimeState`` from the JSONField column.

        Returns a fresh empty model if the column is empty (new
        session). The legacy ``engine_state`` is never read here —
        v2 sessions own ``runtime_state`` exclusively.
        """
        if self._state is not None:
            return self._state
        raw = getattr(self.session, "runtime_state", None) or {}
        self._state = SessionRuntimeState.from_jsonable(raw)
        return self._state

    def save_runtime_state(self, state: SessionRuntimeState) -> None:
        """Persist the typed state back to the JSONField column."""
        self._state = state
        self.session.runtime_state = state.to_jsonable()
        self.session.save(update_fields=["runtime_state"])

    # ------------------------------------------------------------------
    # Two-phase commit — Phase B (Phase 1 §4)
    # ------------------------------------------------------------------

    def commit_pending_pose(self, pending: PendingPose) -> SessionRuntimeState:
        """Phase B commit of a PendingPose returned by the pose tool.

        Appends to ``delivered_lesson_step_ids`` and writes
        ``open_question``. The token-consumption path is gone
        (v2-prune step 4); the pose tool no longer uses tokens.
        """
        from apps.tutoring.v2.contracts import QuestionSource

        with emit_span("audit", "tool.commit") as span:
            state = self.load_runtime_state()

            now = datetime.now(timezone.utc)
            if (
                pending.question_ref.source == QuestionSource.LESSON_STEP
                and pending.question_ref.id
                and pending.question_ref.id not in state.delivered_lesson_step_ids
            ):
                state.delivered_lesson_step_ids.append(int(pending.question_ref.id))
            state.open_question = OpenQuestion(
                source=pending.question_ref.source,
                id=pending.question_ref.id,
                canonical=pending.canonical,
                rendered_stem=pending.rendered_stem,
                answer_type=pending.answer_type,
                visible_context_at_pose=pending.visible_context,
                posed_at=now,
            )
            state.attempts_on_open_question = 0
            self.save_runtime_state(state)
            if span is not None:
                span["payload"] = {
                    "source": pending.question_ref.source,
                    "question_id": pending.question_ref.id,
                    "delivered_count": len(state.delivered_lesson_step_ids),
                }
            return state

    # ------------------------------------------------------------------
    # TutoringContext assembly (Phase 2 §2.7)
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        *,
        client_kind: str = "web",
        current_objective: str = "",
        full_transcript: Optional[list[dict]] = None,
    ) -> TutoringContext:
        """Build a frozen TutoringContext snapshot for a service call.

        Pulls per-session inputs (student, lesson, institution, persona,
        locale, profile_summary) and combines them with the loaded
        runtime state. ``full_transcript`` defaults to the session's
        complete turn history — no windowing per §7 item 10.
        """
        session = self.session
        student = session.student
        lesson = session.lesson
        institution = session.institution

        profile = getattr(student, "student_profile", None)
        profile_summary = ""
        grade_level = ""
        tutor_persona = ""
        if profile is not None:
            profile_summary = profile.profile_summary or ""
            grade_level = profile.grade_level or ""
            personality = profile.tutor_personality
            if personality is not None:
                tutor_persona = getattr(personality, "name", "") or ""

        institution_name = getattr(institution, "name", "") or ""
        locale = getattr(lesson, "language", None) or getattr(
            getattr(lesson, "course", None), "language", None
        ) or "en"

        # Resolve lesson-level metadata. ``current_objective`` derives
        # from the ACTIVE step's ``enabling_objective`` first
        # (per-step objective progress is what gates close_topic /
        # advancement — Mastery Learning: gate every step on its
        # own evidence, not on the lesson-level objective). Falls back
        # to ``Lesson.objective`` when the step has no
        # enabling_objective, and to the explicit caller value when
        # supplied. The previous behaviour (always use Lesson.objective)
        # caused single-objective tracking across all steps, which fired
        # close_topic on the first objective hit and shipped the exit
        # ticket after one practiced step — the GEO-S5 run-5 premature-
        # close finding.
        lesson_title = (getattr(lesson, "title", "") or "").strip()
        unit = getattr(lesson, "unit", None)
        course = getattr(unit, "course", None)
        lesson_subject = (
            getattr(course, "subject_type", "") or ""
        ).strip().lower()

        step_objective, is_final_step = self._current_step_objective_and_position(
            session,
        )
        if not current_objective:
            current_objective = step_objective or (
                getattr(lesson, "objective", "") or ""
            ).strip()

        if full_transcript is None:
            full_transcript = self._load_full_transcript()

        teacher_script, worked_example = self._current_step_pedagogy(session)

        runtime_state = self.load_runtime_state()
        assessable_slots_remaining = self._count_assessable_slots_remaining(
            lesson=lesson, runtime_state=runtime_state,
        )

        skills_snapshot = self._load_filtered_skills_snapshot(
            profile=profile, lesson=lesson, course=course,
        )

        return TutoringContext(
            session_id=session.id,
            student_id=student.id,
            institution_id=institution.id if institution else 0,
            lesson_id=lesson.id if lesson else 0,
            locale=locale,
            grade_level=grade_level,
            institution_name=institution_name,
            tutor_persona=tutor_persona,
            client_kind=client_kind if client_kind in ("web", "mobile") else "web",
            full_transcript=full_transcript,
            runtime_state=runtime_state,
            profile_summary=profile_summary,
            current_objective=current_objective,
            lesson_title=lesson_title,
            lesson_subject=lesson_subject,
            current_step_teacher_script=teacher_script,
            current_step_worked_example=worked_example,
            is_final_step=is_final_step,
            assessable_slots_remaining=assessable_slots_remaining,
            skills_snapshot=skills_snapshot,
        )

    def _load_filtered_skills_snapshot(
        self, *, profile, lesson, course,
    ) -> dict:
        """Return the slice of ``StudentProfile.skills_snapshot`` whose
        objective tags overlap this lesson's objectives.

        Source-of-truth shape per ``competency_tracker.refresh_student_
        snapshot``: ``{str(course_id): {objective_tag: {pct, level,
        source, attempts}}}``. We pull the current course's slice and
        intersect tags with the union of (a) ``lesson.objective``, (b)
        ``lesson.enabling_objectives``, (c) each ``LessonStep.
        enabling_objective``, all normalised via the same
        ``_normalize_tag`` helper that the writer uses.

        Returns ``{}`` when:
          - the student has no ``StudentProfile`` row;
          - the course slice is empty;
          - no overlap exists between snapshot tags and lesson
            objectives;
          - any ORM read raises (fail-soft per Phase 1 design memo
            §"Defense-in-depth").

        Plan: ``memory/skills_snapshot_v2_wiring_plan.md`` (2026-05-29).
        """
        if profile is None or lesson is None or course is None:
            return {}
        try:
            raw_slice = (profile.skills_snapshot or {}).get(
                str(course.id), {}
            )
        except Exception as exc:
            logger.warning(
                "[ContextManager] skills_snapshot read raised %s — "
                "treating as empty",
                type(exc).__name__,
            )
            return {}
        if not raw_slice:
            return {}

        from apps.tutoring.competency_tracker import _normalize_tag

        relevant_tags: set[str] = set()
        primary = (getattr(lesson, "objective", "") or "").strip()
        if primary:
            relevant_tags.add(_normalize_tag(primary))
        for tag in (getattr(lesson, "enabling_objectives", None) or []):
            if isinstance(tag, str) and tag.strip():
                relevant_tags.add(_normalize_tag(tag))
        try:
            steps_qs = getattr(lesson, "steps", None)
            if steps_qs is not None:
                for step in steps_qs.all():
                    step_tag = (
                        getattr(step, "enabling_objective", "") or ""
                    ).strip()
                    if step_tag:
                        relevant_tags.add(_normalize_tag(step_tag))
        except Exception as exc:
            logger.warning(
                "[ContextManager] lesson.steps iteration raised %s — "
                "continuing with lesson-level objectives only",
                type(exc).__name__,
            )

        if not relevant_tags:
            return {}

        filtered: dict[str, dict] = {}
        for tag, data in raw_slice.items():
            if not isinstance(tag, str) or not isinstance(data, dict):
                continue
            if _normalize_tag(tag) in relevant_tags:
                filtered[tag] = data
        return filtered

    def _count_assessable_slots_remaining(
        self, *, lesson, runtime_state,
    ) -> int:
        """Count un-delivered LessonSteps that still have a non-empty
        ``question`` field.

        Mirrors ``TutorEngine._assessable_slots_remaining`` (single
        source of truth) but lives here so the value is on
        ``TutoringContext`` before the engine runs. The close_topic
        prompt's ``lesson_complete_signal`` reads this so the LLM uses
        the exit-ticket phrasing whenever the engine will actually fire
        the modal — including the case where intermediate steps remain
        but they are instruction-only (no questions to assess).

        Fail-open returns 1 ("more work remains") on any DB / model
        miss so an instrumentation glitch never spoofs a premature
        lesson-complete signal.
        """
        if lesson is None or not hasattr(lesson, "steps"):
            return 1
        try:
            from apps.curriculum.models import LessonStep
            delivered = set(runtime_state.delivered_lesson_step_ids or [])
            return (
                LessonStep.objects
                .filter(lesson_id=lesson.id)
                .exclude(id__in=delivered)
                .exclude(question__isnull=True)
                .exclude(question__exact="")
                .count()
            )
        except Exception:
            return 1

    def _current_step_objective_and_position(
        self, session,
    ) -> tuple[str, bool]:
        """Return ``(enabling_objective, is_final_step)`` for the active step.

        ``is_final_step`` lets close_topic phrase its transition
        correctly: "let's move on" while more steps remain, "exit
        ticket" on the last step. Both default to safe values on any
        miss (empty objective, treat as final to avoid orphan-progress
        sessions stuck mid-lesson).
        """
        lesson = getattr(session, "lesson", None)
        if lesson is None or not hasattr(lesson, "steps"):
            return "", True
        try:
            steps = list(lesson.steps.all())
        except Exception:
            return "", True
        if not steps:
            return "", True
        idx = getattr(session, "current_step_index", 0) or 0
        if idx < 0 or idx >= len(steps):
            return "", True
        step = steps[idx]
        objective = (getattr(step, "enabling_objective", "") or "").strip()
        is_final = (idx == len(steps) - 1)
        return objective, is_final

    def _current_step_pedagogy(self, session) -> tuple[str, str]:
        """Resolve teacher_script + worked-example text for the active step.

        Both default to empty strings on any miss (no lesson, no steps,
        step out of range, model attribute absent). Subject-agnostic:
        the explain / worked_example move prompts and the safe-terminal
        templates use whichever is non-empty.
        """
        lesson = getattr(session, "lesson", None)
        if lesson is None or not hasattr(lesson, "steps"):
            return "", ""
        idx = getattr(session, "current_step_index", 0) or 0
        try:
            step = lesson.steps.all()[idx]
        except (IndexError, AttributeError, Exception):
            return "", ""
        teacher_script = (getattr(step, "teacher_script", "") or "").strip()
        worked_example = _render_worked_example_text(step)
        return teacher_script, worked_example


    def _load_full_transcript(self) -> list[dict]:
        """Load every prior turn for this session ordered oldest-first.

        Returns a list of ``{role, content, created_at}`` dicts.
        Includes student + tutor turns; excludes the system roles
        (legacy engine occasionally writes those — v2 does not).
        """
        from apps.tutoring.models import SessionTurn

        rows = (
            SessionTurn.objects
            .filter(session=self.session)
            .exclude(role=SessionTurn.Role.SYSTEM)
            .order_by("created_at")
            .values("role", "content", "created_at")
        )
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            }
            for r in rows
        ]


def _render_worked_example_text(step) -> str:
    """Flatten ``LessonStep.educational_content.worked_example`` to text.

    Output shape (subject-agnostic):
        Problem: <problem text>
        1. <action> — <explanation>
        2. ...
        Final answer: <final>

    Returns empty string when the field is absent / malformed. The
    StudentTutor block + safe-terminal template treat an empty string
    as "no anchor available" and adapt accordingly.
    """
    edu = getattr(step, "educational_content", None) or {}
    if not isinstance(edu, dict):
        return ""
    we = edu.get("worked_example") or {}
    if not isinstance(we, dict):
        return ""
    problem = str(we.get("problem") or "").strip()
    steps_raw = we.get("steps") or []
    final = str(we.get("final_answer") or "").strip()
    if not (problem or steps_raw or final):
        return ""
    lines: list[str] = []
    if problem:
        lines.append(f"Problem: {problem}")
    for i, s in enumerate(steps_raw, start=1):
        if not isinstance(s, dict):
            continue
        action = str(s.get("action") or s.get("step") or "").strip()
        explanation = str(s.get("explanation") or "").strip()
        if action and explanation:
            lines.append(f"{i}. {action} — {explanation}")
        elif action:
            lines.append(f"{i}. {action}")
        elif explanation:
            lines.append(f"{i}. {explanation}")
    if final:
        lines.append(f"Final answer: {final}")
    return "\n".join(lines).strip()
