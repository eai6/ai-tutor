# Local-model tool compliance — the placement fix was refuted by its own A/B

**Status**: **the fix is deleted.** 2026-07-29: the end-to-end A/B this memo
listed as "Unfinished" was run, and it refuted the fix. `render_turn_directive`
is gone from the engine — not gated, removed, because an offline 4B model with
a 24k system prompt cannot afford instruction text that does not pay for
itself. Call-1 tool compliance is **still an open problem** (1/5 in the
verification run).

Finding 1 stands. **Finding 2 (system-prompt length) was itself refuted on
2026-07-29 — see "Round 2" below**: it was measured on truncated prefixes and
does not survive against complete prompts of the same length. Finding 3 stands
*as an isolated result* and does not transfer to a real turn — read "The A/B
refuted it" before acting on anything here. Supersedes the H2 recommendation in
`memory/jetson_qwen_tool_compliance_plan.md`.

**The current best answer to "why does Call 1 skip the tool" is in Round 2: the
student's message register decides it.** A bare value gets a tool call; the same
value wrapped in conversational prose does not, and no prompt-side change tested
so far moves that.

The replay harness survives the fix: `scripts/probe_tool_loop.py` holds the
captured Call-1 payload and its own copy of the directive text as the known-bad
control arm. **Score any future hypothesis there before it touches the
engine** — that is the transferable lesson from this whole episode.

## The question

`simple_tutor` requires tool calls. On the offline Jetson, `qwen3-4b-jetson`
emitted a tool on Call 1 of only **2 of 9 turns**, so nearly every turn needed
the Call-2 repair. That repair costs a second 8-10s LLM call on a box where a
turn already takes 16-19s, and it is why streaming covered so few turns
(`memory/offline_streaming_plan.md`, P3 findings).

## Finding 1 — Ollama ignores `tool_choice` entirely. H2 is a dead end.

