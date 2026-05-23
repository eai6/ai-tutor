# Tutor engine redesign plan

*AI Tutor • drafted 2026-05-23 • status: DRAFT for edit*

Five connected pieces of work, all triggered by the same root issue:
**`apps/tutoring/conversational_tutor.py` is 12,341 lines** and most
recent fixes have been band-aids on top of an under-specified state
contract (inline-authored detector, hold-gate escape valve, figure-ref
step_has_media gate). The engine works but adding the next round of
structural changes inside the same file will not survive.

This plan is the structural pivot. It is intentionally bigger than
`tutor_responsiveness_plan.md` (which addresses empty-turn / dropped-
request / stuck-transition bugs in the current file). Land that one
first; this one rewrites the substrate.

## Scope (in order of dependency)

1. **Decompose the engine** into `apps/tutoring/engine/` modules.
2. **Intent classifier** to replace the "every student message is an answer" assumption.
3. **Flexible grader** that handles bank / inline-pose-tool / free-authored questions uniformly.
4. **Council of judges** (Gemini Flash + OpenAI mini + Haiku) with consolidation.
5. **Single-shot regen** — eliminate cycle 2+, make cycle 1 surgical enough to guarantee clean output.

Parallel research track:
- **Prompt-following diagnosis**: why does the LLM emit inline questions despite tool availability, and which prompt edits move the needle. Feeds (3), (4), (5).

Out of scope (do not slip in):
- Multi-agent decomposition of the tutor itself (per CLAUDE.md conservative bias and Cemri et al. 2025).
- Streaming responses (Azure constraint, separate work).
- Mobile / offline.

---

## Direct answers to questions raised

- **Where does step advancement run?** `apps/tutoring/conversational_tutor.py::_should_advance_step()` at ~line 10417, called from `respond()` between "judge accepts (or regen exhausts)" and `_save_turn()`, around lines 10201–10238. It is **the last decision before persistence** — so any redesign that wants intent or council-vote to influence advancement must inject before this call.
- **Why doesn't the LLM follow tool / judge instructions?** Open hypotheses listed in §1.5 below. Needs measurement before prompt edits.

---

## Phase 1 — Engine decomposition

**Goal**: Move `conversational_tutor.py` from one 12k-line file to a
package without behavior change. Make the next four phases tractable.

### Target structure

```
apps/tutoring/engine/
├── __init__.py              # re-exports ConversationalTutor for `from apps.tutoring.conversational_tutor import ConversationalTutor` callers
├── tutor.py                 # ConversationalTutor class — orchestration only (~500-800 LOC target)
├── state.py                 # SessionState enum, EngineState Pydantic model, persistence (load/save)
├── respond.py               # respond() / _respond_impl main flow
├── pre_eval.py              # working analyzer + intent classifier integration (math/MCQ move to grader/)
├── advancement.py           # _should_advance_step, exit-ticket triggers, remediation entry/exit
├── scaffolding.py           # _scaffolding_in_progress, reveal gates, hold-gate escape valve
├── media.py                 # _build_media_catalog, signal parsing, _step_media_ids
├── prompt_builder.py        # system prompt assembly (large; extract from _build_system_prompt)
├── judge_orchestrator.py    # combined_judge invocation + validator stack + regen dispatch
├── intent.py                # NEW (Phase 2) — intent classifier + routing
├── grader/                  # NEW (Phase 3) — tool-using grader agent (see Phase 3 for sub-structure)
└── tests/                   # smoke + unit tests per module
```

Keep `apps/tutoring/conversational_tutor.py` as a **thin re-export shim**
for one release so external imports (`from apps.tutoring.conversational_tutor
import ...`) keep working. Delete after one deploy cycle confirms nothing
imports private symbols.

### Sequencing

This is a pure code-motion refactor — risk is in *missing* a coupling,
not in design choices. Do in **slices**, not one big-bang move:

