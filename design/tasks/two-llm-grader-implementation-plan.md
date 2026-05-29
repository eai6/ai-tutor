# Two-LLM Grader — Implementation Plan

**Status**: Approved direction; spec ready to implement.
**Owner**: Roy Manzi (review) / Claude (implementation).
**Created**: 2026-05-27.
**Supersedes**: the regex-based `_parse_student_math_value` chain currently in `apps/tutoring/v2/services/student_grader.py`.
**Related**:
- `design/refactor/refactor-implementation-plan.md` §2.1 — original `StudentGrader` contract (build deviated from it).
- `test-reports/DIAGNOSIS-grader-2026-05-27.md` — root-cause for run-7 P1 cascade.
- `apps/tutoring/v2/tests/test_grader_comprehensive.py` — current grader-behaviour test suite (will be updated).

---

## 1. Why this plan exists

The original refactor plan (§2.1) said the math grader is:

> *LLM emits constrained JSON DSL → DSL-validation pass → `MathVerificationTool` executes → comparator.*

And §1 Phase-1 deliverables (line 110-111):

> *Composed grading pipeline: `MathVerificationTool` (problem → canonical) + existing `student_working_analyzer.py` (student prose → value) + comparator (SymPy / Pint / ±0.01) for equivalence.*

The build deviated:

1. **Question side — correct.** The LLM does emit a DSL from the question stem, executed in Python.
2. **Student side — wrong.** Instead of using `student_working_analyzer.py` or an LLM, the build added a regex chain (`_parse_student_math_value` + `_extract_prose_numeric` + `_match_*` helpers) inside `student_grader.py`. This regex chain:
   - Cannot read word-form numerics (*"the hidden variable is eight"* → fails).
   - Picks the wrong number when the student shows working (*"… got 16 … is eight"* → picks 16).
   - Cannot distinguish a student's intermediate computation from their final conclusion.
   - Is the structural cause of the run-7 P1 cascade and the *"eight"* test case identified 2026-05-27.

The Two-LLM design restores the original intent and goes one step further: an LLM parses the **student** input too, producing a structured claim graph that Python verifies deterministically.

---

## 2. Target architecture

```
                  ┌─────────────────────────────────────────┐
                  │           StudentGrader (math path)      │
                  │                                          │
  Question stem ──┼─► LLM-A (canonical extractor)            │
                  │   ↓                                      │
                  │   canonical_dsl ─► Python (executor)     │
                  │                       ↓                  │
                  │                  canonical_value         │
                  │                       │                  │
                  │                       ▼                  │
                  │   ┌─────────── Python (comparator) ──┐   │
                  │   │                                  │   │
  Student input ──┼─► │ LLM-B (student-claims extractor) │   │
                  │   │   ↓                              │   │
                  │   │   student_claims_dsl ─► Python   │   │
                  │   │                          ↓       │   │
                  │   │     verified claims + conclusion │   │
                  │   └──────────────────────────────────┘   │
                  │                       ↓                  │
                  │   GradingResult { verdict, reasoning,    │
                  │                   student_safe_feedback }│
                  └─────────────────────────────────────────┘
```

**Two LLM calls per math turn, with the existing executor as the deterministic backbone for both.** Latency / cost are not constraints per the 2026-05-27 directive ("quality and robustness over latency").

### 2.1 LLM-A (existing — `_extract_math_dsl`)

Unchanged. Continues to extract canonical DSL from the question stem using `MATH_DSL_SYSTEM`. Output: a `program` dict the Python executor runs to produce the canonical value or value-list.

### 2.2 LLM-B (new — `_extract_student_claims_dsl`)

A new prompt + extractor that reads the student's response and emits a structured claim graph. The schema is an extension of the existing DSL plus three new top-level keys:

