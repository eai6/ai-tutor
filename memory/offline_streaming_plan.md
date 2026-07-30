# Streaming for offline tutoring — Plan (2026-07-29)

**Status**: approved 2026-07-29, in flight. Phase checkboxes live in the delivery
table below.

The detailed expansion of **WS4** in `memory/tutor_latency_output_length_plan.md`.
That file stays authoritative for WS1–WS2; its **WS3 is already done** (Ollama
500s were classified transient in `138a94b`, so `_is_transient_error` at
`engine.py:1655` already reads `exc.response.status_code`).

## Context

The offline kiosk runs the whole Django app on a Jetson Orin Nano over its own
WiFi hotspot, tutoring against a local Ollama Qwen model. `memory/latency_bench_local_vs_cloud.md`
established that TTFT is ~0.7s on both local and cloud, and the *entire* gap is
decode rate × output length: ~16 tok/s local vs ~60–75 cloud, i.e. ~6.9s vs ~2.2s
for a 100-token reply. A real turn is worse than that benchmark suggests, because
a turn is not one call.

Today the student stares at a typing indicator for the whole turn and then the
reply lands at once. Streaming won't make the model faster, but it converts a
long opaque wait into visible progress — which is the difference between a
student waiting and a student giving up.

The "no SSE / chunked streaming" rule in `CLAUDE.md` is scoped to Azure Container
Apps. The Jetson is gunicorn on a LAN, so the rule does not bind here. Everything
below is gated so the Azure path stays byte-identical.

## Current state (audited)

**A turn is three serialized LLM calls, and only the last is streamable.**

1. `run_safety_judge` — `apps/tutoring/views.py:1095`. Uses `ModelConfig`
   purpose=`judge`, **falling back to purpose=`tutoring`** — the same local Qwen.
   Fail-soft (never raises), no kill switch. Blocking, before the tutor runs.
2. Call 1 — `engine.py:484` via `_call_llm` (`engine.py:1715`). Model picks tools;
   `_dispatch_tools` (`engine.py:501`) then runs the grader. **The verdict is known here.**
3. Call 2 — `_run_second_call` (`engine.py:528`, defined `:2035`) writes the
   student-facing reply. Skipped entirely when Call 1 emitted no tools and there
   is nothing to repair (`engine.py:2058-2062`).

**Eight post-generation transforms then rewrite the text** (`engine.py:559-599`).
Three are streaming-relevant and all fire offline (local Qwen profiles set `_family`):

- `_scrub_engine_vocab` (`:1086`) — drops tool-JSON lines and engine-vocab sentences; can return `''`.
- `_align_reply_polarity` (`:1245`) — replaces the **first sentence** when it contradicts the grader verdict.
- `_filter_reveals` (`:1301`) — **safety-relevant**: redacts the reference answer from a wrong-answer hint. qwen3:14b printed `(Answer: A)` verbatim.

Others append (`_ensure_posed_question_in_text` `:581`, `_auto_pose_fallback` `:587`),
prepend (`_dedupe_reply` `:1193`), or replace wholesale (`_empty_reply_placeholder` `:568`).
`respond_for_view` can also overwrite the whole reply with a canned exit-ticket
transition (`engine.py:2445-2462`).

**No Ollama streaming exists.** `OllamaClient.generate_with_tools`
(`apps/llm/client.py:1544`) hardcodes `'stream': False` (`:1657`). There is no
`generate_stream` override, so it inherits the base fallback (`:756`) that calls
`generate()` and yields one chunk — fake streaming.

**Reusable pieces already in the tree:**
- `createStreamingMessage()` (`templates/tutoring/chat_tutor.html:2894`) and
  `updateStreamingMessage(div, content, media)` (`:2906`) — dead code from the
  retired `ConversationalTutor` era, but exactly the right shape.
- `_adapt_ollama_response` (`client.py:1702`) — reuse unchanged.
- `apps/tutoring/cli/render.py` is deliberately pure (dict in, string out).

