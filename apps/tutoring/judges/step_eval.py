"""
DEPRECATED (Phase 3 §3.5 — refactor implementation plan).

This module is part of the legacy tutoring pipeline. The v2 grader /
tutor / conformance engine in ``apps.tutoring.v2`` replaces it. Kept
loaded for resume of in-flight legacy sessions and as the kill-switch
fallback (``NEW_TUTOR=off``). **Do not add new features here.**

Deletion gate (Phase 3 §3.5):
  1. v2 has served prod traffic ≥ 4 weeks post-cutover.
  2. Zero kill-switch flips during that window.
  3. Three consecutive weekly benchmark runs within ±2 pp of
     cutover numbers on each P1 category.
  4. No open P1 incidents tied to the v2 engine.

Original module docstring follows:

Step-evaluation judge — answers two questions for the engine:

  - answer_correct: did the student's last input correctly answer the
    posed question? (True / False / None tri-state)
  - step_complete: should the engine advance to the next step?

Single-task, focused prompt (~2KB instead of the combined judge's 9KB).
The deterministic verdict is fed in as `deterministic_verdict` and the
LLM anchors on it for arithmetic / MCQ — its job is broader judgment
(equivalent forms, partial credit, working quality), not re-checking
arithmetic the deterministic layer already verified.

When `deterministic_verdict` is conclusive (true/false), the LLM call
is SKIPPED entirely and the deterministic verdict is returned as-is.
The LLM only runs when the deterministic check returned None.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field

from apps.tutoring.judges._instructor_helper import (
    get_instructor_from_client,
    structured_completion,
)
from apps.tutoring.tracing import traced_judge

logger = logging.getLogger(__name__)


@dataclass
class StepEvalResult:
    answer_correct: Optional[bool] = None
    step_complete: bool = False
    reasoning: str = ""
    skipped: bool = False
    skip_reason: str = ""
    source: str = ""  # "deterministic" | "llm" | "skipped"


_SYSTEM = (
    "You are a focused step-evaluation reviewer for a tutoring session. "
    "Decide TWO things, ONLY:\n"
    "  1) answer_correct: did the student's last input correctly answer "
    "the question being posed? (true / false / null)\n"
    "  2) step_complete: based on the completion_criteria, should the "
    "engine advance to the next step? (true / false)\n"
    "\n"
    "ANCHOR ON `step_context.posed_question`. That is the EXACT question "
    "the student was asked when they wrote their reply. The "
    "`tutor_response` you also receive is the tutor's REACTION to that "
    "reply — it may contain a NEW question for the next turn, but you "
    "must NOT grade the student's input against that new question. "
    "If posed_question is empty, fall back to the last question in "
    "step_context.recent_conversation. Never use a question that only "
    "appears in tutor_response.\n"
    "\n"
    "Tri-state for answer_correct:\n"
    "  - true: clear, demonstrably correct answer to posed_question.\n"
    "  - false: clear, demonstrably wrong answer to posed_question.\n"
    "  - null: the student is acknowledging the teaching ('ok', 'got it', "
    "'interesting'), asking their own question, expressing confusion, "
    "or the input isn't an answer attempt at all. ALSO null when the "
    "student gave a sensible-but-non-final response (a method "
    "description, a partial step) and posed_question expects a final "
    "value — don't mark wrong, mark null. NEVER mark a conversational "
    "engagement as wrong.\n"
    "\n"
    "ANCHOR ON THE DETERMINISTIC VERDICT WHEN PROVIDED:\n"
    "  - input.deterministic_verdict is the result of a programmatic "
    "arithmetic / MCQ-letter check that already ran. When non-null, "
    "treat it as ground truth.\n"
    "  - deterministic_verdict=true → return answer_correct=true unless "
    "you can clearly identify wrong working that landed on the right "
    "number by coincidence.\n"
    "  - deterministic_verdict=false → return answer_correct=false UNLESS "
    "the student wrote an equivalent form (5 1/4 vs 21/4 vs 5.25), a "
    "typo with otherwise correct working, or the deterministic check "
    "compared against the wrong expected_answer. Override only when "
    "you can name the specific reason in step_eval_reasoning.\n"
    "  - deterministic_verdict=null → judge from your own reading.\n"
    "\n"
    "MCQ EQUIVALENCE — override deterministic_verdict=false when the "
    "student's free-text answer matches the CORRECT OPTION'S CONTENT.\n"
    "When input.step_context.mcq_options is set (a dict like {A: '8', "
    "B: '40', ...}), the question is multiple choice. The student may "
    "answer with the letter ('C') OR the option's content ('8', "
    "'x = 8', 'the answer is 8'). All three forms are correct.\n"
    "Use input.step_context.correct_option_text as the canonical "
    "value. If the student's input expresses that value (numerically "
    "or as 'variable = value'), return answer_correct=true even when "
    "deterministic_verdict=false. The deterministic check only matches "
    "letters; equivalence is YOUR job. Examples:\n"
    "  - mcq_options={A:'8',B:'40',C:'320',D:'64'}, "
    "correct_option_text='8', student='x = 8' → answer_correct=true\n"
    "  - mcq_options={A:'8',...}, correct_option_text='8', "
    "student='I got 8' → answer_correct=true\n"
    "  - mcq_options as above, correct_option_text='8', "
    "student='320' → answer_correct=false (wrong value)\n"
    "Cite the equivalence in step_eval_reasoning so the override is "
    "auditable.\n"
    "\n"
    "The deterministic layer protects against arithmetic errors. Your "
    "job is broader judgment — equivalent forms, partial credit, "
    "working quality, MCQ-content equivalence — NOT re-doing arithmetic."
)

from apps.tutoring.judges._prompt_meta import prompt_fingerprint
PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_SYSTEM)


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
class _StepEvalVerdict(BaseModel):
    answer_correct: Optional[bool] = Field(
        default=None,
        description=(
            "True = clear correct answer to posed_question; False = "
            "clear wrong answer; null = acknowledgement / question / "
            "non-answer engagement (don't mark wrong)."
        ),
    )
    step_complete: bool = Field(
        default=False,
        description="True iff the engine should advance to the next step.",
    )
    reasoning: str = Field(
        default="",
        description="One-sentence rationale (≤280 chars).",
        max_length=400,
    )


@traced_judge('step_eval')
def run_step_eval_judge(
    *,
    student_input: str,
    tutor_response: str,
    step_context: dict,
    llm_client=None,
) -> StepEvalResult:
    """Single-task step evaluator. Returns answer_correct + step_complete.

    Args:
      student_input: student's last message.
      tutor_response: the tutor's clean response (post media-signal parsing).
      step_context: dict with step_type, completion_criteria,
        expected_answer, recent_conversation, deterministic_verdict, etc.
        (Built by conversational_tutor._build_step_eval_context.)
      llm_client: BaseLLMClient. None disables the LLM fallback —
        deterministic verdict still applies.

    Skip cases (no LLM call):
      - empty step_context (no step in flight)
      - deterministic_verdict is conclusive (true/false) → return as-is
      - llm_client is None
    """
    if not step_context:
        return StepEvalResult(
            skipped=True,
            skip_reason="empty_step_context",
            source="skipped",
        )

    det = step_context.get("deterministic_verdict")
    det_source = step_context.get("deterministic_source", "")

    # Deterministic short-circuit. The deterministic layer is reliable
    # for arithmetic / MCQ — running the LLM on top is just a chance
    # for it to drift. Only call the LLM when the deterministic check
    # genuinely couldn't decide.
    if det is True or det is False:
        # Source label includes the deterministic kind (numeric / mcq) so
        # downstream telemetry + tests can distinguish "verified by math"
        # vs "verified by MCQ-letter match".
        det_kind = (det_source or "").strip().lower()
        # `mcq_letter` is the actual source string emitted by
        # _build_step_eval_context — collapse to `mcq` for the label.
        if det_kind in ("numeric",):
            src = "deterministic_numeric"
        elif det_kind in ("mcq", "mcq_letter"):
            src = "deterministic_mcq"
        else:
            src = "deterministic"
        return StepEvalResult(
            answer_correct=det,
            step_complete=False,  # let exchange-count + LLM step_complete handle this
            reasoning=f"deterministic {det_source}: {'correct' if det else 'incorrect'}",
            source=src,
        )

    if llm_client is None:
        return StepEvalResult(
            skipped=True,
            skip_reason="no_llm_client",
            source="skipped",
        )

    # LLM call — deterministic was inconclusive (free-text, short-answer
    # without a clean numeric form, etc.).
    payload = {
        "student_input": (student_input or "")[:300],
        "tutor_response": (tutor_response or "")[:1500],
        "step_context": step_context,
    }
    user_prompt = (
        "Run step evaluation on the turn below.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    instructor_client = get_instructor_from_client(llm_client)
    if instructor_client is None:
        return StepEvalResult(
            skipped=True,
            skip_reason="instructor_unavailable",
            source="skipped",
        )

    try:
        verdict = structured_completion(
            instructor_client,
            _StepEvalVerdict,
            system_prompt=_SYSTEM,
            user_prompt=user_prompt,
            max_tokens=3500,
            provider=str(getattr(llm_client.config, 'provider', '')),
        )
    except Exception as e:
        logger.warning("[StepEvalJudge] call failed: %s", e)
        return StepEvalResult(
            skipped=True,
            skip_reason=f"llm_error: {type(e).__name__}",
            source="skipped",
        )

    return StepEvalResult(
        answer_correct=verdict.answer_correct,
        step_complete=bool(verdict.step_complete),
        reasoning=(verdict.reasoning or "")[:280],
        source="llm",
    )
