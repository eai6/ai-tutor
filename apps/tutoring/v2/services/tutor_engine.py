"""TutorEngine — top-level orchestrator for the v2 conversational tutor.

Per Phase 2 §2.3 the engine:

  - Selects moves deterministically from inputs (no LLM call — see
    ``move_selection.py``).
  - Enforces safety valves (§7 item 3): max 40 turns / session, max 12
    turns / objective, force-close after 6 consecutive verdict-less
    turns. These are the *outer* fence; ``pivot`` / ``close_topic``
    are expected to fire first under normal conditions.
  - Routes turns through StudentGrader → StudentTutor → conformance,
    with a single conformance retry and a verdict-keyed safe template
    fallback.
  - Fires the ``StudentSkillMastery`` write hook after a ``correct``
    or ``wrong`` verdict — dashboard-only side effect, NOT consumed
    by move selection or move prompts.
  - Emits per-stage spans through ``apps.tutoring.tracing.emit_span``
    so the per-turn rollup landing in ``SessionTurn.judge_outputs.v2_trace``
    is complete.

Phase 2 Task #4 ships the move state machine + safety valves +
StudentSkillMastery write hook. Phase 2 Tasks #5–#8 wire the grader,
tutor, conformance, and exit-ticket services through ``respond()``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    GradingResult,
    SessionRuntimeState,
    TutoringContext,
    Verdict,
)
from apps.tutoring.v2.services.context_manager import ContextManager
from apps.tutoring.v2.services.move_selection import ALLOWED_MOVES, select_move

logger = logging.getLogger(__name__)


# Safety-valve caps per §7 item 3. Sub-decisions to tune from pilot data.
MAX_TURNS_PER_SESSION = 40
MAX_TURNS_PER_OBJECTIVE = 12
MAX_VERDICTLESS_RUN = 6


@dataclass
class TurnResult:
    """Bundle returned from TutorEngine.respond() / start_session()."""

    response_text: str
    runtime_state: SessionRuntimeState
    selected_move: str
    verdict: Optional[GradingResult] = None
    fallback_used: bool = False
    v2_trace: dict = field(default_factory=dict)


class TutorEngine:
    """Top-level orchestrator. Stateless services; state lives on
    ``ContextManager``."""

    def __init__(self, context_manager: ContextManager) -> None:
        self.context_manager = context_manager

    # ------------------------------------------------------------------
    # Turn entrypoints (Phase 2)
    # ------------------------------------------------------------------

    def start_session(self, context: TutoringContext) -> TurnResult:
        """Produce the opening turn for a new session.

        Phase 2 Task #12 wires this into ``chat_start_session``. Until
        StudentTutor's per-move prompts land (Task #7) the engine
        cannot generate the opening body — surfaces NotImplementedError
        so any wiring oversight fails loudly.
        """
        raise NotImplementedError(
            "TutorEngine.start_session — wired in Phase 2 Task #12"
        )

    def respond(
        self,
        context: TutoringContext,
        student_input: str,
    ) -> TurnResult:
        """Run one full turn end-to-end.

        Phase 2 Task #12 ties StudentGrader (Task #5/#6), StudentTutor
        (Task #7), and ConformanceCheck (Task #8) together here. Until
        those land, callers see ``NotImplementedError`` — this is the
        single seam the entire conversational engine routes through.
        """
        raise NotImplementedError(
            "TutorEngine.respond — wired in Phase 2 Task #12"
        )

    # ------------------------------------------------------------------
    # Pure-function move selection (Phase 2 §2.3 — fully shipped)
    # ------------------------------------------------------------------

    def pick_move(
        self,
        *,
        verdict: Optional[GradingResult],
        runtime_state: SessionRuntimeState,
        profile_summary: str = "",
        objective_just_opened: bool = False,
        current_objective: str = "",
    ) -> str:
        """Pure-function move pick, then safety-valve override.

        Safety valves (per §7 item 3) take precedence over the
        principled move table — a session that has hit the per-objective
        cap or has drifted 6 verdict-less turns *must* close, even when
        the move table would otherwise pick something else.
        """
        with emit_span("audit", "tutor.move_selection") as span:
            valve_override = self._safety_valve_override(
                runtime_state=runtime_state, verdict=verdict,
            )
            if valve_override is not None:
                if span is not None:
                    span["payload"] = {
                        "selected_move": valve_override,
                        "valve_override": True,
                    }
                return valve_override

            move = select_move(
                verdict=verdict,
                runtime_state=runtime_state,
                profile_summary=profile_summary,
                objective_just_opened=objective_just_opened,
                current_objective=current_objective,
            )
            if move not in ALLOWED_MOVES:
                # Defensive normalization — should never fire because
                # `select_move` returns from a closed set.
                logger.warning(
                    "[TutorEngine] select_move returned unknown move %r; "
                    "falling back to pose_question",
                    move,
                )
                move = "pose_question"
            if span is not None:
                span["payload"] = {
                    "selected_move": move,
                    "valve_override": False,
                }
            return move

    # ------------------------------------------------------------------
    # Safety valves (§7 item 3)
    # ------------------------------------------------------------------

    def _safety_valve_override(
        self,
        *,
        runtime_state: SessionRuntimeState,
        verdict: Optional[GradingResult],
    ) -> Optional[str]:
        """Return ``close_topic`` when any safety valve trips, else ``None``.

        Outer fence — moves normally fire first. The valves catch:
          - Per-session cap (40 turns) — force-close current topic.
          - Per-objective cap (12 turns) — force-close current topic.
          - Verdict-less drift (6 consecutive) — force-close to break
            free-chat loops where neither side returns to an answerable
            question.
        """
        counters = runtime_state.safety_valve_counters
        if counters.turns_in_session >= MAX_TURNS_PER_SESSION:
            return "close_topic"
        if counters.turns_on_current_objective >= MAX_TURNS_PER_OBJECTIVE:
            return "close_topic"
        if counters.verdictless_turns >= MAX_VERDICTLESS_RUN:
            return "close_topic"
        return None

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
        """Apply counter updates after a turn completes.

        Call ordering is important — this runs *after* the response is
        finalized + conformance has passed (or the safe template has
        fired). The counters belong to the runtime state and are
        persisted via ContextManager.save_runtime_state by the caller.
        """
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
            if verdict.verdict == Verdict.UNVERIFIED:
                runtime_state.unverified_run_length += 1
            else:
                runtime_state.unverified_run_length = 0

        runtime_state.safety_valve_counters = counters
        return runtime_state

    # ------------------------------------------------------------------
    # StudentSkillMastery write hook (dashboard-only — §2.3)
    # ------------------------------------------------------------------

    def record_skill_practice(
        self,
        *,
        verdict: GradingResult,
        lesson_step: Any = None,
        hints_used: int = 0,
    ) -> None:
        """Fire ``SkillAssessmentService.record_practice`` for correct/wrong.

        Per Phase 2 §2.3: this write keeps the teacher dashboard,
        prerequisite gating, and per-objective competency aggregates
        live. It is **NOT** consumed by ``TutorEngine`` move selection
        or by ``StudentTutor`` move prompts.

        Skipped for ``partial`` and ``unverified`` verdicts (the
        dashboard signal is binary).
        """
        if verdict is None:
            return
        if verdict.verdict not in (Verdict.CORRECT, Verdict.WRONG):
            return
        if lesson_step is None:
            return

        # Local imports — heavy module, defer until we actually fire.
        from apps.tutoring.personalization import SkillAssessmentService

        student = self.context_manager.session.student
        session = self.context_manager.session

        skill = self._resolve_skill_for_step(lesson_step)
        if skill is None:
            logger.debug(
                "[TutorEngine] no skill resolved for step=%s — "
                "skipping record_practice",
                getattr(lesson_step, "id", None),
            )
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
        except Exception as exc:  # never block a turn on a dashboard write
            logger.warning(
                "[TutorEngine] record_practice failed for skill=%s: %s",
                getattr(skill, "id", None), exc,
            )

    def _resolve_skill_for_step(self, lesson_step: Any):
        """Resolve ``Skill`` row from a ``LessonStep`` via enabling-objective.

        Mirrors the legacy resolution path used by
        ``ConversationalTutor`` (exact match on ``enabling_objective_text``
        with a partial-icontains fallback). Scoped to the lesson's
        course for multi-tenancy safety.
        """
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
