# Simple Tutor Engine — Build Milestones

Started 2026-05-25. Pairs with `simple_tutor_engine_plan.md`,
`grading_system_research.md`, `tutor_engine_research.md`.

**Branch:** `simple-tutor-engine` (off `main`, NOT `dev`).
**Final target:** drive one Geography lesson end-to-end on staging with the
new engine, with all 5 tool calls observed and the grading tier breakdown
visible in `SessionTurn.judge_outputs`.

Each milestone is **independently shippable** with a clear test gate. If
a milestone's tests fail, that milestone does not land — we don't carry
broken code forward.

Test pattern: `apps/tutoring/tests/test_<module>.py` per CLAUDE.md.
Factory fixtures live in `apps/tutoring/tests/factories.py`.

---

## M0 — Branch + scaffolding

**Goal:** working branch off main, this milestone doc committed.

**Files:**
- `memory/simple_tutor_engine_milestones.md` (this file)
- `apps/tutoring/tests/factories.py` (verify exists; create if not)

**Tests:** none — setup only.

**Acceptance:**
- [ ] `simple-tutor-engine` branch pushed to origin
- [ ] `git log --oneline -1` shows the milestones-doc commit
- [ ] `pytest apps/tutoring/tests/` still passes (no regression from branch creation)

**Estimated size:** 15 minutes.

---

## M1 — Schema migration + deps

**Goal:** `TutorSession.engine` + `TutorSession.current_question_id` fields,
new math grading deps installed.

**Files:**
- `apps/tutoring/models.py` — add 2 fields to TutorSession
- `apps/tutoring/migrations/00XX_add_engine_field.py` — new migration
- `requirements.txt` — add `latex2sympy2-extended`, `math-verify`

**Schema delta:**
```python
class TutorSession(models.Model):
    # ... existing fields ...
    engine = models.CharField(
        max_length=16,
        default='v1',
        choices=[('v1', 'conversational_tutor'), ('simple', 'simple_tutor')],
        db_index=True,
        help_text='Which runtime engine handles this session.',
    )
    current_question_id = models.IntegerField(
        null=True, blank=True,
        help_text='Set by pose_question tool; cleared on record_answer or advance_step.',
    )
```

**Tests:** `apps/tutoring/tests/test_session_engine_field.py`
- [ ] Default value is 'v1' — existing sessions unaffected
- [ ] Choice validation rejects unknown values
- [ ] Migration applies cleanly on a fresh SQLite DB
- [ ] Migration is reversible (forward then backward roundtrip works)
- [ ] `current_question_id` can be set + cleared
- [ ] Querying `TutorSession.objects.filter(engine='simple')` works

**Acceptance:**
- [ ] `python manage.py migrate` clean on SQLite
- [ ] `python manage.py makemigrations --check` reports no pending model changes
- [ ] All M1 tests pass
- [ ] `pip install -r requirements.txt` succeeds locally
- [ ] `python -c "import math_verify, latex2sympy2_extended; print('OK')"` succeeds

**Estimated size:** 30 minutes.

---

## M2 — Grader scaffolding + MCQ grader

**Goal:** `apps/tutoring/grader.py` with the dispatcher + Tier-1 MCQ grader.

**Files:**
- `apps/tutoring/grader.py` (NEW)
- `apps/tutoring/tests/test_grader_mcq.py` (NEW)

**Public API:**
```python
class Verdict(Enum):
    CORRECT = 'correct'
    PARTIAL = 'partial'
    INCORRECT = 'incorrect'

@dataclass(frozen=True)
class GradeResult:
    verdict: Verdict
    confidence: float                    # 0.0–1.0
    tier: str                            # 'mcq'|'math'|'embed_gate'|'verifier_llm'
    per_criterion_scores: dict[str, float]
    justification: str
    needs_followup: bool

def grade_answer(*, question, student_answer: str) -> GradeResult: ...
```

**Tests (M2 covers MCQ only):**
- [ ] Exact match: student types "B" → CORRECT, confidence=1.0
- [ ] Letter with prefix: "Option B" → CORRECT
- [ ] Letter with explanation: "I think B because…" → CORRECT
- [ ] Full option text match: student types the option's full text → CORRECT
- [ ] Lowercase: "b" → CORRECT (case-insensitive extraction)
- [ ] Wrong letter: "A" when correct is "B" → INCORRECT
- [ ] Multi-letter shouldn't false-positive: "BA" → INCORRECT
- [ ] Number form: "1" when correct is "B" (option 2) → CORRECT (alternate index)
- [ ] Empty answer: "" → INCORRECT, confidence=1.0
- [ ] Non-MCQ question routed to MCQ grader: raises ValueError (defensive)
- [ ] Result tier is always 'mcq' for MCQ inputs
- [ ] needs_followup always False for MCQ (deterministic, no ambiguity)

