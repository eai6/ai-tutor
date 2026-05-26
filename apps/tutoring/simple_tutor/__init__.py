"""Simple tutor engine — alternative runtime for the AI Tutor.

A prompt-engineered single-LLM-call tutor with 5 tools and deterministic
grading. Replaces the ~12k-line stateful ``apps/tutoring/conversational_tutor.py``
for sessions with ``TutorSession.engine == 'simple'``.

Modules in this package:
    grader     — Tier-1 (MCQ + math), Tier-1.5 (embedding gate),
                 Tier-2 (cross-family verifier LLM). Deterministic first;
                 verifier LLM only for the middle confidence band.
    state      — Sliding window + step-anchored summaries (M6)
    prompts    — Stateless system-prompt template + 5 tool schemas (M7)
    tools      — Server-side handlers for pose_question / record_answer /
                 advance_step / request_figure / redirect_off_topic (M8)
    engine     — The respond(session, user_input) -> dict entry point (M9)

References:
    memory/simple_tutor_engine_plan.md
    memory/simple_tutor_engine_milestones.md
    memory/grading_system_research.md
    memory/tutor_engine_research.md
"""
