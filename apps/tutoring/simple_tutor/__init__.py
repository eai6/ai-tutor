"""Simple tutor engine — alternative runtime for the AI Tutor.

A prompt-engineered single-LLM-call tutor with 4 tools and deterministic
grading. Selected via the ``SIMPLE_TUTOR_ENGINE`` env var (NOT a per-
student toggle — the student never picks).

Modules in this package:
    grader     — Tier-1 (MCQ + math), Tier-1.5 (embedding gate),
                 Tier-2 (cross-family verifier LLM). Deterministic first;
                 verifier LLM only for the middle confidence band.
    state      — Sliding window + step-anchored summaries (M6)
    prompts    — Stateless system-prompt template + 4 tool schemas (M7)
    tools      — Server-side handlers for record_answer / advance_step /
                 request_figure / redirect_off_topic + flow primitives (M8)
    engine     — The respond(session, user_input) -> dict entry point (M9)

References:
    memory/simple_tutor_engine_plan.md
    memory/simple_tutor_engine_milestones.md
    memory/grading_system_research.md
    memory/tutor_engine_research.md
"""
import os


# simple_tutor is now the DEFAULT engine (2026-06-09). The legacy
# ConversationalTutor runs ONLY if SIMPLE_TUTOR_ENGINE is explicitly set to a
# falsy value below. Unset / empty / anything else → simple_tutor. This makes
# the default survive environment rebuilds (the flag no longer has to be set
# out-of-band for simple_tutor to run).
_FALSY = {'off', 'false', '0', 'no', 'disabled', 'legacy', 'v1', 'old'}


def is_enabled() -> bool:
    """Return True when the simple-tutor engine should handle new turns.

    simple_tutor is the default; this returns True unless SIMPLE_TUTOR_ENGINE is
    explicitly set to a falsy value (off/false/0/no/legacy/v1). Read fresh from
    ``os.environ`` on every call so the engine can be flipped without a process
    restart. Per-turn cost is one env lookup — negligible.
    """
    val = (os.environ.get('SIMPLE_TUTOR_ENGINE') or '').strip().lower()
    return val not in _FALSY
