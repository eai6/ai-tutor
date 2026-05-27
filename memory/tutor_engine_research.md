# Conversational Tutor Engine Research — 2026-05-25

Research sweep on production conversational-tutor engines (Khanmigo, Duolingo
Max, BEA 2025 submissions, IntelliCode) to inform the simple-tutor rebuild.
Pairs with `simple_tutor_engine_plan.md`. Findings condensed into design rules
at `auto-memory/feedback_simple_tutor_engine_design.md`.

**Audience:** anyone implementing or modifying `apps/tutoring/simple_tutor.py`.
Read once before touching the engine or proposing a structural change.

---

## Architectural patterns to mirror (production-validated)

### 1. Pre-computed scaffolds passed in context, not generated
Khanmigo's biggest accuracy win was forcing the model to consume human-authored
exercises, hints, and solution steps before responding. Our `LessonStep.content_html`
+ `ExitTicketQuestion.correct_answer` is exactly this — pass the current step and
correct answer in-prompt, **never re-derive**.

### 2. Deterministic computation behind a tool
Khanmigo added a calculator tool rather than trusting GPT-4's arithmetic.
Mirror: `record_answer` tool that runs Tier-1 / Tier-2 grading server-side; the
LLM only extracts the answer text and reads the verdict back. Stops the model
from "feeling generous" with wrong answers (sycophancy).

### 3. Roleplay-style scenario authoring (Duolingo Max)
Humans write the opening message and per-scenario goals; the model only fills
the middle. Our `LessonStep` already encodes the goal — keep step instructions
in the system prompt and let the LLM improvise only the turn surface.

### 4. Tool-separated state mutation <how does the llm pose a questions? and how does the tutor differentiate a studetn aswer from student question>
Production tutors converge on **4 canonical tools**:
- `record_answer(question_id, extracted_text)` — extract + delegate grading
- `advance_step(reason)` — server moves to next step, triggers summary computation
- `request_figure(figure_id)` — server validates ID against pre-generated catalog
- `redirect_off_topic(reason)` — explicit moderation guardrail after 2 off-topic turns

Each tool is a single Python function that mutates DB state. **The LLM never writes
JSON state directly.** This is the dominant pattern across 2025 BEA submissions
and IntelliCode.

### 5. Sliding window + step-anchored summary
Khan + Duolingo + the "ConversationSummaryBuffer" pattern: keep the last ~6-10
turns verbatim, replace older turns with a one-line per-step summary keyed by
`LessonStep.id` — e.g., `"Step 3 mastered after 2 attempts; misconception:
confused weather/climate"`. Cheap, deterministic, debuggable.

**Critical:** pre-compute the summary on `advance_step` call. Do NOT summarize
via LLM mid-session — adds latency + cost + variance.

---

## Failure modes to design against (with literature-attested rates)

