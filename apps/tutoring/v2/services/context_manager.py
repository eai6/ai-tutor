"""ContextManager — single owner of the SessionRuntimeState boundary.

Per Phase 2 §2.7, the ContextManager:
  - Assembles ``TutoringContext`` for each service call (transcript +
    profile snapshot + objective + KB chunks + verdict).
  - Owns load/save of ``TutorSession.runtime_state`` via the typed
    Pydantic model.
  - Implements Phase B commit of ``PendingPose`` objects — the only
    code path that mutates the posed-question ledger or writes
    ``open_question``.

Phase 1 ships the load/save boundary + the commit_pending_pose hook.
Phase 2 wires it into the service-call assembly path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from apps.tutoring.v2.contracts import (
    OpenQuestion,
    PendingPose,
    PosedQuestionLedgerEntry,
    SessionRuntimeState,
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

        now = datetime.utcnow()
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
