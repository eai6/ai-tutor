"""ContextManager — single owner of the SessionRuntimeState boundary.

Per Phase 2 §2.7, the ContextManager:
  - Assembles ``TutoringContext`` for each service call (transcript +
    profile snapshot + objective + KB chunks + verdict).
  - Owns load/save of ``TutorSession.runtime_state`` via the typed
    Pydantic model.
  - Implements Phase B commit of ``PendingPose`` objects — the only
    code path that mutates the posed-question ledger or writes
    ``open_question``.

Service calls receive **frozen snapshots**, not live state — this is
what the "stateless services" claim means in practice. Mutation
happens through this manager's save / commit methods only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from apps.tutoring.v2.contracts import (
    OpenQuestion,
    PendingPose,
    PosedQuestionLedgerEntry,
    SessionRuntimeState,
    TutoringContext,
)


class ContextManager:
    """Owns the typed-state boundary for a single TutorSession."""

    def __init__(self, session) -> None:
        """``session`` is a ``apps.tutoring.models.TutorSession`` instance."""
        self.session = session
        self._state: Optional[SessionRuntimeState] = None

    # ------------------------------------------------------------------
    # Load / save boundary
    # ------------------------------------------------------------------

    def load_runtime_state(self) -> SessionRuntimeState:
        """Hydrate ``SessionRuntimeState`` from the JSONField column.

        Returns a fresh empty model if the column is empty (new
        session). The legacy ``engine_state`` is never read here —
        v2 sessions own ``runtime_state`` exclusively.
        """
        if self._state is not None:
            return self._state
        raw = getattr(self.session, "runtime_state", None) or {}
        self._state = SessionRuntimeState.from_jsonable(raw)
        return self._state

    def save_runtime_state(self, state: SessionRuntimeState) -> None:
        """Persist the typed state back to the JSONField column."""
        self._state = state
        self.session.runtime_state = state.to_jsonable()
        self.session.save(update_fields=["runtime_state"])

    # ------------------------------------------------------------------
    # Two-phase commit — Phase B (Phase 1 §4)
    # ------------------------------------------------------------------

    def commit_pending_pose(self, pending: PendingPose) -> SessionRuntimeState:
        """Phase B commit of a Phase-A validated PendingPose.

        Consumes the single-use token (if any), appends to the
        ``posed_question_ledger``, and writes ``open_question`` with
        the captured ``visible_context_at_pose`` snapshot.

        Called only by ``TutorEngine`` after structural conformance
        approves the candidate response. On second conformance
        failure / safe-template fallback, this hook is NOT called and
        no state mutation occurs.
        """
        # Local import to avoid circular import at module load.
        from apps.tutoring.v2.tools.token_cache import token_cache

        if pending.token:
            # Atomic single-use consumption — raises if already
            # consumed or unknown.
            token_cache.consume(self.session.id, pending.token)

        state = self.load_runtime_state()

        now = datetime.now(timezone.utc)
        ledger_entry = PosedQuestionLedgerEntry(
            source=pending.question_ref.source,
            id=pending.question_ref.id,
            jaccard_signature=pending.jaccard_signature,
            posed_at=now,
        )
        state.posed_question_ledger.append(ledger_entry)
        state.open_question = OpenQuestion(
            source=pending.question_ref.source,
            id=pending.question_ref.id,
            canonical=pending.canonical,
            rendered_stem=pending.rendered_stem,
            jaccard_signature=pending.jaccard_signature,
            visible_context_at_pose=pending.visible_context,
            posed_at=now,
        )
        state.attempts_on_open_question = 0
        self.save_runtime_state(state)
        return state

    # ------------------------------------------------------------------
    # TutoringContext assembly (Phase 2 §2.7)
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        *,
        client_kind: str = "web",
        current_objective: str = "",
        full_transcript: Optional[list[dict]] = None,
    ) -> TutoringContext:
        """Build a frozen TutoringContext snapshot for a service call.

        Pulls per-session inputs (student, lesson, institution, persona,
        locale, profile_summary) and combines them with the loaded
        runtime state. ``full_transcript`` defaults to the session's
        complete turn history — no windowing per §7 item 10.
        """
        session = self.session
        student = session.student
        lesson = session.lesson
        institution = session.institution

        profile = getattr(student, "student_profile", None)
        profile_summary = ""
        grade_level = ""
        tutor_persona = ""
        if profile is not None:
            profile_summary = profile.profile_summary or ""
            grade_level = profile.grade_level or ""
            personality = profile.tutor_personality
            if personality is not None:
                tutor_persona = getattr(personality, "name", "") or ""

        institution_name = getattr(institution, "name", "") or ""
        locale = getattr(lesson, "language", None) or getattr(
            getattr(lesson, "course", None), "language", None
        ) or "en"

        if full_transcript is None:
            full_transcript = self._load_full_transcript()

        return TutoringContext(
            session_id=session.id,
            student_id=student.id,
            institution_id=institution.id if institution else 0,
            lesson_id=lesson.id if lesson else 0,
            locale=locale,
            grade_level=grade_level,
            institution_name=institution_name,
            tutor_persona=tutor_persona,
            client_kind=client_kind if client_kind in ("web", "mobile") else "web",
            full_transcript=full_transcript,
            runtime_state=self.load_runtime_state(),
            profile_summary=profile_summary,
            current_objective=current_objective,
        )

    def _load_full_transcript(self) -> list[dict]:
        """Load every prior turn for this session ordered oldest-first.

        Returns a list of ``{role, content, created_at}`` dicts.
        Includes student + tutor turns; excludes the system roles
        (legacy engine occasionally writes those — v2 does not).
        """
        from apps.tutoring.models import SessionTurn

        rows = (
            SessionTurn.objects
            .filter(session=self.session)
            .exclude(role=SessionTurn.Role.SYSTEM)
            .order_by("created_at")
            .values("role", "content", "created_at")
        )
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            }
            for r in rows
        ]