**Constraints:** `infra/systemd/ai-tutor.service:77` runs gunicorn `--workers 2`
with default **sync** workers. `OLLAMA_NUM_PARALLEL=1` already serializes turns on
the model. `performSend` (`chat_tutor.html:2516`) uses `NetHelpers.fetchWithRetry`
with `retries: 2` — replaying a POST is wrong for a partially-consumed stream.

## Target design

### The core decision: streaming is an advisory preview, not the source of truth

Incremental filtering **cannot** be made byte-identical to the batch pipeline.
Concrete divergences, all verified:

- `_PAREN_RE` and `_ANSWER_PAREN_RE` legitimately span sentence boundaries — flushing at `. ` emits the first half before the paren pass can fire.
- `_is_tool_json_line` is *line*-local, not sentence-local.
- `_scrub_engine_vocab`'s terminal `if not re.search(r'[A-Za-z0-9]', result): result = ''` is whole-text; you cannot un-emit text that later collapses to empty.
- `_XML_TOOL_TAG_RE` needs both tags present.

So: **`respond()` still runs the full eight-transform pipeline on the complete raw
text, unchanged.** That result is what persists and what the final event carries.
The stream carries a conservatively-cut *safe prefix*. Persisted output is
byte-identical to today by construction.

Because the transport sends **cumulative snapshots** (not append-only deltas) and
`updateStreamingMessage` already re-renders the whole bubble, a final payload that
differs from the streamed prefix is a free correction, not a retraction hack.

### `streaming_safe_prefix` — one new pure function

New module `apps/tutoring/simple_tutor/stream_filter.py`. Signature:

```python
streaming_safe_prefix(raw_so_far: str, *, verdict: str | None,
                      reference: str | None, session) -> str
```

Cut `raw_so_far` at the last position that is simultaneously (i) a line boundary
or sentence end, (ii) outside any unbalanced `(`, (iii) outside any unclosed XML
tool tag. Apply the **existing, unmodified** filters to that prefix. Hold the rest.

Two hard rules:

- **Head rule.** Withhold the first sentence until `_FIRST_SENTENCE_END_RE` matches,
  so `_align_reply_polarity` can replace it before the student reads it. Costs
  ~1.2s of first-text latency at 16 tok/s. Accepted: a visible "That's right!" →
  "Not quite" flip is worse than a short wait, and the `status` event covers
  perceived responsiveness from t=0.
- **Safety rule.** When `verdict == 'incorrect'` and an `InFlightQuestion` with a
  non-empty `reference_answer` exists, cut on **sentence boundaries only** so
  `_filter_reveals` always sees whole sentences. Non-negotiable.

### The seam: an `on_delta` callback, not a generator

`respond()` is ~300 lines with many early returns; converting it to a generator
forces every `return` into `yield`-then-`return` and every caller into a driver loop.

Add one optional kwarg `on_delta: Callable[[str], None] | None = None`, threaded
`respond()` → `_run_second_call` → `_call_llm` → client. **Default `None` is
today's exact path**, which is what the `TUTOR_STREAMING=1` gate keys on.
`respond_for_view` takes and forwards it.

The engine wraps the raw callback in a small `_StreamGate` holding `raw_so_far`,
`verdict`, `reference`, `last_emitted_len`, and calls `on_delta(safe_prefix)` only
when the prefix grows. Transports never see raw model text — the engine stays the
single source of truth for filtering.

Only Call 2 gets `on_delta`. Call 1's text is pre-text subject to repair, and the
verdict the filters need isn't known until after `_dispatch_tools`.

### `OllamaClient.generate_with_tools(..., on_delta=None)`

Same method, not a sibling. When `on_delta` is set: `payload['stream'] = True`,
`requests.post(..., stream=True, timeout=600)`, iterate `resp.iter_lines()`.

