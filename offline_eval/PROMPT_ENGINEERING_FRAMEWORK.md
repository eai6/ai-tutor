# Model-Specific Prompt-Engineering Framework — Top-25 Tutor-Eval Models

**Audience:** AI Tutor engineering team (Principal / senior) · **Status:** reference spec
**Source of truth:** `offline_eval/leaderboard_combined.csv` (44 models, 60-scenario single-turn tool-loop harness) + `offline_eval/FINDINGS_offline_model_eval.md`
**Grounded in:** `prompting-fundamentals-expert`, `claude-prompting-expert`, `gemini-prompting-expert` skills + per-family web research against official 2024–2026 docs (citations inline).
**Last built:** 2026-06-24

> **Scope note.** This document tunes prompts *per model family*, keyed to the families that actually lead **our** benchmark. Several model IDs in our leaderboard are point-versions ahead of public documentation (`claude-opus-4-7`, `gemini-3.5-flash`, `grok-4.20`, `glm-5`, `deepseek-v3.2`). Guidance is written at the **family level** and anchored to the nearest publicly-documented version; where a specific point-version's behavior is unverified, it is flagged.

---

## Part 0 — Executive Summary

1. **The leaderboard contradicts the common "Llama/Mistral are the OSS leaders" assumption.** **No Llama model is in the top 25** (best is `llama3.1:8b`, rank 26). Mistral squeaks in once (`mistral-nemo:12b`, rank 16). The actual leaders are **Claude (top 3), then a wall of Chinese open-weight MoEs** — Qwen (×6 in the top 25), GLM (×3), DeepSeek (×2), Kimi — plus **xAI Grok (×4)** and **Gemini (×4)**.

2. **One variable explains more than any prompt trick: thinking-mode vs instruct-mode.** On our *tool-driven* tutor, pure-reasoning variants collapse (`deepseek-r1` 22%, `qwen3-next-80b-thinking` 2%) because chain-of-thought tokens exhaust the generation budget before the tool call fires, while the **instruct siblings land 63–65%**. Every modern family now ships this toggle; **choosing the right side of it is the highest-leverage decision in this whole document.**

3. **"Newer" is not "better," twice in our own data.** `glm-4.7` (67%) > `glm-5` (57%); `grok-4.1-fast` (72%) > `grok-4.20` (45–48%). **Pin model IDs and re-benchmark on version bumps** — never roll forward blind.

4. **Chat-template fidelity is a silent correctness tax on every open-weight model.** ChatML for Qwen, `[gMASK]<sop>…<|assistant|>` for GLM, fullwidth-bracket sentinels for DeepSeek, `[INST]` with version-dependent whitespace for Mistral. A hand-rolled template that's off by one space or one EOS token underperforms the published benchmark — for reasons that look like "model is dumb" but are really "prompt is malformed."

5. **Eval prompting and production prompting diverge by design.** Benchmark harnesses use max reasoning budget, multi-sample averaging, greedy or fixed sampling, and answer-extraction formats (`\boxed{}`). Production wants lowest-variance-acceptable single calls, persona suppression, constrained output schemas, and a *tool-loop-aware token budget*. Porting an eval prompt verbatim into production reliably underperforms.

---

## Part 1 — Leaderboard Assessment (the baseline)

The top 25 from `leaderboard_combined.csv`, grouped by foundational family. **Pass** = cross-family pass rate (primary metric); **Rubric** = 0–1 teaching-quality (Anthropic-Haiku judge).

| Family (count in top 25) | Models (rank · pass% · rubric) |
|---|---|
| **Anthropic Claude** (3) | opus-4-7 (1 · 90 · .88) · haiku-4-5 (2 · 82 · .86) · sonnet-4-6 (3 · 78 · .82) |
| **Alibaba Qwen** (6) | qwen3-coder-480b (5 · 68 · .76) · qwen3-next-80b-instruct (8 · 65 · .71) · qwen3-235b-instruct (9 · 63 · .74) · qwen2.5:14b (15 · 55 · .66) · qwen2.5:7b (17 · 52 · .71) · qwen2.5:3b (22 · 45 · .61) |
| **xAI Grok** (4) | grok-4.1-fast-reasoning (4 · 72 · .80) · grok-4.1-fast-non-reasoning (13 · 57 · .72) · grok-4.20-non-reasoning (19 · 48 · .64) · grok-4.20-reasoning (21 · 45 · .65) |
| **Google Gemini** (4) | gemini-2.5-flash (7 · 65 · .74) · gemini-3.1-pro (11 · 58 · .71) · gemini-3.5-flash (18 · 50 · .66) · gemini-2.5-pro (23 · 43\* · .64) |
| **Zhipu GLM** (3) | glm-4.7 (6 · 67 · .75) · glm-5 (12 · 57 · .73) · glm4:9b (24 · 43 · .60) |
| **DeepSeek** (2) | deepseek-v3.2 (10 · 58 · .71) · deepseek-v3.1 (20 · 45 · .62) |
| **Moonshot Kimi** (1) | kimi-k2-thinking (14 · 57 · .68) |
| **Mistral** (1) | mistral-nemo:12b (16 · 53 · .67) |
| **IBM Granite** (1) | granite3.1-dense:8b (25 · 33 · .56) |

