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
from apps.tutoring.v2.services.move_escalation import (
    escalation_target,
    is_terminal as is_terminal_move,
)
from apps.tutoring.v2.services.move_selection import ALLOWED_MOVES, select_move
from apps.tutoring.v2.services.question_extractor import (
    ExtractionResult,
    QuestionExtractor,
)
from apps.tutoring.v2.services.student_grader import StudentGrader
from apps.tutoring.v2.services.student_tutor import StudentTutor
from apps.tutoring.v2.services.templates import MoveAnchor, render_safe_template

logger = logging.getLogger(__name__)


# Safety-valve caps per §7 item 3. Sub-decisions to tune from pilot data.
MAX_TURNS_PER_SESSION = 40
MAX_TURNS_PER_OBJECTIVE = 12
MAX_VERDICTLESS_RUN = 6


def _doing_rate_observability(window: list) -> dict:
    """Compact doing-rate summary for ``v2_trace`` (Phase 4)."""
    truthy = sum(1 for b in window if bool(b))
    total = len(window)
    return {
        "attempted": truthy,
        "total": total,
        "rate": (truthy / total) if total else None,
    }


@dataclass
class TurnResult:
    """Bundle returned from TutorEngine.respond() / start_session().

    ``is_lesson_complete`` is True only when ``close_topic`` fired on
    the FINAL lesson step. Intermediate ``close_topic`` turns (still
    more steps to teach) leave this False so the router keeps the
    session active and re-opens the next step's tutoring. This is the
    contract that lets one ``close_topic`` move advance per-step
    rather than ending the whole lesson on the first objective hit
    (Science of learning principle: Mastery Learning — gate
    every step on its own evidence; Active Learning — practice
    counts must accumulate per session, not collapse to one).
    """

    response_text: str
    runtime_state: SessionRuntimeState
    selected_move: str
    verdict: Optional[GradingResult] = None
    fallback_used: bool = False
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
        conformance: Optional[ConformanceCheck] = None,
        media_service: Optional[MediaService] = None,
        question_extractor: Optional[QuestionExtractor] = None,
    ) -> None:
        self.context_manager = context_manager
        self.grader = grader or StudentGrader()
        self.tutor = tutor or StudentTutor()
        self.conformance = conformance or ConformanceCheck(grader=self.grader)
        self.media_service = media_service or MediaService()
        self.question_extractor = question_extractor or QuestionExtractor()

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
            v2_trace["grounding"] = dict(
                getattr(tutor_resp, "grounding", {}) or {}
            )
        except Exception as exc:
            logger.warning(
                "[TutorEngine] start_session tutor.respond raised %s",
                type(exc).__name__,
            )
            response_text = render_safe_template(
                verdict=None,
                student_claim_present=False,
                next_action_text="Let's get started together.",
                move_anchor=self._build_move_anchor(
                    context=context,
                    selected_move=move,
                    runtime_state=runtime_state,
                ),
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
        from apps.tutoring.v2.services.intent_classifier import (
            INTENT_ATTEMPTING,
            classify_student_intent,
            intent_to_move,
        )
        from apps.tutoring.v2.services.move_selection import (
            update_doing_rate_window,
        )

        open_q_stem = (
            runtime_state.open_question.rendered_stem
            if runtime_state.open_question is not None
            else ""
        )
        # One LLM call (Haiku-backed intent classifier) per turn.
        # The intent result drives BOTH the help-request override AND
        # the Active-Learning doing-rate window (Phase 4 — Principle
        # #1 Active Learning Ch.10).
        student_intent = classify_student_intent(
            student_input=student_input,
            open_question_stem=open_q_stem,
        )
        help_request_move = intent_to_move(student_intent)
        is_help_request = help_request_move is not None

        # Update the doing-rate window: True when the student attempted
        # an answer, False when they hedged / asked for help / sent
        # meta input. Empty / whitespace input counts as False.
        attempting = (
            bool((student_input or "").strip())
            and student_intent == INTENT_ATTEMPTING
        )
        update_doing_rate_window(runtime_state, attempting=attempting)

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
                    # Phase 4 Fix 2a (moved upstream) — clear the
                    # open_question NOW, before move selection picks
                    # confirm_and_advance / confirm_and_extend.
                    # Otherwise the in-turn new pose trips the
                    # stickiness gate against the stale open
                    # question. Principle #4 Mastery Learning Ch.13:
                    # a satisfied question is no longer the
                    # knowledge frontier and must not anchor the next
                    # tutor turn's posing.
                    if runtime_state.open_question is not None:
                        runtime_state.open_question = None
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
            help_request_move=help_request_move,
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
        # Grounding headers the LLM emitted (or didn't) — preserved
        # for v2_trace.grounding so the observability dashboard can
        # surface turns where the LLM's stated grounding contradicts
        # the visible response.
        grounding = dict(getattr(first_resp, "grounding", {}) or {})

        # 3b. Post-render question extraction (Phase 4, Fix 2c).
        # Subject-agnostic Haiku call counts the distinct action prompts
        # in the rendered turn. Two invariants enforced here, BEFORE
        # the deterministic conformance gates:
        #   - One action prompt per turn
        #     (Principle #5 Minimising Cognitive Load Ch.14 —
        #     one idea per turn).
        #   - Active end on every turn
        #     (Principle #1 Active Learning Ch.10 — student must be
        #     *doing* on ≥60% of turns).
        # The extractor outcome is surfaced to conformance as violation
        # hints; the existing retry → escalation path handles it. The
        # extractor is FAIL-SOFT: on outage it reports a single action
        # prompt so conformance behaviour is unchanged. This preserves
        # the design principle of deterministic gates as safety floors,
        # not flow controllers.
        first_extraction = self.question_extractor.extract(
            tutor_text=attempt_text, selected_move=selected_move,
        )
        extractor_violations = self._extractor_violations(
            extraction=first_extraction, selected_move=selected_move,
        )
        retry_extraction: Optional[ExtractionResult] = None

        # 4. Conformance check.
        prior_student_turn = self._prior_student_turn(context)
        bank_stems = self._bank_stems_for_context(context)
        recent_student_turns = self._recent_student_turns(context)
        attached_media_count, figure_facts = self._media_counts_and_facts(
            attempt_text=attempt_text, catalog=media_catalog,
        )
        lesson_has_media = bool(media_catalog)
        pose_tool_available = self._pose_tool_available(
            context=context, runtime_state=runtime_state,
        )
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
            pending_pose=first_resp.pending_pose,
            pose_tool_available=pose_tool_available,
        )
        # Phase 4 Fix 2c — fold extractor violations into the
        # conformance result so the existing retry path handles them
        # identically to deterministic-gate violations.
        if extractor_violations:
            conf_result.violations = list(conf_result.violations) + list(
                extractor_violations
            )
            conf_result.passed = False

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

            retry_extraction = self.question_extractor.extract(
                tutor_text=retry_text, selected_move=selected_move,
            )
            retry_extractor_violations = self._extractor_violations(
                extraction=retry_extraction, selected_move=selected_move,
            )

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
                pending_pose=retry_resp.pending_pose,
                pose_tool_available=pose_tool_available,
            )
            retry_conf.retry_used = True
            if retry_extractor_violations:
                retry_conf.violations = list(retry_conf.violations) + list(
                    retry_extractor_violations
                )
                retry_conf.passed = False

            if retry_conf.passed:
                attempt_text = retry_text
                conf_result = retry_conf
                # Retry's PendingPose replaces the first attempt's —
                # Phase A ran from scratch on retry.
                committed_pose = retry_resp.pending_pose
                grounding = dict(
                    getattr(retry_resp, "grounding", {}) or {}
                )
            else:
                # Phase 4 Fix 3 — move ESCALATION rather than a
                # verdict-keyed prose blob. The move-escalation ladder
                # picks the principled neighbour move (test → teach
                # the method → re-frame → hand off) and runs ONE more
                # tutor + conformance attempt. The escalated move's
                # safe-terminal template is the floor if THAT also
                # fails — so the student still gets the escalated
                # move's pedagogy, not a generic apology.
                #
                # Active Learning preserved: every move in the ladder
                # ends with an action the student takes
                # (Principle #1 Active Learning Ch.10).
                escalated_move = escalation_target(selected_move)
                if (
                    escalated_move
                    and escalated_move != selected_move
                    and escalated_move in ALLOWED_MOVES
                ):
                    (
                        esc_text,
                        esc_pose,
                        esc_conf,
                        esc_extraction,
                        esc_grounding,
                    ) = self._run_escalation_attempt(
                        context=context,
                        verdict=verdict,
                        escalated_move=escalated_move,
                        media_catalog=media_catalog,
                        student_input=student_input,
                        prior_student_turn=prior_student_turn,
                        bank_stems=bank_stems,
                        recent_student_turns=recent_student_turns,
                        attached_media_count=attached_media_count,
                        figure_facts=figure_facts,
                        lesson_has_media=lesson_has_media,
                        runtime_state=runtime_state,
                    )
                    if esc_conf.passed:
                        attempt_text = esc_text
                        conf_result = esc_conf
                        conf_result.retry_used = True
                        # Record the escalation in the trace via the
                        # selected_move surface so observability shows
                        # WHICH move was emitted.
                        selected_move = escalated_move
                        committed_pose = esc_pose
                        retry_extraction = esc_extraction
                        grounding = esc_grounding
                    else:
                        # Escalation also failed — fall through to the
                        # safe terminal template, but keyed to the
                        # ESCALATED move so the floor delivers that
                        # move's pedagogy minimum (worked example body
                        # / explain framing / etc.).
                        fallback_used = True
                        conf_result = esc_conf
                        conf_result.fallback_used = True
                        committed_pose = None
                        retry_extraction = esc_extraction
                        selected_move = escalated_move
                        attempt_text = render_safe_template(
                            verdict=verdict,
                            student_claim_present=(
                                conf_result.labels.student_claim_present
                                if conf_result.labels is not None
                                else False
                            ),
                            next_action_text="",
                            move_anchor=self._build_move_anchor(
                                context=context,
                                selected_move=escalated_move,
                                runtime_state=runtime_state,
                            ),
                        )
                else:
                    # Terminal move (close_topic) or no escalation
                    # available — render the safe template at the
                    # original move. Discard any pending pose.
                    fallback_used = True
                    conf_result = retry_conf
                    conf_result.fallback_used = True
                    committed_pose = None
                    attempt_text = render_safe_template(
                        verdict=verdict,
                        student_claim_present=(
                            conf_result.labels.student_claim_present
                            if conf_result.labels is not None
                            else False
                        ),
                        next_action_text="",
                        move_anchor=self._build_move_anchor(
                            context=context,
                            selected_move=selected_move,
                            runtime_state=runtime_state,
                        ),
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

        # 6b. Step advancement on close_topic — when more steps remain
        # in the lesson, advance to the next step rather than ending
        # the lesson. Only the FINAL step's close_topic marks the
        # session lesson-complete. (Mastery Learning — every
        # step gets its own evidence bar; Active Learning —
        # multi-step lessons must accumulate practice across steps,
        # not collapse to the first objective hit.)
        is_lesson_complete = False
        advanced_to_step_index: Optional[int] = None
        if selected_move == "close_topic":
            advanced_to_step_index = self._advance_step_if_possible(
                runtime_state=runtime_state,
            )
            is_lesson_complete = advanced_to_step_index is None

        # 7. Build v2_trace rollup for SessionTurn.judge_outputs.v2_trace.
        # ``question_extractor`` records the final-attempt action_count
        # / has_active_end so observability can spot Active-Learning
        # violations even when conformance passed.
        final_extraction: ExtractionResult = (
            retry_extraction if retry_extraction is not None else first_extraction
        )
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
            "is_lesson_complete": is_lesson_complete,
            "advanced_to_step_index": advanced_to_step_index,
            "question_extractor": {
                "action_count": final_extraction.action_count,
                "has_active_end": final_extraction.has_active_end,
                "available": final_extraction.available,
            },
            "doing_rate_5turn": _doing_rate_observability(
                runtime_state.student_doing_rate_window or []
            ),
            # GRADER / EVIDENCE grounding lines the LLM emitted (or
            # didn't) for the final-attempt response. ``missing`` is a
            # list of header names absent from the response — non-empty
            # values let the observability dashboard surface turns where
            # the prompt's grounding directive wasn't followed.
            "grounding": grounding,
        }

        return TurnResult(
            response_text=attempt_text,
            runtime_state=runtime_state,
            selected_move=selected_move,
            verdict=verdict,
            fallback_used=fallback_used,
            v2_trace=v2_trace,
            is_lesson_complete=is_lesson_complete,
            advanced_to_step_index=advanced_to_step_index,
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
        help_request_move: Optional[str] = None,
    ) -> str:
        """Pure-function move pick, then safety-valve override.

        Safety valves (per §7 item 3) take precedence over the
        principled move table — a session that has hit the per-objective
        cap or has drifted 6 verdict-less turns *must* close, even when
        the move table would otherwise pick something else.

        ``help_request_move`` is an optional pre-computed intent override
        (set by ``TutorEngine.respond`` after running the Haiku-backed
        intent classifier once per turn). When provided, ``select_move``
        skips its on-demand classifier call.
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
                help_request_move=help_request_move,
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
                    move_anchor=self._build_move_anchor(
                        context=context,
                        selected_move=move,
                        runtime_state=context.runtime_state,
                    ),
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

    def _advance_step_if_possible(
        self,
        *,
        runtime_state: SessionRuntimeState,
    ) -> Optional[int]:
        """Advance ``session.current_step_index`` to the next step.

        Called when ``close_topic`` fires. Returns the new step index
        when an advance happened, or ``None`` when the active step was
        the final step (signalling lesson completion to the caller).

        Side effects when advancing:
          * Increments ``session.current_step_index`` and persists.
          * Clears ``runtime_state.open_question`` — the previous
            step's open question is no longer in play.
          * Resets ``runtime_state.attempts_on_open_question`` to 0.
          * Resets ``runtime_state.unverified_run_length`` to 0.
          * Marks the just-closed objective progress as ``closed=True``
            so ``_objective_evidence_sufficient`` no longer fires on it.
          * Resets ``safety_valve_counters.turns_on_current_objective``
            so the per-objective cap restarts for the new step.

        Subject-agnostic — works the same way for math, geography, or
        any other lesson type. Fail-soft on any DB error: returns
        ``None`` (treated as final-step) so a flaky lesson model never
        blocks lesson completion.
        """
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
            return None  # final step — lesson is done

        # Persist the step advance to the session row.
        try:
            session.current_step_index = next_idx
            session.save(update_fields=["current_step_index"])
        except Exception as exc:
            logger.warning(
                "[TutorEngine] failed to persist current_step_index advance: %s",
                type(exc).__name__,
            )
            return None

        # Reset per-step runtime state. The just-closed objective is
        # marked closed in objective_progress so future move-selection
        # passes don't keep firing close_topic on it; the new step's
        # objective starts fresh on the next assemble_context.
        runtime_state.open_question = None
        runtime_state.attempts_on_open_question = 0
        runtime_state.unverified_run_length = 0
        # Mark the just-closed objective as closed (the
        # ``_objective_evidence_sufficient`` check skips closed ones).
        # We don't know the objective key without re-reading the prior
        # context, so close any progress entry that matched the prior
        # step's enabling_objective.
        for key, prog in runtime_state.objective_progress.items():
            if prog is not None and not prog.closed and prog.correct >= 1:
                prog.closed = True
                runtime_state.objective_progress[key] = prog
        # Reset per-objective turn counter so the new step gets its
        # own MAX_TURNS_PER_OBJECTIVE budget.
        runtime_state.safety_valve_counters.turns_on_current_objective = 0
        return next_idx

    def _run_escalation_attempt(
        self,
        *,
        context: TutoringContext,
        verdict: Optional[GradingResult],
        escalated_move: str,
        media_catalog: list,
        student_input: str,
        prior_student_turn: str,
        bank_stems: list[str],
        recent_student_turns: list[str],
        attached_media_count: int,
        figure_facts: list[str],
        lesson_has_media: bool,
        runtime_state: SessionRuntimeState,
    ) -> tuple[str, Any, ConformanceResult, ExtractionResult]:
        """Run one tutor + conformance + extractor pass at the
        escalated move (Phase 4 Fix 3).

        The escalation re-enters StudentTutor from scratch — different
        move prompt, fresh tool path. Returns the same shape as the
        first / retry pass so the caller can swap it in if conformance
        passes.
        """
        with emit_span("audit", "tutor.escalation") as span:
            esc_resp = self._invoke_tutor_or_fallback(
                context=context,
                verdict=verdict,
                move=escalated_move,
                media_catalog=media_catalog,
                student_input=student_input,
            )
            if span is not None:
                span["payload"] = {"escalated_move": escalated_move}
        esc_text = esc_resp.text

        esc_extraction = self.question_extractor.extract(
            tutor_text=esc_text, selected_move=escalated_move,
        )
        esc_extractor_violations = self._extractor_violations(
            extraction=esc_extraction, selected_move=escalated_move,
        )

        esc_conf = self.conformance.run(
            candidate_response=esc_text,
            verdict=verdict,
            runtime_state=runtime_state,
            selected_move=escalated_move,
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
            posed_via_tool=(esc_resp.pending_pose is not None),
            lesson_has_media=lesson_has_media,
            pending_pose=esc_resp.pending_pose,
            pose_tool_available=self._pose_tool_available(
                context=context, runtime_state=runtime_state,
            ),
        )
        if esc_extractor_violations:
            esc_conf.violations = list(esc_conf.violations) + list(
                esc_extractor_violations
            )
            esc_conf.passed = False
        return (
            esc_text,
            esc_resp.pending_pose,
            esc_conf,
            esc_extraction,
            dict(getattr(esc_resp, "grounding", {}) or {}),
        )

    def _extractor_violations(
        self,
        *,
        extraction: ExtractionResult,
        selected_move: str,
    ) -> list[str]:
        """Turn extractor output into conformance violations.

        Subject-agnostic. Two violations possible:
          - ``one_question_per_turn`` when ``action_count > 1`` —
            stacked questions (run-6 GEO T16 worked_example + Port
            Louis MCQ; run-6 GEO T18/T20 follow-on).
          - ``active_end_required`` when ``has_active_end=False``
            UNLESS the move is ``close_topic`` (which legitimately
            hands off to the exit-ticket retrieval rather than ending
            on an inline action prompt).

        Returns empty when no violations or when the extractor was
        unavailable (fail-soft).
        """
        if not extraction.available:
            return []
        violations: list[str] = []
        if extraction.action_count > 1:
            stacked_preview = "; ".join(extraction.stacked_examples[:3])
            violations.append(
                "one_question_per_turn: rendered turn contains "
                f"{extraction.action_count} action prompts; emit exactly "
                f"one this turn. (Principle #5 Minimising Cognitive Load — "
                f"one idea per turn). Stacked examples: "
                f"{stacked_preview}"
            )
        if (
            extraction.action_count == 0
            and selected_move != "close_topic"
            and not extraction.has_active_end
        ):
            violations.append(
                "active_end_required: rendered turn does not close on an "
                "action the student takes. End with a question, choose-and-"
                "explain, or 'now you try' the student can act on this "
                "turn. (Principle #1 Active Learning — student must be "
                "doing on ≥60% of turns)."
            )
        return violations

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

    def _pose_tool_available(
        self,
        *,
        context: TutoringContext,
        runtime_state: SessionRuntimeState,
    ) -> bool:
        """True when the lesson has at least one un-posed bank slot.

        Mirrors the filter ``build_anthropic_pose_question_tool``
        applies. When this is False the LLM had no tool surface to
        call — the conformance ``all__no_assessment_in_prose`` rule
        should not penalise a prose practice prompt because there was
        no tool path available. Fail-soft: returns True on DB error
        so the existing tighter contract holds.
        (Principle #11 Testing Effect / Retrieval Practice Ch.20 — the
        retrieval loop only consolidates when the question lands with
        a verdict; if the only path to a verdict is unavailable, do
        not penalise the LLM for trying prose.)
        """
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

    def _build_move_anchor(
        self,
        *,
        context: TutoringContext,
        selected_move: str,
        runtime_state: SessionRuntimeState,
    ) -> MoveAnchor:
        """Build the pedagogy anchor handed to ``render_safe_template``.

        Subject-agnostic: pulls open-question stem from runtime state
        and step-level content (``teacher_script`` /
        ``worked_example``) from the pre-resolved TutoringContext
        fields. The safety floor uses whichever fields are populated;
        empty fields trigger generic-shape fallbacks within the
        templates module.
        """
        open_q = runtime_state.open_question
        obj_key = (context.current_objective or "_").strip() or "_"
        obj_progress = runtime_state.objective_progress.get(obj_key)
        return MoveAnchor(
            selected_move=selected_move,
            open_question_stem=(open_q.rendered_stem if open_q else ""),
            objective=context.current_objective or "",
            teacher_script=context.current_step_teacher_script or "",
            worked_example=context.current_step_worked_example or "",
            turns_in_session=(
                runtime_state.safety_valve_counters.turns_in_session
            ),
            objective_correct=(obj_progress.correct if obj_progress else 0),
            objective_attempts=(obj_progress.attempts if obj_progress else 0),
        )

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
