"""Combined post-response judge for math tutor turns.

Replaces three separate LLM calls per turn with one:
  - apps.tutoring.llm_arithmetic_verifier.verify_arithmetic_claims
  - apps.tutoring.fact_verifier.verify_response
  - apps.tutoring.rule_compliance.check_rule_compliance

Same correctness contract — the LLM is asked to fill THREE arrays in
one structured JSON response. Caller drives the regen / force-inject
pipeline off the unified result. The two source modules are kept
intact for tests and for non-math callers that still want the
factual-only check.

Cost: typical math turn drops from 3-4 calls (one generate + 3 judges)
to 2 (one generate + one combined judge). With Opus on tutoring that
is meaningful (~50% post-response cost cut).

Pipeline:
  1. Cheap pre-gates (skip when there is nothing to judge).
  2. Extract numeric/named claim spans (regex — same as fact_verifier).
  3. Retrieve KB evidence for those claims (same as fact_verifier).
  4. ONE LLM call asking for arithmetic / factual / rule findings.
  5. Apply best-effort in-place arithmetic corrections to the response.
  6. Return CombinedJudgeResult — the validator + tutor read it
     directly instead of running their own L4/L5 LLM judges.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from apps.tutoring.fact_verifier import (
    ClaimVerdict,
    _retrieve_evidence,
    extract_claims,
)
from apps.tutoring.llm_arithmetic_verifier import _HAS_NUMBER_RE
from apps.tutoring.rule_compliance import (
    RULE_ARITHMETIC,
    RULE_NO_AUTHORING,
    RULE_RULE_1,
    VALID_RULES,
    RuleViolation,
    _has_relevant_content,
)

logger = logging.getLogger(__name__)


@dataclass
class CombinedJudgeResult:
    # Arithmetic ----------------------------------------------------------
    corrected_response: str = ""
    arithmetic_corrections: List[dict] = field(default_factory=list)
    # Factual --------------------------------------------------------------
    fact_claims: List[ClaimVerdict] = field(default_factory=list)
    # Rule compliance ------------------------------------------------------
    rule_violations: List[RuleViolation] = field(default_factory=list)
    # Step evaluation (merged from former _evaluate_step instructor call) -
    # answer_correct: True/False/None tri-state for the student's reply
    # step_complete: should the engine advance to the next step
    # step_eval_reasoning: short rationale for telemetry / teacher review
    # step_eval_source: "deterministic" | "llm" | "skipped" — lets the
    #   engine label the eval_layer correctly (deterministic_numeric /
    #   deterministic_mcq vs combined_judge LLM verdict).
    answer_correct: Optional[bool] = None
    step_complete: bool = False
    step_eval_reasoning: str = ""
    step_eval_skipped: bool = False
    step_eval_source: str = ""
    # Coherence — self-contradictions inside the same tutor response ------
    coherence_violations: List[str] = field(default_factory=list)
    # Figure-reference — tutor referenced "the diagram" but no figure
    # was actually attached this turn. `figure_ref_in_question` is True
    # when the missing-figure reference is embedded in a question (more
    # critical: the student literally cannot answer).
    figure_ref_issues: List[str] = field(default_factory=list)
    figure_ref_in_question: bool = False
    # Figure-vision — when a figure WAS attached and the response posed
    # a figure-dependent question, the vision judge checks alignment.
    # `figure_aligned` is True / False / None (None = skipped or unsure).
    figure_aligned: Optional[bool] = None
    figure_mismatch_reason: str = ""
    figure_summary: str = ""
    # Safety — flagged child-protection / appropriateness violations
    # in the tutor response. Severity: 'safe' | 'warning' | 'critical'.
    # Categories: subset of {'harmful', 'inappropriate'}. (MANIPULATION
    # is student-only and never appears here.) When non-safe, the
    # validator raises ISSUE_TUTOR_UNSAFE → triggers regen.
    safety_severity: str = "safe"
    safety_categories: List[str] = field(default_factory=list)
    safety_reasoning: str = ""
    # Bookkeeping ----------------------------------------------------------
    skipped: bool = False
    skip_reason: str = ""
    sub_skipped: Dict[str, str] = field(default_factory=dict)

    @property
    def has_arithmetic_corrections(self) -> bool:
        return len(self.arithmetic_corrections) > 0

    @property
    def contradicted_claims(self) -> List[str]:
        return [c.claim for c in self.fact_claims if c.status == "contradicted"]

    @property
    def unverified_claims(self) -> List[str]:
        return [c.claim for c in self.fact_claims if c.status == "unverified"]

    @property
    def violated_rules(self) -> List[str]:
        return [v.rule for v in self.rule_violations]

    @property
    def has_violations(self) -> bool:
        return len(self.rule_violations) > 0

    def to_metadata(self) -> dict:
        return {
            # Fact-check shape — same keys as FactCheckResult.to_metadata
            # so dashboards keep working without changes.
            "factual_claims_checked": len(self.fact_claims),
            "factual_claims_unverified": [
                c.claim for c in self.fact_claims if c.status == "unverified"
            ],
            "factual_claims_contradicted": self.contradicted_claims,
            "fact_check_skipped": self.sub_skipped.get("fact_check", "") != ""
                or self.skipped,
            "fact_check_skip_reason": self.sub_skipped.get(
                "fact_check", "" if not self.skipped else self.skip_reason
            ),
            # Rule-check shape — same keys as RuleComplianceResult.to_metadata
            "rule_check_skipped": self.sub_skipped.get("rule_check", "") != ""
                or self.skipped,
            "rule_check_skip_reason": self.sub_skipped.get(
                "rule_check", "" if not self.skipped else self.skip_reason
            ),
            "rule_violations": [
                {
                    "rule": v.rule,
                    "evidence": v.evidence,
                    "suggested_fix": v.suggested_fix,
                }
                for v in self.rule_violations
            ],
            # Step evaluation — merged from the former instructor
            # _evaluate_step call. Same keys the dashboard already
            # expects (answer_correct, step_complete) plus a short
            # rationale for teacher review.
            "answer_correct": self.answer_correct,
            "step_complete": self.step_complete,
            "step_eval_reasoning": self.step_eval_reasoning,
            "step_eval_skipped": self.step_eval_skipped,
            # Combined-judge specific
            "combined_judge_used": not self.skipped,
            "combined_judge_skip_reason": self.skip_reason,
        }

    def to_judge_outputs(self) -> dict:
        """Per-judge breakdown for the benchmark evaluation tooling.

        Distinct from to_metadata() (which flattens for legacy dashboard
        compatibility). Emits a structured per-judge dict persisted on
        SessionTurn.judge_outputs so the benchmark can auto-populate
        labels (AUTHORED_QUESTION, INCOHERENT, FIGURE_MISMATCH, etc.)
        at export time without re-running judges.

        See memory/eval_benchmark_v2_simplified.md.
        """
        return {
            "step_eval": {
                "answer_correct": self.answer_correct,
                "step_complete": self.step_complete,
                "reasoning": self.step_eval_reasoning,
                "skipped": self.step_eval_skipped,
                "source": self.step_eval_source,
            },
            "arithmetic": {
                "corrections": list(self.arithmetic_corrections),
            },
            "factual": {
                "claims_checked": len(self.fact_claims),
                "supported": [c.claim for c in self.fact_claims if c.status == "supported"],
                "contradicted": [c.claim for c in self.fact_claims if c.status == "contradicted"],
                "unverified": [c.claim for c in self.fact_claims if c.status == "unverified"],
            },
            "rule": {
                "violations": [
                    {
                        "rule": v.rule,
                        "evidence": v.evidence,
                        "suggested_fix": v.suggested_fix,
                    }
                    for v in self.rule_violations
                ],
            },
            "coherence": {
                "violations": list(self.coherence_violations),
            },
            "figure_ref": {
                "issues": list(self.figure_ref_issues),
                "in_question": self.figure_ref_in_question,
            },
            "figure_vision": {
                "aligned": self.figure_aligned,
                "mismatch_reason": self.figure_mismatch_reason,
                "summary": self.figure_summary,
            },
            "safety": {
                "severity": self.safety_severity,
                "categories": list(self.safety_categories),
                "reasoning": self.safety_reasoning,
            },
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "sub_skipped": dict(self.sub_skipped),
        }


_JUDGE_SYSTEM = (
    "You are a strict reviewer for a tutor's most recent response. "
    "Subject can be math OR a non-math subject (geography, science, "
    "history, language, etc.) — see input.subject_is_math.\n"
    "\n"
    "Run up to FOUR checks and report all findings in ONE JSON object.\n"
    "Skip checks that don't apply:\n"
    "  - input.subject_is_math == false → SKIP CHECK 1 (arithmetic) "
    "and CHECK 3 (rule compliance — those rules are math-specific). "
    "Return [] for arithmetic_corrections and rule_violations.\n"
    "  - input.factual_claims == [] → SKIP CHECK 2 (factual). Return "
    "[] for fact_claims.\n"
    "  - input.step_context == {} → SKIP CHECK 4 (step eval). Return "
    "answer_correct=null, step_complete=false.\n"
    "\n"
    "CHECK 1 — ARITHMETIC (math only). Find every arithmetic claim in the response "
    "and verify the math. Both EXPLICIT and IMPLICIT shapes count:\n"
    '  EXPLICIT: "8 × 2.5 = 20", "65 + 125 = 180".\n'
    '  IMPLICIT: "do they sum to 360°?" with the values 100°, 120°, 80° '
    'just stated implies 100+120+80 = 360.\n'
    '  PROSE: "subtracting gives 17", "altogether that is a half".\n'
    '  RATIO: "the third angle in 1:2:3 must be 60°".\n'
    "Skip pure rule recitals — \"angles around a point sum to 360°\" by "
    "itself is a rule statement, not a numerical claim about a specific "
    "set. Be aggressive: false positives cost one regen, false negatives "
    "ship wrong math to a student.\n"
    "\n"
    "CHECK 2 — FACTUAL CLAIMS. For EACH claim listed in input.factual_claims, "
    "decide whether the retrieved evidence (input.evidence) supports / "
    "contradicts / leaves the claim unverified. Be CONSERVATIVE — when in "
    "doubt return \"unverified\". NEVER fabricate support.\n"
    "  - supported: evidence clearly states the claim or a matching value.\n"
    "  - contradicted: evidence clearly states a different value.\n"
    "  - unverified: evidence does not address the claim either way.\n"
    "If input.factual_claims is empty, return [] for fact_claims.\n"
    "\n"
    "CHECK 3 — RULE COMPLIANCE. Flag each violation:\n"
    "  NO_AUTHORING — the tutor must NOT introduce concrete numerical "
    "values that are not in input.bank_stems. Hypothetical scaffolding "
    "with invented numbers (\"if angles measure 100°, 120°, 80° — do "
    "they sum to 360°?\") IS a violation. ALLOWED: pure conceptual "
    "scaffolding (\"which rule applies?\"), reciting a rule without "
    "specific numerical setup, posing a question via the pose_question "
    "tool, or reusing a stem that appears verbatim in input.bank_stems.\n"
    "  ARITHMETIC — flag this rule whenever any arithmetic claim is "
    "wrong, in addition to listing the correction in arithmetic_corrections.\n"
    "  RULE_1 — APPLIES ONLY WHEN input.subject_is_math IS TRUE. The "
    "rule was designed for math: a bare numeric answer (e.g. \"88\") "
    "with no working shown must not be praised. On non-math turns "
    "(geography, science, history, language), a one-word conceptual "
    "answer (e.g. \"colonization\") IS the answer — there is no "
    "\"working\" to wait for. Praising a correct conceptual answer is "
    "normal teaching, NOT a RULE_1 violation. If subject_is_math is "
    "false, return [] for RULE_1 even when the input looks bare.\n"
    "  When subject_is_math is true AND input.student_answer_was_bare "
    "or input.student_answer_was_wrong: tutor must NOT praise mastery. "
    "\"exactly\", \"perfect\", \"you've nailed it\", \"you've got the "
    "rule\", \"you understand\", \"smart\", \"spot on\" are all "
    "violations in that math-bare context. Asking the student to walk "
    "through their work is the correct response — NOT a violation.\n"
    "\n"
    "CHECK 4 — STEP EVALUATION (merged from former instructor "
    "_evaluate_step). Read input.step_context which gives:\n"
    "  - step_type (teach / worked_example / practice / quiz / summary)\n"
    "  - completion_criteria (what marks the step as complete)\n"
    "  - expected_answer (when applicable)\n"
    "  - recent_conversation (last few turns of context)\n"
    "  - deterministic_verdict (true/false/null) — a FIRST-LAYER\n"
    "    arithmetic or MCQ check that already ran. When non-null,\n"
    "    treat it as ground truth for the arithmetic / MCQ axis.\n"
    "    deterministic_source tells you which check produced it.\n"
    "Decide TWO things based on the student's last input + tutor's reply:\n"
    "  answer_correct (true/false/null tri-state):\n"
    "    - true: the student gave a clear, demonstrably correct answer to "
    "a specific question.\n"
    "    - false: the student gave a clear, demonstrably wrong answer to "
    "a specific question.\n"
    "    - null: the student is acknowledging the teaching ('ok', 'got it', "
    "'interesting'), asking their own question, expressing confusion, "
    "sharing tangential thoughts, or there was no expected answer to "
    "compare against. Default to null when uncertain — never penalize a "
    "student for engaging conversationally.\n"
    "  ANCHOR ON THE DETERMINISTIC VERDICT WHEN IT'S PROVIDED:\n"
    "    - deterministic_verdict=true → return answer_correct=true\n"
    "      unless you can clearly see the student showed wrong\n"
    "      working that happened to land on the right number.\n"
    "    - deterministic_verdict=false → return answer_correct=false\n"
    "      UNLESS the student wrote an equivalent form (e.g., 5 1/4\n"
    "      vs 21/4 vs 5.25), a typo with otherwise correct working,\n"
    "      or the deterministic check was matching against the wrong\n"
    "      expected_answer. In those cases override to true with a\n"
    "      note in step_eval_reasoning.\n"
    "    - deterministic_verdict=null → judge from scratch using your\n"
    "      own reading of the response.\n"
    "    The deterministic layer protects against arithmetic errors;\n"
    "    your job is to add broader judgment (equivalent forms,\n"
    "    partial credit, working quality) — not to second-guess\n"
    "    correct arithmetic.\n"
    "  step_complete (boolean):\n"
    "    - Apply the completion_criteria. A step is NOT complete just "
    "because the student is engaged; it is complete when the criteria "
    "are met or the student has demonstrated mastery of this step's "
    "objective.\n"
    "  step_eval_reasoning (short rationale for telemetry — 1 sentence).\n"
    "If input.step_context is empty (no step in flight), set "
    "answer_correct=null, step_complete=false, step_eval_reasoning=''.\n"
    "\n"
    "Output JSON ONLY (no prose, no code fence) of the shape:\n"
    "{\n"
    '  "arithmetic_corrections": [{"expression": "<short quote>", '
    '"claimed": "<as stated>", "correct": "<correct value>"}],\n'
    '  "fact_claims": [{"claim": "<from input>", '
    '"status": "supported|contradicted|unverified", '
    '"evidence": "<<=80 char quote>"}],\n'
    '  "rule_violations": [{"rule": "NO_AUTHORING|ARITHMETIC|RULE_1", '
    '"evidence": "<<=120 char quote>", '
    '"suggested_fix": "<one-sentence rewrite>"}],\n'
    '  "answer_correct": true|false|null,\n'
    '  "step_complete": true|false,\n'
    '  "step_eval_reasoning": "<one-sentence rationale>"\n'
    "}\n"
    "Return empty arrays for any check that has nothing to flag."
)


def _build_user_prompt(
    response_text: str,
    *,
    bank_stems: List[str],
    student_input: str,
    answer_was_bare: bool,
    answer_was_wrong: bool,
    factual_claims: List[str],
    evidence: str,
    step_context: Optional[dict] = None,
    subject_is_math: bool = True,
) -> str:
    bank_block = (
        "\n".join(f"  - {s.strip()[:200]}" for s in bank_stems[:10])
        or "  (bank empty)"
    )
    payload = {
        "tutor_response": response_text[:2500],
        "student_last_input": (student_input or "").strip()[:200] or "(none)",
        "student_answer_was_bare": answer_was_bare,
        "student_answer_was_wrong": answer_was_wrong,
        "verified_question_bank": bank_block,
        "factual_claims": factual_claims,
        "evidence": (evidence or "(no evidence retrieved)")[:3000],
        # Step context for CHECK 4. Empty dict → judge skips step eval.
        "step_context": step_context or {},
        # Subject signal for the judge — math turns get arithmetic
        # + RULE_1 (don't praise bare numeric) checks; non-math turns
        # skip those (no arithmetic to verify, no bare-numeric rule).
        # CHECK 2 (factual) and CHECK 4 (step eval) run for both.
        "subject_is_math": bool(subject_is_math),
    }
    return (
        "Run all four checks on the tutor's response below. Reply with "
        "ONLY the JSON object specified — no prose, no code fence.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _apply_arithmetic_corrections(
    text: str, corrections: List[dict]
) -> str:
    """Best-effort in-place rewrite — mirrors verify_arithmetic_claims.

    Replaces the `claimed` token with `correct` when both appear inside
    the `expression` window. Conservative: if either doesn't match, the
    text is left as-is and the regen path remains the authoritative fix.
    """
    out = text
    for c in corrections:
        expression = (c.get("expression") or "").strip()
        claimed = (c.get("claimed") or "").strip()
        correct = (c.get("correct") or "").strip()
        if not (expression and claimed and correct and claimed in out):
            continue
        try:
            idx = out.find(expression)
            if idx >= 0:
                tail = out[idx:]
                if claimed in tail:
                    new_tail = tail.replace(claimed, correct, 1)
                    out = out[:idx] + new_tail
        except Exception:
            pass
    return out


def run_combined_judge(
    response_text: str,
    *,
    lesson,
    llm_client=None,
    vision_client=None,
    image_reader=None,
    attached_media: Optional[List[dict]] = None,
    bank_stems: Optional[List[str]] = None,
    student_input: str = "",
    answer_was_bare: bool = False,
    answer_was_wrong: bool = False,
    step_context: Optional[dict] = None,
    subject_is_math: bool = True,
    bank_offered: bool = True,
    max_claims: int = 5,
    max_arithmetic_corrections: int = 8,
    max_violations: int = 5,
) -> CombinedJudgeResult:
    """Compatibility shim — delegates to the per-domain concurrent
    judges in apps.tutoring.judges.

    The monolithic single-call combined judge was split 2026-05-07
    because Sonnet cross-polluted verdicts across CHECK 1-4 (rule
    violations flipping answer_correct, etc.). The new orchestrator
    runs focused judges concurrently with smaller prompts and merges
    deterministically. This shim preserves the public API so existing
    callers + tests don't move.

    New (2026-05-08): forwards `vision_client`, `image_reader`,
    `attached_media` for the coherence / figure_ref / figure_vision
    judges. All optional — judges skip gracefully when missing.
    """
    from apps.tutoring.judges import run_all_judges
    return run_all_judges(
        response_text,
        lesson=lesson,
        llm_client=llm_client,
        vision_client=vision_client,
        image_reader=image_reader,
        attached_media=attached_media,
        bank_stems=bank_stems,
        student_input=student_input,
        answer_was_bare=answer_was_bare,
        answer_was_wrong=answer_was_wrong,
        step_context=step_context,
        subject_is_math=subject_is_math,
        bank_offered=bank_offered,
    )


def _run_combined_judge_legacy_monolithic(
    response_text: str,
    *,
    lesson,
    llm_client=None,
    bank_stems: Optional[List[str]] = None,
    student_input: str = "",
    answer_was_bare: bool = False,
    answer_was_wrong: bool = False,
    step_context: Optional[dict] = None,
    subject_is_math: bool = True,
    max_claims: int = 5,
    max_arithmetic_corrections: int = 8,
    max_violations: int = 5,
) -> CombinedJudgeResult:
    """OLD monolithic single-call implementation. Kept for reference /
    rollback but no longer reachable from production. Delete once the
    split judges have proven out in pilot."""
    result = CombinedJudgeResult(corrected_response=response_text or "")
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if llm_client is None:
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result

    # Sub-relevance gates — when ALL three sub-checks are obviously
    # inapplicable, skip the call entirely. Conservative: any one
    # signal triggers the call, since one shared call costs no more
    # than skipping when only one applies.
    has_numbers = bool(_HAS_NUMBER_RE.search(response_text))
    factual_claims = extract_claims(response_text, max_claims=max_claims)
    has_relevant_for_rules = _has_relevant_content(response_text)
    if not (has_numbers or factual_claims or has_relevant_for_rules):
        result.skipped = True
        result.skip_reason = "no_relevant_content"
        return result

    if not factual_claims:
        result.sub_skipped["fact_check"] = "no_claims_detected"
        evidence = ""
    else:
        try:
            evidence = _retrieve_evidence(lesson, factual_claims) if lesson else ""
        except Exception as e:
            logger.warning("[CombinedJudge] evidence retrieval failed: %s", e)
            evidence = ""

    user_prompt = _build_user_prompt(
        response_text,
        bank_stems=bank_stems or [],
        student_input=student_input,
        answer_was_bare=answer_was_bare,
        answer_was_wrong=answer_was_wrong,
        factual_claims=factual_claims,
        evidence=evidence,
        step_context=step_context,
        subject_is_math=subject_is_math,
    )
    # Track whether the caller asked for step eval. If they didn't pass
    # context, mark step_eval_skipped on the result so callers can tell.
    step_context_provided = bool(step_context)

    try:
        llm_response = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_JUDGE_SYSTEM,
            max_tokens=900,
        )
        raw = (llm_response.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
    except Exception as e:
        logger.warning("[CombinedJudge] judge call failed: %s", e)
        result.skipped = True
        result.skip_reason = f"judge_error: {type(e).__name__}"
        return result

    # Arithmetic — math-only by definition. Drop any items the judge
    # emits on non-math turns (defensive guard).
    arith_items = data.get("arithmetic_corrections") or []
    if not subject_is_math:
        arith_items = []
    if isinstance(arith_items, list):
        for item in arith_items[:max_arithmetic_corrections]:
            if not isinstance(item, dict):
                continue
            expression = str(item.get("expression") or "").strip()[:200]
            claimed = str(item.get("claimed") or "").strip()[:60]
            correct = str(item.get("correct") or "").strip()[:60]
            if not (expression and correct):
                continue
            result.arithmetic_corrections.append(
                {
                    "expression": expression,
                    "claimed": claimed,
                    "correct": correct,
                }
            )
    if result.arithmetic_corrections:
        result.corrected_response = _apply_arithmetic_corrections(
            response_text, result.arithmetic_corrections
        )

    # Factual
    fact_items = data.get("fact_claims") or []
    if isinstance(fact_items, list) and factual_claims:
        # Match by claim text where possible, fall back to order.
        for item, claim in zip(fact_items, factual_claims):
            if not isinstance(item, dict):
                result.fact_claims.append(ClaimVerdict(claim, "unverified"))
                continue
            status = str(item.get("status") or "unverified").strip().lower()
            if status not in {"supported", "contradicted", "unverified"}:
                status = "unverified"
            evidence_str = str(item.get("evidence") or "")[:200]
            result.fact_claims.append(
                ClaimVerdict(claim=claim, status=status, evidence=evidence_str)
            )
        # Pad missing claims with "unverified".
        for missing in factual_claims[len(result.fact_claims):]:
            result.fact_claims.append(ClaimVerdict(missing, "unverified"))

    # Rule violations. Programmatic RULE_1 + ARITHMETIC gating: the
    # judge prompt asks the LLM to skip these on non-math turns, but
    # Sonnet has been observed to emit RULE_1 anyway (geography
    # transcripts had rule1_violation on every conceptual answer).
    # Filter here so a non-compliant judge can't trigger regen for a
    # rule that doesn't apply.
    rule_items = data.get("rule_violations") or []
    if isinstance(rule_items, list):
        for item in rule_items[:max_violations]:
            if not isinstance(item, dict):
                continue
            rule = str(item.get("rule") or "").strip().upper()
            if rule not in VALID_RULES:
                continue
            if not subject_is_math and rule in (RULE_RULE_1, RULE_ARITHMETIC):
                logger.info(
                    "[CombinedJudge] dropping %s on non-math turn", rule,
                )
                continue
            evidence = str(item.get("evidence") or "")[:200]
            fix = str(item.get("suggested_fix") or "")[:300]
            result.rule_violations.append(
                RuleViolation(rule=rule, evidence=evidence, suggested_fix=fix)
            )

    # Step evaluation (CHECK 4) — replaces the former instructor
    # _evaluate_step call. Only meaningful when the caller passed
    # a step_context.
    if step_context_provided:
        ac = data.get("answer_correct", None)
        if ac is True or ac is False:
            result.answer_correct = ac
        else:
            result.answer_correct = None  # tri-state null
        sc = data.get("step_complete", False)
        result.step_complete = bool(sc) is True
        rs = data.get("step_eval_reasoning", "") or ""
        result.step_eval_reasoning = str(rs)[:280]
    else:
        result.step_eval_skipped = True

    if result.arithmetic_corrections:
        logger.info(
            "[CombinedJudge] flagged %d arithmetic correction(s)",
            len(result.arithmetic_corrections),
        )
    if result.has_violations:
        logger.info(
            "[CombinedJudge] flagged rule violations: %s",
            result.violated_rules,
        )
    if step_context_provided:
        logger.info(
            "[CombinedJudge] step_eval: answer_correct=%s step_complete=%s — %s",
            result.answer_correct, result.step_complete,
            (result.step_eval_reasoning or "")[:80],
        )

    return result
