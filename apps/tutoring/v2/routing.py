"""Routing helpers for the v2 engine — used by ``apps.tutoring.views``.

Per Phase 1 §6:
  - At session creation, ``ensure_engine_version_set(session)`` picks
    'legacy' or 'v2' based on ``NEW_TUTOR``, persists it, and (for v2)
    initializes ``runtime_state`` with an empty typed SessionRuntimeState.
  - At resume/respond, ``is_v2_session(session)`` is the sticky flag.

Phase 2 (§2.7 + §3.3 wiring) extends this module with the dispatch
helpers that the views actually call:

  - ``v2_start_dispatch(session)`` — produces the opening tutor turn.
  - ``v2_respond_dispatch(session, message)`` — runs a full
    StudentGrader → StudentTutor → ConformanceCheck loop and persists
    one student + one tutor SessionTurn.
  - ``v2_resume_dispatch(session)`` — surfaces a continuation message
    on session resume.
  - ``v2_review_dispatch(session)`` — surfaces a review-mode opener.

All four preserve the legacy view's response JSON shape so the
frontend chat / artifact-panel / exit-ticket-modal contracts stay
unchanged (per the plan's "preserved runtime surfaces" section).
"""

from __future__ import annotations

import logging
from typing import Optional

from apps.tutoring.tracing import flush_spans, reset_span_buffer, start_span_buffer
from apps.tutoring.v2.config.flags import (
    ENGINE_LEGACY,
    ENGINE_V2,
    select_engine_version,
)
from apps.tutoring.v2.contracts import SessionRuntimeState
from apps.tutoring.v2.services.context_manager import ContextManager

logger = logging.getLogger(__name__)


# Placeholder copy retained only for tests that exercise the Phase 1
# routing seam directly. Phase 2 dispatch functions never return this.
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
    """Phase 1 placeholder — retained for backward-compat with the
    Phase 1 test suite. Phase 2 dispatch helpers below are what the
    views actually call now."""
    cm = ContextManager(session)
    state = cm.load_runtime_state()
    cm.save_runtime_state(state)
    return _envelope(
        session=session,
        message=V2_PHASE1_PLACEHOLDER,
        phase="engage",
        extra={"v2_placeholder": True},
    )


# ──────────────────────────────────────────────────────────────────────
# Phase 2 dispatch helpers — invoked from apps/tutoring/views.py
# ──────────────────────────────────────────────────────────────────────


def v2_start_dispatch(session) -> dict:
    """Produce the opening tutor turn for a freshly-created v2 session."""
    from apps.tutoring.models import SessionTurn
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    cm = ContextManager(session)
    engine = TutorEngine(cm)
    token = start_span_buffer()
    result = None
    try:
        try:
            context = cm.assemble_context()
            result = engine.start_session(context)
        except Exception as exc:
            logger.warning(
                "[v2_start_dispatch] start_session raised %s",
                type(exc).__name__,
            )
            return _envelope(
                session=session,
                message="Welcome — let's get started together.",
                phase="engage",
                extra={"v2_error": type(exc).__name__},
            )

        # Persist the opening tutor turn so spans have a parent row.
        turn = SessionTurn.objects.create(
            session=session,
            role=SessionTurn.Role.TUTOR,
            content=result.response_text,
            metadata={
                "engine_version": ENGINE_V2,
                "v2_trace": result.v2_trace,
                "fallback_used": result.fallback_used,
                "selected_move": result.selected_move,
            },
            judge_outputs={"v2_trace": result.v2_trace},
        )
        flush_spans(turn.id)
        return _envelope(
            session=session,
            message=result.response_text,
            phase="engage",
            extra={"selected_move": result.selected_move},
        )
    finally:
        reset_span_buffer(token)


