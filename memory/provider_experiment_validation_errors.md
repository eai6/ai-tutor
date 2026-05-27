# Provider Experiment Validation Errors (2026-05-19)

3-cell validation slice of `run_model_experiment`:

    --models opus,gemini-3-pro,gpt-5  --lessons 540  --personas struggler  --max-turns 10

Goal: verify the new provider-aware prompt builders (Phase 1+2 of
task #229) work end-to-end through the experiment harness before
committing to a 36-cell run.

Result: **slice surfaced 5 distinct bugs** — 2 blocking, 1 GPT-5
specific, 2 transient API capacity. Cell 1 (Opus) completed at
max-turns; cells 2 (Gemini 3 Pro) and 3 (GPT-5) errored mid-run.

Full log: `/tmp/exp_validation.log`. Final report:
`memory/deepmind_model_experiment_results.md`.

## A. `GeminiClient.generate_with_tools` missing `tool_choice` kwarg ⛔ BLOCKING

```
TypeError: GeminiClient.generate_with_tools() got an unexpected keyword argument 'tool_choice'
```

- **Count**: 4 SelfRetry failures across cell 2
- **Origin**: tutor engine self-retry calls `generate_with_tools(tool_choice=...)` but Gemini's signature at `apps/llm/client.py:793` doesn't accept it. Only Anthropic's signature accepts `tool_choice` (line 354).
- **Impact**: Every regen attempt on Gemini fails instantly. Gemini lessons can't recover from validator-flagged turns — ships dirty by default.
- **Fix**: Add `tool_choice: dict | str | None = None` kwarg to `GeminiClient.generate_with_tools`. Translate to Gemini's `tool_config.function_calling_config`:
  - `{"type": "tool", "name": "X"}` → `mode="ANY", allowed_function_names=["X"]`
  - `"required"` → `mode="ANY"`
  - `"none"` → `mode="NONE"`
  - `"auto"` / None → `mode="AUTO"` (default)
- **Source**: `apps/llm/client.py:793-855`

## B. `OpenAIClient.generate_with_tools` missing `tool_choice` kwarg ⛔ BLOCKING

```
TypeError: OpenAIClient.generate_with_tools() got an unexpected keyword argument 'tool_choice'
```

- **Count**: 2 SelfRetry failures across cell 3
- **Origin**: same shape as (A) — engine passes `tool_choice`, OpenAI client doesn't accept it.
- **Impact**: same as (A) but for OpenAI variants.
- **Fix**: Add `tool_choice` kwarg to `OpenAIClient.generate_with_tools`. Pass through OpenAI's native `tool_choice` parameter directly — schemas are already compatible:
  - `{"type": "tool", "name": "X"}` → `{"type": "function", "function": {"name": "X"}}`
  - `"required"` / `"none"` / `"auto"` → pass verbatim
- **Source**: `apps/llm/client.py:562-625`

## C. GPT-5 family uses `max_completion_tokens` not `max_tokens` ⛔ BLOCKING for GPT-5+

```
openai.BadRequestError: Error code: 400 - Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.
```

- **Count**: 1 failure on the first GPT-5 call in cell 3 (then cell errored out)
- **Origin**: OpenAI deprecated `max_tokens` for GPT-5 + o-series. Older models (GPT-4o etc.) still accept it.
- **Impact**: GPT-5, GPT-5.2, o3, o4-mini all fail their first call.
- **Fix**: In `OpenAIClient._generate_impl`, detect model name prefix and translate param:
  ```python
  is_new_gen = self.config.model_name.startswith(("gpt-5", "o1", "o3", "o4", "o5"))
  param_name = "max_completion_tokens" if is_new_gen else "max_tokens"
  kwargs[param_name] = max_tokens or self.config.max_tokens
  ```
- **Source**: `apps/llm/client.py` — wherever OpenAIClient passes `max_tokens` to the API
- **Related o-series note**: Also need to strip CoT-inducing prompts for o-series (already noted in `openai-tutor-prompt` skill as a Phase 2b hook).

## D. Gemini 2.5 Flash 503 UNAVAILABLE — environmental

```
google.genai.errors.ServerError: 503 UNAVAILABLE. {'error': {'message': 'This model is currently experiencing high demand...'}}
```

- **Count**: 6 occurrences across all 3 cells (judge calls + Gemini tutor calls)
- **Origin**: Google capacity-shedding on Gemini 2.5 Flash (peak hours).
- **Impact**:
  - Judge calls: handled by `judge_fallback` chain → Haiku 4.5, no functional issue.
  - Tutor call (cell 2): not retried; cell ended with `reason=error → 503`.
- **Fix**:
  1. Add retry-with-backoff for 503/429 in `GeminiClient._generate_impl` and `generate_with_tools` (3 attempts, exponential 1s/2s/4s).
  2. Make tutor-path Gemini failures fall through to a documented cross-vendor fallback (currently no such fallback — tutor doesn't have a `tutor_fallback` ModelConfig the way judges do).
- **Source**: `apps/llm/client.py:700-728` (Gemini `_generate_impl`)

## E. Anthropic Opus 4.7 529 OverloadedError — environmental

```
anthropic.OverloadedError: Error code: 529 - {'type': 'overloaded_error', 'message': 'Overloaded'}
```

- **Count**: 4 SelfRetry cycle failures in cell 1
- **Origin**: Anthropic capacity-shedding.
- **Impact**: Self-retry couldn't run → Opus shipped dirty on those turns (regen unable to clean).
- **Fix**: Add retry-with-backoff for 529 (and 429) in `AnthropicClient` — anthropic-python SDK has built-in retries but the wrapper may not be using them. Verify `max_retries` is set on the client init.
- **Source**: `apps/llm/client.py` `AnthropicClient.__init__` and the `messages.create` call sites

## F. Cell errored out instead of graceful partial-results ⚠️ HARNESS

```
[2/3] gemini-3-pro: reason=error → 503 UNAVAILABLE...
[3/3] gpt-5:         reason=error → 503 UNAVAILABLE...
```

- The harness recorded cells 2+3 as `reason=error` with no turn-level data instead of partial results.
- **Fix**: In `run_model_experiment`, wrap each `simulate_session` call to catch transient API errors (503/529/timeout) and either:
  - Retry the cell once after backoff, OR
  - Record what completed + mark reason=`partial_error` so the data isn't lost.
- **Source**: `apps/tutoring/management/commands/run_model_experiment.py`

## G. `Failed to build Seychelles context block: contains lookup is not supported on this database backend` ℹ️ INFORMATIONAL

- Not blocking — context block is optional. Pollutes the log heavily (fires on every turn).
- **Cause**: Using `__contains` JSON-field lookup on SQLite (local dev) where it's not supported. Works on Postgres (prod).
- **Fix (optional, low priority)**: Either gate the lookup on Postgres backend, OR pre-compute the context once per session and cache it.

## Cell 1 (Opus) — actual completion stats

The only cell that ran to its max-turns cap:

- **10 turns**, terminated by max_turns cap
- **47%** tool-use rate (5 of 10 turns called pose_question or pose_inline_question)
- **3/10** regen-clean-cycle-1 (rest needed regen ≥ 2 or shipped dirty)
- **543s** wall (~9 min — slowed by ~6 Gemini 503s + 4 Anthropic 529s during the cell)
- **Leak detector fired 2×** on Opus saying "B" explicitly — the no-reveal directive (commit `218cfde`) is not strong enough yet on Opus when student is struggling. Worth a follow-up.
- **Handoff judge fired ~3×** on "teaching paragraph that just ends — no invitation" — regen couldn't always fix.

## Priority order

1. **A** + **B** (Gemini/OpenAI `tool_choice`) — blocks all non-Anthropic benchmarking. Must fix before any meaningful Gemini/OpenAI run.
2. **C** (GPT-5 `max_completion_tokens`) — blocks the entire GPT-5 family.
3. **F** (cell-level error handling in harness) — without this, transient errors will keep wasting full cell runs.
4. **D** + **E** (retry-with-backoff on 503/529) — improves reliability across the board.
5. **G** (Seychelles context SQLite lookup) — cosmetic, defer.

## After fixes — recommended retest

Same 3-cell slice with the same flags. If all 3 cells complete cleanly, expand to:
- Plan's agreed 9-model matrix (Opus + 4 Gemini + 4 OpenAI variants)
- Or the existing harness 9-model matrix (Opus/Sonnet/Haiku + Gemini 3.1 Pro/Flash + 2.5 Flash + GPT-5/4o/4o-mini)

Refs: `memory/provider_specific_prompt_system_plan.md`, commits `cd0fcb8` (Phase 1) + `e6743a9` (Phase 2 builders) + `ddc9c36` (subject_code).