| Failure | Rate / source | Concrete defense |
|---|---|---|
| **Sycophancy** (model flips correct→wrong after student "isn't it X?") | 58% (Stanford) | Grading lives in a tool the LLM cannot overrule; response generator sees verdict, not raw answer |
| **Answer leakage in hints** (model gives away canonical answer) | 30% (GPT4Hints/GPT3.5Val paper) | Gate hints behind exchange count; post-generation regex check forbids canonical answer string in hint text |
| **Multi-turn drift** (model forgets current step's objective) | 39% perf drop (Microsoft 2025) | Re-state active step's objective + correct answer at top of **every** system prompt — turn 1 prompt == turn 20 prompt; only rolling history changes |
| **Off-topic spiral / cost spiral** | Common; Khanmigo built explicit moderation guardrails | Hard `redirect_off_topic` tool that the LLM is instructed to call after 2 off-topic turns |
| **Hallucinated figure references** | Same root cause as our old `|||MEDIA:N|||` bug | Pass figure catalog to the prompt; `request_figure` tool validates the ID exists server-side |

---

## State management — what production systems do

- **Last 6-10 turns verbatim** in the prompt
- **Older turns** → one-line per-step summary keyed by `LessonStep.id`
- **Pre-compute summaries on `advance_step`**, NOT mid-turn
- **All engine state in DB rows**, mutated by tool handlers only
- **Async post-hoc judges** for observability (NOT runtime inline)

Khan/Duolingo's convergent answer to "how much context history do they keep":
short verbatim window + structured summary keys. No LLM-summarized history
mid-turn — that's where cost + variance come from.

---

## Pedagogical patterns with empirical support

- **Socratic questioning over direct explanation** — well-attested in tutoring
  literature, but requires the system prompt to explicitly forbid answer-dumping
- **One question per turn** — keeps cognitive load low; production tutors enforce
  this in the system prompt
- **Hints behind exchange count** — only after N attempts. Defeats the
  answer-leakage failure mode partially
- **Working before answer (math)** — already in AI Tutor's
  `auto-memory/feedback_math_tutoring.md`; carries over to the new engine

Direct LLM implementations of the 5E method (Engage/Explore/Explain/Elaborate/
Evaluate) are not well-documented in production; most systems use step-tagged
pre-generated content with a phase label, which is essentially what AI Tutor
already does.

---

## Lowest-risk simple engine design (research's recommendation)

> Given AI Tutor's constraints — Opus 4.7, pre-generated steps, pgvector KB,
> pilot scale — the lowest-risk design is a **single-call agent with 4 tools
> and a stateless prompt template**.
>
> Per turn:
> - Build prompt from (system + step objective + correct answer + media
>   catalog + last 8 turns + step summary log + student input)
> - Call Opus once with tools `[record_answer, advance_step, request_figure,
>   redirect_off_topic]`
> - Execute any tool calls server-side (grading is exact-match, not LLM)
> - Persist results, return the text
>
> No phase machine, no judge fan-out at runtime (judges become async post-hoc
> evals), no engine-state JSON the LLM writes to. State lives in DB rows
> mutated only by tool handlers.
>
> This mirrors Khanmigo's converged architecture, dodges the multi-agent
> error-amplification trap (Cemri 2025, already in CLAUDE.md), and shrinks
> the engine from ~12,000 lines to ~800.

---

## Reading list

- [Khan Academy's 7-Step Approach to Prompt Engineering for Khanmigo](https://blog.khanacademy.org/khan-academys-7-step-approach-to-prompt-engineering-for-khanmigo/) — canonical writeup
- [Khanmigo Math Computation and Tutoring Updates](https://blog.khanacademy.org/khanmigo-math-computation-and-tutoring-updates/) — concrete tool-use pattern for math
- [Khan Academy × Langfuse case study](https://langfuse.com/users/khan-academy) — what observability they actually run (matches our trace-logging-before-decomposition stance)
- [BEA 2025 MSA Shared Task paper](https://arxiv.org/pdf/2505.18549) — disagreement-aware tutor evaluation, directly relevant to our unified judge

Other sources consulted:
- [Introducing Duolingo Max](https://blog.duolingo.com/duolingo-max/)
- [Challenging the Evaluator: LLM Sycophancy](https://aclanthology.org/2025.findings-emnlp.1222.pdf)
- [Ensemble of Specialized LLMs for Adaptive Tutoring](https://arxiv.org/pdf/2603.23990)
- [IntelliCode: Multi-Agent LLM Tutoring](https://arxiv.org/pdf/2512.18669) — cautionary tale on multi-agent
- [GPT-4 Tutor / GPT-3.5 Validator hint study](https://arxiv.org/pdf/2310.03780) — the answer-leakage paper

---

## Anti-patterns (explicitly do NOT do)

- LLM-summarized history mid-turn (cost + variance)
- Multi-agent orchestration (CLAUDE.md: Cemri 2025 found 17× error amplification)
- LLM-written engine state JSON (use tool handlers + DB rows)
- Inline judges blocking the response (move to async post-hoc)
- Re-deriving correct answers (pass them in-prompt; never let the LLM compute them)
- Untyped JSON state (CLAUDE.md inconsistency to avoid)
