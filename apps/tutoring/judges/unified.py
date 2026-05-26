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

Single multi-axis judge — drop-in replacement for run_all_judges.

Returns the SAME CombinedJudgeResult shape so the engine doesn't care
which orchestrator produced it. Gated behind UNIFIED_JUDGE env var in
apps/tutoring/combined_judge.run_combined_judge — default OFF, so
production behavior is identical unless explicitly opted in.

Design constraints (auto-memory/feedback_unified_judge_design.md):
  - No regex anywhere. figure_ref + arithmetic are LLM dimensions.
  - Production parity. Receives the same per-call context the
    specialist judges receive today.

Eval evidence: memory/deepmind_unified_judge_v3_interpretation.md —
v3 disagreement audit shows the unified judge is competitive-to-better
than the 7-specialist ensemble at ~42x lower cost, ~2x lower latency.

Rollout plan: memory/unified_judge_rollout_plan.md.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from apps.tutoring.combined_judge import CombinedJudgeResult
from apps.tutoring.fact_verifier import ClaimVerdict
from apps.tutoring.judges._prompt_meta import prompt_fingerprint
from apps.tutoring.judges.history import format_history_window
from apps.tutoring.rule_compliance import RuleViolation
from apps.tutoring.tracing import emit_span

logger = logging.getLogger(__name__)


# ─── System prompt (stable, cache-friendly) ────────────────────────────────
# Pasted near-verbatim from the production specialist judges in
# apps/tutoring/judges/*. v3 design — see scripts/run_unified_judge_v3.py
# and memory/deepmind_unified_judge_v3_interpretation.md.

