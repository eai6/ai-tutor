# Math Tutor False-Positive Fix — Plan (2026-04-24)

## Problem

The tutor sometimes praises wrong answers while simultaneously stating the correct one as if the student said it. Live example from a production session:

- Student: `"3 3/4"` (wrong — expected `"5 1/4"` for 21÷4)
- Tutor: `"Brilliant, Vaani! You've got it — 21/4 = 5 1/4 kg. You correctly divided 21 by 4 to get 5 whole groups with 1 left over."`

Worse, this is just the surface. Throughout the same session the student gave three bare answers in a row ("7 x 6/8 = 42/8", "simplify to 21/4", "3 3/4") with no working shown — violating the explicit "no bare-answer evaluation" rule in the math system prompt.

## Root cause (from audit)

**The evaluator runs AFTER the tutor has already spoken.** Sequence in `apps/tutoring/conversational_tutor.py::respond()`:

1. Line 1215 — `_generate_contextual_response()` calls the LLM and gets the tutor reply
2. Line 1223 — parse media signal
3. Line 1227 — `verify_calculations()` fixes the tutor's own arithmetic (not the student's)
4. Line 1288 — **tutor response saved to DB and returned to user**
5. Line 1250 — `_analyze_student_response()` evaluates correctness (via `_evaluate_step()` at line 3006)
6. Line 3328 — `self.last_answer_correct` updated

Step 5/6 are effectively observational. They update `is_correct` state but do nothing to correct the already-sent response.

**Other contributing factors**:

- `apps/tutoring/grader.py::grade_numeric()` (line 84-128) exists but is **not called from the tutor flow** — it's only used for exit ticket grading. And it doesn't parse fractions or mixed numbers (`float("21/4")` → ValueError, `float("3 3/4")` → ValueError). So even if we called it, it would fail silently.
- `math_tools.verify_calculations()` validates the *tutor's* arithmetic, not the *student's* answer against the expected answer.
- Math system prompt (line 2549-2621) contains strong instructions ("Do NOT say 'correct', 'right', 'good answer'... until the student has shown their working"). The LLM ignores them regularly. Prompt-only enforcement is insufficient.
- `Course.is_math` (line 49-53 in `apps/curriculum/models.py`) is a keyword match on `Course.title` — brittle. A course titled "Fractions Review" wouldn't be detected as math.
- `SessionTurn.metadata` is `JSONField(default=dict)` but never populated — rich ground for logging eval results, unused today.
- Existing tests (`apps/tutoring/tests/test_keyword_evaluator.py`) only cover the keyword fallback, not the LLM evaluator. No math-specific tests.

## Target design

Fix in four layers, applied in order. Each adds defense-in-depth.

### Layer 1 — Deterministic numeric check (PRE-generation)

Before the tutor LLM is called, do a cheap deterministic check for math steps:

```
student_input: "3 3/4"
expected_answer: "5 1/4"  (from LessonStep.expected_answer)

parsed_student = 3.75
parsed_expected = 5.25
deterministic_is_correct = False   (diff > tolerance)
```

Parser handles: integers, decimals, improper fractions (`"21/4"` → 5.25), mixed numbers (`"3 3/4"` → 3.75), percentages (`"75%"` → 0.75), units (`"5 1/4 kg"` — strip unit first).

If the expected answer can't be parsed as a number (free-text math explanation), layer 1 returns `None` and we fall through to later layers.

### Layer 2 — Inject the truth into the response prompt

When layer 1 returns a definite result, inject it into the system prompt used to generate the tutor reply:

```
<evaluation_signal>
Student's numeric answer: 3.75
Expected numeric answer: 5.25
This answer is INCORRECT.
You MUST NOT say "correct", "right", "brilliant", "well done", "you got it".
Ask the student to show their working. Do not state the correct answer yet.
</evaluation_signal>
```

This makes the constraint a fact the LLM has to respect at generation time, not a rule it might ignore. It also means the LLM can't hallucinate "21/4 = 5 1/4" as if the student said it — because the prompt tells it the student said 3.75.

### Layer 3 — Post-generation praise filter (guardrail)

After the tutor response is generated, if `deterministic_is_correct == False`, strip praise words and re-prefix the response:

```
praise_words = [
  "brilliant", "correct", "right", "exactly", "perfect",
  "you got it", "well done", "excellent", "nicely done",
  "that's it", "spot on", "great job",
]
```

Regex-strip (case-insensitive) from the first sentence. If stripping mangles the response (more than N praise hits), replace the first sentence with `"Let's check this one together."` instead of partial-mangling.

This is defense-in-depth — layer 2 should prevent praise generation; layer 3 catches cases where the LLM defies the prompt anyway.

### Layer 4 — Working-required gate for practice steps

