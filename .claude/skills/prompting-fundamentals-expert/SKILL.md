---
name: prompting-fundamentals-expert
description: Universal prompt engineering principles that apply to any LLM provider (Claude, OpenAI, Gemini, Llama). Auto-loads when designing prompts, debugging prompts, choosing output formats, defending against prompt injection, or planning evals. Covers Chain-of-Thought, in-context learning, prompt sensitivity, hallucination mitigation, prompt injection defense, iterative refinement via evals, DSPy / programmatic prompt construction, and the 2024-2026 shift to evals-driven and context-engineering paradigms. Strongly opinionated about anti-patterns (polite filler, negative-only instructions, threats, vague qualifiers).
---

# Prompting Fundamentals — Expert

Universal principles. Provider-specific tactics live in `claude-prompting-expert`, `openai-prompting-expert`, `gemini-prompting-expert`. **Read this first when designing or debugging any prompt.**

## TL;DR — the rules in order

1. **Write evals before prompts.** A held-out eval set of 20-100 cases with expected outputs is the prerequisite for iteration. Without it, you're optimizing on vibes — and prompt outputs are alarmingly sensitive to surface form (Sclar et al. showed up to 76-point accuracy swings from format changes alone).
2. **For modern reasoning models, strip the scaffolding.** Few-shot examples and "think step by step" *hurt* o-series, Claude extended thinking, and Gemini Thinking models — they already reason internally. State the problem plainly + output format.
3. **For long context: query last.** Place documents first, instructions and question at the bottom. Anthropic reports ~30% quality improvement on multi-doc inputs with this structure.
4. **Constrained-decoding JSON > prompted JSON.** Asking for JSON in prose drops reasoning accuracy 10-15%. Use the provider's schema API (OpenAI Structured Outputs, Anthropic `output_config.format`, Gemini `response_schema`) which constrains at decode time.
5. **No prompt-only defense against prompt injection.** Use architectural mitigations (Meta's "Agents Rule of Two", CaMeL dual-LLM, instruction hierarchy). Delimiter tricks alone fail.

## Chain-of-Thought (CoT) — when it helps, when it hurts

**Core idea.** Eliciting intermediate reasoning steps before the final answer substantially improves performance on multi-step problems ([Wei et al. 2022](https://arxiv.org/abs/2201.11903)). Kojima et al. found "Let's think step by step" unlocks zero-shot CoT across arithmetic, symbolic, and logic tasks ([arXiv 2205.11916](https://arxiv.org/abs/2205.11916)).

| Apply when | Avoid when |
|---|---|
| Multi-step math, planning, multi-hop QA | Modern reasoning models (o-series, Claude extended thinking, Gemini Thinking) |
| Tasks where a one-shot guess is fragile | Latency-critical paths — CoT bloats outputs |
| GPT-4 / Claude 3.x / Gemini 1.5 / Llama 3 | Simple lookups, classification |

**Pitfall: CoT on reasoning models.** Raschka observed few-shot prompting consistently *degrades* o-series output ([Understanding Reasoning LLMs](https://magazine.sebastianraschka.com/p/understanding-reasoning-llms)). Reasoning models do best with a plain problem statement + output format.

## In-context learning / few-shot

**Core idea.** GPT-3 ([Brown et al. 2020](https://arxiv.org/abs/2005.14165)) showed k=1-50 task demonstrations match fine-tuned baselines without gradient updates. Gains are largest going from 0 to 1-3 examples; performance plateaus around 5-10.

**Recency bias is real.** Balanced prompts ending in one class push predictions toward that class — the last example disproportionately shapes the answer ([Zhao et al.](https://arxiv.org/pdf/2102.09690), [learnprompting debiasing guide](https://learnprompting.org/docs/reliability/debiasing)).

| Apply when | Avoid when |
|---|---|
| Classification, extraction, style-mimicking, format demonstration | Reasoning models (o-series, etc.) — examples often hurt |
| Task is easier shown than described | Examples drawn from a different distribution than real inputs |

**Pitfalls:**
- Examples leak the wrong format → model copies it
- All-same-label sequences → recency bias
- Examples that subtly conflict with prose instructions → model follows examples
- Randomize order, balance classes, place the most representative example last

## Prompt structure conventions

Reliable template: **role → task → context → examples → input → format**.

**Anthropic's XML-tag convention** ([docs.claude.com](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)): wrap each region in semantic tags (`<instructions>`, `<context>`, `<example>`, `<input>`, `<output_format>`). Consistency matters more than the specific names. Works on every provider — Claude was trained heavily on XML and outperforms with it; OpenAI and Gemini also tolerate XML well.

**Instructions-last for long context.** For documents ≥20K tokens, place the document near the top and put the question AFTER it. Anthropic reports ~30% improvement on multi-doc inputs ([long-context tips](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/long-context-tips)). Reason: attention recency — final tokens steer generation most strongly.

**Pitfall.** Burying the task at the top of a 50K-token prompt. Repeat critical instructions at the bottom if context is long.

## Output formatting — when constraint hurts

**Core idea.** Asking for JSON/XML/markdown helps when downstream code parses outputs, but constrained decoding can harm reasoning. [Tam et al. (arXiv 2501.10868)](https://arxiv.org/html/2501.10868v1) showed forcing strict JSON during generation drops reasoning-task accuracy 10-15% versus free-form-then-extract.

**Better pattern.** Let the model reason in prose inside `<reasoning>`, then emit `<answer>` JSON. Or do two passes: reason first, format second. OpenAI's [Structured Outputs](https://openai.com/index/introducing-structured-outputs-in-the-api/) uses constrained decoding to guarantee schema adherence with less degradation than prompted JSON-mode.

| Apply when | Avoid when |
|---|---|
| Machine-consumed outputs, tool calls, extraction | Tasks requiring genuine reasoning (math, analysis) |
| Provider supports constrained-decoding schema (Structured Outputs, `output_config.format`, `response_schema`) | Stacking 3 competing format directives |

## Prompt sensitivity — variance is real

**Core idea.** Outputs are alarmingly brittle to surface form.

- [Lu et al. 2022](https://arxiv.org/abs/2104.08786): permuting few-shot example order swings accuracy from SOTA to random across model sizes. Best orderings don't transfer between models.
- [Sclar et al. 2024](https://arxiv.org/abs/2310.11324): up to **76-point accuracy swings** on LLaMA-2-13B from meaning-preserving format changes (`Q:` vs `Question:`, separator choice). Sensitivity persists with scaling and instruction tuning.

**Robustness techniques.**
- Evaluate across multiple paraphrases / orderings; report a range, not a point estimate.
- Use FormatSpread-style sweeps.
- Pin a format once you've found a good one.
- Treat any single-run comparison as suspect — variance from formatting noise often exceeds the effect you're measuring.

## Iterative refinement — prompts are code

**Core idea.** Version prompts, write evals, change one thing at a time.

**Build the eval set first.** 20-100 diverse cases with expected outputs. Without held-out evals, you're memorizing failures rather than fixing classes of them.

**Diff and minimize.** When a prompt works, strip lines one at a time and confirm eval scores hold — most prompts are bloated. When a prompt fails, isolate by showing the failing input alone, then add context until the line that triggers failure is identified.

**Programmatic optimization.** [DSPy](https://dspy.ai/) reframes prompts as compiled artifacts: input/output signatures + optimizers ([MIPROv2](https://dspy.ai/api/optimizers/MIPROv2/), [GEPA](https://dspy.ai/api/optimizers/GEPA/overview/)) search the prompt space against your metric. GEPA (Agrawal et al. 2025) uses reflective evolution with textual feedback and often beats RL with far fewer rollouts.

**Pitfall.** A/B testing without held-out evals — you'll overfit to memorable failures.

## Hallucination mitigation

**Core idea.** Hallucinations stem from rewarding confident guessing during training ([OpenAI white paper](https://cdn.openai.com/pdf/d04913be-3f6f-4d2b-b283-ff432ef4aaa5/why-language-models-hallucinate.pdf)).

Stack of mitigations, by effectiveness:

| Technique | Mechanism |
|---|---|
| **RAG with grounding instructions** | Retrieve passages + tell the model to use *only* those passages |
| **Explicit "say I don't know" license** | Many models hedge when given permission to abstain ([Anthropic guide](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)) |
| **Citation requirements** | Extract verbatim quotes first, then answer; if no supporting quote exists, retract |
| **Chain-of-Verification** | Generate verification questions, answer them *independently* (no context bleed from the draft), revise final response |

**Pitfall.** RAG without grounding instructions. The model still draws on parametric memory and silently mixes it with retrieved text.

## Prompt injection defense

**Core idea.** Indirect prompt injection ([Greshake et al.](https://arxiv.org/abs/2302.12173)) hides instructions inside retrieved data — emails, web pages, tool outputs — that the LLM treats as instructions rather than content. **There is no reliable prompt-only defense.**

Practical mitigations, ordered by strength:

1. **Architectural isolation** — [CaMeL](https://simonwillison.net/2025/Apr/11/camel/) dual-LLM: privileged planner + quarantined data-handling LLM. Plans never touch untrusted data; data-LLM never produces instructions.
2. **Meta's "Agents Rule of Two"** ([Willison summary](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/)): an agent may have at most 2 of {access untrusted input, access sensitive data, take consequential action}. If you need all three, redesign.
3. **Instruction hierarchy** — system > developer > user > tool output. Each level instructed to ignore lower-level instructions that contradict higher-level ones.
4. **XML tag delimiters** — wrap untrusted content in `<untrusted_data>...</untrusted_data>` and instruct the model to treat anything inside as data only. Weakest defense but still helps.

**Pitfall.** Filtering for "ignore previous instructions" — adaptive attacks defeat all 12 published prompt-only defenses in recent evals.

## Universal anti-patterns

| Anti-pattern | Better |
|---|---|
| **Polite filler** ("please", "thank you") — wastes tokens, no quality gain | Direct imperatives. System prompts that bake in "no filler responses, no 'Certainly!' or 'Great question!'" yield tighter outputs |
| **Negative-only instructions** ("don't be verbose", "don't hallucinate") | Positive framing ("respond in ≤3 sentences", "cite a source for every claim") |
| **Vague qualifiers** ("be thorough but concise") | Quantify ("3-5 paragraphs", "max 200 tokens") |
| **Conflicting instructions** stacked across system + developer + user | Audit for contradictions before each release |
| **Threats and bribes** ("you will be unplugged") | Folk wisdom with no replicated effect on frontier models. Skip. |
| **CoT scaffolding on reasoning models** | State problem + output format; let the model think internally |

## Recent thinking (2024-2026)

### From prompt engineering to evals-driven optimization

The field has moved from hand-crafted prompts to compiled, eval-driven systems.

- **DSPy** ([dspy.ai](https://dspy.ai/)) replaces prompt strings with signatures + modules + optimizers. GEPA and MIPROv2 search prompt space against your metric.
- **OpenAI evals framework** ([github.com/openai/evals](https://github.com/openai/evals)) — YAML-driven custom evals, registry of benchmarks.
- **Simon Willison's framing**: "write evals first, prompts second; treat the prompt as the smallest replaceable part of a larger system" ([essay](https://simonwillison.net/2025/Mar/11/using-llms-for-code/)).

### Reasoning models change the playbook

Raschka's [Understanding Reasoning LLMs](https://magazine.sebastianraschka.com/p/understanding-reasoning-llms):
- **No few-shot.** Examples constrain the internal reasoning trace.
- **No "think step by step".** Duplicates internal CoT.
- **State problem + output format. Stop.**

### Context engineering, not prompt engineering

Anthropic's framing ([Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)): curating what the model sees matters more than phrasing tricks. Retrieval, memory, tool outputs, scratchpads. The prompt is the smallest controllable lever.

### Pitfall

Copying 2022-era prompt recipes (elaborate roleplay, CoT scaffolding, "you are an expert") onto 2026 reasoning models — often a net negative. Audit before porting.

## Safety rules

❌ **Don't** trust a prompt change without held-out eval scores.
❌ **Don't** add CoT scaffolding on reasoning models.
❌ **Don't** force strict JSON during generation for reasoning tasks — reason first, format second.
❌ **Don't** ship a single-trial "better" prompt — variance from formatting noise often exceeds the effect.
❌ **Don't** rely on prompt-only prompt-injection defenses. Architect for it.
❌ **Don't** use threats, bribes, or all-caps "CRITICAL" — they don't replicate and can overtrigger modern models.
❌ **Don't** stuff conflicting instructions into system + developer + user — audit first.

✅ **Do** write evals before iterating.
✅ **Do** use constrained-decoding APIs when shape guarantees matter.
✅ **Do** place query/instructions LAST in long contexts.
✅ **Do** strip CoT scaffolding when porting to reasoning models.
✅ **Do** measure variance across paraphrases — not a single point estimate.
✅ **Do** prefer architectural defenses (dual-LLM, Rule of Two) over delimiter tricks.

## Key sources

**Papers:**
- [Chain-of-Thought — Wei et al.](https://arxiv.org/abs/2201.11903)
- [Zero-Shot Reasoners — Kojima et al.](https://arxiv.org/abs/2205.11916)
- [GPT-3 Few-Shot — Brown et al.](https://arxiv.org/abs/2005.14165)
- [Fantastically Ordered Prompts — Lu et al.](https://arxiv.org/abs/2104.08786)
- [Quantifying Prompt Sensitivity — Sclar et al.](https://arxiv.org/abs/2310.11324)
- [Calibrate Before Use — Zhao et al.](https://arxiv.org/pdf/2102.09690)
- [Indirect Prompt Injection — Greshake et al.](https://arxiv.org/abs/2302.12173)
- [Structured Outputs Benchmark — Tam et al.](https://arxiv.org/html/2501.10868v1)

**Provider guides (cross-applicable):**
- [Anthropic — Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
- [Anthropic — Long context tips](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/long-context-tips)
- [Anthropic — Context engineering for agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [OpenAI — Why Language Models Hallucinate](https://cdn.openai.com/pdf/d04913be-3f6f-4d2b-b283-ff432ef4aaa5/why-language-models-hallucinate.pdf)
- [OpenAI — Structured Outputs](https://openai.com/index/introducing-structured-outputs-in-the-api/)

**Programmatic optimization:**
- [DSPy](https://dspy.ai/) — [optimizers](https://dspy.ai/learn/optimization/optimizers/) — [GEPA](https://dspy.ai/api/optimizers/GEPA/overview/) — [MIPROv2](https://dspy.ai/api/optimizers/MIPROv2/)

**Practitioner writeups:**
- [Raschka — Understanding Reasoning LLMs](https://magazine.sebastianraschka.com/p/understanding-reasoning-llms)
- [Simon Willison — CaMeL](https://simonwillison.net/2025/Apr/11/camel/)
- [Simon Willison — Agents Rule of Two](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/)
- [Simon Willison — Agentic prompts](https://simonwillison.net/guides/agentic-engineering-patterns/prompts/)

**Tutorials:**
- [Prompting Guide — CoT](https://www.promptingguide.ai/techniques/cot)
- [LearnPrompting — Debiasing](https://learnprompting.org/docs/reliability/debiasing)

## Further context

- `claude-prompting-expert` — Claude-specific tactics (XML tags, prompt caching, adaptive thinking)
- `openai-prompting-expert` — OpenAI-specific (system/developer/user, o-series, Structured Outputs)
- `gemini-prompting-expert` — Gemini-specific (multimodal, 1M context, grounding)
- `agent-orchestration-expert` — for multi-agent design
- `claude-api` — Anthropic SDK mechanics