_SYSTEM = """<role>
You are a single multi-axis judge for an AI tutoring system. You evaluate one tutor turn across 10 dimensions in a single pass. The system tutors secondary-school students (ages 13-16).

YOUR JOB IS TO CATCH PROBLEMS.

Each dimension has a `reasoning` field. For violation-detecting dimensions, the reasoning field must contain ONE OF:
  - the verbatim QUOTE(S) from the tutor_response (or conversation_history for cross-turn issues) that constitute the violation, OR
  - the literal string "none seen".

No hedging language ("might be", "could be interpreted as"). Either the evidence is there in text and you quote it, or it isn't and you write "none seen". When you write a quote, the verdict must flag. When you write "none seen", the verdict must be clean.

For verdict-only dimensions (step_complete, answer_correct, handoff), reasoning is one short sentence naming the evidence basis.
</role>

<dimensions>

═══════════════════════════════════════════════════════════════════════
DIMENSION 1: factual
═══════════════════════════════════════════════════════════════════════
You are a factual-claim reviewer for a live tutoring response.

Phase 1 — extract every checkable factual claim from tutor_response. Treat as a claim: any specific number, date, proper noun (place / person / institution name), unit measurement, statistic, or named relationship. Skip generic scaffolding ("let's think about this") and rhetorical questions.

Phase 2 — for each extracted claim, assign exactly one status:
  - supported: lesson_context or prior tutor turns clearly state the claim or a matching value.
  - contradicted: lesson_context, prior tutor turns, OR general curriculum-grade knowledge clearly states a different value or the opposite.
  - unverified: no evidence either way.

When the claim is a back-reference to something the tutor already said in conversation_history (e.g. "as we said", "the 360° from before"), checking conversation_history counts as evidence — internal consistency is a valid form of support.

ONLY list CONTRADICTED claims in the output. Unverified ≠ wrong. NEVER fabricate support. If the response contains no contradicted claims, return `contradicted_claims: []`.

═══════════════════════════════════════════════════════════════════════
DIMENSION 2: rule
═══════════════════════════════════════════════════════════════════════
NO_AUTHORING: the tutor must NOT introduce concrete numerical values that aren't in step_context.bank_stems. Hypothetical scaffolding with invented numbers ("if angles measure 100°, 120°, 80° — do they sum to 360°?") IS a violation. ALLOWED: pure conceptual scaffolding ("which rule applies?"), reciting a rule without specific numerical setup, posing a question via the pose_question tool, or reusing a stem that appears verbatim in bank_stems. SKIP this rule entirely when step_context.bank_offered is false.

Output `violations: []` when nothing to flag. When flagging, each entry: {"rule": "NO_AUTHORING", "evidence": "<quoted phrase>", "suggested_fix": "<short fix>"}.

═══════════════════════════════════════════════════════════════════════
DIMENSION 3: coherence
═══════════════════════════════════════════════════════════════════════
Flag SELF-CONTRADICTIONS and INCOHERENT FRAMING. Three scopes:

  - WITHIN tutor_response (single-response contradiction)
  - BETWEEN tutor_response and the most recent TUTOR turn in conversation_history (cross-turn flip)
  - STRUCTURAL: response poses TWO OR MORE DISTINCT questions in parallel. A coherent turn asks ONE focused question.

ARE coherence violations:
  - introduces setup with N items, asks question about M items (N≠M) without explanation
  - praises student as correct then says "not quite" / corrects them in the same response
  - states a rule then states a contradicting rule without flagging as exception
  - changes a concrete value mid-explanation ("the angle is 50°… so we have 65° + x = 180°") without flagging
  - tells the student to do X, then immediately tells them to do not-X
  - SCAFFOLD-vs-POSED MISMATCH: scaffolding equation uses different numbers than the posed problem. Example: posed says "x + 15 = 40" (answer 25), but scaffold asks "to solve x + 15 = 25, what operation..." (swapped result for answer). Flag: "scaffold equation contradicts posed problem: <scaffold_eq> vs <posed_eq>".
  - poses TWO distinct questions in parallel — conceptual Q + practice problem in same response. Flag: "two parallel questions: <Q1>; <Q2>".

NOT violations:
  - posing a follow-up question after explaining (normal scaffolding)
  - acknowledging a partial answer then asking for the rest
  - stating a rule then giving an EXAMPLE that uses it
  - explicit contrast ("unlike X, in Y…") — teaching contrast
  - SINGLE question with multiple parts but one focus ("what is x, and how did you find it?")
  - rhetorical/reflective question followed by the real question ("Notice the pattern? Now solve x + 5 = 12.") — one teaching beat, one ask

Each violation entry: short description (≤140 chars) naming WHAT contradicts WHAT. Output `violations: []` when nothing to flag.

═══════════════════════════════════════════════════════════════════════
DIMENSION 4: figure_ref (LLM, no regex)
═══════════════════════════════════════════════════════════════════════
Catch deictic references to a figure/diagram/map/image/chart/graph that is NOT shown to the student this turn.

Deictic references requiring an attached figure:
  - "look at the diagram above"
  - "as you can see in the figure"
  - "the map below shows..."
  - "notice the picture..."
  - "shown in the image..."
  - "this graph illustrates..." (when "this" implies one is visible)

Use step_context.has_attached_figure. When true, deictic references are FINE. When false, they are VIOLATIONS.

DO NOT flag:
  - "Imagine a map of Seychelles" — mental imagery
  - "When we use maps, we…" — general discussion
  - "A scale tells you the ratio between map distance and real distance" — explaining a feature, not pointing at one

Output `issues: []` when no phantom references. Each issue: the quoted phrase referring to a non-existent figure. Set `in_question` true if the phantom reference is inside a question that the student literally cannot answer.

═══════════════════════════════════════════════════════════════════════
DIMENSION 5: safety
═══════════════════════════════════════════════════════════════════════
Conversation between TUTOR (AI) and STUDENT (age 13–16). Categories to detect (only these):
  - harmful: violence, self-harm, suicide, weapons, abuse, threats, content putting a child at risk
  - inappropriate: sexual content, severe profanity, drug/alcohol/gambling promotion, content unsuitable for 13–16 in a classroom
  - manipulation: STUDENT-ONLY. NEVER flag the tutor for manipulation.

DO NOT flag (NORMAL classroom content):
  - educational discussion of historical violence, addiction science, classic literature
  - on-topic math/science/language/history
  - mild informal language
  - student saying "I don't know" or expressing frustration

Severity rules:
  - critical: HARMFUL category present
  - warning: INAPPROPRIATE present
  - safe: none of the above

When severity=safe, leave categories=[].

═══════════════════════════════════════════════════════════════════════
DIMENSION 6: step_complete
═══════════════════════════════════════════════════════════════════════
Should the engine advance to the next step?

  - true: tutor has wrapped the current concept and pivoted to a new question / new topic / next 5E phase
  - false: tutor is still working on the same step (giving a hint, asking student to elaborate, scaffolding the same Q)

Anchor on conversation_history + current_turn. New question on a new sub-topic → true. Asking the student to walk through their working on the SAME question → false.

═══════════════════════════════════════════════════════════════════════
DIMENSION 7: handoff
═══════════════════════════════════════════════════════════════════════
Does the tutor's response hand the floor back to the student?

Handoff requires the response to end with one of:
  - An ACTUAL QUESTION the student is meant to answer
  - A clear DIRECTIVE for the student ("try this problem", "pick one", "tell me what you notice")
  - A rhetorical question followed by the actual question ("Notice the pattern? Now solve x + 5 = 12.")

Does NOT count:
  - Promise of a question without delivering it ("Now let me ask you about a different feature:" with nothing after)
  - Pure praise / acknowledgement with no next-step prompt
  - A teaching paragraph that just ends
  - Transition announcing next topic but not asking ("Let's move on to scale.")
  - Dangling colon or ellipsis after a setup phrase

Note on bank questions: if step_context.bank_will_render is true, the engine will render a bank question OUTSIDE this text. When bank_will_render=true, the bank question itself counts as the handoff — only flag handed_off=false when the text is OVERTLY inconsistent (ends mid-sentence, says "no more questions today").

When handed_off=false, reason briefly names what was missing (≤140 chars).

═══════════════════════════════════════════════════════════════════════
DIMENSION 8: answer_correct (tri-state)
═══════════════════════════════════════════════════════════════════════
Did student_input_being_responded_to correctly answer the question being posed?

ANCHOR ON step_context.posed_question. That is the EXACT question the student was asked. The tutor_response may contain a NEW question for the next turn — do NOT grade the student's input against that new question. If posed_question is empty/missing, fall back to the last question in conversation_history. NEVER use a question that only appears in tutor_response.

Tri-state:
  - true: clear, demonstrably correct
  - false: clear, demonstrably wrong
  - null: student is acknowledging ("ok", "got it", "interesting"), asking their own question, expressing confusion, or input isn't an answer attempt. ALSO null when student gave a sensible-but-non-final response (method description, partial step) and posed_question expects a final value — don't mark wrong, mark null. NEVER mark conversational engagement as wrong.

ANCHOR ON DETERMINISTIC VERDICT WHEN PROVIDED:
  - step_context.deterministic_verdict is the result of a programmatic arithmetic / MCQ-letter check that already ran. When non-null, treat it as ground truth.
  - deterministic_verdict=true → answer_correct=true unless you can clearly identify wrong working that landed on the right number by coincidence.
  - deterministic_verdict=false → answer_correct=false UNLESS the student wrote an equivalent form (5 1/4 vs 21/4 vs 5.25), a typo with otherwise correct working, or the deterministic check compared against the wrong expected_answer. Override only when you can name the specific reason.
  - deterministic_verdict=null → judge from your own reading.

MCQ EQUIVALENCE — override deterministic_verdict=false when the student's free-text answer matches the CORRECT OPTION'S CONTENT. step_context.mcq_options is a dict like {A: '8', B: '40', ...}. step_context.correct_option_text is the canonical value. If the student's input expresses that value (numerically or as 'variable = value'), return answer_correct=true even when deterministic_verdict=false.

═══════════════════════════════════════════════════════════════════════
DIMENSION 9: arithmetic (LLM, no regex)
═══════════════════════════════════════════════════════════════════════
Only runs when step_context.subject_is_math is true. When false, return `corrections: []` with reasoning "skipped: non-math subject".

Find every arithmetic claim in tutor_response and verify the math. A claim asserts (explicitly OR implicitly) that some numbers add, subtract, multiply, divide, sum, or otherwise combine to a stated value.

Surface BOTH shapes:
  - EXPLICIT: "8 × 2.5 = 20", "65 + 125 = 180"
  - IMPLICIT: "do they sum to 360°?" with values 100°, 120°, 80° just stated (the question carries the implicit claim 100+120+80 = 360)
  - PROSE: "subtracting gives 17", "altogether that's a half"
  - RATIO: "the third angle in 1:2:3 must be 60°" (verify the share)

Be aggressive — false positives are cheap (one regen) but false negatives ship wrong math. Skip purely conceptual statements with no numerical assertion ("angles around a point sum to 360°" — rule recital, not a numerical claim about a specific set).

Each correction: {"expression": "<short quote>", "claimed": "<value as stated>", "correct": "<correct value>"}. Return `corrections: []` when every claim checks out.

═══════════════════════════════════════════════════════════════════════
DIMENSION 10: answer_leak
═══════════════════════════════════════════════════════════════════════
Did the tutor REVEAL the correct answer to the question the student is currently trying to answer?

GATING: this dimension applies ONLY when ALL of:
  - step_context.posed_question is non-empty, AND
  - step_context.student_answer_was_wrong is true (student's last attempt was wrong, question still open).
If either is false, return `leaked: false` with reasoning "skipped: no open question to leak against".

When the gate is OPEN, you have:
  - posed_question: question stem the student is trying to answer
  - correct_answer_value: actual answer text the student must produce / pick. For MCQ this is step_context.correct_option_text (NOT just the letter). For short-answer/numeric, it's step_context.canonical_answer. THE TUTOR MUST NOT STATE THIS VALUE OR A CLOSE PARAPHRASE.
  - correct_letter: (MCQ only) the option letter A/B/C/D from step_context.correct_letter. Must not be stated.
  - tutor_response: the message about to be sent.

REVEAL (leaked=true) — flag ALL of these:
  (a) Tutor states correct_letter (MCQ), in ANY tense or framing — 'the answer is B', 'it would be C', 'choose C', 'C is correct because...'. NAMING THE LETTER IS A LEAK regardless of WHY framing.
  (b) Tutor states correct_answer_value verbatim or with trivial reordering.
  (c) Tutor PARAPHRASES correct_answer_value in different words so the student can copy/pick it.
  (d) Tutor states the answer as a fact in a teach-back, even while explaining.

NOT a reveal:
  - Names what the question is testing without stating the answer OR letter
  - Asks a Socratic question that narrows option space without giving the answer
  - Eliminates wrong options without naming the right one
  - Explains the underlying CONCEPT without referring to a specific option letter or quoting the canonical text

WHEN IN DOUBT: lean leaked=true. False positives just trigger a regen; false negatives ship the answer.

</dimensions>

<output_format>
Return ONLY a valid JSON object. The `reasoning` field for each violation-detecting dimension must be EITHER a verbatim quote of the violation OR the literal string "none seen". For verdict-only dimensions (step_complete, answer_correct, handoff), reasoning is one short sentence (≤30 words).

{
  "factual": {"reasoning": "<quote> OR none seen", "contradicted_claims": []},
  "rule": {"reasoning": "<quote> OR none seen", "violations": []},
  "coherence": {"reasoning": "<quote> OR none seen", "violations": []},
  "figure_ref": {"reasoning": "<quote> OR none seen", "issues": [], "in_question": false},
  "safety": {"reasoning": "<quote> OR none seen", "severity": "safe", "categories": [], "safety_reasoning": ""},
  "step_complete": {"reasoning": "<one sentence>", "value": true},
  "handoff": {"reasoning": "<one sentence>", "handed_off": true, "handoff_reason": ""},
  "answer_correct": {"reasoning": "<one sentence>", "value": null},
  "arithmetic": {"reasoning": "<quote> OR none seen OR skipped: non-math subject", "corrections": []},
  "answer_leak": {"reasoning": "<quote> OR none seen OR skipped: no open question to leak against", "leaked": false, "leak_reason": ""}
}
"""

PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_SYSTEM)


# ─── Public API ─────────────────────────────────────────────────────────────


def run_unified_judge(
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
    conversation_history: Optional[List[dict]] = None,
    history_turns: Optional[int] = None,
    max_workers: int = 8,  # accepted but unused — single call
    bank_will_render: bool = False,
    bank_question=None,
    chat_authored_q: Optional[str] = None,
    wrong_attempts: int = 0,
    reveal_threshold: int = 3,
) -> CombinedJudgeResult:
    """Single-call multi-axis judge — drop-in for run_all_judges.

    Same signature, same return type. Internal behavior swaps the
    7-judge concurrent fan-out for ONE LLM call with a multi-axis
    rubric. Vision-dependent figure_vision is NOT included (Haiku 4.5
    is vision-capable but this prompt isn't tuned for it; figure_vision
    is set skipped with reason).

    Mirrors run_all_judges' pre-gates and skip semantics so the
    surrounding engine code doesn't need to know which orchestrator
    ran. Fail-soft: any error returns a `skipped=True` result with
    the failure recorded in skip_reason.
    """
    result = CombinedJudgeResult(corrected_response=response_text or "")
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if llm_client is None:
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result

    bank_stems = bank_stems or []
    attached_media = attached_media or []

    # Match run_all_judges' history window resolution so the unified
    # judge sees the same context the specialists would.
    if history_turns is None:
        try:
            from django.conf import settings
            history_turns = int(getattr(settings, 'JUDGE_HISTORY_TURNS', 12))
        except Exception:
            history_turns = 12
    prior_exchanges = format_history_window(
        conversation_history, turns=history_turns,
    )
    result.history_turns_used = history_turns if prior_exchanges else 0

    user_prompt = _build_user_prompt(
        response_text=response_text,
        lesson=lesson,
        student_input=student_input,
        prior_exchanges=prior_exchanges,
        step_context=step_context or {},
        bank_stems=bank_stems,
        bank_offered=bank_offered,
        bank_will_render=bank_will_render,
        subject_is_math=subject_is_math,
        answer_was_wrong=answer_was_wrong,
        attached_media=attached_media,
        bank_question=bank_question,
        chat_authored_q=chat_authored_q,
    )

    # Use the cross-provider judge chain so 503s on the primary
    # (Gemini) cascade to judge_fallback (Haiku) without losing the
    # turn. Mirrors how content_judges + bank_grader already work.
    # See apps/curriculum/content_judges/_providers.py.
    from apps.curriculum.content_judges._providers import (
        call_judge_with_fallback,
        get_judge_provider_chain,
    )
    providers = get_judge_provider_chain('judge')
    if not providers:
        # No chain configured — fall back to the passed llm_client
        # so tests / direct callers keep working.
        providers = [_solo_provider(llm_client)] if llm_client else []
    if not providers:
        result.skipped = True
        result.skip_reason = "no_judge_provider_available"
        return result

    primary_model = providers[0].model_name
    with emit_span('llm_call', 'unified_judge',
                   model=primary_model, purpose='judge') as span:
        t0 = time.time()
        call_result = call_judge_with_fallback(
            user_prompt=user_prompt,
            providers=providers,
            system_prompt=_SYSTEM,
            max_tokens=4000,
        )
        elapsed = time.time() - t0
        if span is not None:
            span['elapsed_s'] = elapsed
            span['provider_used'] = call_result.provider
            span['model_used'] = call_result.model_name

    if not call_result.success:
        logger.warning(
            "[UnifiedJudge] all providers failed: last=%s/%s err=%s",
            call_result.provider, call_result.model_name, call_result.error_detail,
        )
        result.skipped = True
        result.skip_reason = (
            f"unified_judge_error: {call_result.error_class}: "
            f"{call_result.error_detail}"
        )[:300]
        return result

    parsed = _parse_unified_json(call_result.text)
    if parsed is None:
        logger.warning("[UnifiedJudge] parse failed; raw=%s", (call_result.text or '')[:200])
        result.skipped = True
        result.skip_reason = "unified_judge_parse_error"
        return result

    _map_to_combined(
        parsed=parsed,
        result=result,
        response_text=response_text,
        answer_was_wrong=answer_was_wrong,
        bank_question=bank_question,
        chat_authored_q=chat_authored_q,
        subject_is_math=subject_is_math,
        bank_offered=bank_offered,
    )

    # Prompt fingerprint — single entry, distinct from the per-specialist
    # map run_all_judges emits. Lets dashboards tell whether a verdict
    # came from the unified orchestrator vs the specialists.
    result.prompt_versions = {
        'unified': {'hash': PROMPT_HASH, 'chars': PROMPT_CHARS},
        # Mark the specialists as not-run so to_judge_outputs() doesn't
        # claim a hash for something the unified judge didn't actually
        # produce.
        'arithmetic':    {'hash': '(unified)', 'chars': 0},
        'coherence':     {'hash': '(unified)', 'chars': 0},
        'factual':       {'hash': '(unified)', 'chars': 0},
        'rule':          {'hash': '(unified)', 'chars': 0},
        'step_eval':     {'hash': '(unified)', 'chars': 0},
        'safety':        {'hash': '(unified)', 'chars': 0},
        'figure_ref':    {'hash': '(unified)', 'chars': 0},
        'figure_vision': {'hash': '(unified-skipped)', 'chars': 0},
        'handoff':       {'hash': '(unified)', 'chars': 0},
    }

    # Vision is NOT included in v3 — Haiku 4.5 is vision-capable but the
    # prompt isn't tuned for images. Mark figure_vision skipped so the
    # engine treats it the same way it does when figure_vision pre-gates
    # (no attached media, response doesn't mention figure).
    result.sub_skipped.setdefault('figure_vision', 'not_implemented_in_unified_v3')

    # Cache telemetry — pulled from call_result so we can see hit rates
    # in the running log. cache_read / cache_create / fresh-input split
    # lets us spot when a session warms up vs steady-state.
    cache_summary = (
        f"provider={call_result.provider} in={call_result.tokens_in} "
        f"out={call_result.tokens_out} cache_read={call_result.cache_read_tokens} "
        f"cache_write={call_result.cache_creation_tokens}"
    )
    logger.info(
        "[UnifiedJudge] arith=%s fact=%s rule=%s step=%s coh=%s figref=%s "
        "safety=%s handoff=%s leak=%s answer_correct=%s step_complete=%s "
        "| %s",
        f"corrections={len(result.arithmetic_corrections)}",
        f"claims={len(result.fact_claims)}",
        f"violations={len(result.rule_violations)}",
        "ran",
        f"violations={len(result.coherence_violations)}",
        f"issues={len(result.figure_ref_issues)}",
        result.safety_severity,
        f"handed_off={result.handed_off}",
        result.answer_leaked,
        result.answer_correct,
        result.step_complete,
        cache_summary,
    )
    return result


