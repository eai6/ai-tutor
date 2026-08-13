"""Re-export of the synthetic-student personas used by the eval harness.

The personas themselves live in apps/tutoring/student_sim/personas.py — the
eval harness deliberately reuses them rather than duplicating definitions
(the eval and the simulator should test the same student personas).
"""
from ai_tutor.apps.tutoring.student_sim.personas import PERSONAS, Persona, get_persona

__all__ = ['PERSONAS', 'Persona', 'get_persona']
