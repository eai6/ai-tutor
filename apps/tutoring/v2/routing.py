"""Routing helpers for the v2 engine — used by ``apps.tutoring.views``.

Per Phase 1 §6:
  - At session creation, ``ensure_engine_version_set(session)`` picks
    'legacy' or 'v2' based on ``NEW_TUTOR``, persists it, and (for v2)
    initializes ``runtime_state`` with an empty typed SessionRuntimeState.
  - At resume/respond, ``is_v2_session(session)`` is the sticky flag.

Phase 1 only adds the read + dispatch; Phase 2 makes ``v2`` do
something useful. Until then, v2 sessions return a placeholder
response from the dispatch helpers below — they do NOT raise
NotImplementedError into the request flow.
"""

from __future__ import annotations

from typing import Optional

from apps.tutoring.v2.config.flags import (
    ENGINE_LEGACY,
    ENGINE_V2,
    select_engine_version,
)
from apps.tutoring.v2.contracts import SessionRuntimeState
from apps.tutoring.v2.services.context_manager import ContextManager


# Placeholder copy returned for v2-routed turns during Phase 1. Replaced
# in Phase 2 when TutorEngine.respond() / start_session() land.
V2_PHASE1_PLACEHOLDER = (
    "The new conversational engine is enabled for this session but "
    "isn't fully wired up yet. (Phase 1 of the refactor lands the "
    "schema + tooling; Phase 2 lands the conversation behavior.)"
)


def ensure_engine_version_set(session) -> str:
    """Pick + persist ``engine_version`` for a freshly-created session.

    No-op if already set (sticky-per-session). For v2 sessions,
    initialize ``runtime_state`` with an empty typed snapshot.
    """
    current = (session.engine_version or "").strip().lower()
    chosen = select_engine_version(current or None)
    fields_to_save: list[str] = []
    if current != chosen:
        session.engine_version = chosen
        fields_to_save.append("engine_version")
    if chosen == ENGINE_V2 and not session.runtime_state:
        session.runtime_state = SessionRuntimeState().to_jsonable()
        fields_to_save.append("runtime_state")
    if fields_to_save:
        session.save(update_fields=fields_to_save)
    return chosen


def is_v2_session(session) -> bool:
    return (session.engine_version or "").strip().lower() == ENGINE_V2


def v2_placeholder_response(session, *, kind: str = "respond") -> dict:
    """JSON-able placeholder response for v2-routed sessions in Phase 1.

    Initializes the runtime_state if it hasn't been written yet (so
    the Phase 1 exit criterion — "NEW_TUTOR=on boots a new session
    and writes a valid SessionRuntimeState snapshot to runtime_state"
    — holds even for sessions created before the dispatch landed).
    """
    cm = ContextManager(session)
    state = cm.load_runtime_state()
    cm.save_runtime_state(state)
    return {
        "session_id": session.id,
        "message": V2_PHASE1_PLACEHOLDER,
        "phase": "engage",
        "media": [],
        "show_exit_ticket": False,
        "exit_ticket": None,
        "is_complete": False,
        "step_number": 0,
        "total_steps": 0,
        "engine_version": ENGINE_V2,
        "v2_placeholder": True,
    }