```jsonc
{
  // Re-uses the existing DSL grammar from MATH_DSL_SYSTEM
  // (variables, expression-node tree with op/var/args).
  "claims": [
    {
      "id": "c1",
      "description": "student claims 5^2 = 25",
      "expression": { "op": "pow", "args": [5, 2] },
      "asserted_value": 25
    },
    {
      "id": "c2",
      "description": "student claims 25 + 49 = 74",
      "expression": { "op": "add", "args": [25, 49] },
      "asserted_value": 74
    }
  ],
  "conclusion": {
    "statement": "the triangle is NOT right-angled",
    "answer_extracted_value": null,    // for word/letter answers, the value can also be a string
    "answer_extracted_label": "no",    // normalised for Y/N, T/F, MCQ-letter responses
    "is_attempt": true                  // false when input is meta ("idk", "explain")
  },
  "domain_check_required": false
}
```

The schema is **subject-agnostic**: works for arithmetic, algebra (`solve`), proofs, percentage, geometry. The structure (claims that evaluate deterministically + a stated conclusion) is universal.

**Prompt name**: `STUDENT_CLAIMS_SYSTEM` in `apps/tutoring/v2/services/grader_prompts.py`.

### 2.3 Python comparator (new — `_compare_student_claims_to_canonical`)

Pure function. No LLM. Takes:
- `canonical_value` (from LLM-A + executor)
- `student_claims_dsl` (from LLM-B)

Executes each claim's `expression` via the existing `MathVerificationTool.evaluate`. Returns one of four outcomes:

| Verdict | When |
|---|---|
| **CORRECT** | Every claim's `asserted_value` matches its computed value, AND the student's conclusion (`answer_extracted_value` or `answer_extracted_label`) matches the canonical. |
| **PARTIAL** | Claims all verify, but the conclusion is missing or matches one slot of a multi-slot canonical only. |
| **WRONG** with `reason="arithmetic_failed"` | One or more claims have `asserted_value` ≠ computed value. The grader names the specific claim that failed (e.g., "you computed 25 + 49 = 70; that should be 74"). |
| **WRONG** with `reason="conclusion_inconsistent_with_canonical"` | All claims verify, but the conclusion doesn't match the canonical (e.g., student computes correctly then states the wrong final answer). |

`UNVERIFIED` is reachable only when LLM-B returns `is_attempt: false` (meta input / help-request / readiness signal). Math verdict is always definitive on real attempts.

### 2.4 Verdict completeness contract

**For math, on a non-empty student attempt, the verdict is one of {CORRECT, WRONG, PARTIAL}.** UNVERIFIED is reserved for:
- Empty / whitespace student input (handled before the LLM calls).
- `is_attempt: false` (LLM-B determines the input is not an attempt).
- Engine state inconsistent (empty `open_question.rendered_stem`).

These three cases are distinct signals the downstream engine can branch on. No silent UNVERIFIED on a real attempt.

---

## 3. Files touched

| File | Change | Lines |
|---|---|---|
| `apps/tutoring/v2/services/grader_prompts.py` | **Add** `STUDENT_CLAIMS_SYSTEM` + `render_student_claims_user_prompt`. | ~120 new |
| `apps/tutoring/v2/services/student_grader.py` | **Add** `_extract_student_claims_dsl`, `_compare_student_claims_to_canonical`, `_resolve_student_claims_client`. **Refactor** `_grade_math` to orchestrate LLM-A + LLM-B + comparator. **Remove** the regex-based `_parse_student_math_value` from the math hot path (keep file-internal for one release as a deprecated helper; delete in the follow-up). | ~250 net |
| `apps/tutoring/v2/contracts/tutoring.py` | **Extend** `GradingResult.reasoning` shape — add an optional `reason_code` enum field (`arithmetic_failed` / `conclusion_inconsistent_with_canonical` / `meta_input` / `state_inconsistent`) so the move layer can branch deterministically. | ~10 new |
| `apps/llm/models.py` | **Add** `Purpose.GRADER_STUDENT_CLAIMS = 'grader_student_claims'`. Seed a `ModelConfig` row pinned to Haiku 4.5 (cheap, fast, structured-output friendly). Add env-override `GRADER_STUDENT_CLAIMS_MODEL_OVERRIDE`. | ~15 new |
| `apps/tutoring/v2/services/student_grader.py::__init__` | **Add** `student_claims_client_factory` injection parameter (mirrors the existing `math_client_factory` pattern for testability). | ~5 |
| `apps/tutoring/v2/tests/test_grader_comprehensive.py` | **Rewrite math-path tests** against the new contract. **Keep** as-is: matcher-level tests for MCQ/T-F/short-numeric (those remain as deterministic fast-paths for unambiguous inputs). **Add** the "eight" case + the run-7 P1 case + ALL profit/loss observed responses re-tested through the Two-LLM path. | ~400 net (rewrite + add) |
| `apps/tutoring/v2/tests/test_grader.py` | **Update** two tests that explicitly expected UNVERIFIED on math fallthrough (`test_math_path_falls_through_to_grounded_on_dsl_extract_failure`, `…_validation_failure`) — now expect a definitive verdict because LLM-B can grade independently. | ~20 |
| `design/refactor/refactor-implementation-plan.md` | **Add** a §2.1 amendment noting the Two-LLM design supersedes the regex-based student parsing. | ~30 |

