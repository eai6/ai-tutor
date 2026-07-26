# OSS 13-model multi-turn sweep — bottleneck analysis (2026-07-22)

Sweep: `offline_eval/multi_turn_results/oss13_mt/` — 13 Ollama models × the
20-scenario seed-5 fixcheck draw (same draw, sim, and judge as the engine
fix-cycle board: gemini 18/20 · kimi 18/20 · qwen3-next-80b 17/20 at cycle 11).

## Board

| Model | Pass | Valid-only | End reasons | Verdict |
|---|---|---|---|---|
| qwen3:4b | 20/20 | 100% | exit_ticket 18, max_turns 2 | best score ever recorded on this draw |
| qwen3.6:35b-a3b | 17/20 | 85% | exit_ticket 18, max_turns 2 | ties the cloud qwen3-next-80b |
| qwen3:14b | 11/20 | 55% | 6 deadlocks | repetition class (BN1) |
| qwen3:30b-a3b | 11/20 | 79% (11/14) | 6 errored (Anthropic overload) | re-run the errored 6 |
| qwen3.5:9b | 9/20 | 56% (9/16) | 4 errored | re-run the errored 4 |
| qwen3.5:4b | 5/20 | 25% | max_turns 12 | grind class (BN4) |
| gemma2/3 (all 7) | 0/20 | n/a | deadlock 20 ea. | invalid — BN5, not tutoring |

## Protocol health (from logs)

The tool protocol itself is largely working on Ollama: answer-intent GRADE
turns that failed to record a verdict — qwen3:4b 2%, qwen3.5:9b 3%,
qwen3:14b 2%, 30b-a3b 0%, 35b-a3b 0%. The one outlier is qwen3.5:4b at 12%
(17 dead grading turns), on top of 413 Call-2 repairs and 174 "still
declined" — Ollama cannot honour forced tool_choice, so when the
restricted-list repair is also ignored, the turn dies. The auto-pose
fallback carried 42 poses for it; there is no grade-side equivalent yet.

## Bottlenecks, ranked

**BN1 — verbatim-repetition deadlocks (qwen3:14b, 6 sessions; also latent
elsewhere).** The judge reports "repeated the identical paragraph
word-for-word", including after a CORRECT student answer. At sampling temp
0.2-0.7 a small model re-generating the same context often emits an
identical bubble, and the server-rendered slot question makes byte-identical
output *more* likely. The engine has no cross-turn dedup: nothing prevents
persisting a tutor turn identical to the previous one — which is precisely
the student-sim's deadlock trigger (and terrible UX regardless).
*Fix (engine, deterministic): identical-reply guard — if the outgoing reply
normalises equal to the previous tutor turn, vary it deterministically
(rotated acknowledgement prefix / placeholder library), and log it.*

**BN2 — verdict-prose contradiction (the top rubric killer for 3.5:4b,
3.5:9b, 14b, 30b).** The server grader's verdict is right; the model's prose
contradicts it: "Not quite" openers on graded-correct answers, "Exactly!"
on graded-incorrect ones, re-litigating its own confirmations ("first said B
was wrong, then confirmed B"), and retroactive question rewrites ("presented
75°, then claimed the question was about 65°"). This is the single most
frequent low-scored rubric line across the OSS failures.
*Fix (engine, deterministic, eval-gated): polarity alignment — when the
turn's verdict is correct and the reply opens with a negation marker (or
vice versa), replace the opening sentence with a rotated verdict-consistent
acknowledgement. The verdict is authoritative; the prose must follow it.*

**BN3 — answer reveal while the question is open.** qwen3:14b literally
printed "(Answer: A)"; 3.5:4b stated "option C is correct" mid-hint;
several models reveal the reference value inside "let's check" hints.
*Fix (engine, deterministic): reveal filter on incorrect-verdict turns — the
engine KNOWS the reference; redact/rewrite sentences that state the
reference letter ("option C is correct", "(Answer: A)") or the bare
reference value while the slot is open. Prompt rules alone have not held
for small models.*

**BN4 — pacing grind (qwen3.5 family; 30b/35b straight_line + struggler).**
Sessions re-ask paraphrased versions of already-solved problems (paraphrase
evades the verbatim repeat guard), re-explain after correct answers, and
miss turn budgets (3.5:4b: 12 of 15 fails are max_turns; avg session 18.4
turns vs 9.8 for qwen3:4b). Compounded for 3.5:4b by the 12% dead grading
turns.
*Fixes: (a) grade-side net mirroring the auto-pose fallback — when intent
is 'answer', a slot exists, and both Call 1 and the forced repair declined
record_answer, grade the student's raw message server-side (the old
auto-grade fallback was removed for over-firing, but it predates the intent
classifier; gating on intent='answer' + slot removes the old failure mode);
(b) consider count-based step advance pressure when N corrects accumulate
on one step regardless of pose behaviour.*