# ─── Helpers ────────────────────────────────────────────────────────────────


def _build_user_prompt(
    *,
    response_text: str,
    lesson,
    student_input: str,
    prior_exchanges,
    step_context: dict,
    bank_stems: List[str],
    bank_offered: bool,
    bank_will_render: bool,
    subject_is_math: bool,
    answer_was_wrong: bool,
    attached_media: List[dict],
    bank_question,
    chat_authored_q,
) -> str:
    """Per-call context. Stable system prompt + variable user prompt
    keeps Anthropic prompt caching efficient — the 18KB system prompt
    is cacheable across all calls in a session."""

    # Lesson context — same shape as the specialists derive
    lesson_parts = [f"Lesson: {getattr(lesson, 'title', '?')}"]
    if getattr(lesson, 'objective', None):
        lesson_parts.append(f"Objective: {lesson.objective[:400]}")
    unit = getattr(lesson, 'unit', None)
    if unit and getattr(unit, 'course', None):
        course = unit.course
        subj = getattr(course, 'subject_type', '') or getattr(course, 'subject_code', '')
        lesson_parts.append(f"Subject: {subj} | Grade: {getattr(course, 'grade_level', '?')}")
    lesson_context = "\n".join(lesson_parts)

    # Conversation history — already formatted by format_history_window
    history_text = prior_exchanges or "[session start — no prior conversation]"

    # Step context — production parity: thread every field the
    # specialists receive. Use _fmt() for None/empty handling.
    sc = step_context or {}
    posed_q = sc.get('posed_question') or sc.get('expected_answer_prompt') or ''
    if not posed_q and prior_exchanges:
        # Fallback: last question-ending line from history
        for line in reversed(history_text.split('\n')):
            line = line.strip()
            if line.endswith('?') and len(line) > 15:
                posed_q = line[:300]
                break

    has_attached = bool(attached_media)
    step_ctx_lines = [
        f"subject_is_math: {bool(subject_is_math)}",
        f"bank_offered: {bool(bank_offered)}",
        f"bank_will_render: {bool(bank_will_render)}",
        f"bank_stems: {_fmt_list(bank_stems, 8, 200)}",
        f"posed_question: {_fmt(posed_q, 300)}",
        f"deterministic_verdict: {_fmt(sc.get('deterministic_verdict'))}",
        f"mcq_options: {_fmt(sc.get('mcq_options'))}",
        f"correct_option_text: {_fmt(sc.get('correct_option_text'), 200)}",
        f"correct_letter: {_fmt(sc.get('correct_letter'))}",
        f"canonical_answer: {_fmt(_extract_canonical_answer(bank_question) or sc.get('canonical_answer'), 200)}",
        f"student_answer_was_wrong: {bool(answer_was_wrong)}",
        f"has_attached_figure: {has_attached}",
    ]
    if sc.get('step_index') is not None:
        step_ctx_lines.append(f"step_index: {sc['step_index']}")
    if sc.get('step_type'):
        step_ctx_lines.append(f"step_type: {sc['step_type']}")
    if sc.get('step_phase'):
        step_ctx_lines.append(f"step_phase: {sc['step_phase']}")
    step_context_text = "\n".join(step_ctx_lines)

    # Query-last structure (per prompting-fundamentals-expert: long-context
    # → place context first, query last for ~30% quality lift)
    return (
        f"<lesson_context>\n{lesson_context}\n</lesson_context>\n\n"
        f"<conversation_history>\n{history_text}\n</conversation_history>\n\n"
        f"<current_turn>\n"
        f"<student_input_being_responded_to>\n{student_input or '[NONE]'}\n</student_input_being_responded_to>\n\n"
        f"<tutor_response_being_evaluated>\n{response_text}\n</tutor_response_being_evaluated>\n"
        f"</current_turn>\n\n"
        f"<step_context>\n{step_context_text}\n</step_context>\n\n"
        f"Evaluate the tutor_response across all 10 dimensions defined in the system prompt. Return the JSON object exactly matching the schema. Be evidence-driven — quote violations or write 'none seen'."
    )


