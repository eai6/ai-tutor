# Grading System Research — 2026-05-25

Research sweep on reliable grading / answer-verification for the AI Tutor's
new simple-tutor engine. Pairs with `simple_tutor_engine_plan.md`. Findings
condensed into design rules at `auto-memory/feedback_grading_design_rules.md`
(committed only as a label — actual file lives in user auto-memory).

**Audience:** anyone implementing or modifying tutor grading. Read once
before touching `apps/tutoring/grader.py` or proposing a grader change.

**Constraints driving the research:**
- Tier 1 + Tier 2 only (deterministic verifier + cross-family LLM verifier)
- No Tier 3 (human review) during live tutoring — verdict must land in-session
- p95 latency budget: 2 seconds for a turn
- Tutor LLM (Claude/Opus) must NOT be the same call that grades

---

## Failure modes (and what makes them worse)

LLM judges in 2025-2026 literature consistently break on five biases:

1. **Position bias** — order of answers affects judgment (Shi et al., 2024)
2. **Verbosity / length bias** — longer answers score higher regardless of correctness
3. **Sycophancy** — RLHF amplifies this; model defers to confident student claims
4. **Self-preference** — same-family models rate each other higher
5. **Authority / confidence bias** — confident-but-wrong beats hedged-and-right

Adaline's 2026 audit found frontier models still fail bias tests >50% of the time on judge tasks. **The single most important compound failure** is sycophancy + length bias: long, confident, agreeable wrong answers get auto-graded correct.

**Mitigations with empirical support:**
- Temperature 0 + structured output schema (instructor + Pydantic)
- Rubric decomposition — score per dimension with one-sentence justification beats single verdict
- CoT-before-score in the schema — model commits to verdict BEFORE justifying
- For pairwise comparison tasks: position swap + average
- Cross-family verifier (defeats self-preference)
- Context-free verifier — verifier doesn't see the tutoring conversation (no inherited sycophancy)

---

## State of the art: Automatic Short-Answer Grading (ASAG)

**Benchmarks:** SciEntsBank, Beetle, ASAP-SAS, Mohler remain standard.

**2025 EDM and arXiv findings converge on three points:**

1. Rubric-conditioned prompting on open models **matches fine-tuned BERT** on these benchmarks.
2. Fine-tuned 7B models (Llama-3.1, Mistral) **beat zero-shot GPT-4-class** on domain content. (Not relevant for v1 — fine-tuning infra burden too high.)
3. **RAG-augmented grading** (retrieve canonical answer + rubric, then grade) outperforms pure zero-shot.

**Dominant production pattern: hybrid embedding-similarity + LLM verifier.** Cosine similarity gates obvious matches and obvious misses; LLM only adjudicates the middle band. This is what we're adopting.

---

## Math grading: WolframAlpha vs alternatives

**Decision: skip WolframAlpha for v1.**

| Tool | Coverage | Latency | Cost | Verdict |
|---|---|---|---|---|
| `sympy` + `latex2sympy2_extended` (HF fork) | ~95% of secondary-school math | <50ms | Free | ✅ Use |
| HuggingFace `Math-Verify` library | De-facto standard since GSM8K/MATH eval cleanup | <50ms | Free | ✅ Use (drop-in on top of sympy) |
| WolframAlpha API | Marginal +5% (calculus, weird notation) | 300-1000ms | ~$25/1k calls | ❌ Skip for v1, revisit if Tanzania pilot adds calculus |

**Math-Verify** (`huggingface/Math-Verify`) is the cleanest reference. Handles `$\frac{1}{2}$ = 0.5 = 50%` natively. Reference implementation pattern: PrairieLearn's `pl-symbolic-input` (symbolic-then-numeric fallback).

---

## Verifier LLM prompt design (production-validated)

Sources: Braintrust, promptfoo, SurePrompts 2026.

**Structure:** role + rubric + question + reference answer + student answer → JSON `{per-criterion scores, justifications, final verdict, confidence}`

**Critical rules:**
- Verifier **should see** the question and reference answer.
- Verifier **should NOT see** the tutoring conversation — context-free verifiers are more calibrated and don't inherit tutor sycophancy.
- **Multi-criteria scoring beats single verdict** for short open-response. Even for short answers, score on (correctness, completeness, reasoning) at minimum.
- **One-sentence justification per criterion** is the cheapest CoT that meaningfully improves calibration.

**Pydantic schema — verdict FIRST:**
```python
class GradeResult(BaseModel):
    verdict: Literal['correct', 'partial', 'incorrect']  # FIRST — anchors decision
    per_criterion_scores: dict[str, float]
    confidence: float
    justification: str                                    # LAST — rationalize after
```

