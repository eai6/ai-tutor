# Tutor output-length / latency fix — Jetson offline

## Context

Tutor turns on the Jetson Orin Nano Super take **34–118s**, which is too slow to
test with, let alone deploy to a classroom. Three rounds of measurement today
narrowed the cause, and two earlier hypotheses were wrong and are recorded here
so nobody re-runs them:

- **Not** partial GPU offload — that was real and is now fixed (`num_gpu=99`,
  decode 4.0 → 11.4 tok/s). Already applied, tests green.
- **Not** tool-array churn invalidating the KV cache. A realistic turn sequence
  showed llama.cpp keeps both tool-array prefixes warm: prefill is 20.8s on turn
  1 and **1.5s from turn 2 onward**. The "94× penalty" was a cold-start artifact.
- **It is decode volume.** The tutor generates 115–193 tokens on call 2 of every
  turn, at 11.4 tok/s.

Goal: cut turn latency roughly in half, and cut *perceived* latency much further,
without losing tool compliance or the pedagogy the prompt encodes.

## Measurements (Jetson Orin Nano Super, qwen3.5:4b, Ollama 0.30.7, 2026-07-27)

| quantity | value |
|---|---|
| decode | 11.4 tok/s (100% GPU) |
| prefill | 434 tok/s — 20.8s cold, **1.5s warm** |
| output tokens (n=22) | min 27, **median 89**, p90 175, max 193 |
| call 1 (tool call) | 27–40 tokens ≈ 3s |
| call 2 (prose reply) | 115–193 tokens ≈ **10–17s** |
| system prompt | ~8,000–9,300 tokens |
| `max_tokens` | **3072** on the Jetson profile vs 1024 default (`engine.py:1737`) |

Steady-state turn ≈ 3s prefill + 13–20s decode. The gap to the observed 34–118s
is cold-start prefill (~40s on the first turn of a session, since
`OLLAMA_KEEP_ALIVE` is still 5m) and failed calls that never retry (below).

## Target design

Four workstreams, in value order. Ship and measure each independently — do not
bundle, for the same reason the rung-1 plan gives.

### 1. Instrument the call (do this first)

Ollama already returns `prompt_eval_count/duration` and `eval_count/duration`
on every response; the client throws them away. Add them to the existing
`[OllamaTools] response:` log line in `apps/llm/client.py` (beside the current
`in=`/`out=`), and surface `eval_duration` on `SessionTurn.metadata` alongside
the tool-protocol data.

Every latency number in this plan was reconstructed from wall-clock and manual
`curl` probes. That is not a basis for tuning. CLAUDE.md's "instrument before
splitting" applies directly.

**Est: 0.5 day.**

### 2. Cut call-2 output length — the actual fix

The prompt **already** instructs brevity, emphatically:
`family_prompts.py:251-256` — *"Keep every reply short and calibrated — never
info-dump. Affirm a correct answer in ONE clause… Length is your most common
pacing failure."* The model ignores it. That is characteristic of small Qwen
models with prose-embedded, negatively-framed constraints buried mid-prompt.

Approach:

- **Consult `prompting-fundamentals-expert` before editing any prompt text.**
  Non-negotiable per CLAUDE.md; there is no Qwen-specific skill, so fundamentals
  plus the existing Markdown-variant family prompt is the right pairing.
- Replace the prose brevity paragraph with a **positive, positional, countable**
  constraint placed at the **end** of the dynamic block, closest to the student
  turn — the prompt already exploits recency for the in-flight slot
  (`prompts.py:744-749`), so this follows an established pattern in the file.
- Add a **`num_predict` ceiling on call 2 only** as a backstop, not the mechanism.
  Set it generously (~256) so it never truncates a well-behaved reply; the prompt
  does the real work. Wire it through the existing `max_tokens` parameter on
  `_call_llm` (`engine.py:1737`) rather than a new knob.
- Lower the Jetson profile's `max_tokens` from 3072 toward the 1024 production
  default. 3072 is not being hit, but it inflates the derived `num_ctx`
  (`client.py:1326`) and therefore KV memory.

**Target: median 89 → ~45 tokens, p90 175 → ~80.** That is ~2× on the dominant
cost. Reply quality is the constraint, not the target — a 45-token reply that
drops the teaching sentence is a regression, not a win.

**Est: 1 day**, most of it in the measure/tune loop.

### 3. Fix the un-retried Ollama 500s — **DONE (`138a94b`)**

Shipped as described. `_is_transient_error` (now `engine.py:1655`) walks
`exc.response.status_code` and matches `'500 server error'`. The rest of this
section is kept for the diagnosis it records.

`_is_transient_error` (`engine.py:1627-1648`) reads `exc.status_code`, but
`requests.HTTPError` carries the code on `exc.response.status_code`, and the
message `"500 Server Error: Internal Server Error for url"` matches none of the
substring fallbacks. So **local Ollama 500s are classified permanent and never
retried** — 6 occurred in this session, each silently degrading a turn to the
placeholder path.