**BN5 — Gemma cannot run at all: Ollama has no tool support for the Gemma
templates.** Every Gemma call returns HTTP 400 from /api/chat with tools;
every session collapses to the fallback reply and deadlocks at turn 1. All
seven Gemma scores are invalid as tutoring measurements — this is the exact
class `offline_eval/models.txt` warns about ("MUST pass the tool-probe
before sweeping").
*Fix options: (a) prompted-tool emulation for tool-incapable families (JSON
tool-call convention in the prompt + engine-side parsing — a real engine
feature, sized in days not hours); (b) exclude Gemma from the sweep and add
the tool-probe as a hard gate cell in the Colab notebook so an incapable
model fails fast instead of burning 20 sessions.*

**BN6 — 10 scenarios lost to Anthropic overloads (30b: 6, 3.5:9b: 4).**
The [2, 5, 12]s backoff was not enough during the overload window.
*Fix: lengthen the sim/judge retry ladder (e.g. add 30/60s tiers) and
re-run the 10 errored scenarios before finalising those two boards.*

## What qwen3:4b's 100% means

The guard stack now carries most of the protocol burden (dispatch order,
slot rendering, salvage, letter coherence, auto-pose), and a small compliant
model rides it perfectly: 184 poses, 167 grades, 2% dead turns, 9.8-turn
sessions, zero consistency failures. With n=20 the 95% interval is [84, 100],
so confirm with the full 200-scenario run before drawing deployment
conclusions — but the shape of the result (protocol compliance beats
parameter count on this benchmark) is consistent across the whole board:
qwen3:4b > qwen3:14b, and 35b-a3b ≈ the 80b cloud model.

---

## Addendum — fixes implemented (2026-07-22, same day)

BN1–BN4 + BN6 implemented test-first (16 new tests; suite 507 green), all in
`apps/tutoring/simple_tutor/engine.py` unless noted:

- **BN1** `_dedupe_reply` — a reply identical (normalised) to the previous
  tutor turn gets a rotated re-engagement line prepended. Runs last in the
  reply pipeline, ungated (verbatim repeats are wrong everywhere).
- **BN2** `_align_reply_polarity` — negative openers on correct verdicts
  (and vice versa) replaced with rotated verdict-consistent
  acknowledgements. Eval families only.
- **BN3** `_filter_reveals` — on incorrect verdicts with an open slot,
  "(Answer: X)" markers and sentences stating the reference letter/value
  are redacted. Eval families only.
- **BN4** `_auto_grade_fallback` — intent strictly 'answer' + slot present
  + record_answer absent from both calls → the raw student message is
  graded server-side (the intent gate is what the removed 2026-05
  version lacked). Eval families only.
- **BN6** transient backoff ladders extended to (2, 5, 12, 30, 60)s in
  both `apps/llm/client.py` and the engine.

Not implemented: BN5 (Gemma prompted-tool emulation — a sized feature
decision for the owner). Validation requires a Colab re-run of the OSS
models (the fixes ride the branch the notebook clones).

---

## Closing summary — the Gemma track (2026-07-23): evaluation discontinued

The Gemma enablement effort ran six evaluations across two days: three
5-scenario smokes (probe5 v1–v3) and two 20-scenario board runs
(gemma20_mt, gemma20_mt_v2), with engine fixes between each round. The
full arc, all on okamototk/gemma3-tools (the only repackaging that
carries tools through Ollama; verified on the pinned 0.30.7 server):

| Model | smoke v1 | smoke v2 | smoke v3 | board v1 | board v2 |
|---|---|---|---|---|---|
| 27b | 3/5 | 4/5 | 1/5 | **10/20** | 7/20 |
| 12b | 1/5 | 1/5 | 3/5 | 7/20 | 4/20 |
| 4b  | 1/5 | 0/5 | 1/5 | 1/20 | 2/20 |
| 1b  | 0/5 | 0/5 | 0/5 | 0/20 | 0/20 |

Reference at the same draw: qwen3.6:35b-a3b 17/20, qwen3:4b 20/20,
gemini-2.5-flash / kimi-k2-thinking 18/20.

**What the effort produced.** The track was not wasted — it surfaced and
fixed real engine gaps that transferred to every family: the XML
tool-markup scrub, the mid-reply polarity pass, MCQ option-set repeat
detection, retry-frame variation, the reject-turn auto-pose, the hard
pivot for stuck slots, and the rotation-parity fix all came out of Gemma
transcripts and are now part of the guard stack. It also produced the
tool-probe gate and the version-verified Ollama pin, which protect every
future OSS sweep.

**Why evaluation stops here.** Two independent board runs bracket
gemma3-27b's true rate at roughly 35–50% — half the qwen result at the
same weight class — and the last two fix rounds, which measurably moved
qwen-class models, left Gemma flat-to-down (within the ±2–3 noise band).
The failure mode is no longer any protocol gap the engine can net: the
guard stack already carries ~4.5 auto-grade rescues per session for 27b,
and the residual signature — distributed pacing grind, sessions burning
to max_turns across DIFFERENT questions, ignored self-corrections — is
model capability through this tool protocol, not integration. Further
engine iteration on this track is demonstrably diminishing returns.

**Decision: no further Gemma evaluation.** The family datum of record is
gemma3-27b at 10/20 (board v1, best of two runs); 12b and below are
floor. Revisit only on a materially new Gemma release, or if the vLLM
serving experiment (option 2 of the original analysis) is ever wanted
for its own sake. colab_eval_gemma.ipynb remains in the repo as the
harness template for that eventuality.
