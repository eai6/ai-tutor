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
from dataclasses import dataclass
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
# When the selected move is in this set AND the lesson has posable bank
# slots remaining, StudentTutor invokes ``generate_with_tools`` so the
# LLM's only legal way to ask a verifiable question is via the
# pose_question tool — the conformance gate
# ``all__no_assessment_in_prose`` enforces the contract on the response.
POSE_CAPABLE_MOVES: frozenset[str] = frozenset({
    "pose_question",
    "confirm_and_extend",
    "scaffold_hint",
    "name_misconception",
    "worked_example",
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
    """

    text: str
    pending_pose: Optional[PendingPose] = None


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
                "[StudentTutor] unknown move %r; defaulting to pose_question", move
            )
            move = "pose_question"

        system_prompt = self._build_system_prompt(context=context, move=move)
        user_prompt = self._build_user_prompt(
            context=context,
            verdict=verdict,
            move=move,
            media_catalog=media_catalog or [],
            student_input=student_input,
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
                response_or_msg, pending_pose, response_text = (
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
                    })
                    span["payload"] = payload
                return TutorResponse(
                    text=response_text, pending_pose=pending_pose,
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
            if span is not None:
                span["tokens_in"] = response.tokens_in
                span["tokens_out"] = response.tokens_out
                payload = span.get("payload") or {}
                payload.update({
                    "selected_move": move,
                    "tool_path": False,
                    "response_chars": len(text),
                    "leaked_tool_call_chars": leaked_chars,
                })
                span["payload"] = payload
            return TutorResponse(text=text, pending_pose=None)

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
    ) -> tuple[Any, Optional[PendingPose], str]:
        """Invoke ``generate_with_tools`` and process the response.

        Returns ``(raw_message, pending_pose, response_text)``.

        On a successful tool_use block the rendered stem from the
        bank is appended to any text blocks the LLM emitted as
        ``lead_in``. On schema rejection / missing tool_use / Phase A
        validation failure, ``pending_pose`` is None and the
        response_text falls back to whatever text blocks the LLM
        emitted (which conformance will judge on its own merits).
        """
        # ``tool_choice={"type":"any"}`` for ``pose_question`` move
        # forces the LLM through a tool call (cannot answer with prose
        # only). For other pose-capable moves we leave it on
        # ``auto`` — the move may legitimately prefer prose
        # (e.g. ``scaffold_hint`` on a wrong attempt).
        tool_choice = (
            {"type": "any"} if move == "pose_question" else {"type": "auto"}
        )
        try:
            message = client.generate_with_tools(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                tools=[tool_dict],
                max_tokens=900,
                tool_choice=tool_choice,
            )
        except (NotImplementedError, AttributeError, TypeError) as exc:
            logger.warning(
                "[StudentTutor] generate_with_tools unavailable (%s); "
                "falling back to text path",
                type(exc).__name__,
            )
            return None, None, ""

        if not hasattr(message, "content") or not isinstance(
            getattr(message, "content", None), list
        ):
            logger.warning(
                "[StudentTutor] generate_with_tools returned non-Message %r; "
                "falling back to text path",
                type(message).__name__,
            )
            return message, None, ""

        text_chunks: list[str] = []
        pending_pose: Optional[PendingPose] = None
        rendered_stem = ""
        total_leaked_chars = 0

        for block in (message.content or []):
            btype = getattr(block, "type", None)
            if btype == "text":
                raw_chunk = (getattr(block, "text", "") or "").strip()
                cleaned, leaked = strip_leaked_tool_call_syntax(raw_chunk)
                if leaked:
                    total_leaked_chars += leaked
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

        return message, pending_pose, response_text

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

        rendered_stem = (step.question or "").strip()
        lead_in = (tool_input.get("lead_in") or "").strip()

        question_ref = QuestionRef(
            source=QuestionSource.LESSON_STEP,
            id=step.id,
        )
        resolve_canonical = make_resolve_canonical_for_lesson(slot_map)

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
                "mcq_option_order": [],
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
    ) -> str:
        """Build the per-turn user prompt.

        Structure (per prompting-fundamentals long-context guidance):
          1. Current objective + open question (short, near top).
          2. Media catalog block (compact, when present).
          3. Full transcript (the largest piece).
          4. Verdict block (only on wrong / partial / unverified; the
             redacted shape — never the canonical for non-correct).
          5. The student's just-submitted input + the move directive
             RESTATED at the END so the model's recency bias steers
             toward following the move.
        """
        objective_block = self._render_objective_block(context)
        media_block = self._render_media_catalog_block(media_catalog)
        transcript_block = self._render_transcript_block(context.full_transcript)
        verdict_block = self._render_verdict_block(
            verdict=verdict, move=move,
        )
        return (
            f"{objective_block}\n\n"
            f"{media_block}\n\n"
            f"=== Conversation transcript so far ===\n"
            f"{transcript_block}\n\n"
            f"{verdict_block}\n"
            f"=== Student's latest input ===\n"
            f"{student_input.strip() or '(no input)'}\n\n"
            f"---\n"
            f"Produce ONE response that executes the MOVE in the system "
            f"prompt for this turn. Follow the move's directives "
            f"exactly; do not invent a different move."
        )

    # ------------------------------------------------------------------
    # Block renderers
    # ------------------------------------------------------------------

    def _render_objective_block(self, context: TutoringContext) -> str:
        """Compact header with the active objective + open question."""
        open_q = context.runtime_state.open_question
        objective = context.current_objective or "(no objective set)"
        if open_q is None:
            return (
                f"=== Current objective ===\n{objective}\n"
                f"Open question: (none)"
            )
        return (
            f"=== Current objective ===\n{objective}\n"
            f"Open question: {open_q.rendered_stem!r} "
            f"(source={open_q.source.value}, id={open_q.id})"
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
