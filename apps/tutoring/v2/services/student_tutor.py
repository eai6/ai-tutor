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
from typing import Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    GradingResult,
    TutoringContext,
    Verdict,
)
from apps.tutoring.v2.services.move_prompts import (
    MOVE_PROMPTS,
    get_move_prompt,
    render_shared_preamble,
)

logger = logging.getLogger(__name__)


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
    ) -> str:
        """Produce the tutor's next utterance for the selected move.

        Plumbing-level invariant: the rendered user prompt below has
        NO ``canonical_answer`` slot for wrong / partial verdicts —
        only ``student_safe_feedback`` reaches the prompt body.
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

            response = client.generate(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                max_tokens=600,
            )
            text = (response.content or "").strip()
            if span is not None:
                span["tokens_in"] = response.tokens_in
                span["tokens_out"] = response.tokens_out
                payload = span.get("payload") or {}
                payload.update({
                    "selected_move": move,
                    "response_chars": len(text),
                })
                span["payload"] = payload
            return text

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
        """
        if not catalog:
            return "=== Media catalog ===\n(none available)"
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