Why verdict-first matters: putting CoT before the verdict causes the model to anchor on its own reasoning. Putting verdict first commits the model to a decision it then has to justify — counterintuitively more accurate.

---

## Confidence thresholds (production-validated)

From the April 2026 paper **"When Can We Trust LLM Graders?"** (arxiv 2603.29559):

- Self-reported confidence ECE ≈ 0.17
- Self-consistency (n=3-5 majority vote) ECE ≈ 0.23
- GPT-OSS-120B reaches ECE 0.10

**Production threshold pattern:**

| Confidence | Action |
|---|---|
| > 0.85 | Auto-accept verdict, surface to student |
| 0.5 – 0.85 | "Partial credit + ask follow-up" — DON'T treat as wrong |
| < 0.5 | "Let's work through this together" — remediation, no verdict shown |

**Self-consistency n=3 only in the middle band.** Calling the verifier 3× for every grade kills p95 latency. Re-run only for confidence in [0.5, 0.85].

**Validation plan:** hold out ~100 labeled turns from real pilot transcripts after first deployment, compute accuracy-rejection curve, tune thresholds per-question from observed data — not vibes.

---

## Top recommendations (in priority order)

1. **Embedding-similarity gate BEFORE LLM verifier** — cheap, fast, kills 40-60% of obvious cases.
2. **Math-Verify + latex2sympy2-extended** for math grading. Skip WolframAlpha for v1.
3. **Rubric-decomposed structured output** with per-criterion justification + self-reported confidence, temperature 0, verifier context-free (no conversation history).
4. **Self-consistency (n=3) only in middle confidence band** [0.5, 0.85] — keeps p95 < 2s.
5. **Log every verdict + confidence + tier + question_id to `SessionTurn.judge_outputs`** — enables post-hoc threshold tuning on real pilot data.

**Single most important failure mode to design against: sycophancy + length bias compounding.**

Defense (baked into the design):
- Verifier is a *separate* model family from the tutor (Gemini judge while tutor is Claude/Opus — already CLAUDE.md routing)
- Context-free verifier
- Structured output with `verdict` field that must be `correct|partial|incorrect` BEFORE any rationale field
- Temperature 0

---

## Reading list (read these once)

- **Judging the Judges** (Shi et al., 2024) — https://arxiv.org/abs/2406.07791 — position bias canonical
- **When Can We Trust LLM Graders?** (2026) — https://arxiv.org/abs/2603.29559 — confidence calibration numbers
- **Rubric-Conditioned LLM Grading** (2026) — https://arxiv.org/pdf/2601.08843 — production rubric design
- **PrairieLearn pl-symbolic-input** — https://docs.prairielearn.com/elements/pl-symbolic-input/ — cleanest reference for math grading cascade
- **latex2sympy2_extended (HuggingFace)** — https://github.com/huggingface/latex2sympy2_extended
- **Math-Verify (HuggingFace)** — drop-in math equivalence checker

Other sources consulted:
- Position Bias in LLM Judges (Brenndoerfer)
- LLM-as-a-Judge Reliability & Bias (Adaline 2026)
- FairJudge: Adaptive Debiased Judge — arxiv 2602.06625
- Confidence Estimation in ASAG with LLMs — arxiv 2605.00200
- Enhancing LLM ASAG with RAG (EDM 2025)
- Estimating LLM Grading Ability via IRT — arxiv 2605.00238
- SymPy Gotchas & Pitfalls (sympy docs)
- LLM-as-Judge Practical Guide (SurePrompts 2026)
- Khanmigo Math Computation Updates (Khan Academy blog)
- Multi-Step Grading Rubrics with LLMs (Green Report)
- Wolfram|Alpha API Pricing (Wolfram products page)

---

## Final design judgement (one paragraph)

Given Tier 1 + Tier 2 with no in-session human gate, the most defensible design is: **Tier 1 = MCQ key match → Math-Verify/sympy symbolic+numeric cascade → embedding cosine similarity threshold gate; Tier 2 = cross-family verifier LLM (Gemini judge while tutor is Claude/Opus), temperature 0, instructor + Pydantic schema with `{verdict: correct|partial|incorrect, per_criterion_scores, justification, confidence}`, context-free (sees question + reference answer + student answer only, NOT conversation), self-consistency n=3 only when confidence is in the 0.5-0.85 band.** Bind every verdict to its `SessionTurn.judge_outputs` row so threshold tuning is data-driven, not vibes-driven. <The Knowledge base should be used for grading too. things like geography can benefit from this.>