1. **Slice A — state.py + media.py**. These have the cleanest boundaries (state persistence, media signal parsing). Land first, get the import shim pattern proven.
2. **Slice B — pre_eval.py + scaffolding.py**. Both are pre-draft sub-modules with isolated state (`_pending_math_check`, `_pending_working_analysis`, `_awaiting_answer`).
3. **Slice C — prompt_builder.py**. Largest extraction (~3-4k LOC). The risk is hidden coupling to `self.*` state — solve by passing a typed `PromptContext` dataclass instead of `self`.
4. **Slice D — judge_orchestrator.py**. Wraps combined_judge + validator + regen. Sets up the seam for Phase 4 (council).
5. **Slice E — advancement.py + respond.py**. Last because they touch all the others.

### Definition of done

- `apps/tutoring/conversational_tutor.py` ≤ 500 LOC (re-export shim only).
- No file in `engine/` exceeds ~1.5k LOC.
- All existing `pytest apps/tutoring/` passes without modification.
- A new structural test: `from apps.tutoring.engine import ConversationalTutor` works, no circular imports, `inspect.getsourcefile()` returns the new path.

### Risk

- **Hidden self-state coupling**. The current file uses `self.*` for ~50+ per-turn flags (`_pending_*`, `_step_just_advanced`, `_bank_signal_used_this_turn`, etc.). Extracting modules means either (a) keeping them on `self` and passing `self` around (sloppy), or (b) introducing a `TurnContext` dataclass that holds per-turn state. Pick (b) — it doubles as the typed-state work in §state.py.
- **Test coverage gaps**. Many code paths are only exercised by integration tests, not unit tests. Before Slice B, add a "before/after" trace-capture harness so we can confirm same turn → same output across the refactor.

### Open questions

- Should `engine/` live under `apps/tutoring/engine/` or be a sibling package `apps/tutor_engine/`? Voting `apps/tutoring/engine/` for locality with tests and `tutoring/views.py`.
- Re-export shim: delete after how long? Suggest one prod deploy + one week of grep checks.

---

## Phase 2 — Intent classifier

**Premise**: Sub-module 1 of the design assumes every student message is
an answer to the prior question. This is wrong. Students ask questions,
request hints, push back, change subject, type `idk`, ask meta-questions
("how many more steps?"). Treating all of those as "answer attempts"
forces the rest of the pipeline (grader, advancement, scaffolding) to
misfire, and the engine *feels* rigid.

### Categories (5 buckets — kept tight on purpose)

| Intent | Definition | Affects grader? | Affects advancement? |
|---|---|---|---|
| `ANSWER_ATTEMPT` | Direct attempt at the posed question | YES — grade it | YES — correct + step_complete → advance |
| `REQUEST_HELP` | Hint, example, clarification, "idk", "I'm stuck", "explain again" | NO | NO — scaffold mode |
| `META_QUERY` | Questions about the session itself ("how many more?", "what's this for?", "can we skip?") | NO | NO — respond directly, then re-pose |
| `OFF_TOPIC` | Chitchat, unrelated content, challenges ("you're wrong") that aren't an answer | NO | NO — gentle redirect |
| `CONTROL` | Explicit state commands: "stop", "end session", "go back" | NO | YES — explicit state mutation |

Five buckets, not nine. Each maps to a distinct downstream behavior, so
collapsing further would lose signal. If usage data later shows
`REQUEST_HELP` lumping unrelated patterns (hint vs. confusion vs.
example), split then — not preemptively.

Default rule: if no question is in flight, an "answer-shaped" message is
classified as `OFF_TOPIC` or `META_QUERY` (the grader is never invoked
without an `ActiveQuestion`).

### Architecture

- New module: `apps/tutoring/engine/intent.py`
- Fast cheap LLM call (Haiku 4.5 or Gemini Flash Lite — TBD by latency budget) returning structured output (Pydantic schema):
  ```python
  class StudentIntent(BaseModel):
      primary: IntentCategory  # enum above
      secondary: Optional[IntentCategory]  # for mixed messages
      confidence: float  # 0..1
      reasoning: str  # one sentence
  ```
