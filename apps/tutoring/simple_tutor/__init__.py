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


# Env-driven enable flag. Truthy values: 'on' / 'true' / '1' / 'yes' / 'simple'.
# Default OFF — preserves the legacy v1 path for all sessions until
# explicitly flipped via Container App env var.
_TRUTHY = {'on', 'true', '1', 'yes', 'simple', 'enabled'}


def is_enabled() -> bool:
    """Return True when the simple-tutor engine should handle new turns.

    Read fresh from ``os.environ`` on every call so the flag can be
    flipped without a process restart (e.g. via ``az containerapp
    update --set-env-vars`` between deploys). Per-turn cost is one env
    lookup — negligible.
    """
    val = (os.environ.get('SIMPLE_TUTOR_ENGINE') or '').strip().lower()
    return val in _TRUTHY