- Accumulate `message.content`, call `on_delta(delta)`.
- Accumulate `message.thinking` separately and **never** forward it.
- Collect `message.tool_calls` — Ollama emits whole tool-call objects, not partial
  JSON fragments, so no incremental parser is needed.
- On the `done: true` line, synthesise the same `data` dict the non-streaming path
  produces (joined content, collected tool_calls, plus `prompt_eval_duration` /
  `eval_duration` / `load_duration` which the final line carries) and hand it to
  the **unchanged** `_adapt_ollama_response`. Same `AdaptedMessage`, same timing
  log at `:1712`, `_dispatch_tools` untouched.

This also delivers WS1 instrumentation for free.

### Transport + frontend

New `chat_respond_stream` view alongside `chat_respond`, sharing all pre-flight
work by extracting the `views.py:1013-1212` preamble (suspension, completed
session, rate limit, JSON parse, safety judge) into a helper. A ~30-line
`apps/tutoring/streaming.py` adapter runs `respond_for_view` in a thread pushing
to a `queue.Queue`; the view generator drains it into `StreamingHttpResponse`.

SSE events:

| event | payload |
|---|---|
| `status` | `{phase: "judging" \| "thinking"}` — emitted immediately |
| `text` | `{content: "<cumulative safe prefix>"}` |
| `done` | the existing 16-key `respond_for_view` payload, unmodified |
| `error` | `{message}` |

Frontend: revive `createStreamingMessage()` on first `text`, `updateStreamingMessage()`
on each subsequent one. On `done`, if the payload text differs from the streamed
text **or** any media/artifact/probe field is present, remove the streaming bubble
and call the existing `addMessage` with the full payload. That keeps media handling
identical and leaves the `:3565` monkey-patch untouched; flicker only in the rare
correction case.

Add `NetHelpers.fetchStream(url, opts)` to `static/js/network-helpers.js` — same
abort/timeout/offline detection, `retries: 0`. On any stream failure, fall back to
one plain `fetch` against `chat_respond`. `drainQueue` stays on the non-streaming
path unchanged.

**Prerequisite — solved without an idempotency key.** The `status` event is
emitted at t=0, before any model work, so *receiving it proves the server accepted
the turn*. The client therefore falls back to `chat_respond` **only when zero
events arrived**. That is a strictly stronger guarantee than a dedupe key would
give, and needs no new model, migration, or stored payload.

(The plan originally specified a `client_turn_id` UUID deduped server-side. P0a
found the underlying bug is narrower than assumed — see below — and the
zero-events rule covers the streaming case, so the key was dropped as unnecessary
machinery.)

### CLI

`tutor_chat.py:259` passes `on_delta` that prints the new suffix and calls
`sys.stdout.flush()` (the command never flushes today, and non-TTY stdout is
4KB block-buffered). `render.py` stays pure — add `format_reply_prefix(text) -> str`
for the ANSI colour. `_emit` still does its final single-shot format when the
`done` payload differs from what streamed.

### Gunicorn

`--workers 2` sync is a real problem — but not for throughput, since
`OLLAMA_NUM_PARALLEL=1` serializes turns anyway. The problem is that two open SSE
connections consume both workers, stalling static assets and every other endpoint
for the whole class. Minimal fix at `infra/systemd/ai-tutor.service:77`:
`--worker-class gthread --workers 2 --threads 8`, keep `--timeout 300`. SSE here is
I/O-blocked on `requests` to Ollama, so threads suffice — no gevent, no async
rewrite. Send `X-Accel-Buffering: no` for future-proofing (no proxy today).

## Out of scope

- Streaming Call 1, or streaming on Azure/production. `TUTOR_STREAMING` stays off there.
- Streaming the exit ticket, review, difficulty-signal, or bank-question endpoints.
- `ConversationalTutor.respond_stream()` (`conversational_tutor.py:3612`) — dead against
  the live engine. Delete the vestigial `StreamingHttpResponse` import at `views.py:1015`
  and leave the rest for the scheduled engine removal.
