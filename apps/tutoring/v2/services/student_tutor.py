"""StudentTutor — generates per-turn tutor utterances per the move table.

Per Phase 2 §2.2:

  - One service entry point: ``respond(context, verdict, move) → str``.
  - One focused per-move prompt per move (lives in
    ``move_prompts.py``). 200–400 tokens each.
  - Shared preamble (growth-mindset framing, locale, persona,
    institution, grade level, mobile-shape directive) — NOT sourced
    from ``science-principles.md`` (its 13-principle table belongs to
    per-move prompts).
  - The tutor receives the **full transcript** — no windowing in MVP
    per §7 item 10.
  - The tutor receives ``student_safe_feedback`` on wrong / partial
    moves; ``private_canonical`` is NEVER passed through. Plumbing
    invariant: this module's prompt template has no slot named
    ``canonical_answer`` for those moves.
  - The tutor receives a media catalog from ``MediaService`` (Phase 2
    inlines a thin selector; Phase 3 extracts it). Dual-coding
    directives (Ch.14 "verbal + visual throughout") inform when to
    emit the ``|||MEDIA:N|||`` signal — wording lives in the
    per-move prompt where it applies.

Span emission: ``tutor.move_call`` wraps the LLM call. The underlying
``BaseLLMClient.generate`` adds its own ``llm_call`` span giving a
two-level trace (move → underlying model call).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    GradingResult,
    PendingPose,
    QuestionRef,
    QuestionSource,
    TutoringContext,
    Verdict,
)
from apps.tutoring.v2.services.move_prompts import (
    MOVE_PROMPTS,
    get_move_prompt,
    render_shared_preamble,
)
from apps.tutoring.v2.tools.pose_question import (
    POSE_QUESTION_LLM_TOOL_NAME,
    ToolRejection,
    build_anthropic_pose_question_tool,
    make_resolve_canonical_for_lesson,
    validate_pose,
)
from apps.tutoring.v2.utilities.tool_call_strip import (
    strip_leaked_tool_call_syntax,
)

logger = logging.getLogger(__name__)


# Moves that may legitimately pose a fresh assessment question this turn.
# Post-router cutover (design/tasks/move-router-implementation-plan.md
# §2.5): every move except ``close_topic`` is pose-capable. The deleted
# ``pose_question`` move's force-tool role is gone — the conformance
# gate ``all__no_assessment_in_prose`` is the only enforcement needed
# now, and all 7 non-terminal moves use ``tool_choice="auto"``.
POSE_CAPABLE_MOVES: frozenset[str] = frozenset({
    "confirm_and_advance",
    "confirm_and_extend",
    "scaffold_hint",
    "name_misconception",
    "worked_example",
    "explain",
    "pivot",
})


@dataclass(frozen=True)
class TutorResponse:
    """One tutor turn's output: visible text + optional PendingPose.

    ``pending_pose`` is set when the LLM called the pose_question tool
    and Phase A validation passed. ``TutorEngine`` commits it via
    ``ContextManager.commit_pending_pose(...)`` ONLY after structural
    conformance approves the visible text (Phase B). On retry or safe
    template fallback the PendingPose is discarded — Phase A re-runs
    from scratch on retry, and no pose is recorded on fallback.

    ``grounding`` carries the GRADER / EVIDENCE header lines the LLM
    emitted before its visible response (see ``SHARED_PREAMBLE_TEMPLATE``).
    The lines are stripped from ``text`` (never reach the student) and
    logged into ``v2_trace.grounding`` for observability — auditors
    can spot turns where the LLM's stated grounding contradicts what
    its visible reply did.
    """

    text: str
    pending_pose: Optional[PendingPose] = None
    grounding: dict = field(default_factory=dict)


class StudentTutor:
    """Stateless tutor generator. Constructed per-turn."""

    def __init__(
        self,
        *,
        tutor_client_factory=None,
    ) -> None:
        """``tutor_client_factory`` lets tests inject a fake LLM client."""
        self._tutor_client_factory = tutor_client_factory

    # ------------------------------------------------------------------
    # Single entry point
    # ------------------------------------------------------------------

    def respond(
        self,
        context: TutoringContext,
        verdict: Optional[GradingResult],
        move: str,
        *,
        media_catalog: Optional[list[dict]] = None,
        student_input: str = "",
        focus_note: str = "",
        principle_emphasis: Optional[list[str]] = None,
    ) -> TutorResponse:
        """Produce the tutor's next utterance for the selected move.

        Plumbing-level invariant: the rendered user prompt below has
        NO ``canonical_answer`` slot for wrong / partial verdicts —
        only ``student_safe_feedback`` reaches the prompt body.

        Tool-use path: when ``move`` is in ``POSE_CAPABLE_MOVES`` and
        the lesson has un-posed bank slots, the LLM is invoked via
        ``generate_with_tools`` with the slot-indexed pose_question
        tool. On a successful tool_use block, Phase A validation runs
        and the returned ``TutorResponse.pending_pose`` is non-None —
        ``TutorEngine`` commits it after structural conformance.

        Text-only path: every other move (``explain``,
        ``confirm_and_advance``, ``close_topic``) goes through the
        plain ``generate()`` call, exactly like before.
        """
        if move not in MOVE_PROMPTS:
            logger.warning(
                "[StudentTutor] unknown move %r; defaulting to scaffold_hint", move
            )
            move = "scaffold_hint"

        system_prompt = self._build_system_prompt(context=context, move=move)
        user_prompt = self._build_user_prompt(
            context=context,
            verdict=verdict,
            move=move,
            media_catalog=media_catalog or [],
            student_input=student_input,
            focus_note=focus_note,
            principle_emphasis=principle_emphasis or [],
        )

        with emit_span("audit", "tutor.move_call",
                       payload={"selected_move": move}) as span:
            client = self._resolve_tutor_client()
            if client is None:
                if span is not None:
                    span["payload"] = {
                        "selected_move": move,
                        "error": "no TUTOR_MOVE client",
                    }
                # Surface a typed failure rather than emit silently —
                # the orchestrator will route to the safe-template
                # fallback. Keep this raise narrow so tests can match
                # on the type.
                raise RuntimeError(
                    "TutorEngine could not resolve a TUTOR_MOVE LLM client"
                )

            tool_dict, slot_map = (None, {})
            if move in POSE_CAPABLE_MOVES and hasattr(client, "generate_with_tools"):
                posed_step_ids = self._posed_step_ids(context)
                tool_dict, slot_map = build_anthropic_pose_question_tool(
                    lesson_id=context.lesson_id,
                    posed_step_ids=posed_step_ids,
                )

            if tool_dict is not None:
                response_or_msg, pending_pose, response_text, grounding = (
                    self._call_with_tools(
                        client=client,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        tool_dict=tool_dict,
                        slot_map=slot_map,
                        context=context,
                        move=move,
                    )
                )
                if span is not None:
                    payload = span.get("payload") or {}
                    payload.update({
                        "selected_move": move,
                        "tool_path": True,
                        "posed_via_tool": pending_pose is not None,
                        "response_chars": len(response_text),
                        "grounding_missing": grounding.get("missing") or [],
                    })
                    span["payload"] = payload
                return TutorResponse(
                    text=response_text,
                    pending_pose=pending_pose,
                    grounding=grounding,
                )

            response = client.generate(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                max_tokens=600,
            )
            raw_text = (response.content or "").strip()
            # Defensive strip: when the system prompt instructs the LLM
            # to use the pose_question tool but we invoked the text-only
            # path (move not in POSE_CAPABLE_MOVES, or no posable slots
            # remaining), the LLM can leak tool-call markup as prose.
            # Observed v2 form: ``<tool_call>{...}</tool_call>``.
            text, leaked_chars = strip_leaked_tool_call_syntax(raw_text)
            if leaked_chars:
                logger.warning(
                    "[StudentTutor] LEAKED_TOOL_SYNTAX in text path "
                    "(move=%s) — stripped %d chars",
                    move, leaked_chars,
                )
            # Pull the GRADER / EVIDENCE grounding headers off so the
            # student never sees them — they exist to keep the
            # response body honest to the verdict + objective state.
            text, grounding = strip_grounding_lines(text)
            if span is not None:
                span["tokens_in"] = response.tokens_in
                span["tokens_out"] = response.tokens_out
                payload = span.get("payload") or {}
                payload.update({
                    "selected_move": move,
                    "tool_path": False,
                    "response_chars": len(text),
                    "leaked_tool_call_chars": leaked_chars,
                    "grounding_missing": grounding.get("missing") or [],
                })
                span["payload"] = payload
            return TutorResponse(
                text=text, pending_pose=None, grounding=grounding,
            )

    # ------------------------------------------------------------------
    # Tool-use path helpers
    # ------------------------------------------------------------------

    def _posed_step_ids(self, context: TutoringContext) -> set[int]:
        """LessonStep ids already in this session's posed-question ledger.

        Used to filter the slot menu so the LLM cannot re-pose a
        question that was already asked — the in-session repeat guard
        in ``validate_pose`` would refuse it, but pre-filtering keeps
        the tool surface honest and avoids surfacing a rejected slot.
        """
        ids: set[int] = set()
        for entry in context.runtime_state.posed_question_ledger:
            if entry.source == QuestionSource.LESSON_STEP:
                ids.add(entry.id)
        return ids

    def _call_with_tools(
        self,
        *,
        client,
        system_prompt: str,
        user_prompt: str,
        tool_dict: dict,
        slot_map: dict[int, Any],
        context: TutoringContext,
        move: str,
    ) -> tuple[Any, Optional[PendingPose], str, dict]:
        """Invoke ``generate_with_tools`` and process the response.

        Returns ``(raw_message, pending_pose, response_text, grounding)``.

        On a successful tool_use block the rendered stem from the
        bank is appended to any text blocks the LLM emitted as
        ``lead_in``. On schema rejection / missing tool_use / Phase A
        validation failure, ``pending_pose`` is None and the
        response_text falls back to whatever text blocks the LLM
        emitted (which conformance will judge on its own merits).
        """
        # Post-router cutover (plan §2.5): all pose-capable moves use
        # ``tool_choice="auto"``. The conformance gate
        # ``all__no_assessment_in_prose`` enforces the contract that
        # any visible assessment question must have come through the
        # tool; the legacy ``{"type":"any"}`` force-mode for
        # pose_question is gone (pose_question itself is gone).
        try:
            message = client.generate_with_tools(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                tools=[tool_dict],
                max_tokens=900,
                tool_choice={"type": "auto"},
            )
        except (NotImplementedError, AttributeError, TypeError) as exc:
            logger.warning(
                "[StudentTutor] generate_with_tools unavailable (%s); "
                "falling back to text path",
                type(exc).__name__,
            )
            return None, None, "", {}

        if not hasattr(message, "content") or not isinstance(
            getattr(message, "content", None), list
        ):
            logger.warning(
                "[StudentTutor] generate_with_tools returned non-Message %r; "
                "falling back to text path",
                type(message).__name__,
            )
            return message, None, "", {}

        text_chunks: list[str] = []
        pending_pose: Optional[PendingPose] = None
        rendered_stem = ""
        total_leaked_chars = 0
        grounding: dict = {}

        for block in (message.content or []):
            btype = getattr(block, "type", None)
            if btype == "text":
                raw_chunk = (getattr(block, "text", "") or "").strip()
                cleaned, leaked = strip_leaked_tool_call_syntax(raw_chunk)
                if leaked:
                    total_leaked_chars += leaked
                # Pull GRADER / EVIDENCE headers off the chunk before
                # it becomes the lead-in. Only the first chunk with
                # headers wins; subsequent chunks have their grounding
                # info merged in case the LLM split the headers
                # across blocks (rare).
                cleaned, chunk_grounding = strip_grounding_lines(cleaned)
                if not grounding:
                    grounding = chunk_grounding
                else:
                    for k in ("grader", "evidence"):
                        if not grounding.get(k) and chunk_grounding.get(k):
                            grounding[k] = chunk_grounding[k]
                if cleaned:
                    text_chunks.append(cleaned)
            elif btype == "tool_use":
                name = getattr(block, "name", "") or ""
                if name != POSE_QUESTION_LLM_TOOL_NAME:
                    logger.warning(
                        "[StudentTutor] unexpected tool_use %r — ignoring",
                        name,
                    )
                    continue
                if pending_pose is not None:
                    # Only the first pose per turn is honored; extra
                    # tool calls are dropped (mirrors legacy behavior).
                    continue
                pending_pose, rendered_stem = self._handle_pose_tool_use(
                    block=block, slot_map=slot_map, context=context,
                )

        if total_leaked_chars:
            logger.warning(
                "[StudentTutor] LEAKED_TOOL_SYNTAX in tool-path text "
                "blocks (move=%s) — stripped %d chars",
                move, total_leaked_chars,
            )

        if rendered_stem:
            # Combine LLM lead-in text + bank stem. The student sees:
            #   <lead_in?>
            #
            #   <bank stem>
            joined_lead = "\n\n".join(c for c in text_chunks if c).strip()
            if joined_lead:
                response_text = f"{joined_lead}\n\n{rendered_stem}"
            else:
                response_text = rendered_stem
        else:
            response_text = "\n\n".join(c for c in text_chunks if c).strip()

        # Default empty grounding when no text chunks were emitted
        # (forced tool_choice="any" can return only a tool_use block).
        if not grounding:
            grounding = {
                "grader": "",
                "evidence": "",
                "missing": ["grader", "evidence"],
            }
        return message, pending_pose, response_text, grounding

    def _handle_pose_tool_use(
        self,
        *,
        block,
        slot_map: dict[int, Any],
        context: TutoringContext,
    ) -> tuple[Optional[PendingPose], str]:
        """Resolve a slot tool_use → PendingPose + rendered stem.

        On any failure (invalid slot, validate_pose rejection) returns
        ``(None, "")`` and the caller falls back to text-only blocks.
        """
        tool_input = getattr(block, "input", {}) or {}
        try:
            slot = int(tool_input.get("slot"))
        except (TypeError, ValueError):
            logger.warning(
                "[StudentTutor] pose_question tool_use missing/invalid slot: %r",
                tool_input.get("slot"),
            )
            return None, ""

        step = slot_map.get(slot)
        if step is None:
            logger.warning(
                "[StudentTutor] pose_question tool_use slot=%d not in slot_map",
                slot,
            )
            return None, ""

        rendered_stem = _render_bank_stem_with_options(step)
        lead_in = (tool_input.get("lead_in") or "").strip()

        question_ref = QuestionRef(
            source=QuestionSource.LESSON_STEP,
            id=step.id,
        )
        resolve_canonical = make_resolve_canonical_for_lesson(slot_map)

        # MCQ option order — populated for choice-style answer types so
        # the visible-context snapshot captures what the student saw.
        # Phase 4 Fix 4a: an MCQ stem with no options is unanswerable;
        # the renderer now concatenates choices into the visible stem
        # and we record them here for the snapshot.
        mcq_option_order = _extract_mcq_letters(step)

        # Phase A validation. The visible_prompt the LLM is committing
        # to is the BANK stem (not the LLM's lead_in) — that's what
        # conformance + repeat guards reason about.
        result = validate_pose(
            session_id=context.session_id,
            student_id=context.student_id,
            raw_args={
                "question_ref": question_ref.model_dump(mode="json"),
                "rendered_stem": rendered_stem,
                "attached_media_ids": [],
                "recent_transcript": [
                    (t.get("content") or "")[:240]
                    for t in (context.full_transcript or [])[-6:]
                ],
                "mcq_option_order": mcq_option_order,
            },
            runtime_state=context.runtime_state,
            asked_questions=self._asked_questions_for_student(context),
            resolve_canonical=resolve_canonical,
            pre_pose_check=lambda **_kw: None,
        )
        if isinstance(result, ToolRejection):
            logger.warning(
                "[StudentTutor] validate_pose REJECTED slot=%d reason=%s "
                "detail=%s",
                slot, result.reason, (result.detail or "")[:120],
            )
            return None, ""

        # Optional lead-in is merged in by the caller — we return just
        # the stem so the caller can decide how to splice text blocks.
        display_stem = rendered_stem
        if lead_in and not lead_in.endswith("?"):
            display_stem = f"{lead_in}\n\n{rendered_stem}"
        return result, display_stem

    def _asked_questions_for_student(
        self, context: TutoringContext,
    ) -> Optional[dict]:
        """Snapshot of ``StudentProfile.asked_questions`` for the cross-
        session repeat guard. Fail-soft to ``None`` (treated as empty
        by the guard) when the profile isn't reachable here."""
        try:
            from apps.accounts.models import StudentProfile

            profile = (
                StudentProfile.objects
                .filter(user_id=context.student_id)
                .only("asked_questions")
                .first()
            )
            if profile is None:
                return None
            return profile.asked_questions or {}
        except Exception as exc:
            logger.warning(
                "[StudentTutor] asked_questions lookup failed (%s) — "
                "guard treats as empty",
                type(exc).__name__,
            )
            return None

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    def _build_system_prompt(
        self,
        *,
        context: TutoringContext,
        move: str,
    ) -> str:
        """Compose the shared preamble + per-move prompt body."""
        preamble = render_shared_preamble(
            locale=context.locale,
            institution_name=context.institution_name,
            grade_level=context.grade_level,
            tutor_persona=context.tutor_persona,
            client_kind=context.client_kind,
            lesson_title=context.lesson_title,
            lesson_subject=context.lesson_subject,
            current_objective=context.current_objective,
            doing_rate_window=list(
                context.runtime_state.student_doing_rate_window or []
            ),
        )
        move_prompt = get_move_prompt(move)
        return (
            f"{preamble}\n"
            f"=== MOVE: {move} ===\n"
            f"{move_prompt.body}"
        )

    def _build_user_prompt(
        self,
        *,
        context: TutoringContext,
        verdict: Optional[GradingResult],
        move: str,
        media_catalog: list[dict],
        student_input: str,
        focus_note: str = "",
        principle_emphasis: Optional[list[str]] = None,
    ) -> str:
        """Build the per-turn user prompt.

        Structure (per prompting-fundamentals long-context guidance):
          1. Current objective + open question (short, near top).
          2. Media catalog block (compact, when present).
          3. Per-turn focus block (router-provided focus_note +
             principle emphasis) — placed AFTER the per-move body in
             the system prompt and BEFORE the transcript so the move's
             general guidance is contextualized by this turn's specific
             direction (plan §2.5).
          4. Full transcript (the largest piece).
          5. Verdict block (only on wrong / partial / unverified; the
             redacted shape — never the canonical for non-correct).
          6. The student's just-submitted input + the move directive
             RESTATED at the END so the model's recency bias steers
             toward following the move.
        """
        objective_block = self._render_objective_block(context)
        evidence_block = self._render_objective_evidence_block(context)
        media_block = self._render_media_catalog_block(media_catalog)
        lesson_content_block = self._render_lesson_step_content_block(context)
        focus_block = self._render_focus_block(
            focus_note=focus_note,
            principle_emphasis=principle_emphasis or [],
        )
        transcript_block = self._render_transcript_block(context.full_transcript)
        verdict_block = self._render_verdict_block(
            verdict=verdict, move=move,
        )
        return (
            f"{objective_block}\n\n"
            f"{evidence_block}\n\n"
            f"{media_block}\n\n"
            f"{lesson_content_block}\n\n"
            f"{focus_block}"
            f"=== Conversation transcript so far ===\n"
            f"{transcript_block}\n\n"
            f"{verdict_block}\n"
            f"=== Student's latest input ===\n"
            f"{student_input.strip() or '(no input)'}\n\n"
            f"---\n"
            f"Produce ONE response that executes the MOVE in the system "
            f"prompt for this turn. Follow the move's directives "
            f"exactly; do not invent a different move. Remember to "
            f"begin with the two GRADER / EVIDENCE header lines required "
            f"by the system prompt."
        )

    def _render_focus_block(
        self,
        *,
        focus_note: str,
        principle_emphasis: list[str],
    ) -> str:
        """Inject the router's per-turn focus + principle emphasis.

        Emits nothing when both fields are empty (legacy callers,
        opening turns where the router fell back to a default).
        ``focus_note`` STEERS the move LLM, it does NOT script it —
        the per-move prompt body in the system prompt remains the
        load-bearing guide.
        """
        focus = (focus_note or "").strip()
        principles = [
            p.strip() for p in (principle_emphasis or []) if (p or "").strip()
        ]
        if not focus and not principles:
            return ""
        lines = ["=== This turn specifically ==="]
        if focus:
            lines.append(f"- Focus: {focus}")
        if principles:
            lines.append(
                f"- Principles to emphasize: {', '.join(principles)}"
            )
        lines.append(
            "(This steers the turn — it does not script the wording. "
            "Use it to specialize the MOVE in the system prompt to "
            "THIS specific situation.)"
        )
        return "\n".join(lines) + "\n\n"

    def _render_lesson_step_content_block(self, context: TutoringContext) -> str:
        """Render lesson-authored direct-instruction + worked-example text.

        These anchors are the authoritative content for this step. The
        ``explain`` and ``worked_example`` move prompts may lift from
        them so the tutor's wording stays inside the lesson's framing
        instead of training-data improvisation. Empty when neither
        anchor was authored — the prompt then falls back to its own
        generation, same as before.
        """
        ts = (context.current_step_teacher_script or "").strip()
        we = (context.current_step_worked_example or "").strip()
        if not ts and not we:
            return (
                "=== Lesson step content ===\n"
                "(no authored direct-instruction or worked-example text "
                "for the current step — generate the explanation / example "
                "yourself, anchored to the lesson title and objective above)"
            )
        parts = ["=== Lesson step content ==="]
        if ts:
            parts.append("Direct-instruction draft (use to anchor `explain`):")
            parts.append(ts)
        if we:
            if ts:
                parts.append("")
            parts.append("Worked example (use to anchor `worked_example`):")
            parts.append(we)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Block renderers
    # ------------------------------------------------------------------

    def _render_objective_block(self, context: TutoringContext) -> str:
        """Compact header with the active objective + open question."""
        open_q = context.runtime_state.open_question
        objective = context.current_objective or "(no objective set)"
        # Lesson position — the close_topic move uses this to phrase
        # its transition correctly ("move on to next step" vs. "ready
        # for exit ticket"). When unknown (legacy / test contexts) we
        # surface the safe default so the tutor still reads as
        # coherent.
        position_hint = (
            "this is the FINAL step of the lesson"
            if context.is_final_step
            else "more steps remain in the lesson after this one"
        )
        if open_q is None:
            return (
                f"=== Current objective ===\n{objective}\n"
                f"Lesson position: {position_hint}\n"
                f"Open question: (none)"
            )
        return (
            f"=== Current objective ===\n{objective}\n"
            f"Lesson position: {position_hint}\n"
            f"Open question: {open_q.rendered_stem!r} "
            f"(source={open_q.source.value}, id={open_q.id})"
        )

    def _render_objective_evidence_block(self, context: TutoringContext) -> str:
        """Objective progress as a small structured block.

        The LLM paraphrases this into the EVIDENCE: grounding line at
        the top of its response. Subject-agnostic — same shape for
        math, geography, any subject.
        (Principle #4 Mastery Learning Ch.13 — the close / mastery
        signal must correspond to evidence of mastery; surfacing the
        actual counts forces the response to ground in them rather
        than improvise praise.)
        """
        key = (context.current_objective or "_").strip() or "_"
        progress = context.runtime_state.objective_progress.get(key)
        turns_in_session = (
            context.runtime_state.safety_valve_counters.turns_in_session
        )
        if progress is None or progress.attempts == 0:
            return (
                "=== Objective evidence ===\n"
                "No attempts on this objective yet — turns in session: "
                f"{turns_in_session}."
            )
        ratio_pct = round(
            100.0 * progress.correct / max(1, progress.attempts)
        )
        return (
            "=== Objective evidence ===\n"
            f"attempts={progress.attempts}, "
            f"correct={progress.correct}, "
            f"wrong={progress.wrong}, "
            f"partial={progress.partial}, "
            f"unverified={progress.unverified}, "
            f"correct_ratio={ratio_pct}%, "
            f"turns_in_session={turns_in_session}"
        )

    def _render_media_catalog_block(self, catalog: list[dict]) -> str:
        """Render the lesson-scoped media catalog the tutor can reference.

        When the catalog is non-empty, includes the dual-coding
        directive from ``MediaService.dual_coding_directive()`` per
        Phase 3 §3.2 — verbal + visual throughout (Ch. 14).

        When the catalog is empty (curriculum content gap), emit an
        explicit "no figures" directive so the LLM doesn't improvise
        a description of a figure that doesn't exist. The geography
        evaluation surfaced this — a "Map Scale and Map Types" lesson
        with zero media is high-risk for hallucinated visuals.
        """
        if not catalog:
            return (
                "=== Media catalog ===\n"
                "(none available — this lesson has no published figures)\n"
                "- Do NOT describe a figure as if one were shown. Do NOT "
                "emit a ``|||MEDIA:N|||`` signal.\n"
                "- If a visual would help, acknowledge the gap briefly "
                "(\"I don't have a digital figure for this yet — ask "
                "your teacher for the printed diagram\") and move on. "
                "Do not invent a description of what the figure would "
                "look like."
            )
        from apps.tutoring.v2.services.media import MediaService

        lines = ["=== Media catalog ==="]
        for idx, entry in enumerate(catalog, start=1):
            title = (entry.get("title") or "").strip() or "(untitled)"
            desc = (entry.get("description") or "").strip()
            lines.append(f"[{idx}] {title}" + (f" — {desc}" if desc else ""))
        lines.append(
            "Append `|||MEDIA:N|||` as the LAST line of your response when "
            "showing a figure (1-based index into the catalog above)."
        )
        lines.append(MediaService.dual_coding_directive())
        return "\n".join(lines)

    def _render_transcript_block(self, transcript: list[dict]) -> str:
        """Render the full transcript (no windowing per §7 item 10).

        Each turn surfaced as ``[role] content``. Empty transcript
        emits an explicit marker so the model knows it's a fresh
        session.
        """
        if not transcript:
            return "(empty transcript — fresh session)"
        rendered = []
        for turn in transcript:
            role = (turn.get("role") or "?").strip()
            content = (turn.get("content") or "").strip()
            rendered.append(f"[{role}] {content}")
        return "\n".join(rendered)

    def _render_verdict_block(
        self,
        *,
        verdict: Optional[GradingResult],
        move: str,
    ) -> str:
        """Render the grader's verdict for the move prompt.

        **Invariant (Phase 2 §2.2):** the rendered block contains NO
        ``canonical_answer`` field for ``wrong`` or ``partial``
        verdicts — only ``student_safe_feedback``. The
        ``private_canonical`` field on ``GradingResult`` is never
        serialised into this block for those verdicts.

        Under ``verdict=correct`` we DO surface the canonical because
        the student has already produced it; an affirmative
        restatement is allowed per §3 conformance rules.
        """
        if verdict is None:
            return "=== Grader verdict ===\n(no graded turn this round)"

        safe = verdict.student_safe_feedback
        payload: dict = {
            "verdict": verdict.verdict.value,
            "student_value": verdict.student_value,
            "bare_answer": verdict.bare_answer,
            "student_safe_feedback": {
                "what_right": safe.what_right,
                "what_missing": safe.what_missing,
                "first_misconception_redacted": safe.first_misconception_redacted,
            },
        }
        if verdict.verdict == Verdict.CORRECT:
            payload["canonical"] = verdict.private_canonical
        return (
            f"=== Grader verdict ===\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    # ------------------------------------------------------------------
    # Client resolution
    # ------------------------------------------------------------------

    def _resolve_tutor_client(self):
        if self._tutor_client_factory is not None:
            return self._tutor_client_factory()
        from apps.tutoring.v2.services.student_grader import (
            _build_client_for_purpose,
        )
        return _build_client_for_purpose("tutor_move")


# ──────────────────────────────────────────────────────────────────────
# Bank-stem rendering helpers (Phase 4 Fix 4a)
# ──────────────────────────────────────────────────────────────────────


_MCQ_LETTER_RE = re.compile(r"^\s*([A-Da-d])\s*[).:\-]")


# Header lines the SHARED_PREAMBLE_TEMPLATE asks the LLM to emit at
# the top of every response. The strip step pulls them off the visible
# text so the student never sees them; the captured values are
# returned for ``v2_trace.grounding`` observability.
_GROUNDING_LINE_RE = re.compile(
    r"^(?P<key>GRADER|EVIDENCE)\s*:\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)


def strip_grounding_lines(text: str) -> tuple[str, dict]:
    """Pull GRADER: and EVIDENCE: header lines off the LLM's response.

    Returns ``(clean_text, grounding_dict)``. ``grounding_dict`` has up
    to three keys:
      - ``grader``: the GRADER line's value, or empty string if missing.
      - ``evidence``: the EVIDENCE line's value, or empty string if missing.
      - ``missing``: list of missing header names (for observability).

    Scans only the FIRST FEW non-empty lines so a literal "GRADER:" or
    "EVIDENCE:" word later in prose isn't mis-stripped. Tolerant of
    blank lines between the headers and the body.
    """
    found: dict[str, str] = {}
    if not text:
        return "", {"grader": "", "evidence": "", "missing": ["grader", "evidence"]}

    lines = text.split("\n")
    body_start = 0
    # Scan the first ~6 non-empty lines for header matches. Allow up to
    # 2 headers to land non-consecutively (defensive against blank-line
    # noise the LLM may insert).
    scanned = 0
    for i, line in enumerate(lines):
        if not line.strip():
            # Blank lines before the body are tolerated; advance body
            # start to skip them later.
            body_start = i + 1
            continue
        if scanned >= 6 or len(found) >= 2:
            break
        scanned += 1
        m = _GROUNDING_LINE_RE.match(line)
        if not m:
            # First non-blank, non-header line marks the body.
            body_start = i
            break
        key = m.group("key").lower()
        if key not in found:
            found[key] = (m.group("value") or "").strip()
        body_start = i + 1

    # Trim any blank lines between the headers and the body.
    while body_start < len(lines) and not lines[body_start].strip():
        body_start += 1

    clean_text = "\n".join(lines[body_start:]).strip()
    missing = [k for k in ("grader", "evidence") if k not in found]
    return clean_text, {
        "grader": found.get("grader", ""),
        "evidence": found.get("evidence", ""),
        "missing": missing,
    }


def _render_bank_stem_with_options(step) -> str:
    """Render a LessonStep's question with an answer-shape suffix.

    Subject-agnostic. The student must always be able to tell from the
    rendered stem *what shape* of answer is expected.

    Resolved cases:
      * ``multiple_choice``: append the ``choices`` list verbatim.
      * ``true_false``: append "(True or False?)" so the student knows
        the answer space is binary. The legacy renderer dropped this
        for True/False steps, producing P1 incomplete-question turns
        (MATHS-S1 2026-05-27 T1442 — "exit ticket" rendered as a bare
        statement with no T/F prompt).
      * Other answer types (short_answer, numeric, etc.): the question
        stem already encodes the expected shape ("Calculate…", "Name
        the…") — return as authored.

    (Principle #1 Active Learning Ch.10 — the student must be able to
    act on the question this turn; missing the answer-shape signal
    breaks the retrieval loop.)

    Returns the empty string when the step has no question text.
    """
    stem = (getattr(step, "question", "") or "").strip()
    if not stem:
        return ""
    answer_type = (getattr(step, "answer_type", "") or "").strip().lower()

    if answer_type == "multiple_choice":
        choices = getattr(step, "choices", None) or []
        if not isinstance(choices, list) or not choices:
            # Authored as MCQ but choices missing — the Phase A
            # safety floor (``_looks_like_mcq_stem_without_options``)
            # catches this case downstream when the stem actually
            # reads like an MCQ. Return the bare stem here.
            return stem
        rendered_choices = [
            c.strip() for c in choices
            if isinstance(c, str) and c.strip()
        ]
        if not rendered_choices:
            return stem
        return f"{stem}\n\n" + "\n".join(rendered_choices)

    if answer_type == "true_false":
        # Only append if the stem doesn't already cue True/False
        # (some authored stems start with "True or False:").
        lower = stem.lower()
        if "true or false" in lower or "true/false" in lower:
            return stem
        return f"{stem}\n\n(True or False?)"

    return stem


def _extract_mcq_letters(step) -> list[str]:
    """Return the ordered list of MCQ letters from a step's choices.

    Used to populate ``visible_context.mcq_option_order`` for the
    conformance + repeat-guard surfaces. Returns empty list for
    non-MCQ steps.
    """
    answer_type = (getattr(step, "answer_type", "") or "").strip().lower()
    if answer_type != "multiple_choice":
        return []
    choices = getattr(step, "choices", None) or []
    if not isinstance(choices, list):
        return []
    letters: list[str] = []
    for choice in choices:
        if not isinstance(choice, str):
            continue
        m = _MCQ_LETTER_RE.match(choice)
        if m:
            letters.append(m.group(1).upper())
    return letters