For math steps of `step_type` in `{practice, quiz}` with a multi-step `expected_answer`, detect bare answers (just a number, no explanation) and reject with:

> "Walk me through how you got that — what steps did you take?"

Track `bare_answer_count` per step in `engine_state`. After 2 bare answers → teacher-visible flag. Don't punish: student may have legitimate reasons (dyslexia, language barrier). Flag, don't block.

## Data model changes

Minimal additions, all backwards-compatible.

**`SessionTurn.metadata`** (already exists, currently unused) — populate with:

```json
{
  "is_correct": false,
  "student_answer_parsed": 3.75,
  "expected_answer_parsed": 5.25,
  "eval_layer": "deterministic_numeric",
  "eval_reasoning": "numeric mismatch: 3.75 vs 5.25",
  "praise_stripped": true,
  "bare_answer": true
}
```

This unlocks future regression queries and teacher visibility without schema migration.

**New field on `Course`** (optional, low priority):

```python
# apps/curriculum/models.py::Course
subject_type = models.CharField(
    max_length=20,
    choices=[('math', 'Math'), ('science', 'Science'), ('humanities', 'Humanities'), ('language', 'Language'), ('other', 'Other')],
    blank=True,
)
```

Backfill via data migration using existing `MATH_KEYWORDS` logic, then prefer `subject_type` over title-keyword matching. Defer to Phase M8; not blocking.

## Code changes (file:line)

### `apps/tutoring/grader.py`

- Extend `extract_number(s: str) -> Optional[float]` (line 93) to handle:
  - Mixed numbers: `"3 3/4"` → 3.75
  - Improper fractions: `"21/4"` → 5.25
  - Percentages: `"75%"` → 0.75
  - Units stripped: `"5 1/4 kg"` → parse just the numeric part
- Keep current float/comma/dollar handling.
- Add unit tests in `apps/tutoring/tests/test_grader_numeric.py`.

### `apps/tutoring/conversational_tutor.py`

- New method `_deterministic_math_check(student_input, step) -> Optional[DeterministicCheckResult]` (near `_evaluate_step`)
  - Returns `(is_correct: bool, student_parsed: float, expected_parsed: float)` or `None` if non-numeric
- Modify `respond()` around line 1215:
  1. Call `_deterministic_math_check()` BEFORE `_generate_contextual_response()`
  2. If result is definite, inject `<evaluation_signal>` into system prompt (new helper `_inject_math_eval_signal()`)
  3. Generate tutor response with the constraint baked in
  4. After generation, run praise filter (`_strip_praise_if_wrong()`) when layer-1 said incorrect
- New method `_strip_praise_if_wrong(response_text, is_correct) -> str`
  - Regex-strip praise words from first sentence
  - If >N hits, replace first sentence with neutral prefix
- `_analyze_student_response()` (line 3309) — populate `SessionTurn.metadata` with eval results
- `_evaluate_step()` (line 3006) — if layer-1 returned definite, use that instead of LLM evaluation (cheaper + more reliable); else fall through to current LLM logic
- Bare-answer detection helper `_is_bare_answer(student_input, step) -> bool` — integer/float/fraction alone, no words. Engine-state counter update.

### `apps/tutoring/models.py::SessionTurn`

- No schema change. Start writing to existing `metadata` JSONField.

### Math system prompt (line 2549-2621)

- Add one line at the top of the block: `"Before any response, check <evaluation_signal> in this prompt. If present, obey it strictly — do not praise if marked INCORRECT."`
- Keep existing rules; they're good, we just need teeth.

### `apps/curriculum/models.py::Course`

- Add `subject_type` field + migration (Phase M8 — defer).

## Regression test using production chat history (user's explicit ask)

**Data available**: all `SessionTurn` rows in the DB. Many math sessions with bare answers + praise words.

### One-off audit script (`scripts/audit_math_false_positives.py`)

```python
"""Find historical cases where the tutor praised an answer the deterministic
check would have rejected. Produces a CSV for manual review."""

from apps.tutoring.models import SessionTurn
from apps.tutoring.grader_ext import parse_math_answer, numeric_equals
from apps.curriculum.models import LessonStep

PRAISE = re.compile(r"\b(brilliant|correct|you got it|exactly right|perfect|well done|spot on)\b", re.I)

def audit():
    results = []
    # Iterate tutor turns with math lessons, paired with preceding student turn
    for tutor_turn in SessionTurn.objects.filter(
        role='tutor',
        session__lesson__unit__course__title__iregex=r"\b(math|algebra|geometry|fraction)\b",
    ).select_related('session', 'step').iterator():
        if not PRAISE.search(tutor_turn.content):
            continue
        prior = SessionTurn.objects.filter(
            session=tutor_turn.session,
            created_at__lt=tutor_turn.created_at,
            role='student',
        ).order_by('-created_at').first()
        if not prior or not tutor_turn.step:
            continue

        student_parsed = parse_math_answer(prior.content)
        expected_parsed = parse_math_answer(tutor_turn.step.expected_answer)
        if student_parsed is None or expected_parsed is None:
            continue

        if not numeric_equals(student_parsed, expected_parsed, tolerance=0.01):
            results.append({
                "session_id": tutor_turn.session_id,
                "step_id": tutor_turn.step_id,
                "student_said": prior.content[:100],
                "student_parsed": student_parsed,
                "tutor_said": tutor_turn.content[:200],
                "expected": tutor_turn.step.expected_answer,
                "expected_parsed": expected_parsed,
            })
    return results
```