- Mobile / `apps/api` offline pack.
- Changing the safety judge (see Open questions).
- Token-level typewriter animation — snapshots at sentence granularity are the design.

## Phased delivery

Estimates are days of focused solo work.

| Phase | Work | Days |
|---|---|---|
| **P0a** ✅ | ~~Fix `_is_transient_error`~~ — already landed in `138a94b`. ~~`client_turn_id` dedupe~~ — replaced by `retryOnNetworkError: false` (see below). **Done 2026-07-29.** | 0.25 |
| **P0b** ✅ | WS2 output cap — mostly already shipped. ~~`num_predict` ceiling ~256~~ rejected on existing evidence; Jetson `max_tokens` → 1024 on the remaining tags. **Done 2026-07-29**, see below. | 1.0 → 0.1 |
| **P1** ✅ | `generate_with_tools` streaming + `on_delta` threaded through the engine + `StreamGate` + `safe_cut_index`. **Done 2026-07-29.** | 2.0 |
| **P2** ✅ | Golden tests — `simple_tutor/tests/test_stream_filter.py`, 29 tests. **Done 2026-07-29.** | 1.0 |
| **P3** ✅ | CLI streaming. **Done 2026-07-29** — and it measured the feature. Read the section below before starting P4. | 0.5 |
| **P4** | SSE view + queue adapter + frontend + `fetchStream`. | 2.0 |
| **P5** | gthread switch + kiosk soak with 5–8 clients. | 0.5 |

**~7.25 days.** P3 is independently shippable after P2 — the CLI is the cheap place
to prove the engine path before any HTTP work.

## P0a as built (2026-07-29)

The pre-existing bug was real but narrower than "we need idempotency keys".
`fetchWithRetry` (`static/js/network-helpers.js:53`) treated **two different
failure classes as one**:

- `502/503/504` — the server saying it did *not* process the request. Safe to replay.
- `TypeError` / `AbortError` — says nothing about whether the server processed it.
  The request may have arrived, run to completion, and only the *response* got
  lost. Replaying re-runs the work.

On the Jetson, crossing the 120s client timeout is ordinary rather than
exceptional (three serialized local calls at ~16 tok/s), so the second class fired
routinely and ran a whole second turn against a session the first was still
mutating — double-persisted turns, double-advanced steps.

Fix: a `retryOnNetworkError` option, default `true` so nothing else changes,
set `false` at the two non-idempotent call sites:

- `templates/tutoring/chat_tutor.html:2516` — `performSend` (the tutoring turn).
- `templates/tutoring/_partials/exit_modal.html:607` — exit-ticket submit, which
  creates and grades an `ExitTicketAttempt` and can complete the session. Its
  disabled-button guard only stops a second *click*, not an internal replay.

Explicit 502/503/504 retries are preserved at both sites. No model, no migration.

## P0b as built (2026-07-29)

WS2 was largely already done, and one thing this plan proposed was **wrong**.

**The `num_predict` ceiling of ~256 is rejected.** `apps/llm/model_profiles.py:236-243`
already records the reasoning, from the earlier `qwen3.5:4b` tuning: measured
output is 27–193 tokens, so a cap near the p90 (175) *bites*, and a cap that bites
truncates mid-sentence — which reads worse to a student than a reply that was
merely long. Do not re-propose it.

**The real length mechanism already exists**: `simple_tutor/prompts.py:1137`
`_render_length_budget()` — a `<reply_length>` block asking for 2–3 sentences,
budgeted as reaction clause / teaching sentence / next question. Stated in
sentences rather than tokens because the model cannot count its own tokens.

