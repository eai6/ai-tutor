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
    GradingRequest,
    GradingResult,
    ObjectiveProgress,
    SessionRuntimeState,
    TutoringContext,
    Verdict,
)
from apps.tutoring.v2.services.conformance import (
    ConformanceCheck,
    ConformanceResult,
)
from apps.tutoring.v2.services.context_manager import ContextManager
from apps.tutoring.v2.services.media import MediaService
from apps.tutoring.v2.services.move_selection import ALLOWED_MOVES, select_move
from apps.tutoring.v2.services.student_grader import StudentGrader
from apps.tutoring.v2.services.student_tutor import StudentTutor
from apps.tutoring.v2.services.templates import render_safe_template

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

    def __init__(
        self,
        context_manager: ContextManager,
        *,
        grader: Optional[StudentGrader] = None,
        tutor: Optional[StudentTutor] = None,
        conformance: Optional[ConformanceCheck] = None,
        media_service: Optional[MediaService] = None,
    ) -> None:
        self.context_manager = context_manager
        self.grader = grader or StudentGrader()
        self.tutor = tutor or StudentTutor()
        self.conformance = conformance or ConformanceCheck(grader=self.grader)
        self.media_service = media_service or MediaService()

    # ------------------------------------------------------------------
    # Turn entrypoints (Phase 2)
    # ------------------------------------------------------------------

    def start_session(self, context: TutoringContext) -> TurnResult:
        """Produce the opening turn for a new session.

        No verdict, no student input — the engine selects an opening
        move (explain or worked_example based on profile) and asks
        StudentTutor to render it.
        """
        runtime_state = context.runtime_state
        media_catalog = self._build_media_catalog(context)

        move = self.pick_move(
            verdict=None,
            runtime_state=runtime_state,
            profile_summary=context.profile_summary,
            objective_just_opened=True,
            current_objective=context.current_objective,
        )
        v2_trace: dict[str, Any] = {
            "selected_move": move,
            "verdict": None,
            "fallback_used": False,
            "retry_used": False,
        }
        pending_pose = None
        try:
            tutor_resp = self.tutor.respond(
                context=context,
                verdict=None,
                move=move,
                media_catalog=media_catalog,
                student_input="",
            )
            response_text = tutor_resp.text
            pending_pose = tutor_resp.pending_pose
        except Exception as exc:
            logger.warning(
                "[TutorEngine] start_session tutor.respond raised %s",
                type(exc).__name__,
            )
            response_text = render_safe_template(
                verdict=None,
                student_claim_present=False,
                next_action_text="Let's get started together.",
            )
            v2_trace["fallback_used"] = True

        # Phase B commit (if a tool-call PendingPose came back).
        # ``start_session`` skips conformance — the opening message
        # has no verdict to gate. We still commit because the LLM
        # used the tool channel; the bank stem is verbatim from a
        # validated step.
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

        # Persist state — opening turn updates counters, no verdict.
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
            fallback_used=v2_trace["fallback_used"],
            v2_trace=v2_trace,
        )

    def respond(
        self,
        context: TutoringContext,
        student_input: str,
    ) -> TurnResult:
        """Run one full turn end-to-end.

        Order (per analysis §3 turn-by-turn flow):
          1. Grade (only when an assessment question is open).
          2. Select move from inputs (pure function + safety valves).
          3. Tutor generates one response.
          4. Conformance check; on reject → ONE retry; on second
             reject → safe terminal template.
          5. Record skill practice (correct/wrong only — dashboard).
          6. Persist runtime state.
          7. Return ``TurnResult`` with v2_trace rollup.

        The caller (views.py) is responsible for persisting the
        ``SessionTurn`` row and flushing buffered spans against it.
        """
        runtime_state = context.runtime_state
        objective_changed = False
        media_catalog = self._build_media_catalog(context)

        # 1. Grade — only when there's an open question AND the student
        # input is plausibly an attempt at it. Explicit help-requests
        # ("can you explain", "show me how", "I don't understand") are
        # not answer attempts and must not be graded: grading them
        # produces a meaningless ``unverified`` verdict that then
        # forces the unverified verdict-matrix rules onto the
        # downstream explain / worked_example response, which can't
        # comply (a worked example by construction makes factual
        # claims and does not "surface uncertainty"). Skip the grader
        # entirely on help-requests; the help-request also overrides
        # move selection below (see move_selection.detect_help_request).
        from apps.tutoring.v2.services.move_selection import detect_help_request

        is_help_request = detect_help_request(student_input) is not None

        verdict: Optional[GradingResult] = None
        if (
            runtime_state.open_question is not None
            and student_input.strip()
            and not is_help_request
        ):
            try:
                verdict = self.grader.grade_student_response(
                    context,
                    GradingRequest(
                        open_question=runtime_state.open_question,
                        student_input=student_input,
                        is_math=self._is_math_lesson(context),
                        kb_chunks=[],  # Phase 3: KB retrieval pass
                    ),
                )
                # Update per-objective progress + attempts counter.
                self._update_objective_progress(
                    runtime_state=runtime_state,
                    verdict=verdict,
                    current_objective=context.current_objective,
                )
                if verdict.verdict == Verdict.CORRECT:
                    runtime_state.attempts_on_open_question = 0
                else:
                    runtime_state.attempts_on_open_question += 1
                # Bare-answer signal counter (Phase 2 §2.1.1). Counter
                # lives on runtime_state for future tuning; not consumed
                # by move selection or move prompts.
                if verdict.bare_answer:
                    key = (context.current_objective or "_").strip() or "_"
                    runtime_state.bare_answer_counts_by_objective[key] = (
                        runtime_state.bare_answer_counts_by_objective.get(key, 0) + 1
                    )
            except Exception as exc:
                logger.warning(
                    "[TutorEngine] grader raised %s — proceeding as unverified",
                    type(exc).__name__,
                )
                verdict = GradingResult(verdict=Verdict.UNVERIFIED)

        # 2. Select move.
        selected_move = self.pick_move(
            verdict=verdict,
            runtime_state=runtime_state,
            profile_summary=context.profile_summary,
            objective_just_opened=False,
            current_objective=context.current_objective,
            student_input=student_input,
        )

        # 3. Tutor → first attempt.
        first_resp = self._invoke_tutor_or_fallback(
            context=context,
            verdict=verdict,
            move=selected_move,
            media_catalog=media_catalog,
            student_input=student_input,
        )
        attempt_text = first_resp.text
        committed_pose = first_resp.pending_pose

        # 4. Conformance check.
        prior_student_turn = self._prior_student_turn(context)
        bank_stems = self._bank_stems_for_context(context)
        recent_student_turns = self._recent_student_turns(context)
        attached_media_count, figure_facts = self._media_counts_and_facts(
            attempt_text=attempt_text, catalog=media_catalog,
        )
        lesson_has_media = bool(media_catalog)
        conf_result = self.conformance.run(
            candidate_response=attempt_text,
            verdict=verdict,
            runtime_state=runtime_state,
            selected_move=selected_move,
            prior_student_turn=prior_student_turn,
            open_question_stem=(
                runtime_state.open_question.rendered_stem
                if runtime_state.open_question
                else ""
            ),
            attached_media_count=attached_media_count,
            figure_facts=figure_facts,
            bank_stems=bank_stems,
            recent_student_turns=recent_student_turns,
            private_canonical=(verdict.private_canonical if verdict else ""),
            context=context,
            posed_via_tool=(first_resp.pending_pose is not None),
            lesson_has_media=lesson_has_media,
        )

        fallback_used = False
        if not conf_result.passed:
            # One retry on rejection — surface violations to the tutor.
            with emit_span("audit", "tutor.retry") as span:
                retry_resp = self._invoke_tutor_or_fallback(
                    context=context,
                    verdict=verdict,
                    move=selected_move,
                    media_catalog=media_catalog,
                    student_input=student_input,
                    violation_hints=conf_result.violations,
                )
                if span is not None:
                    span["payload"] = {"violations": conf_result.violations[:5]}
            retry_text = retry_resp.text

            retry_conf = self.conformance.run(
                candidate_response=retry_text,
                verdict=verdict,
                runtime_state=runtime_state,
                selected_move=selected_move,
                prior_student_turn=prior_student_turn,
                open_question_stem=(
                    runtime_state.open_question.rendered_stem
                    if runtime_state.open_question
                    else ""
                ),
                attached_media_count=attached_media_count,
                figure_facts=figure_facts,
                bank_stems=bank_stems,
                recent_student_turns=recent_student_turns,
                private_canonical=(verdict.private_canonical if verdict else ""),
                context=context,
                posed_via_tool=(retry_resp.pending_pose is not None),
                lesson_has_media=lesson_has_media,
            )
            retry_conf.retry_used = True

            if retry_conf.passed:
                attempt_text = retry_text
                conf_result = retry_conf
                # Retry's PendingPose replaces the first attempt's —
                # Phase A ran from scratch on retry.
                committed_pose = retry_resp.pending_pose
            else:
                # Safe terminal template — never release a free-form
                # response that failed conformance twice. Discard any
                # pending pose; no Phase B commit happens.
                fallback_used = True
                conf_result = retry_conf
                conf_result.fallback_used = True
                committed_pose = None
                next_action = self._render_next_action_for_template(
                    context=context, runtime_state=runtime_state,
                )
                attempt_text = render_safe_template(
                    verdict=verdict,
                    student_claim_present=(
                        conf_result.labels.student_claim_present
                        if conf_result.labels is not None
                        else False
                    ),
                    next_action_text=next_action,
                )

        # 4b. Phase B commit (Phase 1 §4): only commits if conformance
        # accepted AND the LLM called the pose_question tool. Updates
        # ``runtime_state.open_question`` + appends to the ledger so
        # the next turn's grader has something to grade against.
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

        # 7. Build v2_trace rollup for SessionTurn.judge_outputs.v2_trace.
        v2_trace = {
            "selected_move": selected_move,
            "verdict": verdict.verdict.value if verdict else None,
            "verdict_bare_answer": verdict.bare_answer if verdict else False,
            "conformance_violations": list(conf_result.violations),
            "conformance_labels": (
                conf_result.labels.model_dump()
                if conf_result.labels is not None
                else None
            ),
            "retry_used": conf_result.retry_used,
            "fallback_used": fallback_used,
        }

        return TurnResult(
            response_text=attempt_text,
            runtime_state=runtime_state,
            selected_move=selected_move,
            verdict=verdict,
            fallback_used=fallback_used,
            v2_trace=v2_trace,
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
        student_input: str = "",
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
                student_input=student_input,
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

    # ------------------------------------------------------------------
    # Orchestration helpers (Phase 2 §2.3 / §2.7)
    # ------------------------------------------------------------------

    def _invoke_tutor_or_fallback(
        self,
        *,
        context: TutoringContext,
        verdict: Optional[GradingResult],
        move: str,
        media_catalog: list[dict],
        student_input: str,
        violation_hints: Optional[list[str]] = None,
    ):
        """Call the StudentTutor; on any raise, surface a safe template.

        Returns a ``TutorResponse`` (``text`` + optional
        ``pending_pose``). The safe-template fallback has no
        PendingPose attached.
        """
        from apps.tutoring.v2.services.student_tutor import TutorResponse

        # Violation hints — surfaced as a tail-appended directive to
        # the user prompt so the model can reshape. We do this by
        # patching the student_input parameter — the tutor's user
        # prompt incorporates it; cheap + keeps the surface narrow.
        effective_input = student_input
        if violation_hints:
            tail = "\n\n[Conformance retry — your previous response was rejected for: "
            tail += "; ".join(violation_hints[:5])
            tail += ". Rewrite per the MOVE directives.]"
            effective_input = (student_input or "") + tail

        try:
            return self.tutor.respond(
                context=context,
                verdict=verdict,
                move=move,
                media_catalog=media_catalog,
                student_input=effective_input,
            )
        except Exception as exc:
            logger.warning(
                "[TutorEngine] tutor.respond raised %s — safe template",
                type(exc).__name__,
            )
            next_action = self._render_next_action_for_template(
                context=context, runtime_state=context.runtime_state,
            )
            return TutorResponse(
                text=render_safe_template(
                    verdict=verdict,
                    student_claim_present=False,
                    next_action_text=next_action,
                ),
                pending_pose=None,
            )

    def _build_media_catalog(self, context: TutoringContext) -> list[dict]:
        """Build the per-turn media catalog via ``MediaService``.

        Passes the current objective + last student turn as the
        ranking signal so KB-similarity selects the most-relevant
        figures first (Phase 3 §3.2).
        """
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
        """Parse ``|||MEDIA:N|||`` and resolve figure_facts for conformance."""
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
        """Heuristic: lesson's course flagged math. Fail-soft to False."""
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
        """Resolve the current LessonStep from the session (or None)."""
        session = self.context_manager.session
        idx = getattr(session, "current_step_index", 0) or 0
        try:
            lesson = session.lesson
            return lesson.steps.all()[idx] if hasattr(lesson, "steps") else None
        except (IndexError, AttributeError, Exception):
            return None

    def _prior_student_turn(self, context: TutoringContext) -> str:
        """Last student turn from the transcript (empty when none)."""
        for turn in reversed(context.full_transcript or []):
            if turn.get("role") == "student":
                return turn.get("content") or ""
        return ""

    def _recent_student_turns(self, context: TutoringContext) -> list[str]:
        """Last few student turns — used by rule_check's allowed-number set."""
        out: list[str] = []
        for turn in reversed(context.full_transcript or []):
            if turn.get("role") == "student":
                out.append(turn.get("content") or "")
                if len(out) >= 5:
                    break
        return out

    def _bank_stems_for_context(self, context: TutoringContext) -> list[str]:
        """Fetch bank stems for the lesson — used by rule_check's allowed set.

        Includes BOTH ``teacher_script`` (narrative/explanation text)
        AND ``question`` (actual posable bank stems). Earlier versions
        looked at ``teacher_script`` only, which made rule_check flag
        every number that appeared in a tool-posed question stem as
        "authored" — false-positive rejections on the legitimate
        tool path.
        """
        try:
            from apps.curriculum.models import LessonStep
            rows = list(
                LessonStep.objects
                .filter(lesson_id=context.lesson_id)
                .values_list("teacher_script", "question")[:40]
            )
            stems: list[str] = []
            for teacher_script, question in rows:
                if teacher_script:
                    stems.append(teacher_script)
                if question:
                    stems.append(question)
            return stems
        except Exception:
            return []

    def _render_next_action_for_template(
        self,
        *,
        context: TutoringContext,
        runtime_state: SessionRuntimeState,
    ) -> str:
        """One-line next-action hint for the safe terminal template."""
        # The next move TutorEngine would have picked — used to fill
        # the template's "next action" slot per analysis §3.
        next_move = select_move(
            verdict=None,
            runtime_state=runtime_state,
            profile_summary=context.profile_summary,
            objective_just_opened=False,
            current_objective=context.current_objective,
        )
        # Student-facing — no system vocabulary, varied by move. Picked
        # to read naturally when concatenated after a verdict opener.
        return {
            "pose_question": "Here's one for you to try.",
            "confirm_and_advance": "Let's move on.",
            "confirm_and_extend": "Let's push that a bit further.",
            "scaffold_hint": "Let's work the next step.",
            "name_misconception": "Let me show you the slip I'm seeing.",
            "worked_example": "Let me walk one through first.",
            "explain": "Let me set the idea up first.",
            "pivot": "Let's try a different angle on the same idea.",
            "close_topic": "We're ready to wrap this objective.",
        }.get(next_move, "Let's keep going.")

    def _update_objective_progress(
        self,
        *,
        runtime_state: SessionRuntimeState,
        verdict: GradingResult,
        current_objective: str,
    ) -> None:
        """Bump the per-objective counters after a graded turn."""
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
        elif verdict.verdict == Verdict.UNVERIFIED:
            progress.unverified += 1
        runtime_state.objective_progress[key] = progress

    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Session completion (Phase 3 §3.1 — profiler trigger)
    # ------------------------------------------------------------------

    def complete_session(self) -> None:
        """Mark the session COMPLETED and run the end-of-session profiler.

        Idempotent — repeat calls are no-ops once status is COMPLETED.
        Profiler failure must not block completion: ``StudentProfiler``
        already swallows its own exceptions, but we wrap the call too
        as a belt-and-braces guard against import / wiring errors.
        """
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