- **Runs in parallel** with the deterministic grader (both pre-draft, both fast). If grader returns a structured match AND intent says non-ANSWER, grader wins for the verdict but intent still routes the tutor prompt.
- Output persisted to `SessionTurn.metadata['intent']` for trace logging and tuning.

### Integration points

- **Pre-draft**: intent feeds into `prompt_builder.py` as a `<student_intent>` block. Tutor system prompt gets intent-specific instructions appended (e.g., `REQUEST_HINT` → "Provide one hint, do not reveal the answer. Re-ask the same question.").
- **Advancement** (`advancement.py`): `_should_advance_step` reads intent. If intent ≠ `ANSWER_ATTEMPT`, do not consider advancement — same step, no exchange-count increment toward completion.
- **Scaffolding** (`scaffolding.py`): `REQUEST_HINT` / `CONFUSION` enters scaffolding mode without needing a wrong answer first.
- **State machine**: `CONTROL` intents can trigger state transitions directly (e.g., "end session" → `SessionState.COMPLETED`).

### Definition of done

- Intent classifier called on every student turn.
- Trace shows intent + confidence on every `SessionTurn`.
- Scaffolding triggers on `REQUEST_HINT` without requiring a wrong answer (verifiable with a test).
- Advancement does not fire on non-`ANSWER_ATTEMPT` intents.
- Tutor response to "how many more?" answers the meta-query, then re-poses the active question — no advancement, no grader fire.

### Open questions

- **Cost**: intent is one extra LLM call per turn. With Haiku at ~$0.001/turn, this is fine. Confirm with current per-turn baseline.
- **Latency**: parallel with grader, so should be ≤ 500ms added wall time. Confirm.
- **Conflict policy**: grader says "answer is wrong" but intent says `CLARIFY_QUESTION` — what wins? Probably intent (don't grade something the student didn't intend as an answer). Needs decision.
- **Overlap with `tutor_responsiveness_plan.md` Phase 2**: that plan proposes a lightweight "intent detection" too — this is the larger structured version. Decide whether to land that one first (small, surgical) or skip it in favor of this.

---

## Phase 3 — Flexible, tool-using grader

**Premise**: Today the grader handles bank questions (via
`bank_grader.py`) and math pre-check. Inline-posed questions (via the
`pose_question` tool) and free-text LLM-authored questions are graded
by LLM or not graded at all — that's the LLM-grading-LLM hole.

User direction: **(a)** give the tutor freedom to author questions
inline; **(b)** make the grader smart enough to handle all modes **by
giving it tools** — math equivalence, knowledge-base search, web
search, etc. The grader becomes a small tool-using agent, not a
match-or-LLM-fallback dispatcher.

This phase has two halves:
- **3A**: Question capture (one `ActiveQuestion` record; one structured path via `pose_question`).
- **3B**: Tool-using grader agent (math + KB + web + lookup tools).

### 3A — Two question paths, one record

**Earlier drafts proposed a `<answer>` block convention** for
inline-authored questions. Dropped — LLMs are unreliable at emitting
exact HTML-style markers in natural prose, and we'd end up writing
flexible regex parsers that quietly fail. Instead, **extend the
existing `pose_question` tool** to carry the expected answer
optionally. One structured path, no parsing fragility.

| Path | Mechanism | Has expected_answer? |
|---|---|---|
| **Tool-fired** | LLM calls `pose_question(text, question_id?, expected_answer?, answer_type?, tolerance?)` | Always for bank (`question_id` populates it); optional for authored (LLM commits if it wants determinism) |
| **Drifted** | LLM emits a question in plain text, no tool call | No — judge catches it, grader recovers in full agentic mode (see Drift recovery below) |