**What was actually left**: `max_tokens` was still 3072 on three tags, including
`local_ollama/qwen3-4b-jetson` — the one the kiosk and `./chat.py` actually run
(`serve.py:35`, `chat.py:43`). Brought to 1024, matching the already-migrated
`qwen3.5:4b`. Framed honestly in the code as a **runaway guard, not a latency
win**: neither value binds on a normal 27–193 token reply, but a repetition loop
at ~16 tok/s costs a student 64 s at 1024 against 192 s at 3072. `qwen3.5:0.8b`
(intent classifier) and `qwen3.5:9b` (does not fit the box) keep 3072.

**Pre-existing red test found and fixed.** `QwenLocalTagProfileTest::test_tags_pin_a_jetson_safe_context`
asserted `max_tokens == 3072` over a tag list containing `qwen3.5:4b`, which moved
to 1024 in an earlier commit — so it was failing on `main` before this work
started. The exact-value assertion was over-specification; the test's stated
purpose is that these tags don't fall through to the generic cloud `r"qwen3"`
pattern. Relaxed to `assertLessEqual(max_tokens, 3072)` and paired with a new
`test_jetson_tutoring_tags_carry_the_runaway_guard` that pins 1024 on the five
tutoring tags.

## P1 + P2 as built (2026-07-29)

Shipped as designed, plus four things the design did not anticipate. All four
are the same class of problem: **a filter that is safe to run once per turn is
not automatically safe to run once per chunk.**

1. **`_rotation_index` mutates and saves.** `engine.py:1192` increments a
   persisted counter and calls `session.save()` on every invocation. Running
   `_align_reply_polarity` per chunk would have rotated the acknowledgement
   between snapshots — the student watching "Exactly!" become "Nice work!" —
   and issued a DB write per chunk on SQLite. Fixed by making the index
   injectable: `_align_reply_polarity(..., rotation_index=None)`. The gate
   resolves it once and `respond()` passes the same value to the final batch
   pass, so the streamed opener and the persisted opener are the same string.
2. **`_filter_reveals` re-queried `InFlightQuestion` per chunk.** Same fix
   shape: `_filter_reveals(..., reference=None)`, gate resolves once.
3. **Transient retry replays the generation.** `_invoke_with_transient_retry`
   re-runs the whole call, so the gate would have appended attempt 2's tokens
   to attempt 1's partial text and emitted the concatenation. Added an
   `on_attempt` hook; the gate exposes `begin_attempt()` and is itself the
   callable, so `_call_llm` discovers the reset without a second parameter.
4. **Fail-closed ordering bug, caught by its own test.** The reference was
   resolved lazily inside `safe_prefix()`, so on a lookup failure the *first*
   snapshot escaped before `_resolved_reference` was ever set — it leaked one
   sentence and only blocked afterwards. Resolution now happens in `feed()`
   before the guard decides.

