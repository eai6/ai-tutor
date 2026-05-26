"""StudentProfiler — end-of-session summarization (Phase 3 §3.1).

Reads/writes ``StudentProfile.profile_summary`` TEXT and
``StudentProfile.asked_questions`` JSONB (both columns shipped in
Phase 1). Write cadence: async, end-of-session only for MVP.

Phase 1: skeleton + docstring. Phase 3: full implementation using
the ``PROFILER_SUMMARY`` ModelConfig purpose.
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import ProfileUpdate, TutoringContext


class StudentProfiler:
    """Skeleton. Implemented in Phase 3."""

    def summarize_session(self, context: TutoringContext) -> ProfileUpdate:
        """Generate a session summary (strengths, struggles, examples shown).

        Uses ``ModelConfig.get_for('profiler_summary')`` for the LLM
        call (temperature pinned to 0 — see Phase 1 §7).
        """
        raise NotImplementedError("StudentProfiler.summarize_session — Phase 3")

    def persist(self, student_id: int, update: ProfileUpdate) -> None:
        """Write ``profile_summary`` and merge ``asked_questions`` map.

        ``asked_questions`` map is capped at the last N entries (default
        500) with LRU eviction per Phase 3 §3.1.
        """
        raise NotImplementedError("StudentProfiler.persist — Phase 3")
