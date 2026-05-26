"""ExitTicketService — selects + grades exit-ticket questions.

Phase 2 deliverable (§2.6). No "hold gate" / "force-clear after N"
loop; TutorEngine transitions straight to exit ticket when objective
evidence is sufficient (R6).

Phase 1 ships the skeleton.
"""

from __future__ import annotations


class ExitTicketService:
    """Skeleton. Implemented in Phase 2."""

    def select_questions(self, lesson_id: int, student_id: int) -> list[int]:
        """Pick a subset from the lesson's bank, excluding recent attempts."""
        raise NotImplementedError("ExitTicketService.select_questions — Phase 2")

    def grade_response(
        self,
        exit_ticket_question_id: int,
        student_input: str,
    ) -> dict:
        """Route through StudentGrader (bank_grader first, grounded fallback)."""
        raise NotImplementedError("ExitTicketService.grade_response — Phase 2")
