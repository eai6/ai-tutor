"""Synthetic student simulator.

LLM-driven student personas that drive `ConversationalTutor.respond()`
end-to-end, exercising the same code path real students hit. Resulting
SessionTurns flow into the benchmark sampler tagged as synthetic.

See memory/llm_student_simulator_plan.md for the architecture.

Public API:
- ``StudentClient`` — wraps a `BaseLLMClient` with a persona system prompt.
- ``PERSONAS`` — registry of available personas.
- ``simulate_session`` — *(Phase 2, not yet implemented)* drives one
  end-to-end TutorSession.
"""

from apps.tutoring.student_sim.client import StudentClient
from apps.tutoring.student_sim.driver import (
    SimResult, TranscriptTurn, simulate_session,
)
from apps.tutoring.student_sim.personas import PERSONAS, get_persona

__all__ = [
    'StudentClient',
    'SimResult', 'TranscriptTurn', 'simulate_session',
    'PERSONAS', 'get_persona',
]