Tool signature (expansion of today's `pose_question`):

```python
pose_question(
    text: str,                          # the question shown to the student
    question_id: Optional[str] = None,  # populated for bank questions; engine resolves answer from DB
    expected_answer: Optional[str] = None,   # LLM-supplied answer key for authored questions
    answer_type: Optional[Literal['mcq', 'numeric', 'symbolic_math', 'short_text', 'free_text', 'factual']] = None,
    tolerance: Optional[Dict] = None,    # numeric tolerance, MCQ choice set, expected unit, etc.
)
```

Unified record in `engine_state['active_question']`:

```python
class ActiveQuestion(BaseModel):
    source: Literal['tool_bank', 'tool_authored', 'drifted']
    question_id: Optional[str]  # bank only
    question_text: str
    expected_answer: Optional[str]  # present for tool_bank + tool_authored-with-answer; None for drifted
    answer_type: Literal['mcq', 'numeric', 'symbolic_math', 'short_text', 'free_text', 'factual']
    tolerance: Optional[Dict]
    grader_hints: Optional[Dict]  # e.g. {'kb_lesson_id': X, 'expects_unit': 'm/s'}
    step_index: int
    posed_at_turn: int
```

Tutor system prompt encourages — but does not require — committing
`expected_answer` when calling `pose_question`. Bank questions get it
automatically. Authored questions without `expected_answer` fall through
to the agentic grader (mode 3). Authored questions WITH
`expected_answer` get the fast or tool-assisted path.

### 3B — Grader tools

The grader becomes an LLM-driven dispatcher with a tool registry.
Trivial cases (MCQ with expected letter, exact-match numeric) bypass
the LLM entirely and call the relevant tool directly. Complex cases
(symbolic math, factual claims, open-ended responses) invoke the LLM
with all tools available.

**Tool registry** (lives in `apps/tutoring/engine/grader/tools/`):

| Tool | Purpose | Backed by |
|---|---|---|
| `math.symbolic_equiv(student, expected)` | Symbolic equivalence: `2x+2 ≡ 2(x+1)` | sympy `simplify(a-b) == 0` |
| `math.numeric_within_tolerance(value, expected, tol, unit)` | Numeric match with tolerance + unit conversion | pint + plain Python |
| `math.evaluate_expression(expr, vars)` | Safe expression evaluation | sympy (no eval()) |
| `math.parse_units(text)` | Extract numeric + unit from free text | pint + regex |
| `text.exact_match(student, expected, normalize=True)` | Strip / lowercase / punctuation-tolerant | stdlib |
| `text.fuzzy_match(student, expected, threshold)` | Embedding similarity | sentence-transformers (already in repo) |
| `mcq.resolve_choice(student, choices, expected)` | "B", "Option B", "the second one" → letter | regex + fuzzy |
| `kb.search(query, lesson_context)` | Vector search over curriculum KB | `apps/curriculum/knowledge_base.py` (existing) |
| `web.search_grounded(claim)` | Verify factual claim via web | reuse `_call_gemini_grounded` from `content_judges/_providers.py` |
| `lookup.bank_answer(question_id)` | Pull expected answer for bank question | ORM |

**Why this structure**:
- The expensive tools (web, KB, LLM) only fire when needed — MCQ never hits them.
- Each tool is testable in isolation. No "LLM all the way down".
- Web grounding reuses the infrastructure already proven in the content judges (`_call_gemini_grounded`).
- Math tools are deterministic — sympy + pint solve "is `9.81 m/s²` equivalent to `9.8 N/kg`?" without LLM hallucination.

### Three grader modes (cost-tiered)

1. **Fast path (deterministic, no LLM)** — fires when `ActiveQuestion`
   has an `expected_answer` and `answer_type ∈ {mcq, numeric, short_text}`:
   - MCQ → `mcq.resolve_choice`
   - numeric → `math.numeric_within_tolerance`
   - short_text → `text.exact_match` then `text.fuzzy_match`
   - Returns verdict in <50ms, no LLM call.

2. **Tool-assisted path (LLM + tools)** — fires when `answer_type ∈
   {symbolic_math, factual, free_text}` OR when the fast path returns
   ambiguous (`text.fuzzy_match` below confidence):
   - Cheap LLM (Haiku or Gemini Flash) given the question, student
     response, expected answer (if present), and the full tool registry.
   - LLM decides which tools to call, returns structured verdict.
   - Typical: 1-2 tool calls + 1 LLM turn, ~1-2s.

3. **Full agentic path (LLM, no expected answer)** — fires when
   `expected_answer is None` (free-authored question without `<answer>`
   block, or open-ended question):
   - LLM does KB search + (optionally) web search to construct an
     answer key on the fly, then evaluates the student response against
     that key.
   - More expensive (~3-5s) and the most error-prone — logged as
     `grader_mode=full_agentic` so we can measure how often it fires
     and tune to push more questions into modes 1 and 2.

### Drift recovery: read the judge, don't re-detect

The LLM will sometimes pose a question in plain text **without** calling
`pose_question` — drift, despite instructions. By the time the student
has answered it, the grader sees `engine_state['active_question'] is
None` but a clearly answer-shaped message.

**The council of judges already detects this.** Today's validator has
`ISSUE_NO_QUESTION_TOOL`. Keep it; have the judge also **extract the
question text** into a structured field on the verdict (judges read the
text anyway — make them emit the extraction).

**Flow**:

1. Tutor drafts response.
2. Council of judges runs (BEFORE delivery). If
   `ISSUE_NO_QUESTION_TOOL` fires, the verdict carries the extracted
   question text.
3. Phase 5 single-shot regen tries to repair (re-fire `pose_question`,
   optionally with `expected_answer`). If repair succeeds, normal path
   resumes.
4. If repair fails (regen ships dirty), persist the extracted question
   text on `SessionTurn.metadata['drifted_question']` so the next turn
   has it.
5. Next student turn arrives. Grader checks `active_question` (None) →
   checks prior turn's `metadata['drifted_question']` (present) →
   synthesizes `ActiveQuestion(source='drifted', expected_answer=None,
   question_text=<extracted>)` → grades in full agentic mode.
6. Trace logs `drift_recovered=true`. Drift rate per model / per
   lesson is the signal for the prompt-following research track.

**Why this is better than grader-side heuristics**:
- No duplicate question-detection logic in two places.
- The judge already reads the text; extracting one more field is free.
- The grader stays simple: it just reads the prior turn's metadata.
- The signal is structured and measurable from day one.

**Tie-breaker** when the prior tutor turn contains both a tool-fired
question AND a drifted inline question: tool wins; the judge's
`drifted_question` extraction is logged but grader uses the tool's
`active_question`. Tutor system prompt on the next turn is told this
happened to discourage doubling up.

### Implementation modules

```
apps/tutoring/engine/grader/
├── __init__.py
├── dispatch.py              # routes by ActiveQuestion to fast/tool-assisted/full-agentic
├── agent.py                 # LLM-driven tool-using grader (modes 2 + 3)
├── verdict.py               # GraderVerdict Pydantic schema
└── tools/
    ├── __init__.py          # tool registry + LLM-facing schema definitions
    ├── math.py              # sympy + pint wrappers
    ├── text.py              # match / fuzzy
    ├── mcq.py
    ├── kb.py                # wraps apps/curriculum/knowledge_base.py
    ├── web.py               # wraps the Gemini grounded call from content_judges
    └── lookup.py            # ORM lookups for bank answers
```

`GraderVerdict` schema (all paths return this):

```python
class GraderVerdict(BaseModel):
    is_correct: bool
    partial_credit: float  # 0..1 for PARTIAL_CORRECT cases
    confidence: float
    grader_mode: Literal['fast', 'tool_assisted', 'full_agentic']
    tools_called: List[str]  # for trace + cost tracking
    reasoning: str  # one paragraph for trace
    feedback_for_student: Optional[str]  # short, not the answer — passed to tutor as hint material
```

### Pedagogical justification

Inline-author freedom is preserved: the LLM still authors any question
it wants, in its own voice. The `pose_question` tool wraps that
question and *optionally* carries an `expected_answer` so the grader
gets determinism for free. The student sees only the question text;
the engine sees the answer. When the LLM authors a question without
the tool (drift), the agentic mode catches it via the judge's
extraction.

### Definition of done

- All question paths flow through `grader/dispatch.py`.
- Trace records `grader_mode`, `tools_called`, `confidence`, `question_source` (`tool_bank` / `tool_authored` / `drifted`).
- `bank_grader.py` and `_deterministic_math_check` deleted — replaced by `tools/math.py` + `tools/mcq.py`.
- Inline-authored detection heuristic (`conversational_tutor.py:278-349`) deleted — replaced by `ActiveQuestion` "one in flight at a time" invariant + drift recovery.
- Bank/MCQ verdict latency stays <50ms (no regression).
- `grader_mode=full_agentic` rate is measured and reported in benchmark; drift rate is reported per model and per lesson.
- Math tools handle symbolic equivalence on a frozen set of 20 algebra/geometry equivalence cases (test fixture).
- Web-grounding tool handles 20 factual claims (test fixture) — same source set the content judges use.

### Open questions

- **Tool budget**: cap tool calls per grading turn (e.g., max 3; on exceed, return low-confidence verdict and let auditor/tutor handle). Voting yes — prevents runaway agentic loops.
- **Model for grader agent**: Haiku 4.5 vs Gemini Flash Lite. Haiku is better at tool use; Flash is cheaper. Test.
- **sympy + pint as new deps**: confirm acceptable. Both are pure Python, widely used, no native compilation.
- **Web search cost**: Gemini grounded calls aren't free. Cache by (claim, lesson_id) for the session duration. Probably yes.
- **Rubric questions** (open-ended): rubric stored on `ActiveQuestion.grader_hints['rubric']`, graded in tool-assisted mode.
- **Migration**: existing in-flight free-authored questions get `source='drifted'` on first encounter, then resolved when the next tool-fired question is posed. Acceptable.
- **Same-vendor grader and tutor**: not a blocker. The grader sees a strictly narrower context (question + student response + tools + KB results) than the tutor (full conversation + system prompt + step context), so even same-vendor calls reason differently. Measure disagreement empirically rather than enforce vendor separation.

---

## Phase 4 — Council of judges

**Premise**: Keep the unified-judge architecture (one prompt, ~10 dims),
but run it in **parallel across 3 vendors** and consolidate. Solves the
same-class-judge problem and reduces variance.

### Architecture

- 3 parallel calls per turn:
  - Gemini Flash Lite (current `judge` ModelConfig)
  - OpenAI gpt-4o-mini (current `judge_fallback`)
  - Anthropic Haiku 4.5 (current `judge_fallback_2`)
- Each returns a `UnifiedJudgeVerdict` (same schema today).
- **Consolidation** rules (tunable):
  - `needs_regen`: majority vote (2/3). Default to *not* regen on 1/3 only.
  - `issue_codes`: union — if any judge flags it, surface it.
  - `severity`: max across judges per code.
  - `reasoning`: concatenated by source for trace.
- Cost: 3× judge cost. With current models (~$0.001 per judge call), still <$0.005/turn.
- Latency: parallel via `asyncio.gather` or `concurrent.futures`, so wall time ≈ slowest judge (~2-3s).

### Failure handling

- If 1 of 3 judges errors out → proceed with 2-of-2 majority.
- If 2 of 3 error → proceed with the single remaining judge + trace warning.
- If all 3 error → cascade to current `get_judge_provider_chain` fallback (single judge, sequential).

### Integration

- Lives in `apps/tutoring/engine/judge_orchestrator.py` (extracted in Phase 1).
- Replaces the single `run_combined_judge` call.
- Trace logging: each judge's raw verdict stored in `SessionTurn.judge_outputs['council']`. Consolidation result stored at `SessionTurn.judge_outputs['consolidated']`.

### Why this matters for Phase 5 (single-shot regen)

If regen must succeed in one try, the regen prompt needs **rich, specific
feedback**. Council vote breakdowns ("Gemini said no_question; OpenAI
said figure_ref_without_signal; Haiku said clean") are richer signal than
"the judge flagged 2 issues". Phase 5 depends on this richness.

### Definition of done

- 3-vendor parallel judge call per turn, consolidated verdict drives regen + advancement.
- Per-vendor verdicts persisted in trace.
- Disagreement rate measurable (% of turns where vendors voted differently). Used as a tuning signal.
- Kill switch: `JUDGE_COUNCIL=off` env var falls back to single-judge mode.

### Open questions

- Should we add a **risk-routing layer** before the council fires (skip council entirely for low-risk turns)? That's Phase 4 in the architecture plan and a different ROI calc. Decide: keep them separate, council first then risk-route as Phase 4b.
- Disagreement on `needs_regen` — 1/3 says regen — should it ever override majority? Maybe yes for high-severity codes (`ISSUE_ANSWER_LEAK`, `ISSUE_TUTOR_UNSAFE`). TBD.

---

## Phase 5 — Single-shot regen

**Premise**: Eliminate cycle 2+. Make cycle 1's refinement prompt
specific enough that a clean candidate is guaranteed. If we can't
guarantee it, the regen architecture is wrong — adding more cycles just
hides the problem.

User direction: **there really should not be a thing as regen a second time**.

### What changes

- `DEFAULT_MAX_CYCLES = 1`.
- Regen prompt becomes **issue-type-specific repair templates**:
  - `ISSUE_NO_QUESTION` → "Your response did not end with a question. Add a specific question about [step.objective_text] at the end. Do not change other content."
  - `ISSUE_ANSWER_LEAK` → "Your response revealed the answer ([offending_span]). Rewrite to scaffold without revealing. The expected answer is [expected_answer] — do not include this verbatim or paraphrased."
  - `ISSUE_FIGURE_REF_WITHOUT_SIGNAL` → "You referenced a figure but did not emit the `|||MEDIA:N|||` signal. Either remove the figure reference or end your response with `|||MEDIA:N|||` where N matches the figure in the catalog."
  - ... (one template per validator issue code)
- Council vote breakdown injected into regen prompt: "3/3 judges agree on no_question; 1/3 also flagged figure_ref. Fix no_question first."
- Pre-draft `<evaluation_signal>` block included again so the regen sees the same context.
- **If regen still fails**: do NOT cycle again. Ship the regen with a `regen_failed_clean` trace flag and a `validator_issues` entry. Surfaces in benchmark immediately.

### Why this works

Current regen is "same prompt, lower temperature, add judge feedback at
the end" — noise. The proposed regen is "edit-style prompt with a
specific repair instruction per issue" — a structured edit, not a
re-roll.

### Definition of done

- `DEFAULT_MAX_CYCLES = 1`.
- Repair-template registry: one template per validator issue code. Lives in `apps/tutoring/engine/regen_templates.py`.
- Regen prompt assembled from templates based on consolidated council verdict.
- Trace logs `regen_template_used` per turn.
- Benchmark v2 baseline measures regen success rate before and after.

### Open questions

- What if multiple issues are flagged? Compose templates? Pick highest severity? TBD — probably highest severity wins, others mentioned briefly.
- Cost change: 2-3 cycle regens drop to 1 (cost down) but each regen is now more involved (slight cost up). Net: cost down significantly. Measure.
- **Rollout decision (locked)**: `MAX_CYCLES=1` ships as the unconditional default. No env-var shadow period — cost and latency wins are the point. Templates must be ready on day one; any quality regression is caught by the benchmark and addressed by template iteration, not by re-enabling extra cycles.

---

## Parallel research track — prompt-following diagnosis

**Question raised**: why doesn't the LLM follow tool instructions or
judge instructions? Need data before more prompt edits.

### Hypotheses to test

1. **Instruction overload**: system prompt is ~10k+ tokens; tool-use rules buried mid-prompt.
2. **Conflicting signals**: tool says "use pose_question" but examples elsewhere show inline questions.
3. **Cache invalidation**: prompt structure shifts turn-to-turn → cache misses → instruction drift.
4. **Model bias**: Opus / Sonnet / Gemini have different tool-call vs. inline-text preferences.
5. **Repeat-question salience**: when a question was just asked, the model defaults to "answering then re-asking" inline rather than re-firing the tool.

### Investigation steps

1. Pull 50 turns from prod where the LLM inline-authored despite tool availability. Capture: model, lesson, step phase, conversation length, prompt token count, exact prompt structure.
2. Group by feature (model, step phase, token count) — look for a dominant pattern.
3. Test prompt variants on a frozen eval set:
   - Variant A: pose_question instruction moved to top of system prompt
   - Variant B: explicit "you MUST NOT author questions inline" rule
   - Variant C: removed all examples that show inline questions
   - Variant D: tool re-instructed in user message (closer to query)
4. Measure: tool-call rate, inline-author rate, council-judge clean rate.

### Output

A memo (~1-2 pages) for `memory/prompt_following_diagnosis.md` with
findings and which variants moved the needle. Feeds into Phases 3, 4,
5 — especially the regen templates in Phase 5.

---

## Sequencing & risk

```
Now              Phase 1 (decompose)              ← high risk, no behavior change
                 │
                 ├─ Slice A,B,C,D,E one at a time
                 │
   parallel ──── Research track (prompt-following) ← memo output
                 │
After Phase 1    Phase 2 (intent) + Phase 3 (flexible grader)  ← can run together
                 │
                 Phase 4 (council)                ← needs P1 (judge_orchestrator extracted)
                 │
                 Phase 5 (single-shot regen)     ← needs P4 (council verdicts) + research memo
```

**Don't ship Phase 5 before Phase 4** — single-shot regen depends on
council verdict richness.

**Don't ship Phase 3 before Phase 1** — grader dispatch needs the
`engine_state` typed model from Slice A.

**Phase 2 and Phase 3 can ship together** — they touch different code
paths and reinforce each other (intent + question source both go into
trace).

---

## Acceptance criteria (whole plan)

- `apps/tutoring/conversational_tutor.py` ≤ 500 LOC (shim only).
- Inline-authored detection heuristic deleted (replaced by `active_question` invariant + intent classifier).
- `bank_grader.py` + `_deterministic_math_check` deleted (replaced by `engine/grader/tools/`).
- Hold-gate escape valve invocation rate drops (visible in trace metrics).
- Regen success rate on cycle 1 ≥ 95% on benchmark v2 dataset.
- Cost/turn: judge cost goes up ~2.5× (council), regen cost goes down (single shot), grader cost goes up modestly (LLM for non-fast-path) — net target flat or down. Measure before/after.
- Grader: `grader_mode=full_agentic` rate is reported and trends down as templates / answer-key coverage improves.
- Grader: bank/MCQ verdict latency stays <50ms; tool-assisted verdict P95 <3s.
- Every student turn has an `intent` field in trace.
- Every grader call records `grader_mode`, `tools_called`, `confidence`.
- Every tutor turn has a `question_source` field when it posed a question.

---

## Cross-references

- `memory/tutor_responsiveness_plan.md` — smaller responsiveness fixes; land first.
- `memory/agentic_platform_architecture_plan.md` — overall architecture direction; this plan executes the engine portion of it.
- `memory/eval_benchmark_v2_simplified.md` — benchmark to measure all of the above against.
- `auto-memory/feedback_consult_prompting_skills.md` — consult prompting skills before any prompt edit in Phases 3-5 and the research track.

---

## Open questions for Edward

Resolved by inline comments 2026-05-23:
- Intent categories → simplified to 5 (see Phase 2 table).
- Answer-block syntax → dropped; extending `pose_question` tool instead (see Phase 3A).
- Single-shot regen rollout → ships as unconditional default (see Phase 5 open questions).
- Same-vendor grader vs tutor → not enforced; measure disagreement instead (see Phase 3 open questions).

Still open:

1. **Phase ordering**: ship Phase 1 (decompose) fully before starting any of 2-5, or interleave Slice E with Phase 2? Voting: full Phase 1 first — the cost of touching the old file while restructuring is too high.
2. **Council vendor mix**: Gemini Flash Lite + gpt-4o-mini + Haiku 4.5 — or swap one for a Sonnet for vote weight?
3. **The existing `tutor_responsiveness_plan.md` Phase 2 (intent detection)** overlaps with Phase 2 here. Land that one first as a stepping stone, or skip it in favor of this plan's richer version?