### Uses

1. **Baseline**: how many false-positive praises exist in production? N = size of results.
2. **Sampling**: manually review 30 results to validate the parser + detector have low false-alarm rate. Adjust praise regex + parser edge cases.
3. **Regression harness** (Phase M6): re-run each case through a mocked version of the new `respond()` path, verify layer 1+3 would catch it.
4. **Teacher reach-out** (optional): the flagged sessions represent students who may have been miscertified as mastered. Could regenerate progress after the fix ships.

### Privacy / safety

- Script runs locally against prod DB read-only snapshot, not the live DB.
- Output CSV includes session_id but not personal identifiers; don't email it externally.
- Don't retroactively demote students' `mastery_level` — flag for teacher review only.

## Test strategy

### Unit tests (new)

- `apps/tutoring/tests/test_grader_numeric.py`:
  - `parse_math_answer("3 3/4") == 3.75`
  - `parse_math_answer("21/4") == 5.25`
  - `parse_math_answer("5 1/4 kg") == 5.25`  (unit stripped)
  - `parse_math_answer("75%") == 0.75`
  - `parse_math_answer("not a number") is None`
- `apps/tutoring/tests/test_praise_filter.py`:
  - `_strip_praise_if_wrong("Brilliant! You got 5.", is_correct=False)` doesn't contain "brilliant" or "got it"
  - Preserves content when `is_correct=True`

### Integration tests (new)

- `apps/tutoring/tests/test_math_eval_integration.py`:
  - Fixture: math lesson, step with `expected_answer="5 1/4"`, student says "3 3/4"
  - Mock LLM to return a "Brilliant, you got it" style response
  - Assert final `TutorMessage.content` does NOT contain praise words
  - Assert `SessionTurn.metadata['is_correct'] == False`
  - Assert `engine_state['last_answer_correct'] == False`
- Same fixture with correct student answer → tutor response can praise, `is_correct=True`

### Regression harness

- Take 30 sampled cases from the audit CSV
- Replay through the new `respond()` path (with LLM mocked to return the historical tutor response)
- Assert all 30 cases now get correctly flagged as incorrect + praise stripped

## Out of scope for this iteration

1. **Non-math subjects.** The same "tutor praises wrong answer" bug can happen in Science/etc. Layers 2+3 help there too (once the evaluator runs), but deterministic-check layer 1 is math-specific. Broader rollout after math fix stabilizes.
2. **LLM evaluator replacement.** Current `_evaluate_step()` stays for non-math and for free-text math. Not rewriting the whole evaluator.
3. **Student answer confidence scoring.** Could detect guessing patterns (e.g., random changes on every attempt). Pedagogical, not pressing.
4. **Automatic retroactive progress demotion.** Sessions marked mastered based on false-positive praise won't auto-demote; teacher review only.
5. **Real-time teacher alert** when the bug fires. Log to `metadata`, surface on dashboard later.
6. **Multi-language praise regex.** English only for now; Seychelles pilot uses English-primary instruction.
7. **Bare-answer rejection for non-practice step types.** Layer 4 applies only to `practice` and `quiz` steps initially.

## Phased delivery

| Phase | Work | Files | Est. |
|---|---|---|---|
| **M1** — Numeric parser | Extend `extract_number()` to handle fractions, mixed numbers, percentages, units. Unit tests. | `apps/tutoring/grader.py`, new `test_grader_numeric.py` | 0.5d |
| **M2** — Deterministic check + signal injection | Add `_deterministic_math_check()`, `_inject_math_eval_signal()`. Modify `respond()` to run pre-generation. | `apps/tutoring/conversational_tutor.py` | 1d |
| **M3** — Praise filter | Add `_strip_praise_if_wrong()`. Wire post-generation. | `apps/tutoring/conversational_tutor.py`, new `test_praise_filter.py` | 0.5d |
| **M4** — Eval metadata | Populate `SessionTurn.metadata` with eval results in `_analyze_student_response()`. | `conversational_tutor.py` | 0.5d |
| **M5** — Audit script + baseline | Write `scripts/audit_math_false_positives.py`, run against prod snapshot, review 30 samples, tune parser. | `scripts/`, CSV output | 1d |
| **M6** — Regression harness | Replay sampled audit cases through the new path. Must-pass gate before deploy. | `test_math_eval_regression.py` | 1d |
| **M7** — Integration tests | End-to-end tests for the new flow. | `test_math_eval_integration.py` | 0.5d |
| **M8** — Improve `is_math` detection (defer) | Add `Course.subject_type` + migration + backfill. Replace keyword match. | `apps/curriculum/models.py`, migration | 0.5d |
| **M9** — Bare-answer gate | Layer 4 — detect bare answers, respond with "show working" prompt, engine_state counter. | `conversational_tutor.py` | 0.5d |