`memory/jetson_qwen_tool_compliance_plan.md` names H2 ("forward `tool_choice`
on Ollama") the "highest-value harness change", on the belief that "Modern
Ollama `/api/chat` accepts it". **It does not.** Measured directly against
Ollama 0.30.7, `qwen3-4b-jetson`, n=4 each:

| request | tool calls |
|---|---|
| `tool_choice` omitted | 4/4 |
| `tool_choice: "none"` | **4/4** |
| force a tool that does not exist | **4/4** (no error) |

`"none"` must produce zero if the field were honoured. Forcing a non-existent
tool must error. Neither happened: the field is parsed and discarded.

So `_plan_call1` / `_plan_call2` / `_adaptive_force_now` are inert on every
local model, and `OLLAMA_FORWARD_TOOL_CHOICE=1` changes nothing — an engine
A/B with it on gave the same 2/9 Call-1 tool rate. **Do not spend time on H2.**

## Finding 2 — the suppressor is system-prompt length

The model is not bad at tool calling. With a toy prompt it is near-perfect;
compliance collapses as the real system prompt grows. Prefixes of the *real*
captured POSE prompt (24,005 chars), n=5 each:

| system prompt | tool calls |
|---|---|
| first 8k | **5/5** |
| first 12k | 1/5 |
| first 16k | 3/5 |
| first 20k | 2/5 |
| full 24k | **0/5** |

Controls that ruled out the obvious explanations — all **6/6**:
- toy schema (3 props) / real `pose_question` (6 props, 4.2k) / all 5 real
  schemas (7.4k) → **schema bulk is not the cause**
- toy schema + 9k of padded lesson prose → **generic length is not the cause**

It is 24k of *dense competing instruction* that buries the tool directive.

## Finding 3 — the fix is message ROLE, not recency

The obvious remedy from `prompting-fundamentals-expert` ("repeat critical
instructions at the bottom if context is long") **does not work here**. What
works is moving the directive out of the system prompt entirely:

| where the identical directive sits | POSE | GRADE |
|---|---|---|
| nowhere (baseline) | 0/8 | 1/6 |
| appended to the END of the system prompt | **0/8** | — |
| in the **user message** | **6/8** | **6/6** |

Same text, same position in the token stream, ~60pp swing. Recency within the
system block buys nothing; the user turn is what the model acts on.

**This was already hiding in the codebase.** `_repair_instruction` rescues
these same skipped calls on Call 2 and has always been delivered as a *user*
message. The repair worked for the reason nobody had isolated.

## What shipped

1. **`prompts.render_turn_directive(mode, family)`** — a short per-turn
   directive naming the expected tool, appended to the user message in
   `respond()`. Returns `''` for Anthropic and for `family=None`, so
   production is byte-identical. Written per `prompting-fundamentals-expert`:
   positive framing, a stated reason instead of emphasis, no all-caps, short
   (length is what broke the system prompt).
   **→ DELETED 2026-07-29. Refuted — see below. It shipped on-by-default for
   local families with no kill switch, which is how a 17 s turn became a 164 s
   one on the kiosk. Do not reintroduce it as a flag: unused prompt text in a
   24k budget is a cost with no upside, and the text is preserved in
   `probe_tool_loop.py` where it can be re-tested for free.**

2. **`engine._call_mode(family)` + `TUTOR_CALL_MODE=one|two|auto`.**
   - `two` — the original design: Call 1 picks tools, platform grades, Call 2
     writes the reply *knowing the verdict*. Default for Anthropic.
   - `one` — accept Call 1's prose, skip Call 2 when Call 1 already produced
     the expected tool AND usable text. Default for local families.
   - A missing tool still falls through to the Call-2 repair, so one-call
     only ever skips a call whose work is already done.
   **→ Kept.** It is unaffected by the refutation, but note it is now mostly
   inert on the local model for the same reason the directive existed: Call 1
   skips the tool on ~4/5 turns, so `missing_tool` is set and the turn falls
   through to the repair anyway. The verification run measured `two_call_turns
   5/5` with `TUTOR_CALL_MODE=one`. One-call only pays off once Call-1
   compliance is actually solved.

### The accuracy trade, stated plainly

In one-call mode the reply is written **before** the platform grades, so the
model is predicting its own verdict. `_align_reply_polarity` is the
deterministic net that catches contradictions — and it is load-bearing, not
decorative: it is what turns a model's "Yes — 360° is correct" on a
wrong-answer turn into a verdict-consistent opener.

Note this trade is **not new**. Before this work the offline engine was
already paying two calls while getting one-call quality: Call 2 emitted no
student-visible text on 4 of 5 turns, so the reply the student read was
Call 1's verdict-blind prose either way. One-call makes that explicit and
stops paying for the second call.

## The A/B refuted it (2026-07-29, later the same day)

The end-to-end A/B this memo owed was run. **The directive does not work in the
engine, and it made turns 5-10x slower.**

`scripts/measure_call_compliance.py`, lesson 1137, `error_prone`:

| config | median turn wall | max turn | Call-1 tool calls per turn | max duplicate |
|---|---|---|---|---|
| `baseline` (no directive, two-call) | **17.1 s** | 34.5 s | 0-1 | 1 |
| `directive-two` | **164.1 s** | 411.0 s | 3-21 | 21 |
| `directive-one` | **117.8 s** | 117.8 s | 31 | 31 |
| directive removed, one-call (`one.json`) | **17.7 s** | — | 0-1 | 1 |

`scripts/probe_tool_loop.py --replay` isolated it against the *real captured*
Call-1 payload, n=4 per arm per payload:

| arm | had tool | looped | HTTP 500 | out tokens | secs |
|---|---|---|---|---|---|
| `no_directive` | 4/8 | **0/8** | 0/8 | 24 / 91 | 3.1 / 9.2 |
| `shipped` | 3/8 | **8/8** | **5/8** | capped at 1024 | 91.6 |
| `presence` (`presence_penalty=1.5`) | 3/8 | 5/8 | 5/8 | 192 / capped | 21 / 94 |

**Mechanism.** With the directive on, every generation runs to the
`num_predict` ceiling. It ends one of two ways:

- a duplicate tool-call storm (19-31 x `record_answer` in one response).
  `_dispatch_tools` de-dupes, so the loop is *invisible in behaviour* and costs
  the whole turn budget in decode — which is why it was not caught by reading
  transcripts.
- cut off mid-`<tool_call>`, which Ollama cannot parse, so it returns HTTP 500.
  `_is_transient_error` classifies 500 as retryable (correct for cloud), so
  `_invoke_with_transient_retry` replays the same deterministic ~92 s failure
  across all five `_TRANSIENT_BACKOFF` steps — **~11 minutes for one turn**,
  ending in the placeholder reply.

**And it did not buy the compliance it was built for**: 3/8 turns emitted a
tool with it, 4/8 without.

### Why the isolated probes lied

Findings 2 and 3 were measured against a hand-assembled request. The captured
engine payload is longer and carries the full tool-schema set, and *that* is
where the loop lives. **A prompt-placement result measured outside the engine
is not evidence about a turn inside it** — the next attempt at this should
replay `eval-reports/call_compliance/call1_payload.json` before anything else.

## Round 2 (2026-07-29, later) — it is the STUDENT MESSAGE, not the prompt

Four more hypotheses were scored in the replay harness. Three were refuted and
the fourth located the cause. **144 generations, `qwen3-4b-jetson`, all 5
captured payloads, n=4/cell.** Raw logs under
`eval-reports/call_compliance/`; the harness grew three arm axes for this
(`--sysarm`, `--intentarm`, `--mutate`).

### Metric correction — score the EXPECTED tool, never "a tool"

`tools>0` is the wrong metric and it flattered the full prompt. On payload 4 (a
GRADE turn) the full prompt called `pose_question` on 3 of 4 trials: a tool
call, but the wrong one — it registers a NEW question while silently dropping
the answer the student just gave, which is a worse failure than calling nothing.
A GRADE turn must call `record_answer`; a POSE turn must call `pose_question`.
`probe_tool_loop.py` now records `names` per sample so this is scoreable.

### Refuted 1 — Block-0 LENGTH is not the lever

Finding 2 above (8k prefix 5/5, full 24k 0/5) **does not transfer to real
payloads**, and its prefixes confounded "shorter" with "cut mid-instruction".
Three complete, well-formed prompts of decreasing length, expected-tool scored:

| Block 0 | system | p0 POSE | p1 | p2 | p3 | p4 | total | median s |
|---|---|---|---|---|---|---|---|---|
| `full` (as shipped) | 23.8k | 4/4 | 4/4 | 0/4 | 0/4 | 0/4 | **8/20** | 12.7 |
| `compact` (dedup) | 16.9k | 0/4 | 4/4 | 0/4 | 0/4 | 4/4 | **8/20** | **7.3** |
| `compact_noslot` | 16.8k | 0/4 | 4/4 | 0/4 | 0/4 | 4/4 | 8/20 | 7.4 |
| `terse` (no rationale) | 10.7k | 0/4 | 0/4 | 0/4 | 0/4 | 0/4 | **0/20** | 8.8 |
| `terse_no_reply_rules` | 7.9k | 0/4 | 4/4 | 0/4 | 0/4 | 0/4 | 4/20 | 9.2 |

- A 34% dedup is **compliance-neutral** (8/20 either way) while cutting prompt
  tokens 7,780 → 6,087 (-22%) and median Call 1 12.7 s → 7.3 s (-43%).
- Cutting further **destroys** compliance. `terse` states every rule the
  compact prompt does, as bare imperatives with the justifications removed, and
  scored 0/20. **The rationale is load-bearing on a 4B model** — the opposite of
  what "24k of dense competing instruction" predicted.
- The slot rule paid for nothing: `compact` and `compact_noslot` are identical
  at 8/20, so those 255 chars buy no compliance. Do not keep it on the theory
  that it helps.
- `terse_no_reply_rules` (4/20) beat `terse` (0/20), so within the terse prompt
  the `## Each reply` pedagogy section was actively harmful. That does NOT
  generalise up to `compact`, which keeps the section and scores 8/20.

### Refuted 2 — the pose-shaped few-shot

`full` carries a POSE-only worked turn ending in a literal
``Tool call: `pose_question(...)` `` which both shortened variants dropped, and
`full` is the only arm that scores on p0. Lifting that block verbatim out of
`full` and inserting it into `compact` (`compact_pose_demo`) changed **nothing**:
0/4, same as `compact`. Whatever makes `full` work on p0 is not the example.

### Refuted 3 — the `answer_or_other` intent guidance

`prompts._INTENT_GUIDANCE['answer_or_other']` looked like a textbook defect: it
opens by declaring the platform's own uncertainty, says "use judgement", never
mentions the empty-`extracted_answer` mechanism, and **contradicts Block 0**
(which says to call `record_answer` on every turn with a question in flight)
from the last position in the prompt, where it wins the conflict.

Three rewrites — unconditional-call-with-reason, unconditional-call-terse, and
the *verbatim `answer` guidance* that the working payloads receive — all scored
**0/8** on p2+p3. The guidance text is not what gates the call. A classifier fix
alone would therefore not have worked either, which is worth knowing before
anyone rewrites `intent.py` for this.

### The cause — a conversational preamble suppresses the tool call

Mutating the captured payloads separates message shape from hint history:

| mutation | payloads | any-tool | expected-tool |
|---|---|---|---|
| none (as captured) | p1, p4 | **8/8** | 4/8 |
| `prosify` — prepend "ohh yeah i get it now, its " | p1, p4 | **1/8** | **0/8** |
| none | p2, p3 | 0/8 | 0/8 |
| `bare_answer` — strip the preamble | p2, p3 | 2/8 | 1/8 |
| `attempt0` — force `attempt_count` to 0 | p2, p3 | **0/8** | 0/8 |

The two payloads that fail under every prompt are `ohh wait, so its 450` and
`ohh yeah i get it now, its 360`. The two that succeed are `270` and `150`.
**Prefixing a working payload's bare value with a conversational preamble drops
it from 8/8 to 1/8** — same lesson, same step, same in-flight slot, same
prompt, one clause of student prose. `attempt_count` and the hint history in
`<recent_turns>` are irrelevant: forcing it to 0 changed nothing at all.

Stripping the preamble only partially recovers the failing turns (1/8), so the
mirror is not exact — those turns also carry hint history, and on p3 the value
"360" is the correct answer, which sends the model to `pose_question` instead.
The direction is nonetheless unambiguous and it is the only manipulation of the
four that moved the metric.

**Interpretation.** The model classifies the TURN from the student's register:
a bare value reads as an answer to be recorded, the same value inside chatty
prose reads as conversation to be replied to. This is not reachable from the
system prompt — which is why Block-0 length, an added rule, a worked example,
and four intent-guidance rewrites all came back flat.

### What this means for one-call mode and for streaming

`TUTOR_CALL_MODE=one` and `memory/offline_streaming_plan.md`'s P4 are both
gated on Call-1 compliance, and compliance is now measured as a property of
what the student typed. Real students write like p2 and p3. So:

- **Keep two-call mode as the local default.** The Call-2 repair is delivered as
  a *user* message and it works; four experiments say Call 1 is not
  prompt-fixable for conversational answers.
- **Do not start P4 expecting the streaming ceiling to have moved.** It has not.
- The remaining untried levers are decoding (`presence_penalty`; `repeat_penalty`
  is still 1 on this tag) and answer-shape normalisation, which would have to
  respect `auto-memory/feedback_grading_design_rules.md` (the LLM extracts, the
  grader decides).

## Round 3 (2026-07-30) — the engine A/B, and compact IS a compliance win

`scripts/measure_call_compliance.py` gained a `block_0` dimension
(`QWEN_BLOCK_0=compact`, eval-only, default byte-identical) and the A/B ran over
real sessions, 10 turns per arm, lesson 1137, `error_prone`, `TUTOR_STREAMING=1`
on both. **The probe's "compliance-neutral" verdict was wrong** — it was measured
on 5 captured payloads, and a real session's turn mix is different.

**Raw arm totals are useless here and reading them cost a cycle.** The student
simulator is stochastic, so each run deals a different mix of bare-value and
prose-wrapped answers — and Round 2 established that mix is what drives
compliance. Run 1 dealt the compact arm 7 bare answers to the full arm's 5 and
compact "won" 7/10 vs 2/10; run 2 dealt the compact arm ONE bare answer out of
ten and compact "lost" 2/10 vs 3/10. Neither total measures the prompt. **Always
split by register.**

Pooled over THREE runs / 60 turns, Call 1 emitted a tool:

| student message | `full` (20.5k) | `compact` (13.5k) |
|---|---|---|
| bare value (`270`, `150`) | 6/16 (38%) | 8/10 (80%) |
| prose-wrapped (`ohh wait, so its 360`) | 3/14 (21%) | 2/20 (10%) |

Fisher one-sided p≈0.042 on the bare turns. **Do not treat that as established.**
Compact's bare-turn record by run is **7/7, 1/1, 0/2** — nearly all the signal is
one session, and the third run points the other way. The stochastic student keeps
dealing different register mixes (attempting to force bare answers by switching
to the `capable` persona failed: it produced 8 prose turns out of 10), so getting
a real answer needs many more sessions per arm, not more analysis of these.

What IS consistent across all three runs is the *shape*: prose-wrapped answers
stay bad under both prompts (21% and 10%), which is what Round 2 predicts and
what no prompt has moved.

**Bottom line: the compact prompt is not currently justified.** Its measured turn
latency win is ~1 s, its compliance advantage is one session wide, and run 3
raised a quality flag (below). Leave it unwired.

### A quality signal that argues against compact

`polarity_rewrote` counts turns where `_align_reply_polarity` had to replace the
opening sentence because the model's text contradicted the grader's verdict — a
net catching a real defect, so a higher rate is worse. Run 3: **compact 5/10
against full 0/10** (runs 1 and 2 were level: 0/9 vs 0/7, 1/9 vs 1/9). One run,
so not conclusive either, but it is the sort of regression a 34% prompt cut could
plausibly cause and it should be checked before compact ships.

`streamed` tracked `call1_had_tool` exactly, which confirms the causal chain the
streaming plan assumed: expected tool on Call 1 → verdict known before the reply
is written → Call 2 writes it → streamable. Streaming coverage therefore rises
only as far as bare-turn compliance does.

### Latency: the 43% from the probe does NOT survive. It is ~5%.

Measured with both arms above 987 MB available (runs 2 and 3, the clean ones):

| arm | Block 0 | median turn | median Call 1 | median Call 2 |
|---|---|---|---|---|
| `compact` | 13.5k | **19.0 / 18.7 s** | 15.2 s | 4.0 s |
| `full` | 20.5k | 19.9 / 20.2 s | 16.2 s | 4.3 s |

**~5% on the turn, ~1 s on Call 1** — against the isolated probe's 12.7 s → 7.3 s
(-43%). The probe was measuring output length, not prefill: its compact arm
happened to generate far fewer output tokens on those captured payloads (79 vs
130 on the POSE payload). In a real turn, output length is governed by
`<reply_length>` and the conversation, so the 1,700 prompt tokens saved buy only
their prefill — about a second at this box's prompt-eval rate.

**Generalisable lesson: an isolated-probe latency delta is not a turn latency
delta.** The probe is sound for "did it call the tool" and unreliable for "is it
faster", because it cannot hold output length constant the way a real session
does.

### Compliance COSTS latency, and that is why `full` looked fast

Within the full arm alone (so no memory confound), split by whether Call 1
emitted a tool:

| Call 1 emitted a tool | n | median Call 1 | median Call 2 | turn |
|---|---|---|---|---|
| yes | 2 | 15.7 s | 10.5 s | ~26 s |
| no | 5 | 14.5 s | **4.1 s** | ~19 s |

A non-compliant turn is CHEAPER: the Call-2 repair only registers the tool and
emits no student text, so the student reads Call 1's verdict-blind prose. A
compliant turn pays for a second full generation that writes the reply knowing
the verdict. **So any prompt that raises Call-1 compliance raises turn latency in
two-call mode**, and the full prompt's apparent speed was partly just
non-compliance. This is the same accuracy trade this file already describes,
now with the price attached.

That makes `compact` + `TUTOR_CALL_MODE=one` the configuration worth testing
(`compact-one`), and it retires the note above that one-call mode is "mostly
inert on the local model" — that rested on Call 1 skipping the tool, which the
compact prompt largely fixes.

### A measurement trap this run walked into — read before trusting a latency delta

The first execution of this A/B ran the two arms back to back in one shell. The
second arm started with **52 MB** available (the first arm's process plus
Ollama's pinned 3.89 GB) and every turn came in 4-5x slower — median turn 87 s
against 18.6 s — *improving monotonically* through the run as memory settled.
A prompt 34% shorter cannot make a call 5x slower, and the isolated probe had
measured the same prompt faster per call. The arm was discarded.

Guards added: runs record `mem_available_mb_at_start`, and the correct procedure
is to unload the model between arms
(`curl -s localhost:11434/api/generate -d '{"model":"...","keep_alive":0}'`,
which returned ~4,986 MB) and to alternate arm order. `--summarise` also had to
learn to skip `probe_tool_loop.py`'s artifacts, which share the output directory
and have no `turns` key.

### The compact prompt — the earlier probe-only read, kept for the record

`MARKDOWN_BLOCK_0_COMPACT` in `apps/tutoring/simple_tutor/family_prompts.py`
(13.5k vs 20.5k) is **written and unwired**. It is compliance-neutral at 8/20
with -22% prompt tokens and -43% median Call 1 latency, which on a 16-19 s turn
is worth having on its own terms. Two caveats before wiring it:

1. It **redistributes** which turns fail rather than fixing any: it loses p0
   (the session-opening POSE turn) and gains p4 (where `full` calls the wrong
   tool). n=4 on one payload each is thin.
2. Probe parity is not engine parity. Validate with
   `scripts/measure_call_compliance.py` over real turns before shipping, per the
   lesson at the top of this file.

`MARKDOWN_BLOCK_0_TERSE` should be **deleted, not kept as an option** — 0/20 is
a measured harm, and unused prompt text in this budget is a cost with no upside.

### Still worth trying

Two prompt variants are already written in `probe_tool_loop.py::variants` and
were never run: `single_clause` (drops "then write your response to them", the
clause that invites continued generation after the call) and `once`. The
decoding angle is the more promising one: `ollama show` reports
`repeat_penalty 1` for this tag — repetition control is fully **off** — and
Qwen3-Instruct-2507's own guidance prescribes `presence_penalty` for exactly
this. `SamplingProfile.presence_penalty` and `OllamaClient` already plumb it,
so a win is a one-line `model_profiles` change. The `presence` arm halved the
loop but did not close it at 1.5; sweep the value.

### Independent bug this exposed — fixed 2026-07-29

The retry ladder amplified the failure, and would have amplified any future
local 5xx the same way. Three changes in `engine.py`, all covered by
`RetryBudgetTest` in `simple_tutor/tests/test_bottleneck_fixes.py` (each test
verified to fail with its guard removed):

1. **`_LOCAL_TRANSIENT_BACKOFF = [2]`** — one retry for `local_ollama` instead
   of the cloud ladder's five. The distinction is not caution, it is that the
   two failures are unrelated: a cloud 429/503 means "capacity, come back
   later" and the wait IS the fix; a local 5xx has no queue to drain, so it is
   usually deterministic in the request while costing a full generation per
   attempt. One retry is kept because a local 5xx *can* be a model reload or an
   allocation blip under memory pressure — both real on an 8 GB Jetson.
   `_backoff_for(provider)` picks the ladder; **omitting the provider keeps the
   cloud ladder**, so nothing remote gets silently shortened.

2. **`_is_malformed_generation_error` → zero retries.** Sharper than the
   provider rule, and keyed on the body Ollama actually returns:

   ```
   500 {"error":"llama-server returned invalid tool call arguments for
                 \"pose_question\": unexpected end of JSON input"}
   ```

   "unexpected end of JSON input" means the tool call was cut off mid-arguments
   at `num_predict`. Resending the identical request reproduces it exactly, so
   the retry is guaranteed waste (~92-103 s measured). Layered *under* the
   local ladder deliberately: if a future Ollama rewords the message the match
   lapses and the failure falls back to one retry, so message-sniffing can only
   improve on the fallback, never regress past it.

3. **`_error_detail` logs the response body.** `str(requests.HTTPError)` is only
   `"500 Server Error: Internal Server Error for url: …"` — the provider's
   actual explanation lives in `exc.response.text` and was being dropped. That
   is why the six 500s on 2026-07-27 left nothing diagnosable in the logs and
   needed a probe script to explain. Guarded so a body that raises on read
   cannot make logging the thing that fails.

### Housekeeping

The model still holds an infinite keep-alive (`expires_at` year 2318, 3.89 GB
pinned in VRAM) despite `OLLAMA_KEEP_ALIVE=5m` in the shell env — the service
env differs. That is what keeps free memory near zero and is what killed the
first two A/B attempts.

## Related

- `memory/jetson_qwen_tool_compliance_plan.md` — the parent plan. H2 is now
  refuted; H3/H4/H5 are untouched by this work.
- `memory/offline_streaming_plan.md` — P3 measured the 2/9 Call-1 tool rate
  that started this, and depends on the fix for streaming coverage.
- `memory/tutor_latency_output_length_plan.md` — one-call mode is a larger
  latency lever than anything in WS2.