Add `response.status_code` to the attribute walk and `'500 server error'` to the
substring list. One-line class of fix; add a unit test with a synthetic
`requests.HTTPError`.

**Est: 1 hour.**

### 4. Stream the reply (the biggest perceived win)

> **Superseded by `memory/offline_streaming_plan.md`** (approved 2026-07-29),
> which expands this workstream into a phased design covering both the kiosk web
> chat and the terminal CLI. Read that file before touching streaming. Two things
> this section got wrong: a turn is **three** serialized local calls offline (the
> `run_safety_judge` at `views.py:1095` falls back to the tutoring model), and
> sentence-level buffering **cannot** be made byte-identical to the batch filter
> pipeline — streaming has to be an advisory preview with a final reconcile.

CLAUDE.md's no-SSE rule is scoped to **Azure Container Apps**, which does not
support chunked streaming. The offline Jetson deployment is not on ACA. Ollama
streams natively (`"stream": true`), and `chat_respond` already imports
`StreamingHttpResponse` (`views.py:1015`).

Streaming does not make a turn faster, but it turns a 15s blank wait into ~2s to
first token — which is what a student actually experiences. On a box this slow
it is worth more than workstreams 2 and 3 combined.

Gate it on a `TUTOR_STREAMING=1` env var, default off, so the ACA production path
is byte-identical. Only call 2's prose is streamable — call 1 is a tool call
whose result the engine must dispatch before any text is valid.

**Est: 1–2 days.** Real complexity: the engine post-processes call 2's text
(`_scrub_engine_vocab`, `_dedupe_reply`, `_align_reply_polarity`,
`_filter_reveals`, media-signal stripping) before it is safe to show. Streaming
raw tokens bypasses every one of those. Likely needs sentence-level buffering so
each completed sentence passes the filters before release.

## Out of scope

- Swapping to `qwen3.5:2b` — a separate benchmark (needs a ~2.7 GB pull) and
  must measure tool-call rate, not just speed. Worth doing *after* 1–3, so the
  comparison is against a tuned baseline.
- The distilled Jetson system prompt. It helps the ~40s cold first turn and KV
  headroom, but barely moves steady state now that prefill is warm-cached.
  Revisit after instrumentation quantifies the cold path.
- `ConversationalTutor` removal (`memory/conversational_tutor_removal_plan.md`).
- Raising `OLLAMA_KEEP_ALIVE` — a one-line server-flag change, not a code change;
  fold into whichever session touches the runtime next.

## Files to modify

- `apps/llm/client.py` — timing fields on the response log (WS1); `num_predict`
  pass-through already exists.
- `apps/tutoring/simple_tutor/family_prompts.py` — the brevity constraint (WS2).
  Both the XML and Markdown variants carry near-duplicate length guidance
  (`:251-256` and `:498-506`); change them together or they diverge.
- `apps/tutoring/simple_tutor/engine.py` — `_is_transient_error` (WS3); call-2
  `max_tokens` (WS2); `_persist_tutor_turn` metadata (WS1).
- `apps/llm/model_profiles.py` — Jetson `max_tokens` 3072 → 1024 (WS2).
- `apps/tutoring/views.py` — streaming branch in `chat_respond` (WS4).

## Verification

1. **Latency**: timed 5-turn session over HTTP against lesson 1464 (the harness
   used today), reporting median and p90 turn. Compare to today's baseline
   (34/81/118s on turns 1–3).
2. **Output length**: re-derive the token distribution from the new instrumented
   log; confirm median ≈45 / p90 ≈80.
3. **No compliance regression** — re-run the smoke driver
   (`scratchpad/smoke_tutor.py`) on both Jetson tags and compare
   `pose_question` / `record_answer` / `auto_*` turn counts against today's run.
   A drop in tool-call rate voids the change regardless of the speed win.
4. **Quality eyeball**: read the transcripts. A shorter reply that loses the
   teaching sentence fails, even if every metric improves.
5. `pytest apps/tutoring/simple_tutor/tests/ apps/llm/tests.py` — currently
   **543 passed, 15 subtests**; must stay green.

## Recommended sequence

Original: WS1 (instrument) → WS3 (500s, cheap and independent) → WS2 (the length
fix, with the measure/tune loop) → re-measure → WS4 (streaming) if the numbers
still warrant it.

**Revised 2026-07-29.** WS3 is done (`138a94b`). WS4 was pulled forward on user
direction and now owns WS1: the Ollama streaming path has to read the per-phase
timing fields anyway, so instrumentation falls out of it rather than preceding it.
Live order: WS2 (output cap) → WS4 per `memory/offline_streaming_plan.md`.

## Next step

WS2: cap `num_predict` at ~256 and drop the Jetson `max_tokens` 3072 → 1024. It
shortens all three of a turn's serialized local calls, so it multiplies the
streaming win rather than competing with it.
