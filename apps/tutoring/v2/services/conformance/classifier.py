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

Each label is a narrow binary decision. Provider/model is resolved
via ``ModelConfig.get_for('conformance_classifier')`` — see Phase 1
§7. Temperature is forced to 0 by ``effective_temperature``.

The classifier prompt explicitly avoids:
  - Open-ended negatives ("don't guess") — gemini-prompting-expert
    warns these over-index on Gemini 3.
  - Persona priming or flowery language — same source.
  - JSON-via-prose-only — the prompt asks for strict JSON and the
    parser tolerates fenced output for model-family portability.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from apps.tutoring.tracing import emit_span

logger = logging.getLogger(__name__)


CLASSIFIER_SYSTEM = """\
You classify a tutor's candidate response on nine narrow binary
dimensions. Use ONLY what the response and the prior student turn
say. Each label is True or False.

Definitions:

  affirms_correctness  — the response asserts the student's most
                         recent answer or claim is correct.
  refutes_correctness  — the response asserts the student's most
                         recent answer or claim is wrong.
  surfaces_uncertainty — the response says "I'm not sure" or "let's
                         check" or otherwise visibly declines to
                         adjudicate.
  contains_assessment_question_in_prose
                       — the response asks a question whose answer is
                         a single verifiable value (number, choice,
                         exact phrase) in PROSE rather than via a
                         tool call. Reflective / hint / "why do you
                         think" prompts do NOT count.
  hands_floor_back_or_transitions
                       — the response ends with a directive to the
                         student, a posed tool-question, an explicit
                         topic close, an exit-ticket transition, or
                         a UI transition signal. Administrative /
                         system messages and end-of-session summaries
                         count as True (they are legitimate closure).
  contains_partial_feedback_shape
                       — the response credits something specific the
                         student got right AND names something
                         specific that's missing. Both parts must be
                         present.
  contains_factual_claim
                       — the response asserts a non-arithmetic factual
                         claim (definition, causal explanation,
                         historical fact).
  contains_arithmetic_claim
                       — the response asserts an arithmetic result
                         ("12 + 13 = 25", "they sum to 180°").
  student_claim_present
                       — the student's PRIOR turn contains an
                         assertion of fact (not a question, not pure
                         agreement). Read this label from the prior
                         student turn provided, not the candidate
                         response.

Return strict JSON with exactly these nine keys, each True or False.
"""


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


def _render_user_prompt(*, candidate_response: str, prior_student_turn: str) -> str:
    """Render the user-turn prompt for the nine-label classifier.

    Long context is short here (one tutor response + one student
    turn) so query-at-end matters less, but we still put the input
    blocks first and the labelling ask last per fundamentals.
    """
    return (
        f"Candidate tutor response:\n"
        f"{candidate_response.strip()}\n\n"
        f"Prior student turn:\n"
        f"{(prior_student_turn or '(none)').strip()}\n\n"
        f"---\n"
        f"Return strict JSON with the nine keys defined in the system "
        f"prompt. No prose, no markdown fences."
    )


def run_conformance_classifier(
    *,
    candidate_response: str,
    prior_student_turn: str = "",
    llm_client=None,
) -> ClassifierLabels:
    """Return nine binary labels for the candidate.

    Fail-soft: on LLM unavailability or parse failure, returns a
    *conservative* default — every label False — which the verdict
    matrix will then surface as multiple violations under
    correct/wrong/partial/unverified rule sets, triggering a retry +
    safe template. This keeps a missing classifier from silently
    shipping unchecked content (we choose to fall back to "the rules
    look unsatisfied" rather than to "all clear").
    """
    with emit_span("audit", "conformance.classifier") as span:
        if llm_client is None:
            from apps.tutoring.v2.services.student_grader import (
                _build_client_for_purpose,
            )
            llm_client = _build_client_for_purpose("conformance_classifier")
        if llm_client is None:
            logger.warning(
                "[ConformanceClassifier] no CONFORMANCE_CLASSIFIER client "
                "available — returning conservative default labels"
            )
            if span is not None:
                span["payload"] = {"outcome": "skipped", "reason": "no_client"}
            return ClassifierLabels()

        try:
            response = llm_client.generate(
                messages=[
                    {
                        "role": "user",
                        "content": _render_user_prompt(
                            candidate_response=candidate_response,
                            prior_student_turn=prior_student_turn,
                        ),
                    },
                ],
                system_prompt=CLASSIFIER_SYSTEM,
                max_tokens=400,
            )
            payload = _safe_json_loads(response.content or "") or {}
        except Exception as exc:
            logger.warning(
                "[ConformanceClassifier] LLM call raised %s — conservative default",
                type(exc).__name__,
            )
            if span is not None:
                span["payload"] = {
                    "outcome": "fail_soft",
                    "reason": type(exc).__name__,
                }
            return ClassifierLabels()

        if not isinstance(payload, dict):
            if span is not None:
                span["payload"] = {"outcome": "fail_soft", "reason": "non_dict_payload"}
            return ClassifierLabels()
        # Coerce missing / non-bool values to False conservatively.
        fields: dict[str, Any] = {}
        for key in ClassifierLabels.model_fields:
            raw = payload.get(key)
            fields[key] = bool(raw) if raw is not None else False
        try:
            labels = ClassifierLabels(**fields)
        except Exception:
            if span is not None:
                span["payload"] = {"outcome": "fail_soft", "reason": "coerce_failed"}
            return ClassifierLabels()
        if span is not None:
            span["payload"] = {
                "outcome": "ok",
                "labels": labels.model_dump(),
            }
        return labels


def _safe_json_loads(text: str) -> Optional[Any]:
    """Strip fences + tolerate prose-wrapped JSON. Same shape as the
    grader helper but reproduced here to keep the conformance package
    import-independent."""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        if raw.endswith("```"):
            raw = raw[: -3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