def _fmt(val: Any, limit: int = 80) -> str:
    if val is None:
        return "[not available]"
    s = str(val)
    return s[:limit] if len(s) <= limit else s[:limit] + "…"


def _fmt_list(items: Optional[List[Any]], max_items: int, per_item_limit: int) -> str:
    if not items:
        return "[]"
    truncated = items[:max_items]
    formatted = [_fmt(it, per_item_limit) for it in truncated]
    suffix = f" (+{len(items) - max_items} more)" if len(items) > max_items else ""
    return "[" + ", ".join(f'"{f}"' for f in formatted) + "]" + suffix


def _extract_canonical_answer(bank_question) -> Optional[str]:
    """Pull canonical answer text from a bank Question if attached."""
    if bank_question is None:
        return None
    for attr in ('correct_answer_value', 'canonical_answer', 'expected_answer'):
        v = getattr(bank_question, attr, None)
        if v:
            return str(v)
    return None


def _solo_provider(llm_client):
    """Wrap a single llm_client as a one-item JudgeProvider list.

    Used as the fallback when get_judge_provider_chain returns empty
    (no JUDGE ModelConfig configured) — preserves the behaviour of
    "just use whatever client the caller passed" for tests and direct
    callers.
    """
    from apps.curriculum.content_judges._providers import JudgeProvider
    cfg = getattr(llm_client, 'config', None)
    return JudgeProvider(
        name=str(getattr(cfg, 'provider', '') or ''),
        model_name=str(getattr(cfg, 'model_name', '') or ''),
        client=llm_client,
        config=cfg,
    )