**Acceptance:**
- [ ] All M2 tests pass
- [ ] Coverage on `_grade_mcq` is 100% (use `pytest --cov`)

**Estimated size:** 45 minutes.

---

## M3 — Math grader (Tier 1)

**Goal:** `_grade_math()` with sympy + latex2sympy2_extended + Math-Verify cascade.

**Files:**
- `apps/tutoring/grader.py` (extend)
- `apps/tutoring/tests/test_grader_math.py` (NEW)

**Cascade order:**
```python
def _grade_math(question, student_answer):
    # 1. Math-Verify (handles most edge cases)
    if math_verify.verify(reference=question.correct_answer, target=student_answer):
        return GradeResult(CORRECT, 1.0, 'math', {}, 'math_verify match', False)
    # 2. sympy + latex2sympy2_extended
    try:
        ref = parse(question.correct_answer)
        ans = parse(student_answer)  # tries latex2sympy2_extended then sympify
        if sympy.simplify(ref - ans) == 0:
            return GradeResult(CORRECT, 1.0, 'math', {}, 'sympy symbolic match', False)
    except Exception: pass
    # 3. Numeric fallback with tolerance
    try:
        if abs(float(ref) - float(ans)) < 1e-6:
            return GradeResult(CORRECT, 0.95, 'math', {}, 'numeric tolerance match', False)
    except Exception: pass
    return GradeResult(INCORRECT, 0.95, 'math', {}, 'no math equivalence found', False)
```