\* `gemini-2.5-pro` is flagged **suspect** in FINDINGS — Pro scoring *below* Flash points to a thinking-mode/empty-response harness interaction, not true capability. Treat as an eval artifact pending diagnosis (a worked example of Part 4's thesis).

### What the grouping tells us
- **Frontier ceiling is Claude** (90/82/78). Everything below rank 3 is the open-weight + challenger field.
- **The "best non-Anthropic model in the entire 44-model benchmark" is `grok-4.1-fast-reasoning` at 72%** — ahead of every Gemini, Qwen, GLM, and DeepSeek.
- **Qwen is the depth pick:** six entries spanning a 480B MoE down to a **3B that runs on a phone (45%)**. For our offline/low-connectivity mandate (Mozambique, Tanzania), Qwen2.5 is the on-device family.
- **Conspicuous absences worth stating to stakeholders:** **Llama is not top-25** (Meta's best, `llama3.1:8b`, is rank 26 at 33%); **GPT/OpenAI does not appear in the benchmark at all** (not evaluated). If a roadmap doc claims "we benchmarked the Llama family as a leading option," our own data refutes it.

### Families this framework deep-dives
Claude · Grok · Qwen · GLM · Gemini · DeepSeek · Kimi · Mistral, plus a short note on IBM Granite and the absent Llama. Two of these (Claude, Gemini) have authoritative in-repo skills; the other six are researched against official docs with citations.

---

## Part 2 — Cross-Cutting Foundations (apply before any family-specific tuning)

These are the universal levers (from `prompting-fundamentals-expert`); the family sections only cover **deltas** from these defaults.

### 2.1 The master variable: thinking vs instruct
Every 2025–2026 frontier family ships a **hybrid reasoning toggle**. It is not cosmetic — it changes the chat template, the token budget, and which prompt techniques help vs hurt.

| | **Instruct / non-thinking** | **Thinking / reasoning** |
|---|---|---|
| Best for | tool loops, extraction, classification, high-QPS, latency-sensitive chat | multi-step math, planning, hard single-shot reasoning |
| CoT scaffolding ("think step by step") | **helps** | **redundant / harmful** — competes with the internal trace |
| Few-shot exemplars | **helps** (format-lock) | often **hurts** (constrains the reasoning trace) |
| Token budget risk | low | **high** — reasoning can exhaust `max_tokens` before the answer/tool-call |
| Our tutor-loop result | **63–65%** (Qwen3 instruct) | **2–22%** (Qwen3-thinking, deepseek-r1) |

> **Codebase rule this reinforces:** the production tutor is a tool-loop (model poses question → grades → advances via tool-calls). **Default to instruct/non-thinking variants** for the tutor seat. Reserve thinking mode for offline, non-tool, single-shot reasoning (e.g. a content-generation or hard-grading pass) — and size `max_tokens` for the reasoning trace if you do.

### 2.2 Chat-template fidelity (open-weight only)
For any locally-served model, **use the model's bundled template via `tokenizer.apply_chat_template()` (or the serving framework's loader) — never hand-concatenate role strings.** Template drift (wrong whitespace, missing assistant-generation prompt, wrong EOS token) silently degrades instruction-following and can cause runaway generation. This is the #1 cause of "the open model scores far below its benchmark."

### 2.3 Query-last for long context
Documents first, instruction/question last — Anthropic measures ~30% improvement on multi-doc inputs; Gemini and xAI give the same guidance. Anchor the trailing question ("Based on the entire document above…"). Repeat critical instructions at the bottom if the prompt is long.

### 2.4 Constrained decoding > prompted JSON
Forcing strict JSON *in prose* drops reasoning accuracy 10–15% ([Tam et al.](https://arxiv.org/html/2501.10868v1)). Use the provider's schema API at decode time — Anthropic `output_config.format`, OpenAI Structured Outputs, Gemini `response_schema`, Grok/xAI `response_format.json_schema`, GLM `response_format:{type:json_object}`. For genuine reasoning tasks, reason in prose first, *then* emit the schema'd block.

### 2.5 Anti-patterns that cost us cycles
- Threats / bribes / all-caps `CRITICAL` — don't replicate, and **over-trigger** modern Claude.
- Negative-only instructions ("don't be verbose", "do not guess") — weak everywhere, and **actively harmful on Gemini 3** (over-indexes, breaks arithmetic). Use positive framing with quantified limits.
- Single-trial "this prompt is better" claims — formatting noise (up to 76-pt swings, [Sclar et al.](https://arxiv.org/abs/2310.11324)) often exceeds the effect. **Hold-out evals first.**

---

## Part 3 — Per-Family Deep Dives

Each family covers the three required axes: **Structural Preferences & Syntax**, **Behavioral Nuances**, **Hyperparameter Tuning**. Family-specific eval-vs-production notes roll up into Part 4.

---

### 3.1 Anthropic Claude — ranks 1, 2, 3 (the ceiling)
*Models in benchmark: `claude-opus-4-7`, `claude-haiku-4-5`, `claude-sonnet-4-6`. Our production tutor + the judge cascade's tier-3 (`judge_fallback_2`) run on Claude.*

**Structural Preferences & Syntax.**
- **XML tags are Claude's native structural device** — `<instructions>`, `<context>`, `<example>`, `<input>`, `<output_format>`. They outperform Markdown headings for complex prompts; tag names carry semantic weight (`<untrusted_input>`, `<sensitive_data>`). Pick a vocabulary and reuse it; don't mix XML and Markdown structure randomly.
- **System vs user split:** stable role/tone/policy/format → `system`; per-turn task content → `user`. Keep `system` immutable per conversation so **prompt caching** holds (cache hits = 0.1× input cost; verify via `usage.cache_read_input_tokens`). Never put timestamps/user-IDs in `system` — it busts the cache.
- **Documents first, query last** (~30% gain on multi-doc). Use the `<documents><document index><source><document_content>` nesting; add a quote-then-answer step (`<quotes>` → `<answer>`) to cut hallucination.
- **Structured output:** `output_config.format` with a JSON schema (constrained decoding, guaranteed shape). **Assistant-prefill is gone on 4.6+ (returns 400)** — do not prefill `{` to force JSON; migrate to `output_config.format`. ([structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs))

**Behavioral Nuances.**
- **Literal instruction-following on Opus 4.7.** No silent "above and beyond" — if you want thoroughness, ask for it explicitly. This is why our prompts must *spell out* desired tutor behaviors rather than gesture at them.
- **`CRITICAL:`/`MUST`/`NEVER` shouting over-triggers on 4.5+** — dial down to neutral imperatives. (Directly relevant: our tutor system prompt should avoid emphatic caps stacking.)
- **Adaptive thinking** (`thinking:{type:adaptive}` + `output_config.effort`) replaces manual `budget_tokens` on 4.6+. Auto-skips on easy turns; set a low `effort` floor for hot chat paths. On Opus 4.7, **tool use decreased by default** in favor of reasoning — raise `effort` to `high`/`xhigh` if you want more tool calls (note for the tutor's tool-loop). Pass thinking blocks back across tool turns.
- Low sycophancy relative to the field; strong at the persona/tone work that is our #1 failure category (`persona_handling`).

**Hyperparameter Tuning.**
- **Codebase invariants override stored values** (`ModelConfig.effective_temperature`): **JUDGE purpose = 0 always**; **TUTORING clamped to [0.1, 0.3]**; REGEN ensemble 0.20→0.15 (2 cycles). Do not bypass these at the call site.
- Prefer `effort` + structured outputs over temperature gymnastics. For deterministic extraction, temp 0 + schema. For tutoring warmth, the [0.1, 0.3] clamp is the ceiling — that is deliberate.

---

### 3.2 xAI Grok — ranks 4, 13, 19, 21 (best non-Anthropic model)
*`grok-4.1-fast-reasoning` (72%) is the top non-Claude model in the benchmark. Note: reasoning **helped** 4.1-fast (72 vs 57) but **hurt** 4.20 (45 vs 48) — variant-test, don't assume.*

**Structural Preferences & Syntax.**
- **OpenAI-SDK-compatible** — point the OpenAI client at `https://api.x.ai/v1` ([quickstart](https://docs.x.ai/developers/quickstart)). Roles: `system`/`user`/`assistant`/`tool`. **Exactly one system message, and it must be first** ([chat guide](https://docs.x.ai/docs/guides/chat)).
- **xAI explicitly recommends labeled markup — XML tags or Markdown headers — to delimit tasks/constraints/context** ([guide](https://blog.promptlayer.com/xais-prompt-engineering-guide-for-grok-code-fast-1/)). JSON belongs in the *output contract*, not the input.
- **Reasoning is steered at request time**, not by separate weights: current `reasoning_effort` = `none|low|medium|high` on grok-4.x; the older split exposed `…-reasoning`/`…-non-reasoning` IDs ([reasoning docs](https://docs.x.ai/developers/model-capabilities/text/reasoning), [grok-4-fast](https://x.ai/news/grok-4-fast)). For reasoning mode, **do not add "think step by step"** (redundant, can hurt); for `none`/non-reasoning, classic few-shot + explicit structure help.
- **Structured Outputs** (`response_format.json_schema`) guarantee schema match; tool-arg schemas are **always strict** ([structured outputs](https://docs.x.ai/docs/guides/structured-outputs)). Context windows are ID-specific (grok-4 256K; grok-4-fast/4.1 up to 2M; grok-4.20/4.3 1M) — **verify per ID** ([models](https://docs.x.ai/developers/models)).

**Behavioral Nuances.**
- **Sycophancy regression is the top production caveat.** xAI's own 4.1 card reports sycophancy ≈ 0.19–0.23 vs 0.07 for Grok 4 (higher = worse) — the 4.1 update deliberately pushed personality/EQ ([model card](https://data.x.ai/2025-11-17-grok-4-1-model-card.pdf)). **For neutral tutoring/extraction, suppress the persona in the leading system message**: declare a neutral subject tutor (not "Grok"), forbid editorializing/jokes, and **instruct it to prioritize correctness over agreement and to correct the user when wrong.** xAI open-sources its consumer prompts ([grok-prompts](https://github.com/xai-org/grok-prompts)) so you know what you're overriding (the API does not apply them, but the trained-in personality persists).
- **Hallucination dropped sharply in 4.1** (non-reasoning 12.09%→4.22%) — favorable for grounding facts, but still ground domain content with retrieval, not parametric recall.
- **Native web/X grounding via server-side tools** (`web_search`, `x_search`; legacy `search_parameters` retired 2026-01-12). **For a closed-curriculum tutor, disable these tools** or Grok will reach outside your KB ([web search](https://docs.x.ai/developers/tools/web-search)).

**Hyperparameter Tuning.** ([API ref](https://docs.x.ai/docs/api-reference))
- `temperature` 0–2 (no published default — **set it explicitly**); standard "tune temp **or** top_p, not both."
- **`frequency_penalty`/`presence_penalty`/`stop` are rejected (error, not ignored) on reasoning variants.** `logprobs` unsupported on grok-4.20+.
- **Deterministic extraction:** temp 0–0.1, `seed` set, output bound to a strict JSON schema; on grok-4.3 add `reasoning_effort:"none"`.
- **Fluid tutoring:** temp ~0.5–0.8 + `reasoning_effort:"medium"/"high"` for hard items. **Caveat:** xAI does *not* document "reasoning ignores temperature" (unlike OpenAI) — treat that as unverified and validate per ID.

---

### 3.3 Alibaba Qwen — 6 of the top 25 (the depth + on-device family)
*Spans `qwen3-coder-480b` (68%) → `qwen2.5:7b` (best OSS rubric, 0.71) → `qwen2.5:3b` (phone tier, 45%). **`qwen3-next-80b-thinking` scored 2%** — the clearest example of the thinking-mode trap.*

**Structural Preferences & Syntax.**
- **ChatML** across the whole family: `<|im_start|>{role}\n{content}<|im_end|>`, generation triggered by trailing `<|im_start|>assistant\n` (`add_generation_prompt=True`). `<|im_end|>` is a single special token (EOS id 151645). **Template fidelity is critical** ([concepts](https://qwen.readthedocs.io/en/latest/getting_started/concepts.html)) — use `apply_chat_template()`, don't string-build.
- **Thinking control (Qwen3):** the `enable_thinking` flag (hard switch), plus `/think` and `/no_think` soft switches honored per-turn (most-recent wins), plus `thinking_budget` on DashScope ([Qwen3 blog](https://qwenlm.github.io/blog/qwen3/)). **The 2507 refreshes split the hybrid into dedicated checkpoints** — `…-Instruct-2507` is non-thinking only, `…-Thinking-2507` thinking only. **Pick the checkpoint that matches the seat.** Parse thinking output by the closing token (id 151668) — the open `<think>` may be absent.
- **Qwen3 removed the default system message** — supply an explicit one or you get zero steering (Qwen2.5 shipped a default). Prefer **Markdown** structure; no Qwen-specific XML convention. **Strip prior `<think>…</think>` from history** before re-sending (except in-turn multi-step tool calls).
- Context: Qwen2.5 dense 32K (→128K via opt-in YaRN, which *hurts* short prompts — don't enable globally); Qwen3-2507/Next 256K→1M. Query-last.

**Behavioral Nuances.**
- Strong instruction-following and **119-language** coverage (Qwen3; 29 for Qwen2.5) — relevant to the pt-MZ pilot. **Language-mixing risk under high `presence_penalty`**: keep it low and pin the output language in the system prompt for strict single-language output.
- **CoT/few-shot split:** dense Qwen2.5 and Qwen3 *non-thinking* benefit from CoT + few-shot; **thinking checkpoints should get only an answer-format constraint, not reasoning scaffolding** (official guidance pairs "reason step by step" *only* with `\boxed{}`-style output locks).
- **Tool use:** strong; official path is **Qwen-Agent** (encapsulates tool templates/parsers). vLLM: `--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3`. `qwen3-coder-480b` has a bespoke function-call format + Qwen Code/CLINE support, "comparable to Claude Sonnet on agentic coding."

**Hyperparameter Tuning (official, mode-specific).** ([235B card](https://huggingface.co/Qwen/Qwen3-235B-A22B), [8B card](https://huggingface.co/Qwen/Qwen3-8B))

| | Thinking | Non-thinking / Instruct |
|---|---|---|
| temperature | **0.6** | **0.7** |
| top_p | **0.95** | **0.8** |
| top_k | 20 | 20 |
| Qwen2.5 dense | — | temp 0.7 / top_p 0.8 / top_k 20 / **repetition_penalty 1.05** |

- **"DO NOT use greedy decoding" for thinking models** — causes endless repetition. Never `temperature=0` on a thinking checkpoint; constrain the *output* instead for determinism.
- **Deterministic extraction:** lower temp toward 0 only on **non-thinking/Qwen2.5**; pair with a strict schema + fixed `seed`.
- **Repetition:** raise `presence_penalty` toward 1.5 (watch for language mixing); Qwen2.5 uses `repetition_penalty` instead.
- **Output budget is a real hyperparameter for thinking mode:** size `max_tokens` to ≥32K (thinking) / 16K (non-thinking) or the trace truncates — **exactly the failure that scored `qwen3-next-80b-thinking` at 2% on our tool-loop.**

---

### 3.4 Zhipu GLM — ranks 6, 12, 24
*`glm-4.7` (67%) beats `glm-5` (57%) — newer ≠ better. `glm4:9b` (43%) is a viable local pick once tool-call parsing is wired (FINDINGS confirms it recovered after the engine parsed its format).*

**Structural Preferences & Syntax.**
- Template: `[gMASK]<sop>` prefix, then `<|system|>`/`<|user|>`/`<|assistant|>`/`<|observation|>` role markers; reasoning in `<think>…</think>` ([chat_template.jinja](https://huggingface.co/zai-org/GLM-4.6/resolve/main/chat_template.jinja)). **Tools are injected into a `<|system|>` block before your system content**, as JSON in `<tools>`; tool *calls* use a bespoke `<tool_call>…<arg_key>…<arg_value>…` format; results return under `<|observation|>`.
- **Hybrid reasoning toggle differs by layer:** Z.ai API uses the **object** form `thinking:{type:"enabled"|"disabled"}` (default enabled) — the flat `enable_thinking:bool` form **silently fails** against Z.ai ([params](https://docs.z.ai/guides/overview/concept-param), [bug](https://github.com/badlogic/pi-mono/issues/2025)); self-hosted uses `chat_template_kwargs:{enable_thinking:False}`; `/nothink` is the per-message soft switch. `clear_thinking:false` (SGLang) preserves reasoning across agent turns.
- Model is tuned around **XML-style tags** for its own I/O, so XML/Markdown delimiters align with training. `response_format:{type:"json_object"}` for JSON mode. Context: **GLM-4.6 = 200K** (4.5 = 128K); local GLM-4-9B 128K (or 1M `-Chat-1M` variant).

**Behavioral Nuances.**
- Positioned as an **"ARC" (Agentic/Reasoning/Coding)** family — rewards **agentic framings** (task decomposition, multi-tool plans) over verbose persona priming. Strong tool reliability; explicit harness compatibility (Claude Code, Cline, Roo). No official sycophancy figure — treat like any strong instruct model.
- **Template strips prior turns' reasoning to empty `<think></think>`** — multi-turn agents must re-surface needed state in visible content/tool outputs, not rely on the model re-reading old CoT.
- Thinking mode supplies its own CoT — don't hand-write "step by step"; specify the answer contract. Few-shot most useful in **non-thinking** mode.

**Hyperparameter Tuning.** ([chat-completion ref](https://docs.z.ai/api-reference/llm/chat-completion))
- **Temperature **OR** top_p, not both.** API defaults: **GLM-4.6 temp 1.0 / top_p 0.95**; **GLM-4.5 temp 0.6 / top_p 0.95** (temp range capped at **1.0**, unlike OpenAI's 2.0; `top_k` not exposed via API).
- HF card: general eval temp 1.0; **code tasks top_p 0.95 / top_k 40**.
- **Deterministic extraction:** `do_sample=false` (greedy; temp/top_p ignored) or temp ≈0.2, `thinking.type="disabled"`, `response_format=json_object`. **Fluid reasoning:** temp ~0.8 (or 1.0 default) + thinking enabled.
- **No official separate thinking-mode sampling table exists** — do not assume Qwen's numbers transfer. Strip `<think>` from the answer via `reasoning_content` field / `--reasoning-parser glm45`, don't regex.

---

### 3.5 Google Gemini — ranks 7, 11, 18, 23
*`gemini-2.5-flash` (65%) is the top Gemini. `gemini-2.5-pro` (43%) is flagged **suspect** — Pro<Flash is a thinking-mode harness artifact, the textbook Part-4 cautionary tale. Gemini is also our primary judge tier (`get_judge_provider_chain('judge')`).*

**Structural Preferences & Syntax.**
- **`system_instruction` is a separate top-level parameter**, not a `contents` message — and it's **per-call**, re-sent every `generateContent`. Don't treat it as sticky server state.
- **Native multimodal: image FIRST, text AFTER** for single-image tasks; interleave labeled parts for multi-image. Don't pre-OCR — let Gemini see the figure (directly relevant to our `figure_vision` judge path).
- **Few-shot is recommended by default** ("prompts without few-shot are likely less effective"); 2–5 varied examples, **consistent formatting** (Gemini copies punctuation quirks).
- **1M-token window** — prefer it over RAG when the corpus fits; **query at the end, anchored** ("Based on the entire document above…"). Enable **context caching** (~4× cheaper).
- **Structured output:** `response_mime_type="application/json"` + `response_schema` (Pydantic accepted). **Thinking:** Gemini 3 uses `thinkingLevel` (`minimal|low|medium|high`) — **`thinkingBudget` is backwards-compat only on Gemini 3 and warns "unexpected performance."** Pin thinking to `low` for hot chat paths.

**Behavioral Nuances.**
- **Gemini 3 dislikes flowery prompts** — direct task statements beat persona priming ("you are the world's foremost…"). **Negative instructions over-index** ("do not guess" → breaks arithmetic/logic) — use positive framing with explicit fallbacks ("if the document doesn't say, reply 'Not stated.'").
- **Don't tune temperature on Gemini 3** — docs say keep default 1.0; varying hurts. (This is the opposite reflex from most families.)
- **Pro-below-Flash anomaly:** empty responses under thinking-mode in id-probing produced our suspect 43%. Lesson: Gemini Pro + thinking + a tool harness needs explicit diagnosis before trusting the score — same playbook that recovered GLM-4.

**Hyperparameter Tuning.**
- **Gemini 3: leave temperature at default 1.0.** Use `thinkingLevel`, not `thinkingBudget`. Grounding (`google_search` tool) only for time-sensitive factual queries — **off** for our closed-curriculum tutor.
- Gemini 2.5: `thinkingBudget` (Flash 0–24576, 0 disables; Pro 128–32768; −1 dynamic). Pin to `low`/0 for latency paths. **JUDGE-purpose temp is 0 in our codebase regardless.**

---

### 3.6 DeepSeek — ranks 10, 20 (and R1 at rank 30, a cautionary tale)
*`deepseek-v3.2` (58%, strongest open-weight in FINDINGS) > `deepseek-v3.1` (45%). **`deepseek-r1` scored 22%** — reasoning-only + tool-calling documented as non-thinking-only = poor tool-loop fit.*

**Structural Preferences & Syntax.**
- Two endpoints, one hybrid checkpoint (V3.1+): **`deepseek-chat`** (non-thinking) and **`deepseek-reasoner`** (thinking) ([V3.1 release](https://api-docs.deepseek.com/news/news250821)). Toggle is in the chat template: assistant turn opened with `<｜Assistant｜><think>` (think) vs `<｜Assistant｜></think>` (pre-closed, direct answer). API: `extra_body={"chat_template_kwargs":{"thinking":False}}`. Role sentinels are **fullwidth-bracket** (`<｜User｜>`, `<｜Assistant｜>`) — do not substitute ASCII `<|...|>`.
- **R1-specific official rules (most teams get these wrong):** (1) **no system prompt — put all instructions in the user turn**; (2) **no few-shot — it "consistently degrades" R1, use zero-shot**; (3) **force the response to begin with `<think>\n`** or R1 may skip reasoning ([R1 card](https://huggingface.co/deepseek-ai/DeepSeek-R1), [tech report](https://arxiv.org/html/2501.12948v1)). `deepseek-chat` (V3) takes a normal system prompt.
- Context 128K (V3.1/V3.2); reasoner output cap **includes** the CoT. Query-last.

**Behavioral Nuances.**
- **R1 < V3 on function-calling, multi-turn, JSON output** (stated in the report) — for structured/agentic workloads prefer V3/`deepseek-chat`. **Function calling is unsupported on `deepseek-reasoner`** (original-R1 semantics); V3.1 added Strict Function Calling (Beta). **Never echo `reasoning_content` back into the next request — returns 400.**
- **Language mixing** (R1 optimized for EN/ZH; may mix on other languages despite a consistency reward that costs a little accuracy) — pin output language explicitly; detect on `content`, not `reasoning_content`.
- R1 is explicitly **"sensitive to prompts"** — keep minimal, literal, zero-shot.

**Hyperparameter Tuning.** ([param settings](https://api-docs.deepseek.com/quick_start/parameter_settings))
- **`deepseek-chat` use-case temp table:** coding/math **0.0**, data analysis **1.0**, general conversation **1.3**, translation **1.3**, creative **1.5** (default 1.0, range 0–2).
- **`deepseek-reasoner` ignores temp/top_p/penalties** (no error, no effect). Self-hosted R1: **temp 0.5–0.7 (0.6 ideal), top_p 0.95; never temp 0** (endless repetition). Eval sampling = 0.6/0.95.
- **Unverified:** the popular "API applies T×0.3 transformation" is **not** on the official page (community-reported); trust the use-case table, don't assume a formula.

---

### 3.7 Moonshot Kimi — rank 14 (agentic, the one thinking model that worked here)
*`kimi-k2-thinking` (57%) — notable: a thinking model that scored well on our tool-loop because its **structured tool-calls drove the tutor cleanly**. The exception that proves the rule (its tool-integrated reasoning didn't starve the tool call).*

**Structural Preferences & Syntax.**
- 1T-total/**32B-active** MoE — latency closer to a ~30B model than its headline; relevant to multi-call judge/regen budgeting. **OpenAI- and Anthropic-compatible** API ([README](https://github.com/MoonshotAI/Kimi-K2)) — standard `system/user/assistant/tool` role array; **honors a real system role** (unlike Mistral), so put persona/safety/scope there.
- **K2-Thinking** emits CoT into a **separate `reasoning_content` field**. **Critical and counter-intuitive: KEEP every historical assistant turn's `reasoning_content` as-is across multi-turn / tool loops** — dropping it degrades the interleaved tool-call chain (the *opposite* of OpenAI/Qwen "strip the CoT" guidance) ([thinking docs](https://platform.kimi.ai/docs/guide/use-kimi-k2-thinking-model)). Set **`max_tokens ≥ 16000`** (reasoning + answer share the budget). Context: K2 128K → K2-Instruct-0905 / K2-Thinking **256K**.

**Behavioral Nuances.**
- **Agentic positioning is the headline** — K2-Thinking maintains coherent behavior across **200–300 consecutive tool calls** (SWE-bench Verified 65.8→71.6 Instruct; 71.3 Thinking). This is *why* it survived our tool-loop where other reasoning models died.
- No official sycophancy figure (unverified). Instruct variants follow system directives well. **K2-Thinking: no manual CoT scaffolding**; K2-Instruct takes standard few-shot/CoT.
- Anthropic-endpoint quirk: `real_temp = request_temp × 0.6`. K2-Thinking ships **native INT4 (QAT)** — INT4 is the intended operating point, not a degraded fallback.

**Hyperparameter Tuning.** ([Thinking card](https://huggingface.co/moonshotai/Kimi-K2-Thinking))
- **K2-Instruct:** temp **0.6** (lower toward 0–0.2 for extraction). **K2-Thinking:** temp **1.0**, top_p **0.95**, `max_tokens ≥ 16000` — **do not lower the 1.0** (it's tuned to avoid degenerate reasoning; hosted thinking endpoints forbid changing temp). No official penalty guidance — leave at 0; fix loops with the documented high-temp setting, not penalties.

---

### 3.8 Mistral — rank 16 (the lone non-Chinese OSS in the top 20)
*`mistral-nemo:12b` (53%) beats `llama3.1:8b` (33%) by 20 pts — FINDINGS' "family matters more than size." `mistral:7b` is rank 27.*

**Structural Preferences & Syntax.**
- **`[INST]…[/INST]` template, no OpenAI-style roles in the raw prompt**, with `<s>`/`</s>` BOS/EOS. **Whitespace is load-bearing and version-dependent** (V1 spaced, V2/V3 tight, Tekken no leading space) — "the whitespaces are of extreme importance" ([cookbook](https://docs.mistral.ai/resources/cookbooks/concept-deep-dive-tokenization-chat_templates)). **Use `mistral_common`, never a hand-rolled string** — it tokenizes `request → int` directly, whereas HF `transformers` goes `request → str → int` (the intermediate string is where whitespace bugs creep in), and the two have historically disagreed; if you must use `apply_chat_template`, verify its output byte-for-byte against the `mistral_common` reference for your exact model ([mistral-common](https://github.com/mistralai/mistral-common)). Native function calling exists only in **7B v0.3 and Nemo** — not Mixtral 8x7B / 7B v0.2.
- **No dedicated system role — and placement is the #1 "my system prompt is ignored" bug:** V1 prepends system to the **first** user message; **V2/V3/Tekken prepend it to the *last* user message.** Pass a `system` role and let the tokenizer route it.
- **Tokenizer must match the model:** Mistral 7B/Mixtral use V3 sentencepiece; **Nemo/Pixtral use Tekken (tiktoken, ~128K vocab, ~30% better compression for code + Portuguese/European languages)** — reusing a 7B string-builder for Nemo injects stray spaces and silently hurts quality. Context: 7B/Mixtral 32K; **Nemo 128K**.

**Behavioral Nuances.**
- Standard instruct models (no reasoning channel) — **CoT and few-shot both help**; strip any inline CoT post-hoc. Nemo is **trained for function calling**; **tool-call IDs must be exactly 9 alphanumeric chars** and match call↔result, or the turn is malformed. Mistral 7B struggles with **parallel** tool calls (community-reported) — prefer sequential.
- Multilingual strength (incl. Portuguese) + Tekken compression makes Nemo a sensible pt-MZ on-server candidate. No official sycophancy figure.

**Hyperparameter Tuning.**
- **Mistral Nemo's official quirk: "requires smaller temperatures… use 0.3."** It is **more temperature-sensitive** than 7B/Mixtral — the ~0.7 you'd use elsewhere produces incoherence/repetition on Nemo ([Nemo card](https://huggingface.co/mistralai/Mistral-Nemo-Instruct-2407)). **If you swap 7B→Nemo, you must also drop the temperature.**

| Model | Det. extraction | Fluid / chat | Notes |
|---|---|---|---|
| **Nemo 12B** | 0.0–0.3 | **0.3 (cap)** | official 0.3; fix repetition with temp, not penalties |
| Mistral 7B | 0.0–0.2 | ~0.7 | de-facto default |
| Mixtral 8x7B | 0.0–0.2 | ~0.7 | MoE: 2-of-8 experts, ~47B mem / ~13B active |

---

### 3.9 Brief notes — IBM Granite (rank 25) & the absent Llama (rank 26)
- **IBM Granite 3.1-dense:8b (33%)** — enterprise-tuned, solid tool-calling; for our tutor it sits at the top-25 floor. Standard ChatML-ish template via `apply_chat_template`; treat as a non-thinking instruct model (CoT + few-shot help). Worth keeping only if data-residency/licensing pushes toward IBM.
- **Meta Llama — not top-25.** `llama3.1:8b` (33%, rank 26) and `llama3.2:3b` (28%) trail the equivalent-size Qwen/Mistral. Llama uses its own header template (`<|start_header_id|>{role}<|end_header_id|>`); standard instruct prompting. **Stakeholder takeaway: there is no data here supporting Llama as a leading offline tutor family** — Qwen2.5 dominates Llama at every size tier we tested. Revisit only if a future Llama generation re-enters the benchmark.

---

## Part 4 — Evaluation Prompting vs Production Prompting

This is where most of the silent quality loss happens. Eval harnesses and live applications optimize for different things; an eval-tuned prompt ported verbatim into production reliably underperforms. Our own leaderboard supplies the proof points.

### 4.1 How these models are prompted in eval harnesses
- **Answer-extraction formats:** math appends "reason step by step, put the final answer in `\boxed{}`"; MCQ forces a single-letter `answer` field. These exist to let a grader **regex** the result — they are parsing aids, not behavior.
- **Max reasoning budget + multi-sample averaging:** DeepSeek R1 evals generate k=4–64 samples at temp 0.6/top_p 0.95, 32,768 max tokens, and report pass@1 as the *average*. Grok's headline numbers use **high-effort reasoning with large test-time compute**. Qwen evals allow 32K–81,920 output tokens so the trace isn't truncated.
- **Strict templates, zero/few-shot per the harness convention** (EleutherAI-style), single well-formed task per prompt, **sparse or no system prompt**, no persona.
- **Sampling reproducibility via fixed seeds / greedy** where the benchmark wants determinism.

### 4.2 Why eval-style prompts underperform or fail in production
1. **Eval ignores the token-budget interaction with tool loops — production cannot.** This is the single biggest finding in our data: `qwen3-next-80b-thinking` (2%) and `deepseek-r1` (22%) *have the capability* (their instruct siblings score 63–65%), but in a tool-driven loop the reasoning trace **exhausts the generation budget before the tool call is emitted.** An eval that grades a single final answer never sees this; our production tutor lives or dies by it. **Production rule: default to instruct/non-thinking for tool seats; if you must use thinking, size `max_tokens` for trace + answer + tool call.**
2. **Eval uses max reasoning; production must budget latency/cost.** Grok-4-fast matches benchmark quality with ~40% fewer thinking tokens — in production, dial `reasoning_effort`/`thinkingLevel` to the *lowest level that passes your own evals*, not the benchmark maximum.
3. **Eval has no adversarial persona surface; production does.** Benchmarks run sparse system prompts, so they never expose Grok's 4.1 sycophancy regression or its default personality. Production tutoring/extraction must **explicitly suppress persona and forbid agreement-seeking** — behavior the benchmark number doesn't reflect.
4. **Eval format constraints ≠ production output contracts.** `\boxed{}` is a grader hack. Production needs a real downstream schema enforced by **constrained decoding** (Structured Outputs / `output_config.format` / `response_schema`), and for reasoning-weak models (R1) you route JSON/tool work to the non-reasoning sibling.
5. **Multi-sample averaging is an eval methodology, not a serving pattern.** "Run k times and average" estimates expected quality; production serves one call. If you need that robustness, do *bounded* self-consistency on specific high-stakes calls — not globally (cost-prohibitive).
6. **History hygiene flips between families.** Eval is single-turn; multi-turn production must **strip prior `<think>` for Qwen/GLM/DeepSeek** — but **preserve `reasoning_content` for Kimi K2-Thinking.** A one-size pipeline corrupts one of them.
7. **Template/tokenizer drift is invisible in a well-built harness, common in a hand-built service.** Eval harnesses call `apply_chat_template`; production prompt-builders frequently mis-place Mistral's system block or mismatch Qwen's whitespace — and silently score below benchmark.
8. **"Newer ≠ better" and "reasoning helps" are not transferable assumptions.** Our data: `glm-4.7` > `glm-5`; reasoning helped grok-4.1-fast but hurt grok-4.20. **Re-benchmark every version bump and every reasoning-mode flip on the held-out set.** (See also the `gemini-2.5-pro` suspect row — a thinking-mode × harness artifact that looks like a capability regression.)

### 4.3 The production-resilience checklist (any family)
- [ ] **Pin the exact model ID** — context window, supported params, and personality differ across point-versions.
- [ ] **Choose instruct vs thinking deliberately**; for tool loops, default instruct.
- [ ] **Use the bundled chat template** (open weights) / correct tokenizer version.
- [ ] **Constrain output via the schema API**, not prose JSON.
- [ ] **Suppress persona** for neutral seats (esp. Grok).
- [ ] **Set sampling explicitly** (don't inherit a framework default); honor mode-specific values (never greedy on Qwen/DeepSeek thinking; Nemo ≤0.3; Gemini 3 leave 1.0).
- [ ] **Size `max_tokens`** for the reasoning trace if thinking is on.
- [ ] **Get history hygiene right per family** (strip vs preserve CoT).
- [ ] **Hold-out eval before/after** — single-trial deltas are noise.

---

## Part 5 — Application to the AI Tutor codebase

Mapping the above onto our actual seats (`apps/llm/`, `apps/tutoring/`):

- **Tutor seat (the tool-loop engine).** Default to **instruct/non-thinking** variants. Offline/on-device: **`qwen2.5:7b`** (best OSS rubric, ChatML, temp 0.7/top_p 0.8) on a school server; **`qwen2.5:3b`** on phone/tablet. Cloud challenger: **`grok-4.1-fast-reasoning`** *only because reasoning helped it here* (72%) — but suppress its persona and disable web/x_search. **Do not deploy pure-thinking variants in the tutor seat** without re-architecting the token budget.
- **Judge cascade** (`get_judge_provider_chain('judge')`: Gemini → OpenAI → Haiku 4.5). **JUDGE temp is 0 everywhere** (codebase invariant). For the Gemini tier: `system_instruction` separate param, pin `thinkingLevel` low, leave temp default, `response_schema` for the 10-axis output. For the Haiku tier: XML-tagged rubric, `output_config.format`.
- **Vision/figure paths** (`figure_vision`, `figure_ref`). On Gemini, **image-first then text**; don't pre-OCR. Ask for structured extraction, not "describe this."
- **Content generation** (`content_generator.py`). The single-quoted-dict robustness issue is exactly the "prompted JSON is brittle" problem — prefer the provider schema API where the chosen model supports it; keep `_try_fix_json` as the fallback.
- **pt-MZ localization.** For multilingual tutor candidates, **pin output language in the system prompt** (Qwen/DeepSeek language-mixing risk under aggressive sampling). **Mistral Nemo's Tekken tokenizer compresses Portuguese ~30% better** — a cost/latency argument if Nemo is a server candidate, but pin its temp to **0.3**.

---

## Appendix A — Quick-Reference Matrix

| Family (top model) | Template / API | Reasoning toggle | Det. extraction | Fluid/reasoning | The one quirk that bites |
|---|---|---|---|---|---|
| **Claude** (opus-4-7) | XML tags; `system`+`user`; `output_config.format` | adaptive `effort` | temp 0 + schema | clamp [0.1,0.3] (tutor); effort high | prefill removed (400); caps over-trigger; literal-following |
| **Grok** (4.1-fast) | OpenAI-compat; 1 system msg, first | `reasoning_effort none…high` | 0–0.1 + strict schema + seed | 0.5–0.8 + effort med/high | sycophancy 4.1 — **suppress persona**; penalties error on reasoning |
| **Qwen** (3-coder-480b) | ChatML; pick instruct vs thinking checkpoint | `enable_thinking` / `/think` | non-thinking 0–0.2 | think 0.6/0.95; instruct 0.7/0.8 | **never greedy** on thinking; size max_tokens or trace truncates |
| **GLM** (4.7) | `[gMASK]<sop>`; `thinking:{type}` **object** | object param / `/nothink` | do_sample=false / 0.2 | 0.8–1.0 | flat `enable_thinking` silently fails on Z.ai; temp **or** top_p |
| **Gemini** (2.5-flash) | `system_instruction` separate, per-call | `thinkingLevel` (G3) | schema; **leave temp 1.0** (G3) | leave temp 1.0; thinkingLevel | `thinkingBudget` warns on G3; negatives over-index; image-first |
| **DeepSeek** (v3.2) | fullwidth sentinels; chat vs reasoner endpoint | `chat_template_kwargs:{thinking}` | chat 0.0 (code/math) | reasoner self-host 0.6/0.95 | **R1: no system prompt, no few-shot, force `<think>\n`**; no echo `reasoning_content` |
| **Kimi** (k2-thinking) | OpenAI/Anthropic-compat; real system role | separate `reasoning_content` | Instruct 0–0.2 | **Thinking 1.0/0.95, don't lower** | **PRESERVE** historical `reasoning_content`; max_tokens ≥16k |
| **Mistral** (nemo:12b) | `[INST]`; **no system role** | none (instruct only) | 0.0–0.3 | **Nemo 0.3 cap**; 7B ~0.7 | system → **last** `[INST]` (V2/V3); 9-char tool IDs; match tokenizer |

**Universal:** documents first / query last · constrained decoding > prose JSON · hold-out evals before claiming a prompt is better · pin model IDs and re-benchmark version bumps · no threats / no all-caps `CRITICAL` / positive framing over negative.

---

## Appendix B — Key Sources

**In-repo skills (read first):** `prompting-fundamentals-expert`, `claude-prompting-expert`, `gemini-prompting-expert`, `agent-orchestration-expert`.

**Cross-cutting papers:** [CoT — Wei et al.](https://arxiv.org/abs/2201.11903) · [Prompt sensitivity — Sclar et al.](https://arxiv.org/abs/2310.11324) · [Order sensitivity — Lu et al.](https://arxiv.org/abs/2104.08786) · [Structured-output degradation — Tam et al.](https://arxiv.org/html/2501.10868v1).

**Provider docs:**
- Claude — [prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) · [structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) · [extended/adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
- Gemini — [prompting strategies](https://ai.google.dev/gemini-api/docs/prompting-strategies) · [Gemini 3 prompting guide](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/gemini-3-prompting-guide) · [thinking](https://ai.google.dev/gemini-api/docs/thinking)
- Grok — [reasoning](https://docs.x.ai/developers/model-capabilities/text/reasoning) · [structured outputs](https://docs.x.ai/docs/guides/structured-outputs) · [Grok 4.1 model card](https://data.x.ai/2025-11-17-grok-4-1-model-card.pdf) · [grok-prompts](https://github.com/xai-org/grok-prompts)
- Qwen — [Qwen3 blog](https://qwenlm.github.io/blog/qwen3/) · [Qwen3 tech report](https://arxiv.org/abs/2505.09388) · [235B card](https://huggingface.co/Qwen/Qwen3-235B-A22B) · [concepts/templates](https://qwen.readthedocs.io/en/latest/getting_started/concepts.html)
- GLM — [Z.ai params](https://docs.z.ai/guides/overview/concept-param) · [chat-completion ref](https://docs.z.ai/api-reference/llm/chat-completion) · [GLM-4.5 tech report](https://arxiv.org/abs/2508.06471) · [chat_template.jinja](https://huggingface.co/zai-org/GLM-4.6/resolve/main/chat_template.jinja)
- DeepSeek — [param settings](https://api-docs.deepseek.com/quick_start/parameter_settings) · [reasoning model guide](https://api-docs.deepseek.com/guides/reasoning_model) · [R1 card](https://huggingface.co/deepseek-ai/DeepSeek-R1) · [R1 tech report](https://arxiv.org/html/2501.12948v1)
- Kimi — [Kimi-K2 README](https://github.com/MoonshotAI/Kimi-K2) · [K2-Thinking card](https://huggingface.co/moonshotai/Kimi-K2-Thinking) · [thinking-model docs](https://platform.kimi.ai/docs/guide/use-kimi-k2-thinking-model)
- Mistral — [tokenization/chat-template cookbook](https://docs.mistral.ai/resources/cookbooks/concept-deep-dive-tokenization-chat_templates) · [Nemo card](https://huggingface.co/mistralai/Mistral-Nemo-Instruct-2407) · [function calling](https://docs.mistral.ai/capabilities/function_calling/)

**Unverified items flagged in-text:** DeepSeek API "T×0.3" transformation (community-reported, not official) · per-family quantified sycophancy figures (only Grok publishes one) · Grok "reasoning ignores temperature" (undocumented; OpenAI-style assumption) · lost-in-the-middle curves for Qwen/Grok/GLM (no official per-model study — query-last is a carried-over best practice).