def v2_respond_dispatch(session, message: str) -> dict:
    """Run one full conversational turn — student input → tutor reply.

    Persists ONE student SessionTurn (for the input) + ONE tutor
    SessionTurn (for the reply), with spans flushed against the tutor
    turn. Returns the legacy-shape JSON envelope so the frontend
    contract stays unchanged.
    """
    from apps.tutoring.models import SessionTurn
    from apps.tutoring.v2.services.tutor_engine import TutorEngine

    # 1. Persist the student turn first — spans attach to the tutor
    #    turn, but the student turn must exist in the transcript when
    #    ContextManager assembles the next context.
    student_turn = SessionTurn.objects.create(
        session=session,
        role=SessionTurn.Role.STUDENT,
        content=message,
        metadata={"engine_version": ENGINE_V2},
    )

    cm = ContextManager(session)
    engine = TutorEngine(cm)
    token = start_span_buffer()
    try:
        context = cm.assemble_context()
        result = engine.respond(context, message)
    except Exception as exc:
        logger.warning(
            "[v2_respond_dispatch] TutorEngine.respond raised %s",
            type(exc).__name__,
        )
        reset_span_buffer(token)
        return _envelope(
            session=session,
            message="Something went wrong on my end. Let's try that again.",
            phase="engage",
            extra={"v2_error": type(exc).__name__},
        )

    # 2. Persist the tutor turn + flush spans against it.
    tutor_turn = SessionTurn.objects.create(
        session=session,
        role=SessionTurn.Role.TUTOR,
        content=result.response_text,
        metadata={
            "engine_version": ENGINE_V2,
            "v2_trace": result.v2_trace,
            "fallback_used": result.fallback_used,
            "selected_move": result.selected_move,
        },
        judge_outputs={"v2_trace": result.v2_trace},
    )
    try:
        flush_spans(tutor_turn.id)
    finally:
        reset_span_buffer(token)

    # 3. Lesson completion — fires StudentProfiler (Phase 3 §3.1) and
    # the exit-ticket modal payload. ONLY fires when ``close_topic``
    # was the move AND the engine could not advance to a next step
    # (i.e. the active step was the final one). An intermediate
    # ``close_topic`` (more steps remain) advances the step but keeps
    # the session active — the student keeps tutoring on the new step
    # rather than being shoved to the exit ticket after the first
    # objective hit. Fail-soft: completion failures must not block
    # the response envelope from reaching the student.
    is_complete = False
    exit_ticket_payload: Optional[dict] = None
    if result.selected_move == "close_topic" and result.is_lesson_complete:
        try:
            engine.complete_session()
            is_complete = True
        except Exception as exc:
            logger.warning(
                "[v2_respond_dispatch] complete_session raised %s",
                type(exc).__name__,
            )
        # Surface the exit-ticket payload in the SAME envelope the
        # frontend reads. The chat modal listens for show_exit_ticket
        # + exit_ticket on chat_respond replies (see
        # templates/tutoring/_partials/exit_modal.html); without this
        # the v2 close_topic transition silently never triggers the
        # modal end-to-end. Fail-soft: a missing/empty bank or any
        # exception just leaves the payload null so the student still
        # sees the close message.
        try:
            exit_ticket_payload = _build_exit_ticket_payload(session)
        except Exception as exc:
            logger.warning(
                "[v2_respond_dispatch] exit-ticket payload build raised %s",
                type(exc).__name__,
            )

    extra: dict = {
        "selected_move": result.selected_move,
        "verdict": (result.verdict.verdict.value if result.verdict else None),
        "fallback_used": result.fallback_used,
        "is_complete": is_complete,
    }
    if exit_ticket_payload is not None:
        extra["show_exit_ticket"] = True
        extra["exit_ticket"] = exit_ticket_payload

    return _envelope(
        session=session,
        message=result.response_text,
        phase=("completed" if is_complete else "engage"),
        extra=extra,
    )


def _build_exit_ticket_payload(session) -> Optional[dict]:
    """Sample + serialize this lesson's exit ticket for the modal.

    Reuses the legacy bank-selection + modal-serializer helpers so the
    frontend wire format is identical to the pretest / legacy
    completion path. Returns ``None`` when the lesson has no
    ExitTicket bank — the frontend then skips the modal and the
    student sees the close message only.
    """
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    from apps.tutoring.views import _serialize_pretest_questions_for_modal

    lesson = session.lesson
    if lesson is None:
        return None
    exit_ticket = ExitTicket.objects.filter(lesson=lesson).first()
    if exit_ticket is None:
        return None
    bank = list(
        ExitTicketQuestion.objects.filter(exit_ticket=exit_ticket)
        .exclude(question_type="data_interpretation")
        .order_by("order_index")
    )
    if not bank:
        return None
    POST_TEST_SIZE = 10
    if len(bank) > POST_TEST_SIZE:
        import random as _random
        sampled = _random.sample(bank, POST_TEST_SIZE)
    else:
        sampled = bank
    return _serialize_pretest_questions_for_modal(sampled)


def v2_resume_dispatch(session) -> dict:
    """Re-render the last open question (if any) on session resume.

    Per the Phase 3 §3.4 resume-artifact-preservation test: when an
    open question exists, surface it verbatim on resume — identical
    visible text, attached media IDs, and (for MCQ) the same option
    order. No new pre-pose check, no new question selection, no
    canonical leak in the resume opener. When no open question is
    set, emit a neutral re-entry message.
    """
    cm = ContextManager(session)
    state = cm.load_runtime_state()
    open_q = state.open_question
    extra: dict = {"resume": True}
    if open_q is not None:
        snapshot = open_q.visible_context_at_pose
        msg = (
            f"Welcome back — let's pick up where we left off. "
            f"The open question: {open_q.rendered_stem}"
        )
        # MCQ option order is preserved verbatim across resume.
        if snapshot.mcq_option_order:
            extra["mcq_options"] = list(snapshot.mcq_option_order)
        if snapshot.attached_media_ids:
            extra["attached_media_ids"] = list(snapshot.attached_media_ids)
        extra["open_question"] = {
            "source": open_q.source.value,
            "id": open_q.id,
            "rendered_stem": open_q.rendered_stem,
        }
    else:
        msg = "Welcome back — let's keep going from where we paused."
    return _envelope(
        session=session,
        message=msg,
        phase="engage",
        extra=extra,
    )


def v2_review_dispatch(session) -> dict:
    """Surface a review-mode opener for a completed v2 session.

    MVP scope: a minimal opener; the rich review path lifts later
    when the dashboard surfaces are wired (Phase 3).
    """
    return _envelope(
        session=session,
        message=(
            "Welcome back to review mode — you can re-walk this "
            "lesson or jump to a specific objective."
        ),
        phase="completed",
        extra={"review_mode": True},
    )


def _envelope(
    *,
    session,
    message: str,
    phase: str,
    extra: Optional[dict] = None,
) -> dict:
    """Build the legacy-shape response JSON for the frontend."""
    payload = {
        "session_id": session.id,
        "message": message,
        "phase": phase,
        "media": [],
        "show_exit_ticket": False,
        "exit_ticket": None,
        "is_complete": False,
        "step_number": 0,
        "total_steps": 0,
        "engine_version": ENGINE_V2,
    }
    if extra:
        payload.update(extra)
    return payload