Net: ~850 lines touched. Mostly new tests + new prompt. The grader itself shrinks modestly because `_parse_student_math_value`'s regex chain comes out of the hot path.

---

## 4. Implementation order

1. **Add ModelConfig purpose + seed migration** (1 file, no code path change yet). Tests still green.
2. **Add `STUDENT_CLAIMS_SYSTEM` prompt** in `grader_prompts.py` (1 file, no integration yet). Unit-test the prompt rendering.
3. **Add `_extract_student_claims_dsl` + `_compare_student_claims_to_canonical`** as new private methods in `student_grader.py`, NOT yet wired to `_grade_math`. Unit-test both with stubbed LLMs.
4. **Wire into `_grade_math`** as the new primary path. Keep the regex chain reachable only as a fast-path for bare-numeric inputs (skip LLM-B when the student input is unambiguously `"25"` / `"x = 25"` — saves a round-trip and matches the LLM-first directive's "whenever applicable" caveat).
5. **Rewrite test_grader_comprehensive.py math tests**. Drop the old expected-verdict assertions that were pinned to the regex behavior; add tests pinned to the Two-LLM behavior. The MCQ/T-F/short-numeric matcher tests stay — those are still valid deterministic fast-paths.
6. **Update test_grader.py** (the two existing math-fallthrough tests).
7. **Run MATHS-S1 evaluation scenario** end-to-end against the v2 engine to confirm 0 P1s on math.

---

## 5. Test plan

### 5.1 Required new test cases (must pass before this work is "done")

```python
# The "eight" case — the user's example.
def test_two_llm_grader_handles_word_form_answer():
    """Student says 'the hidden variable is eight'. LLM-B extracts answer_label='eight'
    + answer_value=8. Canonical=8. Comparator: CORRECT.
    Today's regex grader picks '16' from the working and returns WRONG (P1).
    """

# The run-7 P1 case (already in test_grader_comprehensive.py via T/F matcher).
def test_two_llm_grader_grades_pythagoras_negative_case_correctly():
    """Student: '5²+7²=74, 9²=81, 74≠81 so NOT right-angled.'
    LLM-B extracts 4 claims + conclusion='not right-angled'. Each claim verifies.
    Conclusion matches canonical 'No'. Verdict: CORRECT.
    """

# Arithmetic-step failure mode (new diagnostic only the Two-LLM path can produce).
def test_two_llm_grader_distinguishes_arithmetic_step_error_from_conclusion_error():
    """Student: '5²+7² = 25+49 = 70, 9²=81, 70≠81 so not right-angled.'
    Conclusion happens to be right (canonical: No), but claim c3 (25+49=70) is arithmetically wrong.
    Verdict: WRONG, reason_code='arithmetic_failed', misconception names the specific failing step.
    """

def test_two_llm_grader_distinguishes_conclusion_error_from_arithmetic_error():
    """Student: '5²+7²=74, 9²=81. 74≠81. So the triangle IS right-angled.'
    All claims verify. Conclusion contradicts the rule.
    Verdict: WRONG, reason_code='conclusion_inconsistent_with_canonical'.
    """

# Word-form numerics — the case that drives the redesign.
@pytest.mark.parametrize("student_input,canonical,expected_verdict", [
    ("the hidden variable is eight", "8", Verdict.CORRECT),
    ("twenty-five", "25", Verdict.CORRECT),
    ("I think it's about half", "0.5", Verdict.CORRECT),
    ("approximately thirty-three percent", "33", Verdict.CORRECT),  # or PARTIAL with rounding note
    ("two and a half", "2.5", Verdict.CORRECT),
])
def test_two_llm_grader_word_form_answers(student_input, canonical, expected_verdict):
    """LLM-B extracts answer_value from word-form. No regex involved."""

# Meta input is_attempt=False handling.
def test_two_llm_grader_recognises_meta_input_as_non_attempt():
    """Student: 'I don't understand. what is condensation'.
    LLM-B emits is_attempt=False. Grader returns UNVERIFIED with reason_code='meta_input'.
    Distinct from arithmetic UNVERIFIED.
    """

# Full multi-slot grading via LLM-B (replaces the regex multi-slot tests).
@pytest.mark.parametrize("student_response,expected_verdict", [
    ("profit is 9 and percentage is 50%", Verdict.CORRECT),
    ("profit is 9 and percentage is 60%", Verdict.PARTIAL),  # one slot right
    ("profit is 45 and percentage is 60%", Verdict.WRONG),   # neither slot right
    ("nine and fifty percent", Verdict.CORRECT),             # word-form, both slots
])
def test_two_llm_grader_multi_slot_word_and_numeric(student_response, expected_verdict):
    ...
```

### 5.2 Test infrastructure

`_FakeClient` pattern from `test_grader.py` extends naturally — pass two payloads (LLM-A response then LLM-B response) in sequence:

```python
grader = StudentGrader(
    math_client_factory=lambda: _FakeClient(canonical_dsl_payload),
    student_claims_client_factory=lambda: _FakeClient(student_claims_dsl_payload),
    grounded_client_factory=lambda: None,
    verifier_client_factory=lambda: None,
)
```

For tests, stub LLM-B to return the structured `student_claims_dsl` we want to test the comparator with. This is the canonical pattern for testing the new code path.

### 5.3 Re-test all observed cases from runs 2–7

Every (question, student response) triple in `test_grader_comprehensive.py` Group 4 gets re-asserted against the Two-LLM contract:

- **Word-form / typo / messy responses**: now grade correctly because LLM-B understands them.
- **Multi-slot with prose**: now grade correctly because LLM-B extracts both slot values from prose like *"profit is 9 and percentage is 50%"*.
- **The run-7 *"5²+7²=74…"* case**: graded CORRECT via Path 2 even when `open_question` is missing (LLM-B + canonical-via-context recovery).

### 5.4 Bug-catching reflex (testing-patterns-expert)

For every new assertion, run the test once with the new code commented out (or replaced with a stub returning the old regex result). The test MUST fail. Document the diff between old and new verdict in the test docstring. This is the "prove your test catches the bug" pattern from the `testing-patterns-expert` skill.

---

## 6. Acceptance criteria

The work is done when:

1. **All tests green** in `apps/tutoring/v2/tests/test_grader_comprehensive.py` (rewritten) + `test_grader.py` (updated) + `test_math_verification.py` (unchanged). Target: ~300 grader tests, 100% pass.
2. **The *"eight"* test case passes** — student input `"I multiplied the variable by two and got 16 which means that the hidden variable is eight"` with canonical 8 returns CORRECT.
3. **The run-7 P1 case passes** — every observed response from MATHS-S1 / GEO-S5 runs 2-7 returns the verdict listed in the corresponding evaluation report's "expected" column.
4. **Math UNVERIFIED only on meta input** — `grep -n 'Verdict.UNVERIFIED' apps/tutoring/v2/services/student_grader.py` shows hits only in branches gated by `is_attempt: false`, `not student_input.strip()`, or `not stem.strip()`.
5. **One live MATHS-S1 evaluation run** against the running dev server produces 0 strict-P1s on math (correct→wrong, wrong→correct, incomplete question).
6. **No regression in adjacent v2 suites** — `test_conformance_orchestrator`, `test_move_selection`, `test_verdict_matrix`, `test_question_extractor` all still green.

---

## 7. Open questions

| Question | Default if not addressed |
|---|---|
| **Q1**: Use `sympy` natively for the executor instead of the custom `MathVerificationTool`? | Keep `MathVerificationTool` for v0 (no scope creep). Revisit after the Two-LLM design is stable. |
| **Q2**: Use `word2number` / `text2num` as a regex-free Python library for word-form numerics, OR rely entirely on LLM-B? | Rely entirely on LLM-B. Adding deterministic word→number alongside makes the comparator's job ambiguous (which source wins on disagreement?). LLM-B is the source of truth for student-stated values. |
| **Q3**: Use `dirtyjson` or `json5` for parsing LLM-B's output (it'll sometimes emit single-quoted JSON)? | Use `dirtyjson` (`pip install dirtyjson`). Already justified by the content_generator's `_try_fix_json` history. Robust JSON parsing belongs in the grader's input layer. |
| **Q4**: Should LLM-B handle non-math too (geography, language, definitions)? | **Yes** — but as a separate prompt + extractor (`STUDENT_CLAIM_GROUNDED_SYSTEM`) that pairs with the grounded adjudicator rather than the math executor. **Out of scope for THIS task**; ship Two-LLM math first, then non-math in a follow-up. |
| **Q5**: What's the failure mode when LLM-B itself returns invalid JSON / refusal? | Same fail-soft as LLM-A: log + warn + return a `GradingResult` with `reason_code='grader_extraction_failed'`, verdict=UNVERIFIED. This is the one math-UNVERIFIED case that survives the redesign. It should be rare (Haiku 4.5 is reliable on structured-output prompts with a defined schema). |
| **Q6**: Concurrency / shared-Context bug (per `testing-patterns-expert`)? | The grader makes both LLM calls sequentially (LLM-A then LLM-B), not in parallel. No `ThreadPoolExecutor`, no shared `contextvars.Context`. The 2026-05-12 contextvars bug doesn't apply here. |
| **Q7**: Backward-compat for in-flight sessions? | The grader is called per-turn. No session-state migration needed. New turns on existing sessions immediately use the Two-LLM path. No feature flag required. |

