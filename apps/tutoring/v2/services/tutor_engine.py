"""TutorEngine — top-level orchestrator for the v2 conversational tutor.

Post-prune (v2-prune-plan.md):

  - Conformance package, safe templates, safety floors, move
    escalation, and the question extractor are all gone. The engine
    no longer runs an audit-over-generation classifier and never
    falls back to a fake teacher line.
  - Each turn: optional grade → router (LLM) → tutor LLM →
    ``run_gates_with_recovery`` (3 deterministic gates with per-gate
    one-retry-then-degrade) → persist + ship. The response ALWAYS
    ships; the frontend never sees a gate-failed error.
  - No engine-side help-request short-circuit. The router is the
    single source of truth for move selection (per plan §2 / §4.2).
    The router's ``verdict_needed`` signal — landing in Commit D —
    is what decides whether the grader runs.

The deeper engine rewrite to a ~300-LOC thin orchestrator lands in
Commit H (plan §4.5 + step 10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    GradingRequest,
    GradingResult,
    ObjectiveProgress,
    RouterDecision,
    SessionRuntimeState,
    TutoringContext,
    Verdict,
)
from apps.tutoring.v2.services.context_manager import ContextManager
from apps.tutoring.v2.services.media import MediaService
from apps.tutoring.v2.services.move_router import (
    MoveRouter,
    build_router_request,
)
from apps.tutoring.v2.services.safety_gates import (
    GateContext,
    RecoveryResult,
    run_gates_with_recovery,
)
from apps.tutoring.v2.services.student_grader import StudentGrader
from apps.tutoring.v2.services.student_tutor import StudentTutor, TutorResponse


# The full move table per design/tasks/move-router-implementation-plan.md
# §2.5. ``pose_question`` is gone; every move is either non-terminal
# pose-capable or the terminal ``close_topic``.
ALLOWED_MOVES: tuple[str, ...] = (
    "confirm_and_advance",
    "confirm_and_extend",
    "scaffold_hint",
    "name_misconception",
    "worked_example",
    "explain",
    "pivot",
    "close_topic",
)

logger = logging.getLogger(__name__)


def _doing_rate_observability(window: list) -> dict:
    """Compact doing-rate summary for ``v2_trace`` (legacy field)."""
    truthy = sum(1 for b in window if bool(b))
    total = len(window)
    return {
        "attempted": truthy,
        "total": total,
        "rate": (truthy / total) if total else None,
    }


@dataclass
class TurnResult:
    """Bundle returned from TutorEngine.respond() / start_session()."""

    response_text: str
    runtime_state: SessionRuntimeState
    selected_move: str
    verdict: Optional[GradingResult] = None
    fallback_used: bool = False  # always False post-prune (no templates)
    v2_trace: dict = field(default_factory=dict)
    is_lesson_complete: bool = False
    advanced_to_step_index: Optional[int] = None


class TutorEngine:
    """Top-level orchestrator. Stateless services; state lives on
    ``ContextManager``."""

    def __init__(
        self,
        context_manager: ContextManager,
        *,
        grader: Optional[StudentGrader] = None,
        tutor: Optional[StudentTutor] = None,
        media_service: Optional[MediaService] = None,
        move_router: Optional[MoveRouter] = None,
    ) -> None:
        self.context_manager = context_manager
        self.grader = grader or StudentGrader()
        self.tutor = tutor or StudentTutor()
        self.media_service = media_service or MediaService()
        self.move_router = move_router or MoveRouter()

    # ------------------------------------------------------------------
    # Turn entrypoints
    # ------------------------------------------------------------------

    def start_session(self, context: TutoringContext) -> TurnResult:
        """Produce the opening turn for a new session."""
        runtime_state = context.runtime_state
        media_catalog = self._build_media_catalog(context)
        pose_tool_available = self._pose_tool_available(
            context=context, runtime_state=runtime_state,
        )

        move, focus_note, principle_emphasis, _decision = self.pick_move(
            context=context,
            verdict=None,
            student_input="",
            pose_tool_available=pose_tool_available,
            media_catalog=media_catalog,
        )
        v2_trace: dict[str, Any] = {
            "selected_move": move,
            "verdict": None,
            "fallback_used": False,
            "router_focus_note": (focus_note or "")[:160],
            "router_principle_emphasis": list(principle_emphasis or []),
        }
        pending_pose = None
        try:
            tutor_resp = self.tutor.respond(
                context=context,
                verdict=None,
                move=move,
                media_catalog=media_catalog,
                student_input="",
                focus_note=focus_note,
                principle_emphasis=principle_emphasis,
            )
            response_text = tutor_resp.text
            pending_pose = tutor_resp.pending_pose
            v2_trace["grounding"] = dict(
                getattr(tutor_resp, "grounding", {}) or {}
            )
        except Exception as exc:
            logger.warning(
                "[TutorEngine] start_session tutor.respond raised %s",
                type(exc).__name__,
            )
            response_text = "Welcome — let's get started together."

        # Phase B commit (if a tool-call PendingPose came back). The
        # opening turn has no verdict; gates are skipped (no canonical
        # to leak, no figure to misref) — committing the pose is safe.
        if pending_pose is not None:
            try:
                runtime_state = self.context_manager.commit_pending_pose(
                    pending_pose,
                )
            except Exception as exc:
                logger.warning(
                    "[TutorEngine] start_session commit_pending_pose raised %s",
                    type(exc).__name__,
                )

        runtime_state.current_move = move
        runtime_state.move_history = list(runtime_state.move_history) + [move]
        runtime_state = self.update_counters_post_turn(
            runtime_state=runtime_state, verdict=None, objective_changed=True,
        )
        self.context_manager.save_runtime_state(runtime_state)

        return TurnResult(
            response_text=response_text,
            runtime_state=runtime_state,
            selected_move=move,
            verdict=None,
            fallback_used=False,
            v2_trace=v2_trace,
        )

    def respond(
        self,
        context: TutoringContext,
        student_input: str,
    ) -> TurnResult:
        """Run one full turn end-to-end.

        Pipeline (Commit A — steps 1+2+3):
          1. Grade — only when an assessment question is open AND the
             student input is plausibly an attempt at it.
          2. Pick the move via the LLM router (no engine-side floors).
          3. Tutor LLM emits a response (may call ``pose_question`` tool).
          4. ``run_gates_with_recovery`` runs safety / figure_ref /
             answer_leak with per-gate one-retry-then-degrade.
          5. Commit PendingPose (if any), persist counters, ship.
        """
        runtime_state = context.runtime_state
        objective_changed = False
        media_catalog = self._build_media_catalog(context)

        # 1. Grade — only when there's an open question AND the student
        # produced some input. No engine-side help-request short-circuit:
        # the router is the single source of truth for move selection
        # and (in Commit D, via the router's verdict_needed signal) for
        # whether the grader runs at all.
        verdict: Optional[GradingResult] = None
        if (
            runtime_state.open_question is not None
            and student_input.strip()
        ):
            try:
                verdict = self.grader.grade_student_response(
                    context,
                    GradingRequest(
                        open_question=runtime_state.open_question,
                        student_input=student_input,
                        is_math=self._is_math_lesson(context),
                        kb_chunks=[],
                    ),
                )
                self._update_objective_progress(
                    runtime_state=runtime_state,
                    verdict=verdict,
                    current_objective=context.current_objective,
                )
                if verdict.verdict == Verdict.CORRECT:
                    runtime_state.attempts_on_open_question = 0
                    if runtime_state.open_question is not None:
                        runtime_state.open_question = None
                else:
                    runtime_state.attempts_on_open_question += 1
                if verdict.bare_answer:
                    key = (context.current_objective or "_").strip() or "_"
                    runtime_state.bare_answer_counts_by_objective[key] = (
                        runtime_state.bare_answer_counts_by_objective.get(key, 0) + 1
                    )
            except Exception as exc:
                logger.warning(
                    "[TutorEngine] grader raised %s — proceeding "
                    "without a verdict",
                    type(exc).__name__,
                )
                verdict = None

        # 2. Pick the move via the LLM router. No safety_floors layer —
        # the router's decision is the single source of truth.
        pose_tool_available = self._pose_tool_available(
            context=context, runtime_state=runtime_state,
        )
        (
            selected_move,
            router_focus_note,
            router_principle_emphasis,
            router_decision,
        ) = self.pick_move(
            context=context,
            verdict=verdict,
            student_input=student_input,
            pose_tool_available=pose_tool_available,
            media_catalog=media_catalog,
        )

        # 3. Tutor → emit a response. Raises become a one-line safe
        # string (no template module any more).
        first_resp = self._invoke_tutor(
            context=context,
            verdict=verdict,
            move=selected_move,
            media_catalog=media_catalog,
            student_input=student_input,
            focus_note=router_focus_note,
            principle_emphasis=router_principle_emphasis,
        )
        attempt_text = first_resp.text
        committed_pose = first_resp.pending_pose
        grounding = dict(getattr(first_resp, "grounding", {}) or {})

        # 4. Safety gates with per-gate one-retry-then-degrade recovery.
        attached_media_count, figure_facts = self._media_counts_and_facts(
            attempt_text=attempt_text, catalog=media_catalog,
        )
        lesson_has_media = bool(media_catalog)
        gate_ctx = GateContext(
            verdict=verdict,
            open_question_stem=(
                runtime_state.open_question.rendered_stem
                if runtime_state.open_question
                else ""
            ),
            private_canonical=(verdict.private_canonical if verdict else ""),
            attached_media_count=attached_media_count,
            figure_facts=figure_facts,
            available_figure_descriptions=[
                (m.get("description") or m.get("title") or "")[:200]
                for m in (media_catalog or [])
            ],
            posed_via_tool=(committed_pose is not None),
            lesson_has_media=lesson_has_media,
        )

        def _retry_fn(reminder: str) -> str:
            try:
                retried = self._invoke_tutor(
                    context=context,
                    verdict=verdict,
                    move=selected_move,
                    media_catalog=media_catalog,
                    student_input=student_input,
                    focus_note=router_focus_note,
                    principle_emphasis=router_principle_emphasis,
                    extra_reminder=reminder,
                    hold_pending_pose=committed_pose,
                )
                return retried.text or ""
            except Exception as exc:
                logger.warning(
                    "[TutorEngine] gate retry tutor.respond raised %s",
                    type(exc).__name__,
                )
                return ""

        recovery: RecoveryResult = run_gates_with_recovery(
            attempt_text,
            ctx=gate_ctx,
            retry_fn=_retry_fn,
        )
        attempt_text = recovery.text

        # 4b. Phase B commit — only if the LLM called the pose tool. The
        # pose validation already happened inside StudentTutor (Phase A);
        # commit here unconditionally so the next turn's grader has the
        # canonical answer. A degraded response still references the
        # posed question for the student to answer.
        if committed_pose is not None:
            try:
                runtime_state = self.context_manager.commit_pending_pose(
                    committed_pose,
                )
            except Exception as exc:
                logger.warning(
                    "[TutorEngine] commit_pending_pose raised %s — pose dropped",
                    type(exc).__name__,
                )

        # 5. Skill-mastery write hook (dashboard-only, isolated).
        if verdict is not None:
            self.record_skill_practice(
                verdict=verdict,
                lesson_step=self._current_lesson_step(context),
                hints_used=0,
            )

        # 6. Persist state — update move history + counters.
        runtime_state.current_move = selected_move
        runtime_state.move_history = list(runtime_state.move_history) + [selected_move]
        runtime_state = self.update_counters_post_turn(
            runtime_state=runtime_state,
            verdict=verdict,
            objective_changed=objective_changed,
        )
        self.context_manager.save_runtime_state(runtime_state)

        # 6b. Step advancement on close_topic.
        is_lesson_complete = False
        advanced_to_step_index: Optional[int] = None
        if selected_move == "close_topic":
            advanced_to_step_index = self._advance_step_if_possible(
                runtime_state=runtime_state,
            )
            is_lesson_complete = advanced_to_step_index is None

        # 7. Build v2_trace rollup for SessionTurn.judge_outputs.v2_trace.
        v2_trace = {
            "selected_move": selected_move,
            "verdict": verdict.verdict.value if verdict else None,
            "verdict_bare_answer": verdict.bare_answer if verdict else False,
            "gate_failures": [
                {
                    "gate": f.gate,
                    "attempt": f.attempt,
                    "reason": (f.reason or "")[:200],
                    "degraded": f.degraded,
                }
                for f in recovery.failures
            ],
            "gate_first_attempt_failures": recovery.first_attempt_failure_gates,
            "gate_degraded_gates": recovery.degraded_gates,
            "fallback_used": False,
            "is_lesson_complete": is_lesson_complete,
            "advanced_to_step_index": advanced_to_step_index,
            "router": {
                "chosen_move": router_decision.chosen_move,
                "principle_emphasis": list(
                    router_decision.principle_emphasis
                ),
                "focus_note": (router_decision.focus_note or "")[:160],
                "rationale": (router_decision.rationale or "")[:200],
                # Router is now the single source of truth — selected_move
                # always equals router's chosen_move (no engine override).
                "floor_overridden": False,
            },
            "doing_rate_5turn": _doing_rate_observability(
                runtime_state.student_doing_rate_window or []
            ),
            "grounding": grounding,
        }

        return TurnResult(
            response_text=attempt_text,
            runtime_state=runtime_state,
            selected_move=selected_move,
            verdict=verdict,
            fallback_used=False,
            v2_trace=v2_trace,
            is_lesson_complete=is_lesson_complete,
            advanced_to_step_index=advanced_to_step_index,
        )

    # ------------------------------------------------------------------
    # LLM-driven move selection
    # ------------------------------------------------------------------

    def pick_move(
        self,
        *,
        context: TutoringContext,
        verdict: Optional[GradingResult],
        student_input: str,
        pose_tool_available: bool,
        media_catalog: Optional[list[dict]] = None,
    ) -> tuple[str, str, list[str], RouterDecision]:
        """Route the turn via the LLM ``MoveRouter`` — no engine-side floors.

        Returns ``(final_move, focus_note, principle_emphasis,
        router_decision)``. ``final_move`` is the router's chosen move
        verbatim; the engine no longer overrides it.
        """
        with emit_span("audit", "tutor.move_selection") as span:
            request = build_router_request(
                context=context,
                verdict=verdict,
                student_input=student_input,
                pose_tool_available=pose_tool_available,
                media_catalog=media_catalog,
            )
            decision = self.move_router.route(request)
            chosen = decision.chosen_move
            if chosen not in ALLOWED_MOVES:
                logger.warning(
                    "[TutorEngine] router returned unknown move %r; "
                    "falling back to scaffold_hint",
                    chosen,
                )
                chosen = "scaffold_hint"
            if span is not None:
                span["payload"] = {
                    "selected_move": chosen,
                    "router_chosen_move": decision.chosen_move,
                    "router_principles": list(decision.principle_emphasis),
                    "router_focus_note": (decision.focus_note or "")[:80],
                }
            return (
                chosen,
                decision.focus_note,
                list(decision.principle_emphasis),
                decision,
            )

    # ------------------------------------------------------------------
    # Counter bookkeeping
    # ------------------------------------------------------------------

    def update_counters_post_turn(
        self,
        *,
        runtime_state: SessionRuntimeState,
        verdict: Optional[GradingResult],
        objective_changed: bool,
    ) -> SessionRuntimeState:
        """Apply counter updates after a turn completes."""
        counters = runtime_state.safety_valve_counters
        counters.turns_in_session += 1
        if objective_changed:
            counters.turns_on_current_objective = 1
        else:
            counters.turns_on_current_objective += 1

        if verdict is None:
            counters.verdictless_turns += 1
        else:
            counters.verdictless_turns = 0
            # unverified_run_length stays on the model until commit G
            # — always reset under the ternary contract since no
            # verdict path produces unverified any more.
            runtime_state.unverified_run_length = 0

        runtime_state.safety_valve_counters = counters
        return runtime_state

    # ------------------------------------------------------------------
    # StudentSkillMastery write hook (dashboard-only)
    # ------------------------------------------------------------------

    def record_skill_practice(
        self,
        *,
        verdict: GradingResult,
        lesson_step: Any = None,
        hints_used: int = 0,
    ) -> None:
        """Fire ``SkillAssessmentService.record_practice`` for correct/wrong."""
        if verdict is None:
            return
        if verdict.verdict not in (Verdict.CORRECT, Verdict.WRONG):
            return
        if lesson_step is None:
            return

        from apps.tutoring.personalization import SkillAssessmentService

        student = self.context_manager.session.student
        session = self.context_manager.session

        skill = self._resolve_skill_for_step(lesson_step)
        if skill is None:
            return

        try:
            service = SkillAssessmentService(student=student, session=session)
            service.record_practice(
                skill=skill,
                was_correct=(verdict.verdict == Verdict.CORRECT),
                lesson_step=lesson_step,
                hints_used=hints_used,
                practice_type="initial",
            )
        except Exception as exc:
            logger.warning(
                "[TutorEngine] record_practice failed for skill=%s: %s",
                getattr(skill, "id", None), exc,
            )

    # ------------------------------------------------------------------
    # Orchestration helpers
    # ------------------------------------------------------------------

    def _invoke_tutor(
        self,
        *,
        context: TutoringContext,
        verdict: Optional[GradingResult],
        move: str,
        media_catalog: list[dict],
        student_input: str,
        focus_note: str = "",
        principle_emphasis: Optional[list[str]] = None,
        extra_reminder: str = "",
        hold_pending_pose: Any = None,
    ) -> TutorResponse:
        """Call the StudentTutor; on raise, surface a one-line safe string.

        ``extra_reminder`` (used by the gate-recovery retry) is appended
        to ``student_input`` so the tutor's user prompt incorporates it.
        ``hold_pending_pose`` skips the tool path so the held pose is
        re-attached to the returned TutorResponse (used by gate retries
        that want to preserve the original pose).
        """
        effective_input = student_input or ""
        if extra_reminder:
            effective_input = (
                effective_input
                + f"\n\n[Gate retry — {extra_reminder}]"
            )

        try:
            return self.tutor.respond(
                context=context,
                verdict=verdict,
                move=move,
                media_catalog=media_catalog,
                student_input=effective_input,
                focus_note=focus_note,
                principle_emphasis=principle_emphasis,
                hold_pending_pose=hold_pending_pose,
            )
        except Exception as exc:
            logger.warning(
                "[TutorEngine] tutor.respond raised %s — one-line safe string",
                type(exc).__name__,
            )
            return TutorResponse(
                text="Let's stay with the same question and work the next step.",
                pending_pose=None,
            )

    def _build_media_catalog(self, context: TutoringContext) -> list[dict]:
        """Build the per-turn media catalog via ``MediaService``."""
        try:
            return self.media_service.build_catalog(
                lesson_id=context.lesson_id,
                institution_id=context.institution_id,
                topic_hint=context.current_objective or "",
                recent_text=self._prior_student_turn(context) or "",
            )
        except Exception as exc:
            logger.warning(
                "[TutorEngine] media catalog raised %s — empty catalog",
                type(exc).__name__,
            )
            return []

    def _media_counts_and_facts(
        self, *, attempt_text: str, catalog: list[dict],
    ) -> tuple[int, list[str]]:
        """Parse ``|||MEDIA:N|||`` and resolve figure_facts for gate use."""
        try:
            _, indices = self.media_service.parse_signal(attempt_text or "")
        except Exception:
            indices = []
        facts: list[str] = []
        try:
            facts = self.media_service.figure_facts_for_indices(
                catalog=catalog, indices=indices,
            )
        except Exception:
            facts = []
        return len(indices), facts

    def _is_math_lesson(self, context: TutoringContext) -> bool:
        try:
            from apps.curriculum.models import Lesson
            lesson = (
                Lesson.objects
                .select_related("unit__course")
                .filter(pk=context.lesson_id)
                .first()
            )
            if lesson is None:
                return False
            course = getattr(lesson.unit, "course", None)
            return bool(getattr(course, "is_math", False))
        except Exception:
            return False

    def _current_lesson_step(self, context: TutoringContext):
        session = self.context_manager.session
        idx = getattr(session, "current_step_index", 0) or 0
        try:
            lesson = session.lesson
            return lesson.steps.all()[idx] if hasattr(lesson, "steps") else None
        except (IndexError, AttributeError, Exception):
            return None

    def _advance_step_if_possible(
        self,
        *,
        runtime_state: SessionRuntimeState,
    ) -> Optional[int]:
        """Advance ``session.current_step_index`` to the next step."""
        session = self.context_manager.session
        lesson = getattr(session, "lesson", None)
        if lesson is None or not hasattr(lesson, "steps"):
            return None
        try:
            total_steps = lesson.steps.count()
        except Exception:
            return None
        current_idx = getattr(session, "current_step_index", 0) or 0
        next_idx = current_idx + 1
        if next_idx >= total_steps:
            return None

        try:
            session.current_step_index = next_idx
            session.save(update_fields=["current_step_index"])
        except Exception as exc:
            logger.warning(
                "[TutorEngine] failed to persist current_step_index advance: %s",
                type(exc).__name__,
            )
            return None

        runtime_state.open_question = None
        runtime_state.attempts_on_open_question = 0
        runtime_state.unverified_run_length = 0
        for key, prog in runtime_state.objective_progress.items():
            if prog is not None and not prog.closed and prog.correct >= 1:
                prog.closed = True
                runtime_state.objective_progress[key] = prog
        runtime_state.safety_valve_counters.turns_on_current_objective = 0
        return next_idx

    def _prior_student_turn(self, context: TutoringContext) -> str:
        for turn in reversed(context.full_transcript or []):
            if turn.get("role") == "student":
                return turn.get("content") or ""
        return ""

    def _pose_tool_available(
        self,
        *,
        context: TutoringContext,
        runtime_state: SessionRuntimeState,
    ) -> bool:
        """True when the lesson has at least one un-posed bank slot."""
        try:
            from apps.curriculum.models import LessonStep
            posed_ids = {
                e.id for e in runtime_state.posed_question_ledger
                if e.source.value == "lesson_step"
            }
            return (
                LessonStep.objects
                .filter(lesson_id=context.lesson_id)
                .exclude(id__in=posed_ids)
                .exclude(question__isnull=True)
                .exclude(question__exact="")
                .exists()
            )
        except Exception:
            return True

    def _update_objective_progress(
        self,
        *,
        runtime_state: SessionRuntimeState,
        verdict: GradingResult,
        current_objective: str,
    ) -> None:
        key = (current_objective or "_").strip() or "_"
        progress = runtime_state.objective_progress.get(key)
        if progress is None:
            progress = ObjectiveProgress(objective=key)
        progress.attempts += 1
        if verdict.verdict == Verdict.CORRECT:
            progress.correct += 1
        elif verdict.verdict == Verdict.WRONG:
            progress.wrong += 1
        elif verdict.verdict == Verdict.PARTIAL:
            progress.partial += 1
        runtime_state.objective_progress[key] = progress

    # ------------------------------------------------------------------

    def _resolve_skill_for_step(self, lesson_step: Any):
        from apps.tutoring.skills_models import Skill

        eo_text = (getattr(lesson_step, "enabling_objective", "") or "").strip()
        if not eo_text:
            return None
        lesson = getattr(lesson_step, "lesson", None) or getattr(
            self.context_manager.session, "lesson", None
        )
        unit = getattr(lesson, "unit", None) if lesson else None
        course = getattr(unit, "course", None) if unit else None
        if course is None:
            return None

        skill = Skill.objects.filter(
            enabling_objective_text=eo_text,
            is_enabling_objective=True,
            course=course,
        ).first()
        if skill:
            return skill
        return Skill.objects.filter(
            enabling_objective_text__icontains=eo_text[:50],
            is_enabling_objective=True,
            course=course,
        ).first()

    # ------------------------------------------------------------------
    # Session completion
    # ------------------------------------------------------------------

    def complete_session(self) -> None:
        """Mark the session COMPLETED and run the end-of-session profiler."""
        from django.utils import timezone

        from apps.tutoring.models import TutorSession
        from apps.tutoring.v2.services.profiler import StudentProfiler

        session = self.context_manager.session
        if session.status == TutorSession.Status.COMPLETED:
            return

        session.status = TutorSession.Status.COMPLETED
        session.ended_at = timezone.now()
        if not session.completed_lesson_at:
            session.completed_lesson_at = timezone.now()
        session.save(
            update_fields=["status", "ended_at", "completed_lesson_at"],
        )

        try:
            StudentProfiler().run_for_session(session)
        except Exception as exc:
            logger.warning(
                "[TutorEngine] profiler.run_for_session raised %s "
                "for session=%s",
                type(exc).__name__, getattr(session, "id", None),
            )
