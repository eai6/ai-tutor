"""TutorEngine — top-level orchestrator for the v2 conversational tutor.

Per Phase 2 §2.3, move selection is a pure function (not an LLM call).
Inputs: verdict.kind, attempts_on_open_question, objective_progress,
unverified_run_length, current_move, move_history, profile_summary.
verdict.bare_answer is explicitly NOT a move-selection input — it
biases the selected move's prompt content (Phase 2 §2.1.1).

Phase 1: skeleton. Phase 2: full move-table implementation + safety
valves + conformance retry loop.
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import TutoringContext


class TutorEngine:
    """Skeleton. Implemented in Phase 2."""

    def respond(self, context: TutoringContext, student_input: str) -> dict:
        """Run one turn end-to-end.

        Returns a dict with the tutor's response text, the persisted
        SessionRuntimeState snapshot, and the per-turn rollup for
        ``SessionTurn.judge_outputs.v2_trace``.
        """
        raise NotImplementedError("TutorEngine.respond — Phase 2")

    def start_session(self, context: TutoringContext) -> dict:
        """Produce the opening turn for a new session."""
        raise NotImplementedError("TutorEngine.start_session — Phase 2")
