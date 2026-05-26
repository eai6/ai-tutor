"""StudentTutor — generates per-turn tutor utterances per the move table.

Per Phase 2 §2.2: one focused per-move prompt per move (200-400 tokens
each). Per-move prompts are grounded in design/science-principles.md
chapters cited in refactor-analysis §4. The 460-line legacy system
prompt is NOT ported — a small shared preamble + per-move prompt
keeps stable prefix ~1-2 KB per turn.

Phase 1: signature only. Phase 2: full implementation + per-move
prompts with science-principles.md provenance docstrings.
"""

from __future__ import annotations

from typing import Optional

from apps.tutoring.v2.contracts import GradingResult, TutoringContext


class StudentTutor:
    """Skeleton. Implemented in Phase 2."""

    def respond(
        self,
        context: TutoringContext,
        verdict: Optional[GradingResult],
        move: str,
    ) -> str:
        """Produce the tutor's next utterance for the selected move.

        Plumbing-level invariant: the move prompt template has no slot
        named ``canonical_answer`` for wrong/partial verdicts — only
        ``student_safe_feedback`` reaches the prompt.
        """
        raise NotImplementedError("StudentTutor.respond — Phase 2")
