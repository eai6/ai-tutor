"""StudentProfiler — end-of-session summarization (Phase 3 §3.1).

Two persisted outputs land on ``StudentProfile`` at session end:

  - ``profile_summary`` (TEXT) — free-text qualitative recall of the
    student's strengths, struggles, misconceptions named, and examples
    shown. Produced by the LLM call (``PROFILER_SUMMARY`` purpose).
  - ``asked_questions`` (JSONB) — structured map keyed by
    ``"{source}:{id}"`` per §4.1; values ``{last_asked_at: iso8601}``.
    Drives ``cross_session_repeat_guard()`` at the tool boundary.
    Capped at the last 500 entries with LRU eviction.

Two write cadences, one trigger. Both fire end-of-session-only for
MVP (§7 item 2). The asked_questions write is *deterministic* —
extracted directly from ``SessionRuntimeState.posed_question_ledger``
without any LLM call. The profile_summary write uses the
``PROFILER_SUMMARY`` ModelConfig purpose (temperature pinned to 0 —
the profile is a memory artifact read by future sessions, not a
creative output).

The session **read window** for the profiler is the last 10
``TutorSession`` rows ordered by ``ended_at DESC`` — an explicit
``LIMIT 10`` at the read boundary. Physical archival of older
sessions is deferred post-MVP (Phase 3 §3.1 amendment to R2).

Trigger ownership: ``TutorEngine.complete_session()`` calls
``StudentProfiler.run_for_session(session)``. Failures are logged
and swallowed — a profile-write failure must never block session
completion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    PosedQuestionLedgerEntry,
    ProfileUpdate,
    SessionRuntimeState,
    TutoringContext,
)

logger = logging.getLogger(__name__)


# Cap on the structured asked_questions map. LRU eviction at the boundary
# keeps the read cost bounded and the cross-session guard fast.
MAX_ASKED_QUESTIONS_ENTRIES = 500

# Read window for the profiler — only the most recent N sessions are
# exposed to the runtime. Physical archival is a deferred follow-up.
SESSION_READ_WINDOW = 10


class StudentProfiler:
    """End-of-session writer for ``profile_summary`` + ``asked_questions``."""

    # ------------------------------------------------------------------
    # Read boundary — last-N session window (§3.1 R2 amendment)
    # ------------------------------------------------------------------

    @staticmethod
    def recent_sessions_queryset(student, *, limit: int = SESSION_READ_WINDOW):
        """Return the most-recent ``limit`` TutorSession rows for a student.

        Ordered by ``ended_at DESC`` so completed sessions surface first.
        Unended (in-flight) sessions sort last per Django's NULLS LAST
        default. The runtime never reads beyond this window — physical
        archival of older rows is a deferred follow-up.
        """
        from apps.tutoring.models import TutorSession

        return (
            TutorSession.objects
            .filter(student=student)
            .order_by("-ended_at", "-started_at")[:limit]
        )

    # ------------------------------------------------------------------
    # End-of-session entry point
    # ------------------------------------------------------------------

    def run_for_session(self, session) -> Optional[ProfileUpdate]:
        """Run summarization + persistence for a completed session.

        Fail-soft: any exception during summary generation or
        persistence is logged and swallowed. A profiler failure must
        not block session completion.
        """
        from apps.accounts.models import StudentProfile
        from apps.tutoring.v2.services.context_manager import ContextManager

        with emit_span("audit", "profiler.run_for_session") as span:
            try:
                cm = ContextManager(session)
                context = cm.assemble_context()
            except Exception as exc:
                logger.warning(
                    "[StudentProfiler] could not assemble context for "
                    "session=%s: %s",
                    getattr(session, "id", None), exc,
                )
                return None

            try:
                update = self.summarize_session(context)
            except Exception as exc:
                logger.warning(
                    "[StudentProfiler] summarize_session raised %s — "
                    "falling back to deterministic-only update",
                    type(exc).__name__,
                )
                update = ProfileUpdate(
                    profile_summary_text="",
                    asked_questions_delta=self._asked_delta_from_runtime(
                        context.runtime_state,
                    ),
                )

            try:
                self.persist(session.student_id, update)
            except Exception as exc:
                logger.warning(
                    "[StudentProfiler] persist raised %s for student=%s",
                    type(exc).__name__, session.student_id,
                )
                return None

            if span is not None:
                span["payload"] = {
                    "summary_chars": len(update.profile_summary_text or ""),
                    "asked_delta_entries": len(update.asked_questions_delta),
                }
            return update

    # ------------------------------------------------------------------
    # Summary generation (PROFILER_SUMMARY purpose)
    # ------------------------------------------------------------------

    def summarize_session(self, context: TutoringContext) -> ProfileUpdate:
        """Produce a ``ProfileUpdate`` for the session.

        - ``profile_summary_text`` comes from a single LLM call
          (``PROFILER_SUMMARY`` purpose, temperature 0). The model is
          asked to write 3-6 sentences naming strengths, struggles,
          misconceptions, and examples shown.
        - ``asked_questions_delta`` is built deterministically from
          ``SessionRuntimeState.posed_question_ledger`` (committed
          entries only — rejected candidates never reach the ledger,
          per the two-phase commit semantics).
        """
        runtime_state = context.runtime_state
        asked_delta = self._asked_delta_from_runtime(runtime_state)

        summary_text = ""
        client = _build_client_for_purpose("profiler_summary")
        if client is not None:
            try:
                system_prompt = _SUMMARY_SYSTEM_PROMPT
                user_prompt = _render_summary_user_prompt(
                    context=context,
                    runtime_state=runtime_state,
                )
                with emit_span(
                    "llm_call", "profiler.summary_generate",
                    model=getattr(client.config, "model_name", ""),
                    purpose="profiler_summary",
                ) as span:
                    response = client.generate(
                        [{"role": "user", "content": user_prompt}],
                        system_prompt,
                        max_tokens=600,
                    )
                    if span is not None:
                        span["tokens_in"] = getattr(response, "tokens_in", None)
                        span["tokens_out"] = getattr(response, "tokens_out", None)
                summary_text = (response.content or "").strip()
            except Exception as exc:
                logger.warning(
                    "[StudentProfiler] summary LLM call raised %s — "
                    "leaving summary_text empty",
                    type(exc).__name__,
                )
                summary_text = ""

        return ProfileUpdate(
            profile_summary_text=summary_text,
            asked_questions_delta=asked_delta,
        )

    # ------------------------------------------------------------------
    # Persistence (write both columns; LRU-evict asked_questions)
    # ------------------------------------------------------------------

    def persist(self, student_id: int, update: ProfileUpdate) -> None:
        """Write ``profile_summary`` and merge ``asked_questions``.

        ``asked_questions`` merge is LRU-bounded at
        ``MAX_ASKED_QUESTIONS_ENTRIES``: oldest ``last_asked_at``
        entries are evicted first. ``profile_summary`` is overwritten
        only when the new text is non-empty (so a failed LLM call
        leaves the prior snapshot intact).
        """
        from apps.accounts.models import StudentProfile

        profile = StudentProfile.objects.filter(user_id=student_id).first()
        if profile is None:
            # Auto-create — mirrors the dashboard's get_or_create pattern
            # so a session for a brand-new student doesn't silently drop
            # the profile write.
            profile = StudentProfile.objects.create(user_id=student_id)

        fields: list[str] = []

        new_summary = (update.profile_summary_text or "").strip()
        if new_summary:
            profile.profile_summary = new_summary
            fields.append("profile_summary")

        merged = self._merge_asked_questions(
            existing=profile.asked_questions or {},
            delta=update.asked_questions_delta or {},
        )
        if merged != (profile.asked_questions or {}):
            profile.asked_questions = merged
            fields.append("asked_questions")

        if fields:
            profile.save(update_fields=fields)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _asked_delta_from_runtime(state: SessionRuntimeState) -> dict[str, dict]:
        """Build the asked_questions delta from the in-session ledger.

        Each committed ledger entry maps to a single map entry keyed by
        the composite ``"{source}:{id}"`` per §4.1. The timestamp is
        the most-recent ``posed_at`` for that key in this session.
        """
        out: dict[str, dict] = {}
        for entry in state.posed_question_ledger or []:
            key = f"{entry.source.value}:{entry.id}"
            ts = entry.posed_at
            iso = ts.isoformat() if isinstance(ts, datetime) else str(ts or "")
            prior = out.get(key)
            if prior is None or iso > (prior.get("last_asked_at") or ""):
                out[key] = {"last_asked_at": iso}
        return out

    @staticmethod
    def _merge_asked_questions(
        existing: dict,
        delta: dict,
        *,
        cap: int = MAX_ASKED_QUESTIONS_ENTRIES,
    ) -> dict:
        """Merge delta into existing, then LRU-evict to the cap.

        LRU here is "least-recently *asked*" — eviction prefers the
        oldest ``last_asked_at`` values. Stored as a JSON object, so
        ordering is rebuilt from the timestamps every merge.
        """
        merged: dict[str, dict] = {}
        for k, v in (existing or {}).items():
            if isinstance(v, dict):
                merged[k] = dict(v)
        for k, v in (delta or {}).items():
            if not isinstance(v, dict):
                continue
            prior = merged.get(k)
            if prior is None:
                merged[k] = dict(v)
                continue
            # Keep the more-recent ``last_asked_at``.
            a = prior.get("last_asked_at") or ""
            b = v.get("last_asked_at") or ""
            merged[k] = dict(v) if b > a else prior

        if len(merged) <= cap:
            return merged

        # LRU eviction — sort by last_asked_at DESC, keep the top `cap`.
        sorted_items = sorted(
            merged.items(),
            key=lambda kv: (kv[1].get("last_asked_at") or ""),
            reverse=True,
        )
        return {k: v for k, v in sorted_items[:cap]}


# ----------------------------------------------------------------------
# LLM prompt + client helper
# ----------------------------------------------------------------------


_SUMMARY_SYSTEM_PROMPT = (
    "You write a short, factual end-of-session profile for one student "
    "based on this session's transcript and per-objective evidence. "
    "Output 3-6 sentences of plain prose — no headers, no bullets, no "
    "praise filler. Cover: which subskills were attempted, which were "
    "correct vs wrong, any named misconceptions, and any worked "
    "examples the tutor showed. This text is read by future tutoring "
    "sessions as memory, not by the student — keep it specific and "
    "dry. If evidence is thin, say so briefly rather than padding."
)


def _render_summary_user_prompt(
    *,
    context: TutoringContext,
    runtime_state: SessionRuntimeState,
) -> str:
    """Render the user-side prompt for the profiler summary call."""
    lines: list[str] = []
    lines.append(f"Student grade level: {context.grade_level or 'unknown'}")
    lines.append(f"Lesson id: {context.lesson_id}")
    lines.append(f"Current objective: {context.current_objective or '(none)'}")
    lines.append("")
    lines.append("Per-objective evidence:")
    progress = runtime_state.objective_progress or {}
    if not progress:
        lines.append("  (no per-objective evidence accumulated)")
    else:
        for key, prog in progress.items():
            lines.append(
                f"  - {key}: attempts={prog.attempts}, correct={prog.correct}, "
                f"wrong={prog.wrong}, partial={prog.partial}"
            )
    lines.append("")
    if runtime_state.remediation_state and runtime_state.remediation_state.misconception:
        rs = runtime_state.remediation_state
        lines.append(
            f"Named misconception fired this session: {rs.misconception} "
            f"(resolved={rs.resolved})"
        )
        lines.append("")
    lines.append("Move history (chronological):")
    lines.append("  " + ", ".join(runtime_state.move_history[-30:] or ["(none)"]))
    lines.append("")
    lines.append("Recent transcript (last 12 turns):")
    transcript = (context.full_transcript or [])[-12:]
    if not transcript:
        lines.append("  (empty transcript)")
    for turn in transcript:
        role = (turn.get("role") or "?")[:8]
        content = (turn.get("content") or "").strip().replace("\n", " ")
        if len(content) > 300:
            content = content[:300] + "…"
        lines.append(f"  [{role}] {content}")
    lines.append("")
    lines.append(
        "Write the profile now. Do not address the student. "
        "Plain prose, 3-6 sentences."
    )
    return "\n".join(lines)


def _build_client_for_purpose(purpose: str):
    """Resolve the ``ModelConfig`` for the purpose and return a client.

    Fail-soft — returns ``None`` when the purpose has no active config
    or the client can't be constructed. Caller handles ``None`` by
    leaving the LLM-derived field empty.
    """
    try:
        from apps.llm.client import get_llm_client
        from apps.llm.models import ModelConfig
    except Exception:
        return None
    cfg = ModelConfig.get_for(purpose)
    if cfg is None:
        return None
    try:
        return get_llm_client(cfg)
    except Exception as exc:
        logger.warning(
            "[StudentProfiler] get_llm_client(%s) raised %s",
            purpose, type(exc).__name__,
        )
        return None