**`safe_cut_index` holds more constructs than the plan listed.** Added
unbalanced `{`/`[` (an Ollama model leaking a tool call as JSON text is
routine — see `_maybe_parse_text_tool_call` — and half of one must never
reach a student) and unclosed ``` fences.

**Every guard is covered by a test proven to fail without it.** Per
`.claude/skills/testing-patterns-expert`, each was verified by breaking the
implementation and confirming the specific test goes red: filters disabled →
scrub test fails; reveal filter skipped → both reveal tests fail; cut guards
removed → three `safe_cut_index` tests fail; rotation caching removed → both
rotation tests fail; `begin_attempt` neutered → the retry test fails.

**Parity tests must set `TUTOR_MODEL_OVERRIDE`.** `_family` is None in a bare
test run, which is the Anthropic path where the OSS nets are *correctly*
skipped — a parity assertion there passes trivially because no filter runs on
either side. The parity tests pin `local_ollama/qwen3-4b-jetson` so they
exercise the kiosk's real configuration.

Suite: `apps/tutoring/simple_tutor/tests/ apps/llm/tests.py` → **577 passed**,
plus the 2 pre-existing `test_prompts.py` failures described above.

## P3 as built — and what it measured (2026-07-29)

The CLI shipped (`--stream` / `--no-stream`, defaulting to `TUTOR_STREAMING`;
`render.stream_delta` converts cumulative snapshots to terminal tails;
`_StreamPrinter` handles `ending=''` + `flush()`). But putting it in front of a
real model is what made this phase worth doing, and **the headline result is
that the plan's central assumption was wrong.**

### Finding 1 — Call 2 is the wrong call to stream on a local model

The plan says "the student-facing text comes from Call 2." That is true of
Anthropic (`_run_second_call` notes opus makes two calls on 95% of turns). It
is **not** true of `qwen3-4b-jetson`. Measured over 8 turns on lesson 1137,
the visible reply came from **Call 1 on 4 of 5** instrumented turns
(`final_from_call1=True`, `missing_tool=record_answer`).

The local shape is: Call 1 writes the entire reply as prose and skips the
tool; Call 2 exists only to register the repair and emits **no text at all**,
so `_run_second_call` falls back to `text_reply_1`. Streaming Call 2 alone
covered **2/8 turns**.

### Finding 2 — flushing Call 1 fixes coverage and breaks safety

Flushing `text_reply_1` once tools are dispatched lifted coverage to **8/8**,
median 4.1 s saved. It also produced this, live in the terminal:

```
Yes — 360° is the total when you complete one full turn around a point.
  ↻ revised:
Not this time — have another look.
```

The student read an affirmation of a wrong answer and watched it flip — the
exact failure the head rule exists to prevent. Cause: on that turn Call 1
skipped `record_answer`, so **no verdict existed at flush time** and
`_align_reply_polarity` had nothing to act on. Call 2's repair graded it, and
the batch pass rewrote the opener afterwards.

The flush is now guarded: it happens only when a verdict is already recorded,
or when nothing was in flight at turn start (no grade expected, so both
verdict-dependent filters are provably no-ops). Regression test:
`Call1FlushGuardTest`, verified to fail with the guard removed.

### Finding 3 — with the guard, honest coverage is ~2/8 turns

| configuration | turns streamed | median saved |
|---|---|---|
| Call 2 only (as planned) | 2/8 | 2.6 s |
| \+ unguarded Call-1 flush | 8/8 | 4.1 s — **unsafe, rejected** |
| \+ guarded Call-1 flush (shipped) | 2/8 | 2.9 s |
| \+ `OLLAMA_FORWARD_TOOL_CHOICE=1` | 4/8 | 2.8 s |

Median turn is ~16–19 s, so streaming currently removes roughly 3 s of a
16 s wait on a quarter of turns. **That is a small win for the complexity.**

### What this means for P4

The blocker is not the transport — it is tool compliance. Every turn where
Call 1 both writes the prose and skips the tool is unstreamable *by
construction*, because the reply exists before the grade that decides whether
it may be shown.

`OLLAMA_FORWARD_TOOL_CHOICE=1` doubles coverage by pushing the local model
toward the Anthropic shape (Call 1 = tool, Call 2 = prose), which is also the
*safe* shape — the verdict is known before the streamed call. It is left OFF:
it changes tutoring behaviour, `client.py:1591-1600` documents forced-tool
support as unreliable across models, and that decision belongs with the
`jetson_qwen_tool_compliance_plan.md` work, not with streaming.

**Recommendation: qualify tool forcing before building the SSE transport.**
P4 is 2 days of view + frontend work whose payoff is currently ~3 s on a
quarter of turns; the same effort spent on Call-1 tool compliance would raise
the ceiling for P4 first. The CLI already proves the engine seam end to end,
so nothing is blocked by deferring P4.

### Update 2026-07-29 (later) — the compliance work went and came back

The tool-compliance attempt that followed this recommendation was **refuted and
disabled**; see `memory/tool_compliance_root_cause.md`. `OLLAMA_FORWARD_TOOL_CHOICE`
is also confirmed inert — Ollama 0.30.7 parses `tool_choice` and discards it.

So the P4 blocker named above is unchanged and still real: **streaming coverage
is ~2/8 turns and there is no known lever to raise it.** Do not start P4
expecting the ceiling to have moved.

**Update 2026-07-29 (Round 2) — and now we know why.** Four further hypotheses
were measured (Block-0 length across 3 complete prompts, a pose-shaped few-shot,
and 3 rewrites of the `answer_or_other` intent guidance). All flat. The cause is
the **student's message register**: a bare "270" gets its tool call 4/4, the same
value as "ohh yeah i get it now, its 360" gets it 0/4, same prompt and same slot
— and prefixing a working turn with that preamble drops it from 8/8 to 1/8.
Real students write the second way, so Call-1 compliance is not prompt-fixable
and the streaming ceiling is structural. Full numbers in
`memory/tool_compliance_root_cause.md`.

For the record, nothing in P1-P3 was implicated in that regression. Streaming
is default-off (`TUTOR_STREAMING` unset), `on_delta` defaults to `None`
everywhere, and Ollama only sends `stream: true` when a callback is present —
the slow turns were fully buffered. Worth knowing because the regression
*looked* like a streaming bug and cost a diagnosis cycle to place elsewhere:
the two changes shipped in the same uncommitted batch.

## Verification

- **Golden fidelity tests (P2, the load-bearing ones).** Over N recorded raw
  replies: (a) assert the batch pipeline output is unchanged from today; (b) assert
  every streamed prefix is a prefix-consistent subset of the final text; (c) assert
  that for `verdict == 'incorrect'` with a reference answer, no emitted prefix ever
  contains the reference. Include the qwen3:14b `(Answer: A)` case as a fixture.
- **Unit tests** for `streaming_safe_prefix`: unbalanced paren held, unclosed tool
  tag held, first sentence withheld, `''` collapse never emitted.
- **Regression**: `pytest apps/tutoring/` with `TUTOR_STREAMING` unset must be green
  and byte-identical — the default path is untouched.
- **CLI end-to-end**: `./chat.py --lesson <id>` against local Qwen, watch text
  appear progressively; confirm the final rendered reply matches the persisted
  `SessionTurn.content`.
- **Kiosk end-to-end**: drive the running dev server with `mcp__chrome-devtools__*`
  per the CLAUDE.md bug-fix workflow. Screenshot at each stage — status event,
  mid-stream, final — and confirm the correction path (force a `_dedupe_reply`
  repeat) renders cleanly. Visual check is required, not optional.
- **Measure**: report TTF-*text* and total wall time before/after on the same
  lesson, and append the numbers to `memory/latency_bench_local_vs_cloud.md`.

## Open questions

1. **The safety judge is probably the biggest offline latency term and streaming
   cannot hide any of it.** `views.py:1095` falls back to the tutoring model, so on
   the kiosk it is a third full local Qwen call per turn, with no kill switch.
   **Decided: measure first** — P1's instrumentation reports its cost, then decide
   between a dedicated small-model `ModelConfig` row and a `SAFETY_JUDGE` env gate.
   Not touching a student-facing safety control on speculation.
2. **How often does Call 2 actually happen on local Qwen?** `_run_second_call`
   returns early at `engine.py:2058-2062`. If the one-call rate is high, streaming's
   value drops. Measure in P1 — that's a finding, not a reason to stream Call 1.
3. **`updateStreamingMessage` re-parses the full markdown each snapshot.** At
   sentence granularity on a Jetson-served browser this should be fine, but confirm
   in the P5 soak with a long reply.

## Next step

P0a: add the `client_turn_id` dedupe. Today `performSend` retries a POST twice
(`chat_tutor.html:2516`), so a slow Jetson turn can already double-persist; the
streaming fallback to `chat_respond` makes that certain.