**Critical path**: M1 → M2 → M3 → M4 → M5 → M6 → deploy. ~4 days focused work.

M7 can ship alongside M2/M3 incrementally. M8 is a cleanup, not blocking. M9 is a pedagogy enhancement after the false-positive hotfix.

## Open questions (confirm before M1)

1. **Praise words list**. Should it include polite-mild words like "good", "okay", "I see"? Or only strong affirmations?
   **Recommend**: strong affirmations only — "brilliant", "correct", "right", "exactly", "perfect", "you got it", "well done", "excellent", "nicely done", "that's it", "spot on", "great job". Mild acknowledgments ("I see", "okay, let's look") are pedagogically fine and shouldn't be stripped.

2. **Tolerance for numeric equality**. Exact match for integers and fractions; tolerance for messy real-world values?
   **Recommend**: relative tolerance 1e-6 when both values are rational (fraction/integer), relative tolerance 1e-3 when decimals are involved. Handle "5.25 vs 5 1/4" as exactly equal.

3. **What if the LLM ignores the `<evaluation_signal>` injection?** Layer 3 (regex strip) should catch it, but the stripped response might read awkwardly.
   **Recommend**: if more than 2 praise words hit or the first sentence becomes nonsense after strip, replace the first sentence with `"Let's check this one together — can you walk me through your steps?"`. The full response continues, sans praise.

4. **What if `expected_answer` is something like `"any whole number from 1-10"`?** Non-numeric acceptable-answer patterns exist.
   **Recommend**: layer 1 returns `None` in that case → fall through to current LLM evaluator. Only activate deterministic check when `expected_answer` parses to a single number.

5. **Should we regenerate the tutor response entirely if layer 1 says wrong**, instead of just stripping praise?
   **Recommend**: no, not in this iteration. Regeneration is 2× LLM cost and adds latency. The inject-signal-at-generation approach (layer 2) should make regeneration unnecessary. Strip is belt-and-suspenders.

6. **Do we run the deterministic check for all math steps, or only practice/quiz?**
   **Recommend**: all math steps with a numeric `expected_answer`. `teach` and `worked_example` rarely have an expected student answer field populated, so this is effectively self-scoping.

7. **How do we handle the "3 bags of rice" word-problem context (kg suffix)?**
   **Recommend**: parser strips common unit suffixes (`kg`, `cm`, `m`, `l`, `ml`, `s`, `$`, `%`) before numeric extraction. If student or expected answer has a unit mismatch (kg vs g), that's a correctness issue layer 1 should flag.

## What NOT to do

- **Don't** replace the LLM evaluator wholesale — it works for free-text math explanations where deterministic check can't help
- **Don't** block the UI on regeneration if the LLM defies layer 2 — strip and move on
- **Don't** auto-demote students based on the audit — teacher review only
- **Don't** ship without the regression harness (M6). This bug hid for a long time; we want positive evidence it's fixed before deploy
- **Don't** extend the fix to non-math subjects in this iteration — validate the pattern on math first

## Risks

1. **Parser false negatives** (valid answers parse to `None`) → layer 1 misses the case, we fall through to current broken flow. Mitigation: audit CSV surfaces these; widen parser iteratively.
2. **Parser false positives** (two unequal values parse as equal) → rare with relative tolerance, but possible. Mitigation: log all layer-1 decisions to metadata; periodic review.
3. **Re-sequenced `respond()` breaks an untested path** → there are many code paths in `respond()` (remediation, review mode, exit-ticket transition). Mitigation: integration tests cover the main paths before deploy; feature-flag the new path if nervous.
4. **Regression harness is hard to run headless** (LLM calls). Mitigation: harness mocks the LLM and substitutes historical tutor responses. Tests the layer-1+3 logic, not the LLM.
5. **Teacher workflow disruption** if praise-stripping makes responses feel terse. Mitigation: layer-2 signal injection should produce appropriate tone; test with real math teachers before wide rollout.

## Next step

Confirm open-question defaults above, then begin **M1 — numeric parser extension**. Self-contained, unit-testable, unblocks M2-M6 in sequence.