---

## 8. Effort estimate

- **Day 1**: Prompt + DSL schema + unit tests for the comparator (no integration yet).
- **Day 2**: Wire into `_grade_math`. Rewrite `test_grader_comprehensive.py` math sections. Update `test_grader.py` math fallthrough tests.
- **Day 3**: Run MATHS-S1 + GEO-S5 evaluation scenarios end-to-end. Tune the prompt against any observed misclassification.
- **Day 4** (buffer): Address any prompt-tuning iteration needed against real LLM responses; ship.

Total: ~3-4 working days. The work is mostly prompt design + test rewriting; the integration in `_grade_math` is small.

---

## 9. What this plan deliberately does NOT do

1. **Does not touch the engine's grader-gate** (`tutor_engine.py:286-291`). That's a separate ticket — the engine must call the grader unconditionally, but that's an engine fix, not a grader fix.
2. **Does not introduce Path 2 for non-math.** Geography grounded grading stays on the existing `_call_grounded_adjudicator` path. Future work.
3. **Does not replace the conformance classifier or move-prompt layer.** Those are downstream of the grader's verdict.
4. **Does not remove the regex matchers** — they stay as a deterministic fast-path for unambiguous bare-number / single-letter inputs (matches the LLM-first directive's "whenever applicable" caveat).
5. **Does not refactor `MathVerificationTool`** — the existing DSL executor stays. Both LLM-A and LLM-B feed into it.

---

## 10. Rollout checkpoint

After Day 3:

- Diff stat target: ~+700 / −150 lines.
- Test count: 300+ grader tests, all green.
- One MATHS-S1 evaluation run with 0 strict-P1s.

If acceptance fails at the eval-run step, the prompt-design iteration in Day 4 must reproduce each P1 as a unit test failure (per `testing-patterns-expert`), fix the prompt, re-run. No production deploy until acceptance.