**Tests:**
- [ ] Integer exact: `"42"` vs `"42"` → CORRECT
- [ ] Integer wrong: `"42"` vs `"43"` → INCORRECT
- [ ] Decimal: `"0.5"` vs `"1/2"` → CORRECT (fraction-decimal equivalence)
- [ ] Percent: `"50%"` vs `"0.5"` → CORRECT
- [ ] Fraction: `"3/4"` vs `"0.75"` → CORRECT
- [ ] LaTeX fraction: `"\\frac{1}{2}"` vs `"0.5"` → CORRECT
- [ ] Algebraic equivalence: `"2x+2"` vs `"2*(x+1)"` → CORRECT
- [ ] Algebraic wrong: `"x+1"` vs `"x+2"` → INCORRECT
- [ ] Negative fraction: `"-1/2"` vs `"-0.5"` → CORRECT
- [ ] Trailing units stripped: `"5 km"` parsed correctly (note: full unit-awareness out of scope; just don't crash)
- [ ] Garbage input: `"asdf"` vs `"0.5"` → INCORRECT (no crash)
- [ ] Empty: `""` → INCORRECT
- [ ] Tolerance: `"0.50000001"` vs `"0.5"` → CORRECT
- [ ] Outside tolerance: `"0.51"` vs `"0.5"` → INCORRECT
- [ ] Tier is always 'math' for math questions

**Acceptance:**
- [ ] All M3 tests pass
- [ ] No regressions in `test_grader_mcq.py`
- [ ] Coverage on `_grade_math` ≥ 90%

**Estimated size:** 1.5 hours.

---

## M4 — Embedding gate (Tier 1.5)

**Goal:** `_grade_embedding_gate()` — cosine-similarity-only short-circuit
before the LLM verifier.

**Files:**
- `apps/tutoring/grader.py` (extend)
- `apps/tutoring/tests/test_grader_embedding_gate.py` (NEW)

**Thresholds (tunable):**
- `HIGH_SIMILARITY = 0.92` → auto-CORRECT, tier='embed_gate'
- `LOW_SIMILARITY = 0.35` → auto-INCORRECT, tier='embed_gate'
- In-between → return `None` (fall through to verifier LLM)

**Tests:**
- [ ] Identical text → similarity ≈ 1.0 → CORRECT
- [ ] Near-paraphrase → high similarity → CORRECT
- [ ] Unrelated text → low similarity → INCORRECT
- [ ] Mid-similarity → returns None (caller falls to Tier 2)
- [ ] Empty student answer → INCORRECT (low similarity)
- [ ] Whitespace-only student answer → INCORRECT
- [ ] Uses existing `kb_storage.embed` (don't load model in unit test — mock or skip on SQLite)

**Acceptance:**
- [ ] All M4 tests pass
- [ ] No regressions in M2/M3
- [ ] Doc string explains the threshold trade-off (raise HIGH to reduce
      false-positives; lower LOW to reduce verifier calls)

**Estimated size:** 1 hour.

---

## M5 — Verifier LLM (Tier 2)

**Goal:** `_grade_verifier_llm()` — cross-family verifier with structured
output + optional self-consistency.

**Files:**
- `apps/tutoring/grader.py` (extend)
- `apps/tutoring/tests/test_grader_verifier_llm.py` (NEW)

**Schema (verdict FIRST per `feedback_grading_design_rules`):**
```python
class VerifierResponse(BaseModel):
    verdict: Literal['correct', 'partial', 'incorrect']
    per_criterion_scores: dict[str, float]
    confidence: float
    justification: str
```

**Self-consistency rule:**
- First call → if confidence ∉ [0.5, 0.85], return result
- Else: call 2 more times (total n=3), majority vote on verdict, mean confidence

**Verifier prompt (context-free; gets question + reference + student answer ONLY):**
- Lives at `apps/tutoring/prompts/verifier.py` (new file)
- Templated, no engine state, no conversation history
- Rubric criteria default: `['correctness', 'completeness', 'reasoning']`
- Must use `instructor.from_provider('google/gemini-...')` with `response_model=VerifierResponse`
- Temperature = 0

**Tests:** (mock the LLM via `instructor.patch` or model mock fixture)
- [ ] Mocked clean CORRECT response with confidence=0.95 → returns single call result
- [ ] Mocked clean CORRECT response with confidence=0.7 → triggers self-consistency
- [ ] Self-consistency majority: 2 correct + 1 incorrect → CORRECT
- [ ] Self-consistency tie: 1 each verdict → falls back to highest-confidence verdict
- [ ] Verdict field validation: invalid string raises ValidationError → wrapped as PARTIAL with confidence=0.0
- [ ] Schema field order in the Pydantic class: verdict appears BEFORE justification (introspection test)
- [ ] Verifier prompt does NOT contain the tutoring conversation (regex check)
- [ ] Verifier prompt DOES contain the reference answer
- [ ] LLM call uses temperature=0 (mock asserts on call args)
- [ ] Cross-family enforced: tutor=anthropic → verifier=google (assertion in test)

**Acceptance:**
- [ ] All M5 tests pass
- [ ] No regressions in M2/M3/M4
- [ ] Verifier prompt reviewed against `feedback_grading_design_rules`

**Estimated size:** 2.5 hours.

---

## M6 — Session state utilities

**Goal:** sliding-window history + step-anchored summaries + pose/record state.

**Files:**
- `apps/tutoring/simple_tutor_state.py` (NEW) — pure functions, no I/O
- `apps/tutoring/tests/test_simple_tutor_state.py` (NEW)

**Public API:**
```python
def build_recent_window(session, max_turns=8) -> list[Message]: ...
def build_step_summary(session, step) -> str: ...
def step_summary_log(session) -> list[str]: ...     # one line per completed step
def set_current_question(session, question_id) -> None: ...
def clear_current_question(session) -> None: ...
```

**Tests:**
- [ ] Recent window returns last 8 messages (turn-pair count, not individual)
- [ ] Recent window respects step boundary — never crosses into prior step's turns
- [ ] Empty session → empty window
- [ ] Step summary contains: step phase, mastery (correct attempts), misconceptions extracted from `judge_outputs`
- [ ] Step summary is DETERMINISTIC for a given session+step (no LLM)
- [ ] `set_current_question` mutates session.current_question_id
- [ ] `clear_current_question` sets to None
- [ ] `step_summary_log` returns one line per completed step in order

**Acceptance:**
- [ ] All M6 tests pass
- [ ] Pure functions only — no LLM calls anywhere in this module

**Estimated size:** 1.5 hours.

---

## M7 — Consult prompting experts + system prompt builder

**Goal:** the stateless system prompt template.

**MANDATORY pre-step (per CLAUDE.md):** invoke `prompting-fundamentals-expert`
+ `claude-prompting-expert` skills before writing the prompt. Bring back:
- XML tag conventions for Claude
- Prompt-caching markers for the static prefix
- Tool-use schema format Anthropic 2025
- Anti-injection patterns for student-supplied input
- Hint-gating patterns to defend against the 30% answer-leakage failure

**Files:**
- `apps/tutoring/prompts/simple_tutor_system.py` (NEW)
- `apps/tutoring/tests/test_simple_tutor_prompt.py` (NEW)

**Public API:**
```python
def build_system_prompt(
    *, session, step, kb_chunks, figure_catalog, recent_window, step_summaries
) -> tuple[str, list[dict]]:    # returns (system_text_with_cache_markers, tool_schemas)
```

**Tests:**
- [ ] Prompt contains step.objective verbatim
- [ ] Prompt contains all questions' correct_answer text (for grounding, not display)
- [ ] Prompt contains figure catalog with IDs + descriptions
- [ ] Prompt contains all KB chunks
- [ ] Prompt contains step summary log
- [ ] Prompt contains last-N turns as `<recent_turns>`
- [ ] Anti-injection block exists with `<safety>` tag
- [ ] Static prefix (role + rules + safety) is marked for prompt caching
- [ ] No `{var}` interpolation leakage (regex check)
- [ ] Tool schemas: all 5 tools present (pose_question, record_answer,
      advance_step, request_figure, redirect_off_topic) with correct input_schema
- [ ] Prompt instructs the LLM to call `pose_question` whenever it asks one
      of the catalog's questions
- [ ] Prompt instructs the LLM to call `record_answer` when student responds
      with an answer, and to default to clarification mode when ambiguous

**Acceptance:**
- [ ] Prompting-expert consultation summary attached to PR
- [ ] All M7 tests pass
- [ ] Prompt < 4k tokens for an average step (token-count test)

**Estimated size:** 3-4 hours (1-2 hours of which is consult + iteration).

---

## M8 — Tool definitions + server-side handlers

**Goal:** server-side dispatch for each of the 5 tools. Pure DB mutations.

**Files:**
- `apps/tutoring/simple_tutor_tools.py` (NEW)
- `apps/tutoring/tests/test_simple_tutor_tools.py` (NEW)

**Public API:**
```python
def handle_pose_question(session, *, question_id: int) -> dict: ...
def handle_record_answer(session, *, question_id: int, extracted_answer: str) -> dict: ...
def handle_advance_step(session, *, reason: str) -> dict: ...
def handle_request_figure(session, *, figure_id: int) -> dict: ...
def handle_redirect_off_topic(session, *, reason: str) -> dict: ...
```

Each returns a `tool_result` dict (verdict, figure URL, step name, etc.) that
the engine feeds back into the LLM for the next turn.

**Tests (each handler):**
- [ ] `pose_question`: sets `session.current_question_id` correctly
- [ ] `pose_question` with invalid question_id (not in step's catalog) → returns error result, does NOT mutate session
- [ ] `record_answer`: calls `grader.grade_answer` with correct args, writes to `SessionTurn.judge_outputs`, clears `session.current_question_id`
- [ ] `record_answer` for question not in current step: logs warning, still grades (sometimes student answers earlier question)
- [ ] `advance_step`: increments `current_step_index`, triggers step summary write, clears `current_question_id`
- [ ] `advance_step` past last step: transitions to EXIT_TICKET state
- [ ] `request_figure`: looks up `StepMedia` by figure_id, returns URL
- [ ] `request_figure` with invalid id: returns error result, no DB write
- [ ] `redirect_off_topic`: increments off-topic counter on session metadata; logged

**Acceptance:**
- [ ] All M8 tests pass
- [ ] No I/O in tool handlers other than DB mutations + grader call
- [ ] Idempotency: calling `pose_question` twice with the same id is safe

**Estimated size:** 2.5 hours.

---

## M9 — Engine main loop

**Goal:** `simple_tutor.respond(session, user_input) -> dict` — single LLM
call + tool dispatch + persist.

**Files:**
- `apps/tutoring/simple_tutor.py` (NEW)
- `apps/tutoring/tests/test_simple_tutor.py` (NEW)

**Flow (per turn):**
```python
def respond(session, user_input):
    step = _load_current_step(session)
    kb_chunks = _retrieve_kb(session, user_input)
    figures = _load_figure_catalog(step)
    recent = build_recent_window(session)
    summaries = step_summary_log(session)

    system_text, tools = build_system_prompt(
        session=session, step=step, kb_chunks=kb_chunks,
        figure_catalog=figures, recent_window=recent,
        step_summaries=summaries,
    )

    response = anthropic_client.messages.create(
        model='claude-opus-4-7', system=system_text, tools=tools,
        messages=_format_messages(recent, user_input), max_tokens=1024,
    )

    tool_results = []
    text_reply = ''
    for block in response.content:
        if block.type == 'text':
            text_reply += block.text
        elif block.type == 'tool_use':
            tool_results.append(_dispatch_tool(session, block))

    _persist_turn(session, user_input, text_reply, tool_results)
    return {'content': text_reply, 'tool_calls': tool_results}
```

**Tests (mock the Anthropic client):**
- [ ] Happy path: mock LLM returns text + record_answer call. `grade_answer` runs, verdict in result, `SessionTurn` persisted.
- [ ] Multiple tool calls in one response (pose_question + text)
- [ ] No tool calls (just text reply) — clarification mode
- [ ] LLM call args include `tools=` and `system=` matching M7 output
- [ ] Persisted `SessionTurn.metadata['tool_calls']` contains the dispatch records
- [ ] LLM exception → returns sane fallback response, logs error, does NOT crash session
- [ ] `advance_step` tool call → next turn's prompt reflects the new step
- [ ] Hard cap: 10 turns on same step → engine auto-calls `advance_step` (safety valve)
- [ ] `redirect_off_topic` counter increments correctly

**Acceptance:**
- [ ] All M9 tests pass
- [ ] No regressions in M2-M8 tests
- [ ] Engine module line count ≤ 800 (target)

**Estimated size:** 3 hours.

---

## M10 — Engine selector in views.py

**Goal:** one-line dispatch in the HTTP entry point.

**Files:**
- `apps/tutoring/views.py` — one branch in `respond()` view
- `apps/tutoring/tests/test_views_engine_selector.py` (NEW)

**Code:**
```python
# in apps/tutoring/views.py::respond (the HTTP view, ~line 457)
if session.engine == 'simple':
    from apps.tutoring import simple_tutor
    return JsonResponse(simple_tutor.respond(session, user_input))
# else: existing v1 path unchanged
```

**Tests (Django test client):**
- [ ] Session with engine='v1' routes to conversational_tutor (existing behavior)
- [ ] Session with engine='simple' routes to simple_tutor (mock the call, assert routing)
- [ ] Both engines return the same response shape (`content`, optional `media`, etc.)

**Acceptance:**
- [ ] All M10 tests pass
- [ ] `pytest apps/tutoring/tests/` passes end-to-end
- [ ] No v1 behavior changes for existing sessions

**Estimated size:** 1 hour.

---

## M11 — Staging deployment + E2E lesson test

**Goal:** drive one Geography lesson on staging end-to-end with the new engine.

**Steps:**
1. Push to dev → triggers deploy-staging.yml
2. Create one new TutorSession with `engine='simple'` for the Trade/Development
   lesson (admin shell, since the UI toggle is out of v1 scope)
3. Drive the session via chrome-devtools-mcp OR direct HTTP from local
4. Capture full transcript + judge_outputs

**Acceptance criteria (from `simple_tutor_engine_plan.md`):**
- [ ] Each turn responds within 5s p95
- [ ] KB chunks visibly influence prompting (manual review of system prompt vs response)
- [ ] At least one question graded by each tier: MCQ, math, embed_gate, verifier_llm
- [ ] Step advances when student demonstrates understanding
- [ ] Exit ticket runs to completion with verdicts in SessionTurn.judge_outputs
- [ ] No LLM call decides correctness (grep simple_tutor.py for tutor-LLM grading — must be zero)
- [ ] Engine line count ≤ 800 in simple_tutor.py
- [ ] Side-by-side comparison with v1 on the same lesson: simple engine produces
      a coherent dialogue (eyeball test)

**Risks to watch:**
1. LLM ignores tool-use contract → emits text answers
2. Free-text grader threshold mistuned for this lesson's questions
3. Pre-generated content quality issues (lesson steps too thin)

**Estimated size:** half-day (mostly observation + manual driving).

---

## Total estimate

~20-25 hours of focused work, with M7 (prompting) and M9 (engine loop) being
the largest unknowns. Realistic delivery: 4-5 working days.

## What NOT to do during the build

- Add more tools beyond the 5 (Khanmigo's converged set is enough)
- Re-implement v1's safety valves (each safety valve was a workaround for a
  bug we're now eliminating with deterministic state)
- Skip tests "just for v1" — test gates are what keep the engine ≤ 800 lines
- Stream the response — Azure Container Apps doesn't support SSE; buffered
  JSON matches prod constraints
- Touch `conversational_tutor.py` — it stays untouched until simple_tutor
  proves out on the eval benchmark

## What to surface for sign-off mid-build

1. After M5: paste the verifier prompt + a sample VerifierResponse for review
2. After M7: paste the full system prompt template for the Trade/Development
   lesson with realistic data, for review against `feedback_grading_design_rules`
   and `feedback_simple_tutor_engine_design`
3. Before M11: confirm which lesson + which test user account

Refs: `simple_tutor_engine_plan.md`, `grading_system_research.md`,
`tutor_engine_research.md`,
`auto-memory/feedback_grading_design_rules.md`,
`auto-memory/feedback_simple_tutor_engine_design.md`,
`auto-memory/feedback_deterministic_grading.md`.
