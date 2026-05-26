"""Fast-LLM conformance classifier — Phase 2 §2.4.

Returns the **nine binary labels** from analysis §3:

  - affirms_correctness
  - refutes_correctness
  - surfaces_uncertainty
  - contains_assessment_question_in_prose
  - hands_floor_back_or_transitions
  - contains_partial_feedback_shape
  - contains_factual_claim
  - contains_arithmetic_claim
  - student_claim_present  (read from PRIOR student turn, not response)

Each label is a narrow binary decision. Provider/model is selected at
call time via ``ModelConfig.get_for('conformance_classifier')`` — see
Phase 1 §7. Temperature is forced to 0 by ``effective_temperature``.

Phase 2 §2.4 / Task #8 ships the full implementation (instructor-style
constrained-decoding call with strict-JSON output). Phase 2 §2.4 / Task
#2 lands the typed shape + the stub so the rest of the conformance
machinery (gates + verdict matrix + retry loop) can be wired up
without blocking on the LLM call.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClassifierLabels(BaseModel):
    """Nine binary labels emitted by the conformance classifier."""

    model_config = {"frozen": True}

    affirms_correctness: bool = False
    refutes_correctness: bool = False
    surfaces_uncertainty: bool = False
    contains_assessment_question_in_prose: bool = False
    hands_floor_back_or_transitions: bool = False
    contains_partial_feedback_shape: bool = False
    contains_factual_claim: bool = False
    contains_arithmetic_claim: bool = False
    student_claim_present: bool = Field(
        default=False,
        description="Read from the PRIOR student turn, not the candidate response.",
    )


def run_conformance_classifier(
    *,
    candidate_response: str,
    prior_student_turn: str = "",
    llm_client=None,
) -> ClassifierLabels:
    """Return nine binary labels for the candidate.

    Phase 2 Task #8 — full implementation lands when the rest of the
    conformance loop is wired. Stub raises ``NotImplementedError`` so
    callers get a loud failure if the conformance check fires without
    its classifier in place.
    """
    raise NotImplementedError(
        "run_conformance_classifier — Phase 2 Task #8"
    )