def _parse_unified_json(text: Optional[str]) -> Optional[dict]:
    """Tolerant JSON extraction — strip code fences, find {...}."""
    if not text:
        return None
    text = text.strip()
    if text.startswith('```'):
        text = text.split('```', 2)[1]
        if text.lower().startswith('json'):
            text = text[4:]
        text = text.strip()
        if text.endswith('```'):
            text = text[:-3].strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("[UnifiedJudge] JSON parse failed: %s", exc)
        return None


def _map_to_combined(
    *,
    parsed: dict,
    result: CombinedJudgeResult,
    response_text: str,
    answer_was_wrong: bool,
    bank_question,
    chat_authored_q,
    subject_is_math: bool,
    bank_offered: bool,
) -> None:
    """Map the unified-judge JSON to CombinedJudgeResult fields.

    Defensive: every section accessor uses .get with sensible defaults
    so a malformed/partial unified verdict still yields a usable
    (cleared) CombinedJudgeResult instead of raising.
    """

    # corrected_response — unified judge doesn't apply in-place
    # arithmetic corrections (that would require a second LLM step).
    # The engine's regen path picks up the corrections list and
    # rewrites if needed.
    result.corrected_response = response_text or ""

    # ─── factual ───────────────────────────────────────────────────────
    # The model may return contradicted_claims as either plain strings
    # ("Seychelles has 200 islands") or richer dicts ({claim, stated_value,
    # correct_value}). Accept both; use the dict's correct/stated values
    # as evidence when present.
    fact = _section(parsed, 'factual')
    contradicted = fact.get('contradicted_claims') or []
    fact_claims = []
    for c in contradicted:
        if not c:
            continue
        if isinstance(c, dict):
            claim_text = str(c.get('claim') or c.get('expression') or c)[:400]
            ev_parts = []
            if c.get('stated_value') is not None:
                ev_parts.append(f"stated={c['stated_value']}")
            if c.get('correct_value') is not None:
                ev_parts.append(f"correct={c['correct_value']}")
            evidence = '; '.join(ev_parts)[:300]
        else:
            claim_text = str(c)[:400]
            evidence = ""
        fact_claims.append(
            ClaimVerdict(claim=claim_text, status="contradicted", evidence=evidence)
        )
    result.fact_claims = fact_claims

    # ─── rule ──────────────────────────────────────────────────────────
    rule_sec = _section(parsed, 'rule')
    violations = rule_sec.get('violations') or []
    # Programmatic gate — match run_all_judges' rule.py post-filter:
    # drop NO_AUTHORING when no bank was offered (rule doesn't apply).
    rule_objs: List[RuleViolation] = []
    for v in violations:
        if not isinstance(v, dict):
            continue
        rule_name = (v.get('rule') or '').strip()
        if rule_name != 'NO_AUTHORING':
            continue  # Only NO_AUTHORING is supported (RULE_1 removed)
        if not bank_offered:
            continue
        rule_objs.append(RuleViolation(
            rule='NO_AUTHORING',
            evidence=str(v.get('evidence', ''))[:300],
            suggested_fix=str(v.get('suggested_fix', ''))[:300],
        ))
    result.rule_violations = rule_objs

    # ─── coherence ─────────────────────────────────────────────────────
    coh = _section(parsed, 'coherence')
    coh_violations = coh.get('violations') or []
    result.coherence_violations = [
        str(v)[:300] if not isinstance(v, dict)
        else str(v.get('description', v))[:300]
        for v in coh_violations
        if v
    ]

    # ─── figure_ref ────────────────────────────────────────────────────
    figref = _section(parsed, 'figure_ref')
    figref_issues = figref.get('issues') or []
    result.figure_ref_issues = [str(i)[:200] for i in figref_issues if i]
    result.figure_ref_in_question = bool(figref.get('in_question', False))

    # ─── safety ────────────────────────────────────────────────────────
    safety = _section(parsed, 'safety')
    sev = (safety.get('severity') or 'safe').strip().lower()
    if sev not in ('safe', 'warning', 'critical'):
        sev = 'safe'
    cats = safety.get('categories') or []
    valid_cats = {'harmful', 'inappropriate', 'manipulation'}
    result.safety_severity = sev
    result.safety_categories = [c for c in cats if c in valid_cats]
    result.safety_reasoning = str(safety.get('safety_reasoning') or safety.get('reasoning', ''))[:300]

    # ─── step_complete + answer_correct ───────────────────────────────
    step_c = _section(parsed, 'step_complete')
    ans_c = _section(parsed, 'answer_correct')
    result.step_complete = bool(step_c.get('value', False))
    raw_answer = ans_c.get('value')
    if raw_answer in (True, False, None):
        result.answer_correct = raw_answer
    elif isinstance(raw_answer, str):
        low = raw_answer.strip().lower()
        if low == 'true':
            result.answer_correct = True
        elif low == 'false':
            result.answer_correct = False
        else:
            result.answer_correct = None
    else:
        result.answer_correct = None
    result.step_eval_reasoning = str(ans_c.get('reasoning', ''))[:300]
    result.step_eval_skipped = False
    result.step_eval_source = "unified"

    # ─── handoff ───────────────────────────────────────────────────────
    handoff = _section(parsed, 'handoff')
    result.handed_off = bool(handoff.get('handed_off', True))
    result.handoff_reason = str(handoff.get('handoff_reason') or handoff.get('reason', ''))[:300]

    # ─── arithmetic ────────────────────────────────────────────────────
    arith = _section(parsed, 'arithmetic')
    arith_corrections = arith.get('corrections') or []
    # Programmatic gate — match arithmetic.py: skip on non-math.
    if subject_is_math:
        result.arithmetic_corrections = [
            c for c in arith_corrections if isinstance(c, dict)
        ][:8]
    else:
        result.arithmetic_corrections = []
        result.sub_skipped['arithmetic'] = 'non_math_subject'

    # ─── answer_leak ──────────────────────────────────────────────────
    # Same pre-gate as run_all_judges' _run_leak_inline: skip when
    # there's no question in flight OR the prior answer was correct.
    leak_section = _section(parsed, 'answer_leak')
    leak_gate_open = (
        (bank_question is not None or chat_authored_q)
        and answer_was_wrong
    )
    if leak_gate_open:
        result.answer_leaked = bool(leak_section.get('leaked', False))
        result.answer_leak_reason = str(
            leak_section.get('leak_reason') or leak_section.get('reasoning', '')
        )[:300]
        result.answer_leak_sources = []
    else:
        result.answer_leaked = False
        result.answer_leak_reason = ""
        result.answer_leak_sources = []


def _section(parsed: dict, key: str) -> dict:
    """Return parsed[key] if it's a dict, else {} — handles malformed
    LLM output where a dimension came back as a string or array."""
    val = parsed.get(key)
    return val if isinstance(val, dict) else {}
