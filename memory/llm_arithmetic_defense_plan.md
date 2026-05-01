# LLM Arithmetic Defense — Plan (2026-04-30)

## Problem

LLMs cannot reliably do multi-step arithmetic. This breaks the AI Tutor
in three places, each needing its own defense:

1. **Tutor's own arithmetic at runtime** — already partly handled by
   `apps/tutoring/math_tools.py::verify_calculations` and the praise
   filter (see `memory/math_tutor_fix_plan.md`). Just patched in this
   branch with N-term sums, ✓/✗ markers, context-aware openers, and
   bare-answer guidance even without a deterministic check.
2. **Generated curriculum content** — wrong arithmetic baked into
   lesson steps and exit-ticket questions. Worse than runtime errors
   because they persist and affect every student. Includes the most
   damaging failure mode: a wrong answer key marking correct student
   answers as wrong.
3. **Student's working at runtime** — the tutor today doesn't
   step-by-step verify what the student wrote. Two visible bugs:
   (a) when the student gives wrong working with the right final
   answer, the tutor sometimes confirms; (b) when the student stops
   partway through a multi-step solution, the tutor sometimes
   completes the problem for them ("so x = 85") instead of asking
   what comes next.

This plan addresses (2) and (3). The defense has **five layers**, four
at content-generation time (Layers 1-4) and one at runtime (Layer S).
The runtime work assumes (1) is already shipped.

## Current state (from audit)

### What already exists

**`apps/tutoring/math_tools.py::verify_calculations`** (just extended
in this branch):

- Signature: `verify_calculations(text: str) -> tuple[str, list[dict]]`
- Catches: N-term sums (4+ operands), 3-term BIDMAS chains, sequential-
  equals (`a op b = c op d = e`), simple binary, ✓/✗ markers
- Corrections shape: `[{'expression': str, 'claimed': str, 'correct': str}]`
- Logs: `logger.warning("[MathCheck] Fixed ...")`
- Used today only by the tutor runtime in `conversational_tutor.py:1536` and `:1796`

**`apps/tutoring/question_validator.py`** — partial Layer 2 already in
production:

- `is_broken(q) -> Optional[str]` (line 156): drops broken questions
- `verify_fill_in_blank` (line 81): for single-blank numeric, parses
  the equation in the stem (`a + b + c + ___ = total`) and checks the
  stored blank against the computed difference. Tolerance 0.5.
- `verify_mcq_arithmetic` (line 119): same equation parser, checks the
  correct-letter option's first number against the computed answer.
- `_EQUATION_RE` (line 66): only matches `<sum of nums> + <blank> = <total>` —
  no multiplication, no algebra, no rearranged forms.
- `explanation_looks_broken` (line 49): rationalization-pattern detector
  ("wait, let me recalculate", "yes!" at end of explanation).
- Wired into `apps/curriculum/content_generator.py:1464-1483` —
  questions that fail validation are **silently dropped**, never
  regenerated.

### What's missing

- `verify_calculations` is **not called** during content generation.
  Step content (`teacher_script`, `educational_content.worked_example`,
  `educational_content.common_mistakes`, hints) goes to DB unchecked.
- `question_validator` only knows the additive-equation form. Anything
  else (multiplication, multi-step algebra, word problems with derived
  numbers) is unverifiable today.
- No retry on validation failure — a broken question is dropped, not
  fixed. The exit ticket may end up with fewer questions than requested.
- No audit trail. When an arithmetic error is caught + fixed, there's
  no record for teacher review.
- Math-prone content has no parametric form. Every numeric question is
  free-form prose with the LLM responsible for both setup AND
  arithmetic.

### Generation pipeline (file:line)

| Stage | Location | Notes |
|---|---|---|
| Lesson step generation | `apps/curriculum/content_generator.py:737-1088` (`_generate_steps`) | Has profile-validation retry (1 retry, line 1067), no math retry |
| Step persistence | `apps/curriculum/content_generator.py:1149-1193` (`update_or_create`) | The boundary where we insert Layer 1 for steps |
| Exit ticket generation | `apps/curriculum/content_generator.py:1228-1536` (`generate_exit_ticket_for_lesson`) | Single LLM call, no retry on failure |
| Exit ticket persistence | `apps/curriculum/content_generator.py:1530` (`ExitTicketQuestion.objects.create`) | Boundary for Layer 1 + Layer 2 on questions |
| Existing question filter | `apps/curriculum/content_generator.py:1464-1483` (`is_broken(q)`) | Drops, doesn't retry |

### Storage shape facts

- `Lesson.metadata` is a JSONField (line 213, default `dict`). Currently
  carries `{"teaching_steps": [...]}`. **Adding a `verification_audit`
  key needs zero migration.**
- `LessonStep.curriculum_context` is JSONField. Holds pedagogical metadata.
  Could carry per-step audit info, but `Lesson.metadata` aggregation
  reads better for teacher review.
- `ExitTicketQuestion` has no metadata JSONField. Closest is
  `answer_data` (typed-shape per question type). Adding a new
  `verification_audit` field on `ExitTicketQuestion` would require
  migration. Alternatives: roll into `answer_data['_audit']` (no
  migration; mildly hacky) or aggregate into the parent
  `ExitTicket.metadata` (doesn't exist today; would need migration).

### Dependencies

- `sympy==1.14.0` is in `requirements.txt`. Not used anywhere today.
- `instructor` is the LLM-structured-output library; `_call_llm` (line
  1018) goes through it.

## Target design

Four layers, applied in roughly this order during generation:

```
LLM → Layer 1 (verify arithmetic in prose) →
      Layer 2 (verify answer keys vs. stems) →
      Layer 3 (retry once if either fails) →
      Layer 4 (parametric path: skip layers 1-3 entirely for templated questions)
```

Layers 1-3 are defensive: they assume the LLM produces free-form prose
and try to catch errors. Layer 4 is **eliminative**: for question
patterns we move to templates, arithmetic errors become impossible by
construction.

We ship 1 + 2 + 3 in that order. Layer 4 is its own project, started
after we have audit-log evidence of which question types fail most.

---

### Layer 1 — Verify arithmetic in generated prose

**Goal**: catch wrong arithmetic in `teacher_script`, worked-example
prose, hints, common-mistakes call-outs, and free-form question text
before they're saved.

**Where**: two insertion points in `apps/curriculum/content_generator.py`.

#### Insertion point A — step persistence

Location: `apps/curriculum/content_generator.py:1149-1193` (the loop
that calls `LessonStep.objects.update_or_create`).

Before each `update_or_create`, run `verify_calculations` over every
text-bearing field of the step:

```python
from apps.tutoring.math_tools import verify_calculations

def _verify_step_arithmetic(step_data: dict, lesson, audit: list) -> dict:
    """Returns step_data with corrected arithmetic; appends to audit."""
    if not lesson.unit.course.is_math:
        return step_data
    fields_to_check = (
        'teacher_script',
        'question',
        'expected_answer',
        'rubric',
        'hint_1', 'hint_2', 'hint_3',
    )
    for field in fields_to_check:
        original = step_data.get(field) or ''
        if not original:
            continue
        corrected, corrections = verify_calculations(original)
        if corrections:
            step_data[field] = corrected
            audit.append({
                'step_order': step_data.get('order_index'),
                'field': field,
                'corrections': corrections,
            })
    # Nested educational_content
    edu = step_data.get('educational_content') or {}
    for key in ('worked_example', 'common_mistakes', 'key_points'):
        val = edu.get(key)
        if isinstance(val, str) and val:
            corrected, corrections = verify_calculations(val)
            if corrections:
                edu[key] = corrected
                audit.append({
                    'step_order': step_data.get('order_index'),
                    'field': f'educational_content.{key}',
                    'corrections': corrections,
                })
        elif isinstance(val, list):
            new_list = []
            for item in val:
                if isinstance(item, str):
                    corrected, corrections = verify_calculations(item)
                    if corrections:
                        audit.append({
                            'step_order': step_data.get('order_index'),
                            'field': f'educational_content.{key}[]',
                            'corrections': corrections,
                        })
                    new_list.append(corrected)
                else:
                    new_list.append(item)
            edu[key] = new_list
    if edu:
        step_data['educational_content'] = edu
    return step_data
```

Math-only gate (`is_math`) avoids running the regex over geography
prose where "60% of the population" isn't an arithmetic claim. (False
positives on non-math lessons are cheap but pollute audit logs.)

#### Insertion point B — exit ticket persistence

Location: `apps/curriculum/content_generator.py:1485-1530` (the
question-build loop), gate same `is_math` check.

Run `verify_calculations` over `question_text`, `explanation`, and
each `option_a/b/c/d`. For non-MCQ: also `answer_data.text_template`
if present (fill-in-blank stem template).

#### Audit storage

Aggregate per-lesson audit into `lesson.metadata['verification_audit']`:

```json
{
  "verification_audit": {
    "generated_at": "2026-04-30T14:22:11Z",
    "math_check_run": true,
    "step_corrections": [
      {"step_order": 4, "field": "teacher_script", "corrections": [...]}
    ],
    "exit_ticket_corrections": [
      {"question_index": 2, "field": "question_text", "corrections": [...]}
    ],
    "answer_key_mismatches": [],   // populated by Layer 2
    "retry_count": 0                // populated by Layer 3
  }
}
```

Saved after generation completes via `lesson.save(update_fields=['metadata'])`.

**Why metadata not a new model**: zero migration, naturally scoped to
lesson lifecycle (regenerating a lesson resets its audit), aggregation
is a single dict load. A dedicated `ContentVerificationLog` table
would be useful for cross-lesson analytics but is over-engineering for
the v1 — see Open Question #1.

#### Conflict resolution

If `verify_calculations` rewrites `question_text` from
`"60+80+75+70+75=220"` to `"60+80+75+70+75=360"` but the answer key
expects `220`, **Layer 1 should NOT silently rewrite**. The question
has been corrupted at generation; rewriting only one side desyncs
question and answer key.

**Rule**: Layer 1 only auto-corrects fields where there's no answer-
key dependency: `teacher_script`, `educational_content.*`, `hint_*`,
`rubric`. For `question_text`, `expected_answer`, `correct_answer`, and
`option_a/b/c/d` it **detects** but does not silently fix — it
records the issue and lets Layer 2 + Layer 3 handle the disagreement.

This is critical. See Open Question #5.

---

### Layer 2 — Cross-validate answer keys against question stems

**Goal**: catch the most damaging bug class — a wrong answer key.
A student does the math right, gets marked wrong.

**Where**: extend the existing `apps/tutoring/question_validator.py`.

Today's validator handles only sum-with-blank. Extend the supported
patterns:

```python
# In apps/tutoring/question_validator.py — new section

# Pattern A — pure additive sum (already handled): a + b + c + x = total
# Pattern B — additive sum without total, just operands: a + b + c = ?
_SUM_NO_TOTAL_RE = re.compile(
    r'(?<![.\d])((?:-?\d+(?:\.\d+)?\s*°?\s*[+\-]\s*){2,}-?\d+(?:\.\d+)?)\s*°?\s*[=?]'
)

# Pattern C — multiplication chain: a × b × c = ?
_MULT_CHAIN_RE = re.compile(
    r'((?:-?\d+(?:\.\d+)?\s*[×*]\s*){1,}-?\d+(?:\.\d+)?)\s*[=?]'
)

# Pattern D — single-variable equation solvable by isolation:
# "a × x = b"  → x = b/a
# "a + x = b"  → x = b - a
# "x - a = b"  → x = b + a
# "a · x + b = c" → x = (c - b) / a
_LINEAR_EQ_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*([+\-×*])\s*[xX]\s*=\s*(\d+(?:\.\d+)?)'
)
```

Add a unified verifier that returns both a "computed answer" AND the
"claimed answer" so the caller can decide:

```python
def cross_check_question(q: dict) -> Tuple[Optional[str], Optional[dict]]:
    """Return (issue_reason | None, audit_entry | None).

    issue_reason is non-None when the question's stored answer
    disagrees with what the math actually produces.

    audit_entry is the structured data for verification_audit
    regardless of whether there's a mismatch.
    """
```

Add a new public function (call it from `is_broken`):

```python
def is_broken(q: dict) -> Optional[str]:
    # ...existing checks...
    issue, _ = cross_check_question(q)
    if issue:
        return issue
    return None
```

#### Pattern coverage explicitly out of scope (use Layer 4 instead)

- Word problems where the numbers are derived ("Pierre has 3 baskets
  of 12 mangoes; he gives away 1/4...")
- Multi-step problems with intermediate values
- Geometry with figure references
- Unit conversion problems

For these the parser will return "unverifiable" and Layer 2 passes
through. Layer 4 (parametric) is the correct path for these.

#### Behavior on "unverifiable"

Pass through, log to audit as `{verifiable: false, reason:
"no parseable equation in stem"}`. Don't block.

This means many word-problem questions still rely on the LLM being
right. Layer 4 closes that gap for question types we choose to
templatize.

#### Tolerance

`question_validator` today uses `0.5` (half-degree). Make it
configurable per check:

- Integer-only operands → exact match (tolerance 0)
- Float operands → relative tolerance 1e-3
- Degree contexts → 0.5 (preserve existing behavior)

Keep within the existing `_to_float` helper.

---

### Layer 3 — Verify-then-retry generation

**Goal**: when Layers 1+2 detect uncorrectable issues (answer-key
mismatches, arithmetic in question stems), retry the LLM call once
with a constraint block.

**Where**: extend the existing retry pattern in
`apps/curriculum/content_generator.py::_generate_steps`
(line 1055-1078) and add a new retry loop in
`generate_exit_ticket_for_lesson`.

#### Constraint-prompt shape

For arithmetic errors:

```
The previous response had ARITHMETIC ERRORS that must be fixed.
The following expressions were computed incorrectly:

  - "60 + 80 + 75 + 70 + 75 = 220"  →  correct value is 360
  - "8 × 2.5 = 20"                  →  correct value is 20.0  (close enough; not flagged)

REGENERATE the affected content with these corrections applied.
Re-verify every arithmetic claim before responding. If you are unsure
of a calculation, OMIT the explicit numeric step and use a placeholder
like "Step N: solve for x" instead.

Original prompt follows:
=========
<original prompt>
```

For answer-key mismatches:

```
The previous response had QUESTIONS WHERE THE ANSWER KEY DID NOT MATCH
THE QUESTION STEM:

  - Question 3: stem says "85° + 92° + 78° + ___ = 360°"; computed
    blank = 105°; but stored answer = 85°.

REGENERATE these questions only. Either fix the stem to match the
intended answer, or fix the answer key to match the stem. Verify the
arithmetic explicitly before responding.

Original prompt follows:
=========
<original prompt>
```

The two formats keep the LLM focused — pasting the original prompt
verbatim mirrors the existing profile-retry pattern (line 1062-1064).

#### Retry budget

- Steps generation: 1 retry max (matches existing profile-retry budget;
  tutorial latency budget is already tight at ~30s/lesson)
- Exit ticket generation: 1 retry max
- After retry, if errors REMAIN: save with `Lesson.metadata.
  verification_audit.unresolved_errors = [...]` and set
  `Lesson.content_status='ready_with_warnings'` (new value).

The teacher dashboard surfaces `ready_with_warnings` lessons in a
review queue; the lesson is still usable but flagged.

**Why no second retry**: each retry is a full ~$0.10–0.40 LLM call
plus 5-15s latency; we don't want generation cost to balloon. If one
retry doesn't fix it, the failure mode is systematic (LLM doesn't
understand the constraint) and a second retry won't help.

#### Status flag

Add a new value to `Lesson.content_status` (currently
`empty/generating/ready/failed`):

```python
class ContentStatus(models.TextChoices):
    EMPTY                = 'empty'
    GENERATING           = 'generating'
    READY                = 'ready'
    READY_WITH_WARNINGS  = 'ready_with_warnings'   # NEW
    FAILED               = 'failed'
```

Migration touches the choices, not data.

---

### Layer 4 — Parametric question schema

**Goal**: eliminate arithmetic-error class entirely for templated
question types. The LLM emits TEMPLATES; code computes answers.

#### Schema

New Pydantic model in `apps/curriculum/content_generator.py`:

```python
class ParametricQuestionTemplate(BaseModel):
    """A question whose numeric values come from code-evaluable parameters.

    The LLM emits the template + parameter ranges. At persistence time
    a concrete instance is rendered; at retake time another instance
    can be rendered from the same template.
    """
    template_text: str = Field(
        description=(
            'Question stem with named slots in {curly_braces}. '
            'Example: "Three angles around a point are {a}°, {b}°, '
            'and x°. Find x."'
        )
    )
    parameters: Dict[str, ParameterSpec]   # name → spec
    answer_formula: str = Field(
        description=(
            'Pure-Python expression using the parameter names. '
            'Must use only +, -, *, /, **, parentheses, and the safe '
            'math functions math.sqrt, math.sin, math.cos, math.tan. '
            'Example: "360 - a - b"'
        )
    )
    answer_unit: Optional[str] = None      # "°", "m", "kg"
    explanation_template: str              # uses {a}, {b}, {answer} slots

class ParameterSpec(BaseModel):
    type: Literal["int", "float"]
    min: float
    max: float
    step: Optional[float] = None    # None = any value in range
    constraint: Optional[str] = None # e.g. "a + b < 360"
```

#### Storage

Two options for storing templated questions:

**Option A — separate model `ExitTicketQuestionTemplate`** (one-to-many
to `ExitTicket`):
- Carries template + parameter spec
- A renderer creates one or more concrete `ExitTicketQuestion` rows
  from each template at generation time
- Retakes can re-render to get fresh numbers
- Pro: clean separation; templates survive retakes; easy to audit
- Con: new model + migration; renderer is a new code path

**Option B — JSONField on `ExitTicketQuestion`**:
- Add `template_data: JSONField(null=True)` to `ExitTicketQuestion`
- When non-null, the row is a templated instance; the rendered
  numbers are still in the prose fields (so existing display code
  works unchanged)
- The `template_data` enables re-rendering at retake time
- Pro: fewer moving parts; rendering is a one-shot at create-time
- Con: each retake is a fresh row OR you mutate an existing row — both
  patterns have edge cases

**Recommend Option B** (JSONField on ExitTicketQuestion). The
existing display path keeps working without changes; teachers see one
question per row in the admin; retakes generate a new
`ExitTicketAttempt` with re-rendered numbers (we already create new
attempt rows per retake — see `apps/tutoring/models.py:ExitTicketAttempt`).

Migration: add nullable `template_data: JSONField`. Backfill is
nothing (existing rows = `None` = "not templated").

#### Renderer + answer computer

New module `apps/curriculum/parametric_renderer.py`:

```python
def render_template(tmpl: ParametricQuestionTemplate, seed: Optional[int] = None) -> dict:
    """Generate a concrete ExitTicketQuestion-shaped dict from a template."""
    rng = random.Random(seed)
    params = _sample_parameters(tmpl.parameters, tmpl.constraints, rng)
    answer = _compute_answer(tmpl.answer_formula, params)
    return {
        'question_text': tmpl.template_text.format(**params),
        'correct_answer': _format_answer(answer, tmpl.answer_unit),
        'answer_data': {
            'computed': answer,
            'unit': tmpl.answer_unit,
            'parameters': params,
        },
        'explanation': tmpl.explanation_template.format(**params, answer=answer),
        'template_data': tmpl.dict(),
    }

def _compute_answer(formula: str, params: dict) -> float:
    """Safe-eval the answer formula. Whitelist of allowed names."""
```

#### sympy vs. safe-eval

**Recommend safe-eval** (custom AST walker) for v1, not sympy. Reasons:

- The formula scope is "compute a number from parameters." That's pure
  arithmetic + a handful of math functions. No symbolic manipulation.
- sympy is heavy (~50ms import, ~500ms first-use latency, 50MB memory).
  We'd be using it only for `eval`-equivalent.
- Safe-eval is ~30 lines of AST walker (allow `BinOp`, `UnaryOp`,
  `Num`/`Constant`, `Name` from whitelist, `Call` to whitelisted
  funcs). Well-understood pattern.
- sympy can come later if we want algebraic templates ("solve for x in
  a*x + b = c" where the LLM specifies the equation but not the
  isolation). v1 doesn't need that — answer is computed from supplied
  parameters.

Implementation: small AST-walker class, ~30 lines, in
`apps/curriculum/parametric_renderer.py`.

#### Migration path — opt-in per question type

Don't replace free-form generation wholesale. Phase rollout:

| Phase | Question pattern | Notes |
|---|---|---|
| **L4.A** | "Sum to 360°" angle problems | Most common in geometry; easy template; high error rate today |
| **L4.B** | Linear equations (`a*x + b = c`) | Algebra S3 pattern |
| **L4.C** | Percentage of value | Common in S2/S3; high error rate |
| **L4.D** | Two-step word problems with named context | Seychelles flavor; biggest UX win |

Each phase ships with:
- A library of templates the LLM can choose from
- A prompt addition that says "for [pattern], use the parametric
  format below: {schema}"
- A fallback: if the LLM emits a templated question, use it; otherwise
  fall through to free-form Layer 1+2+3

#### Storage shape decision (re-stated)

We store **one row per concrete instance** (Option B above). On
retake we generate a new `ExitTicketAttempt` linking to the original
`ExitTicketQuestion`; if that question has `template_data`, the
runtime question-server can render fresh numbers per attempt. This
is similar to the existing summative-question retake flow.

For v1 we ship "render once at generation time" — same numbers every
attempt. Re-rendering per-attempt is a follow-on (Phase L4.E) once
we have evidence that students benefit from question variation.

---

### Layer S — Student working analyzer (RUNTIME)

**Goal**: at session time, deterministically extract and verify the
student's working step-by-step, telling the tutor LLM exactly which
step is wrong, whether the student has finished, and what to do next.

This layer is **runtime**, not content-gen. It runs on every student
turn during a math session, alongside the existing
`_deterministic_math_check` and `_is_bare_math_answer` in
`apps/tutoring/conversational_tutor.py:1477`.

**Why it's needed**: today, when a student submits
`"95 + 70 + 110 = 275"` and stops, the tutor often jumps ahead and
writes "so x = 85" — solving the rest of the problem for them. The
runtime has no signal that the student is partway through; only the
FINAL answer is checked against `expected_answer`. Similarly, when a
student writes a wrong intermediate step but somehow lands on the
right final answer, the tutor confirms "great!" without diagnosing
the broken working.

#### Five terminal states

| State | Definition | Tutor behaviour |
|---|---|---|
| **NO_WORKING** | 0 equations extracted (bare number or pure prose) | Apply existing Rule 1; **also request a clean step-per-line format** so future extraction works |
| **PARTIAL_CORRECT** | All extracted steps right; last claim ≠ `expected_answer` | Acknowledge specific step; ask what comes next; remind them to keep showing each step |
| **PARTIAL_WRONG** | One or more steps have arithmetic errors AND last claim is intermediate | Address FIRST_ERROR; do not yet talk about completeness |
| **COMPLETE_CORRECT** | All right + last claim = `expected_answer` | Confirm specific steps. **Do not just say "great, next problem."** Ask the student to verify or explain why their approach works |
| **COMPLETE_WRONG** | All steps internally right + last claim ≠ `expected_answer` | Ask the student to walk through their setup; the math is right but the formulation is wrong — diagnose at problem level |

**The pedagogical stance is constant across states**: never
shortcut, always ask the student to engage actively. The state
labels exist so the LLM can phrase its question *specifically*
(naming the step they got right, the error they made, etc.), not
so it can decide whether to "praise and move on."

#### Decision tree

```
                       student input
                              │
                              ▼
                       extract_steps()
                              │
                  ┌───────────┴────────────┐
                  │                        │
              0 steps                   ≥ 1 step
                  │                        │
                  ▼                        ▼
            NO_WORKING               verify each step
                                            │
                                    ┌──────┴──────┐
                                    │             │
                                any wrong?     all right?
                                    │             │
                                    ▼             ▼
                              find FIRST_ERROR  last_claim ≈ expected?
                              + propagation         │
                                    │           ┌───┴───┐
                              ┌─────┴─────┐    yes      no
                              │           │     │       │
                       last claim   last claim  ▼       ▼
                       intermediate? final?  COMPLETE_  PARTIAL_
                              │         │    CORRECT    CORRECT
                              ▼         ▼
                       PARTIAL_WRONG  COMPLETE_WRONG
```

#### S1 — Extraction algorithm (separator-agnostic)

The extractor doesn't depend on newlines, semicolons, commas,
periods, or prose connectives. It finds every `<expr> = <number>`
pattern by walking expression characters:

1. Pre-strip noise around numbers: `$`, `°`, `%`, common units
   (`kg`, `cm`, `m`, `s`). These are not arithmetic and shouldn't
   terminate the walk.
2. `re.finditer(r'=\s*(-?\d+(?:\.\d+)?)', text)` — find every claim.
3. For each, walk left from the `=` while the character is digit,
   `+`, `-`, `*`, `/`, `×`, `÷`, `.`, `(`, `)`, or whitespace.
4. Stop at the first non-arithmetic character (letter, comma,
   semicolon, newline, currency, unit) — that's the expression
   start.
5. Validate: the captured expression must contain at least one
   binary operator (otherwise it's just a number, not a step).

This handles all of:

| Input | Steps extracted |
|---|---|
| `95+70=165 165+110=275 360-275=85` (just spaces) | 3 |
| `95+70=165\n165+110=275\n360-275=85` (newlines) | 3 |
| `95+70=165;165+110=275;360-275=85` (semicolons) | 3 |
| `95+70=165, 165+110=275, 360-275=85` (commas) | 3 |
| `First, 95+70=165. Then 165+110=275. So 360-275=85.` (prose) | 3 |
| `95+70=165 then I did 165+110=275 and finally 360-275=85` | 3 |

Sequential-equals chains (`a op b = c op d = e`) are handled by
splitting into N steps before walking — same logic as
`verify_calculations`'s `double_eq_pattern`.

Falls through to LLM evaluation (no extraction):
- Pure prose without equations
- Variable-only assignments (`x = 85`) where there's no operator on
  the right
- Multi-step chains using only variable names without numeric values

#### S2 — Per-step verification

For each extracted step, compute `expr` independently using the
safe-eval AST walker (shared with Layer 4). Compare to `claim`.
Flag mismatches. Each step gets `{ok: bool, computed: float}`.

#### S3 — Chain analysis (FIRST_ERROR + propagation)

Walk steps in order. For each step beyond step 1, look at the
operands of `expr`. If any equals a prior step's `claim`, link
them. Produces:

- `first_error_idx` — index of the first step with a wrong claim
- `propagated_idxs` — set of step indices whose error is downstream
  of `first_error_idx` (their math is internally right but they
  used a wrong upstream value)

#### S4 — Completeness detection

```python
if last_step.claim ≈ expected_answer:
    state = COMPLETE_CORRECT  # or COMPLETE_WRONG if upstream errors
else:
    state = PARTIAL_CORRECT  # or PARTIAL_WRONG if errors
```

The cheap heuristic for distinguishing PARTIAL_CORRECT from
COMPLETE_WRONG: when math is internally clean and last_claim ≠
expected_answer, default to PARTIAL_CORRECT. The pedagogical cost
of asking "what's next?" when the student thought they were done
is low (brief redirection); the cost of confirming a wrong final
answer is high. Lean toward asking.

A v2 heuristic could check whether a 1-step arithmetic path exists
from `last_claim` to `expected_answer` using numbers from the
question stem — distinguishing PARTIAL (path exists, student
stopped early) from COMPLETE_WRONG (no path, setup error). Defer
to v2; only build if the cheap heuristic causes real problems.

#### S5 — System prompt injection

A `<student_working_analysis>` block is appended to the system
prompt alongside the existing `<evaluation_signal>` and bare-
answer block. Six sample blocks (one per state plus NO_WORKING with
separator request) follow.

**PARTIAL_CORRECT:**

```
<student_working_analysis>
Steps extracted: 1
  Step 1: 95 + 70 + 110 = 275   ✓

Comparison to expected answer:
  expected_answer:        85
  student's last claim:   275

Verdict: PARTIAL_CORRECT
The student's arithmetic is correct, but they have not reached
the final answer. 275 is an intermediate value (it equals the
sum of the three given angles).

ACTION:
- Acknowledge that 95 + 70 + 110 = 275 is right.
- Ask them what comes next.
- Remind them to keep showing each step (so we can check
  together).
- DO NOT compute the remaining step for them.
- DO NOT state the final answer (85).
- DO NOT say "great, so x = 85" — that would solve their
  problem for them.
</student_working_analysis>
```

**NO_WORKING (with separator request):**

```
<student_working_analysis>
Steps extracted: 0
Student input: "I added 95 and 70 first, then 110, and I think
                I got 85 at the end"

Verdict: NO_WORKING
The student described their reasoning in prose but didn't write
out any equations we can verify step-by-step.

ACTION (apply Rule 1 with separator request):
- Do NOT confirm or deny their answer — they showed no
  verifiable working.
- Politely ask them to write each step on its own line, like:
       95 + 70 = 165
       165 + 110 = 275
       360 - 275 = 85
  This way you can check each step with them.
- Frame it pedagogically, not technically: "I want to walk
  through this step by step with you" — not "the system can't
  parse your answer."
</student_working_analysis>
```

Five blocks total, one per state. Sample for PARTIAL_WRONG:

```
<student_working_analysis>
Steps extracted: 1
  Step 1: 95 + 70 + 110 = 285   ✗  (correct: 275)

FIRST ERROR: Step 1 (addition)

Comparison to expected answer:
  expected_answer:        85
  student's last claim:   285  (also intermediate, but wrong)

Verdict: PARTIAL_WRONG

ACTION:
- Address the addition error in Step 1 BEFORE worrying about
  completeness.
- Do NOT state the correct sum (275). Ask them to recompute
  95 + 70 + 110.
- Once they fix the addition, you can prompt them to continue.
</student_working_analysis>
```

**COMPLETE_CORRECT** (NB: never just praise + move on):

```
<student_working_analysis>
Steps extracted: 2
  Step 1: 95 + 70 + 110 = 275   ✓
  Step 2: 360 - 275 = 85        ✓

Comparison to expected answer:
  expected_answer:        85
  student's last claim:   85

Verdict: COMPLETE_CORRECT

ACTION:
- Confirm the answer.
- Be specific about WHICH steps you're praising — name them
  ("you correctly summed the three given angles, then
  subtracted from 360 to find x").
- DO NOT just say "great, next problem!" — the goal is
  learning, not correctness. Ask the student to articulate
  WHY this approach works:
    * "Why did you subtract from 360 instead of some other
       number?"
    * "How would you check this answer?"
    * "What rule did you use?"
- Only after they articulate the reasoning do you move on.
</student_working_analysis>
```

**COMPLETE_WRONG** (math right, setup wrong):

```
<student_working_analysis>
Steps extracted: 2
  Step 1: 95 × 70 = 6650         ✓ (arithmetically)
  Step 2: 6650 - 110 = 6540      ✓ (arithmetically)

Comparison to expected answer:
  expected_answer:        85
  student's last claim:   6540

Verdict: COMPLETE_WRONG
The student's arithmetic is internally correct but they used
multiplication / subtraction in places where the problem calls
for addition / subtraction-from-360. The setup is wrong, not
the arithmetic.

ACTION:
- Do NOT focus on the arithmetic — it's right.
- Ask them to explain WHY they multiplied 95 × 70.
- Walk them back to the problem: "Three angles around a
  point. What rule applies here?"
- Help them re-derive the correct setup.
</student_working_analysis>
```

#### Companion: math_teaching Rule 1.5

Append to the existing `<math_teaching>` block in
`conversational_tutor.py:3208`:

```
=== RULE 1.5 (NEW): NEVER FINISH THE STUDENT'S PROBLEM ===
When the student has shown partial working and stopped at an
intermediate value, your job is to ASK WHAT COMES NEXT — not
to compute the remaining steps for them.
- DO NOT write out "so the answer is X" when X requires a
  step they haven't shown.
- DO NOT compute the next subtraction, multiplication, etc.,
  even if it's "obvious".
- DO acknowledge what they got right, then prompt: "what
  do you do with that to find x?"

If <student_working_analysis> reports PARTIAL_CORRECT, this
rule is binding regardless of how short the remaining work
looks to you.
```

#### Code locations

- New module: `apps/tutoring/student_working_analyzer.py`
  - `dataclass Step(idx, expr, claim, computed, ok, depends_on)`
  - `dataclass WorkingAnalysis(state, steps, first_error_idx, propagated_idxs, final_claim, expected_answer)`
  - `def analyze_working(student_input: str, expected_answer: str|None) -> WorkingAnalysis`
  - `def build_working_analysis_block(analysis: WorkingAnalysis) -> str`
  - `def safe_eval_arithmetic(expr: str) -> Optional[float]` — AST walker
    (will be reused by Layer 4 parametric renderer)
- Hook into `conversational_tutor.py` near line 1505 (where
  `_pending_math_check` is set up)
- New attribute `self._pending_working_analysis` populated
  alongside `self._pending_math_check`
- In `_build_system_prompt` near line 3338, inject the analysis
  block alongside the existing `<evaluation_signal>` blocks
- Persist analysis to `SessionTurn.metadata` so the teacher monitor
  can show chips. New keys:
  * `working_state` — enum string
  * `working_steps_count` — int
  * `working_first_error_idx` — int|None (1-indexed)
  * `working_propagated_idxs` — list[int]
  * `working_final_claim` — float|None
  * `working_expected` — float|None
- Update `templates/dashboard/session_chat_history.html` (around
  line 256) to render new chips next to the existing
  `eval_layer` / `bare_answer` / `praise_stripped` chips:
  * `[ PARTIAL_CORRECT ]` / `[ COMPLETE_CORRECT ]` etc.
  * `[ 2 of 3 steps ]` (when steps_count > 0)
  * `[ first error: step 2 ]` (when first_error_idx not null)
  * `[ propagated to step 3 ]` (when propagated_idxs non-empty)

#### Test strategy

`apps/tutoring/tests/test_student_working_analyzer.py`:
- One fixture per state (5 tests minimum)
- One test per separator format (6 tests, table above)
- Chain analysis: error in step 2, propagation to step 3
- Self-correction: student writes wrong then right (flag both, log
  self-correction marker)
- Edge cases: prose-only, variable assignments, parenthesized
  expressions, mixed units, sequential-equals split

Integration test in `apps/tutoring/tests/test_math_eval_integration.py`:
- Mock LLM, set `expected_answer="85"`, send `"95+70+110=275"`
- Assert `<student_working_analysis>` block in system prompt
- Assert block contains "PARTIAL_CORRECT" and "DO NOT compute"
- Assert tutor response (mocked) does not contain "x =" or final
  answer token

#### Reusable pattern for non-math subjects (architectural note)

Layer S's shape — extract claims, verify deterministically, inject
a structured signal block — is reusable. We deliberately don't
formalise a `ClaimAnalyzer` Protocol in v1, but the v1 module is
written so a second implementation can drop in cleanly:

```python
# apps/tutoring/student_working_analyzer.py (v1 — math-only)

class MathWorkingAnalyzer:
    def analyze(self, student_input: str, expected: str | None) -> Analysis:
        ...
    def build_block(self, analysis: Analysis) -> str:
        ...

# Future: apps/tutoring/student_fact_analyzer.py (v2)
class GeographyFactAnalyzer:
    """Extracts date / place / population claims, verifies against
    SeychellesContext + curriculum KB."""
    def analyze(self, student_input: str, expected: str | None) -> Analysis:
        ...
```

The wiring point in `conversational_tutor.py` becomes:

```python
analyzer = pick_analyzer_for_lesson(self.lesson)
self._pending_working_analysis = analyzer.analyze(
    student_input, current_step.expected_answer,
)
```

For v1, `pick_analyzer_for_lesson` returns `MathWorkingAnalyzer`
when `course.is_math` else `None`. For v2 we add the geography /
science branches and formalise the Protocol once we see what the
second implementation actually needs.

The shared `Analysis` dataclass should generalise: the v1 fields
(`steps`, `first_error_idx`, `state`) become subject-specific; the
top-level `<student_working_analysis>` block is rendered by each
analyzer's own `build_block`. The signal-injection site doesn't
care about the subject.

#### Out of scope for Layer S (v1)

- **Shortcut detection** — a student who computes correctly with
  fewer steps than the canonical solution. E.g. for `95+70+110+x=360`,
  writing only `360-275=85` (skipped showing 95+70+110). Layer S
  reports COMPLETE_CORRECT today; ideally we'd flag SHORTCUTTED.
  Requires content-gen to emit `min_expected_steps` per question;
  defer to v2 if real students do this regularly. Existing bare-
  answer detection already catches the worst case (no working at
  all).
- **Self-correction credit** — when a student writes wrong and
  then right ("wait, 165+110=275"), Layer S reports both. Logging
  it as self-correction (vs. flagging as "had an error") is a
  pedagogical refinement — defer.
- **Variable substitution** — student writes `let s = 95+70+110;
  s = 275; 360 - s = 85`. Layer S would extract `95+70+110=275`
  and `360-275=85`... actually no, the second equation has `s`
  not `275`, so it'd skip. v2 could substitute named values.
- **Per-session separator preference** — tutor remembers "use `;`
  for this session." Not needed because the extractor is already
  separator-agnostic.
- **Non-math subjects** — geography fact-checking, science
  numeric/named claim verification, language conjugation
  verification. Architecture supports it; v2 work.

---

## Data model changes

### Layer 1 (no migration)

- `Lesson.metadata['verification_audit']` — JSONField key, no schema change.

### Layer 2 (no migration)

- Adds keys to `Lesson.metadata['verification_audit']` only.

### Layer 3 (one migration, additive)

```python
# apps/curriculum/migrations/0034_content_status_with_warnings.py
operations = [
    migrations.AlterField(
        model_name='lesson',
        name='content_status',
        field=models.CharField(
            choices=[
                ('empty', 'Empty'),
                ('generating', 'Generating'),
                ('ready', 'Ready'),
                ('ready_with_warnings', 'Ready with warnings'),
                ('failed', 'Failed'),
            ],
            default='empty',
            max_length=25,
        ),
    ),
]
```

### Layer 4 (one migration, additive)

```python
# apps/tutoring/migrations/0026_exit_ticket_question_template_data.py
operations = [
    migrations.AddField(
        model_name='exitticketquestion',
        name='template_data',
        field=models.JSONField(blank=True, null=True),
    ),
]
```

## Backend changes

### Layer 1 (`apps/curriculum/content_generator.py`)

- New helper `_verify_step_arithmetic(step_data, lesson, audit)` (added
  near `_validate_against_profile`).
- New helper `_verify_question_arithmetic(question_dict, lesson, audit)`
  for exit-ticket questions.
- Modify `_generate_steps` to call `_verify_step_arithmetic` per step
  before `update_or_create`. Append to `audit` list.
- Modify `generate_exit_ticket_for_lesson` to call
  `_verify_question_arithmetic` per question before `create()`.
- After all steps + questions persist, write
  `lesson.metadata['verification_audit'] = audit_dict` and save.
- Logging: `print(f"[ContentGen] [{lesson.title}] Math check: {n} corrections", flush=True)`

### Layer 2 (`apps/tutoring/question_validator.py` + content_generator)

- Add `cross_check_question(q) -> (Optional[str], Optional[dict])`.
- Add patterns B, C, D regexes.
- Modify `is_broken(q)` to call `cross_check_question`; return its
  reason on mismatch.
- In `content_generator.py` exit-ticket loop, when `is_broken` returns
  a Layer 2 reason (vs. existing rationalization reason), record to
  audit BEFORE deciding to drop. (Layer 3 decides.)

### Layer 3 (`apps/curriculum/content_generator.py`)

- Add a new helper `_run_arithmetic_retry(prompt, last_response, errors)`
  that builds a constraint block and calls `_call_llm` once more.
- In `_generate_steps`: after Layer-1 verification, if any question-
  level fields had untouchable arithmetic errors (per Layer 1's
  conflict rule) OR Layer 2 cross-check fails on any embedded
  question, trigger one retry.
- In `generate_exit_ticket_for_lesson`: after all questions are
  validated, if Layer 1 + Layer 2 found any uncorrectable issues,
  trigger one retry.
- Track `retry_count` in audit metadata.
- If retry still fails: set `lesson.content_status='ready_with_warnings'`.

### Layer 4 (new module + content_generator + tutoring views)

- New file `apps/curriculum/parametric_renderer.py` (~150 lines):
  - `class SafeArithmeticEvaluator` (AST walker)
  - `class ParametricQuestionTemplate` (Pydantic model)
  - `def render_template(tmpl) -> dict`
  - `def _sample_parameters(spec, constraints, rng)`
- Extend `GeneratedExitTicketQuestion` Pydantic schema with optional
  `template: Optional[ParametricQuestionTemplate]` field.
- Add prompt-section in exit-ticket generation prompt explaining when
  to use the parametric form (per pattern, per Phase L4.A-D).
- In persistence loop: if `q.template` is present, run
  `render_template(q.template)`, write `template_data` field to the
  resulting `ExitTicketQuestion`, populate prose fields from the
  rendered dict.
- For per-attempt re-rendering (deferred to L4.E): add a render hook
  in `apps/tutoring/views.py::summative_take` (line 1721) and
  `chat_tutor` exit-ticket renderer.

### Layer S (new module + conversational_tutor wiring)

- New file `apps/tutoring/student_working_analyzer.py` (~250 lines):
  - `dataclass Step` and `dataclass WorkingAnalysis`
  - `enum WorkingState` — NO_WORKING, PARTIAL_CORRECT,
    PARTIAL_WRONG, COMPLETE_CORRECT, COMPLETE_WRONG
  - `def analyze_working(student_input, expected_answer) -> WorkingAnalysis`
  - `def build_working_analysis_block(analysis) -> str`
  - Re-uses Layer 4's safe-eval AST walker for arithmetic compute
- Hook into `conversational_tutor.py:1505` (after
  `_pending_math_check` setup):
  - New attribute `self._pending_working_analysis`
  - Populate when math step + non-empty student input
- In `_build_system_prompt` (around line 3338):
  - Inject `<student_working_analysis>` block when analysis present
  - Position: AFTER the existing math-eval signal block (last is
    highest salience)
- Append Rule 1.5 to the `<math_teaching>` block at line 3208
- Persist analysis state to `SessionTurn.metadata` for teacher review

## Frontend changes

### Layer 1+2+3

- Teacher dashboard: lesson detail page surfaces
  `verification_audit` summary ("3 arithmetic corrections,
  1 unresolved warning") with a collapsible details panel.
- Filter on the lessons-list view: "Show lessons with warnings only".
- These hook into existing dashboard templates — no new page.

Files to touch:
- `templates/dashboard/curriculum/lesson_detail.html`
- `templates/dashboard/curriculum/course_detail.html`
- `apps/dashboard/views.py::lesson_detail` (read `metadata.verification_audit`)

### Layer 4

- No frontend changes for v1 (rendering happens at generation time;
  display is unchanged).
- Eventually: per-attempt renderer needs a hook in the chat exit-
  ticket modal and summative take page.

## Out of scope

1. **Non-math subjects.** Geography lessons can have wrong dates or
   population numbers; no defense for that here. Layer 1 + Layer 2 are
   gated `is_math`.
2. **Symbolic verification.** Layer 4 with sympy could verify that
   "Solve for x: 2x + 3 = 11" → x=4 by symbolic isolation. Not in v1.
3. **Image-based verification.** Figures generated via
   `image_service` aren't checked for arithmetic; can't be without
   OCR + scene understanding.
4. **Existing-content backfill.** This plan applies to NEW content
   generated after rollout. Existing course content with errors needs
   a separate "audit + fix" sweep, not in this plan.
5. **Per-attempt re-rendering for Layer 4.** Render-once-at-generation
   only for v1. Re-rendering per attempt is L4.E follow-on.
6. **Live runtime arithmetic check on generated step content.** The
   tutor today runs `verify_calculations` on its OWN spoken text
   (line 1536). It does NOT re-verify the step's `teacher_script`
   content as it's quoted into the prompt. Adding a Layer 0
   ("verify content one more time at session start") is an option
   but feels redundant once Layer 1+2+3 are in place.

## Phased delivery

Estimates are solo-dev focused-work days. Add ~50% calendar drag for
parallel work + reviews + re-runs.

| Phase | Work | Files | Est. |
|---|---|---|---|
| **S1** — Layer S extractor + chain analyzer | Step extractor (separator-agnostic), per-step verify, FIRST_ERROR + propagation, completeness states | new `student_working_analyzer.py` | 1d |
| **S2** — Layer S prompt blocks | Build the 5 system-prompt blocks (one per state) | `student_working_analyzer.py` | 0.5d |
| **S3** — Layer S wiring | Hook into `conversational_tutor.py`; persist to `SessionTurn.metadata` | `conversational_tutor.py` | 0.5d |
| **S4** — Rule 1.5 | Append to `<math_teaching>` system prompt | `conversational_tutor.py:3208` | 15m |
| **S5** — Layer S tests | One per state, one per separator, chain analysis, integration test | `test_student_working_analyzer.py`, `test_math_eval_integration.py` | 1.5d |
| **S6** — Teacher monitor chips | Persist to `SessionTurn.metadata`; render chips in `session_chat_history.html` | `conversational_tutor.py`, `session_chat_history.html` | 0.5d |
| **A1** — Layer 1 step verifier | `_verify_step_arithmetic` helper, audit dict, hook in `_generate_steps` | `content_generator.py` | 0.5d |
| **A2** — Layer 1 question verifier | `_verify_question_arithmetic` helper, hook in exit-ticket loop | `content_generator.py` | 0.5d |
| **A3** — Audit metadata + dashboard surfacing | Write to `lesson.metadata`; minimal lesson_detail panel | `content_generator.py`, `lesson_detail.html`, `dashboard/views.py` | 0.5d |
| **A4** — Layer 1 unit tests | Verify each field; assert audit shape | new `test_content_generator_math_check.py` | 0.5d |
| **B1** — Layer 2 patterns B-D | Extend `question_validator.py` regex; `cross_check_question` | `question_validator.py` | 0.5d |
| **B2** — Layer 2 wire-up + tests | Hook in `is_broken`; integration tests | `content_generator.py`, `test_question_validator_v2.py` | 0.5d |
| **C1** — Layer 3 retry helper | `_run_arithmetic_retry`, constraint-prompt builder | `content_generator.py` | 0.5d |
| **C2** — Layer 3 wire-up | Trigger from `_generate_steps` + exit-ticket gen | `content_generator.py` | 0.5d |
| **C3** — `READY_WITH_WARNINGS` status | Migration, model choice update, dashboard filter | `models.py`, migration, `course_detail.html` | 0.5d |
| **C4** — Layer 3 tests | Mock LLM to fail, assert retry, assert metadata | `test_content_generator_retry.py` | 0.5d |
| **D1** — Layer 4 schema + renderer | Pydantic model, AST safe-eval, sample/format/render | new `parametric_renderer.py`, tests | 1.5d |
| **D2** — Layer 4 prompt integration | Extend exit-ticket gen prompt; `template_data` migration | `content_generator.py`, migration | 1d |
| **D3** — Layer 4 first pattern (sum to 360°) | Template library; integration tests; visual confirmation in dashboard | `content_generator.py`, fixtures | 0.5d |
| **D4** — Layer 4 patterns L4.B/C/D | Linear eq, percentages, two-step word problems | template library | 1d each |
| **E1** — Backfill audit (one-off) | Script that runs Layer 1+2 over existing math lessons; produces a CSV | `scripts/audit_existing_content.py` | 0.5d |

**Critical path for Layer S** (the user-visible runtime fix): S1 → S2 → S3 → S4 → S5 → S6. ~4 days. Independent of content-gen layers; ships first.

**Critical path for Layers 1+2+3** (gen-time defense): A1 → A2 → A3 → A4 → B1 → B2 → C1 → C2 → C3 → C4. ~5 days focused work.

**Layer 4 minimum viable** (just the sum-to-360° pattern): D1 → D2 → D3. ~3 days.

**Total to ship S + 1+2+3+L4.A**: ~11.5 focused days. Calendar: 3-4 weeks alongside other work.

### Recommended ordering

1. **Layer S first** (~3.5d). Highest user-visible impact — fixes the
   "tutor finishes the problem" bug live in production sessions today.
   Self-contained; no migrations.
2. **Layer 1 next** (~2d). Cheap; reuses the same `verify_calculations`
   infrastructure. Catches generated-content errors before students see
   them.
3. **Layer 2 + 3** (~3d together). Cross-validation + retry on top of
   Layer 1.
4. **Layer 4 first pattern** (~3d). Eliminates the class for the
   commonest math question type. Defer wider rollout until we see real
   audit-log evidence.
5. **Backfill script (E1)** anytime after Layer 1 ships.

## Open questions (confirm before A1)

### 1. Audit log shape — JSONField or new model?

**Recommend**: JSONField on `Lesson.metadata['verification_audit']`.
Reasons: zero migration; lifecycle aligned with lesson regeneration;
aggregates cleanly into one read for the teacher dashboard. A new
`ContentVerificationLog` table would be useful for cross-lesson
analytics ("what's our error rate on geometry?") but that's a v2
analytics concern — for v1 we just need teachers to see what was
caught on their lesson.

If we later want analytics, we add a sync to a new `ContentVerificationLog`
table without removing the metadata JSON.

### 2. Layer 4 — sympy or custom safe-eval?

**Recommend**: custom safe-eval (~30 lines AST walker). Reasons:
- Scope is "compute a number from parameters", not symbolic.
- sympy adds 50MB memory + 500ms first-use latency.
- safe-eval is well-understood; sympy is overkill.

sympy is already in `requirements.txt` (1.14.0) but unused. Keep it
for future symbolic features; don't pull it into v1.

### 3. Layer 2 — what to do with "unverifiable" questions?

**Recommend**: pass through (don't block, don't drop). Log to audit
with `verifiable: false`. The verifier shouldn't penalize question
patterns it doesn't understand — that would block all word problems.

The trade-off: word problems still rely on LLM correctness. Layer 4
is the path to closing that gap, one question pattern at a time.

### 4. Layer 3 retry budget — 1 or 2?

**Recommend**: 1 retry max. Each retry is a full LLM call (~$0.10–0.40 +
5–15s latency). If one constraint-block retry doesn't fix it, the
failure mode is systematic; a second retry is unlikely to help.

After the retry, if errors remain: `content_status='ready_with_warnings'`.
Teacher reviews. The lesson stays usable.

### 5. Layer 1 conflict — who wins on stem-vs-key disagreement?

This is the most important question. Scenario:
- LLM generates: `"60+80+75+70+75=220"` in `question_text`,
  `correct_answer="220"` in answer key.
- `verify_calculations` says: stem should be `=360`.
- Question: do we rewrite the stem to 360 (answer key still 220), do
  we rewrite the answer key to 360 (stem still 220), or do we leave
  both alone and trigger Layer 3 retry?

**Recommend**: leave both alone, trigger Layer 3 retry. The LLM
intended one of them; we don't know which. Auto-rewriting either side
is more dangerous than asking the LLM to reconcile. Per Layer 1's
"don't auto-correct fields with answer-key dependency" rule.

For step-internal arithmetic (`teacher_script`, `educational_content`,
hints) where there's no answer-key dependency, auto-correction is
safe and that's what Layer 1 does today.

### 6. `is_math` gate — keyword match or new field?

`Course.is_math` (`apps/curriculum/models.py:92`) is a keyword match on
the title today. Reliable enough for the Seychelles pilot ("Math S3").
Tanzania pilot may break this if course names use Swahili.

**Recommend**: ship with the keyword match for v1. Add
`Course.subject_type` field as part of the math-tutor-fix Phase M8
(deferred there too). Both plans flag this.

### 7. Should we run `verify_calculations` on existing content?

**Recommend**: yes, as a one-off audit script (Phase E1). Don't
auto-rewrite existing lessons — produce a CSV of corrections-that-
would-be-applied for teacher review. Teachers manually trigger
regeneration for lessons with significant errors.

### 8. Layer S — PARTIAL_CORRECT vs COMPLETE_WRONG: cheap heuristic or symbolic?

**RESOLVED (2026-04-30):** the user's pedagogical directive is
"always ask the student to show all working; any error must be
addressed; the goal is learning, not correctness." Under that
directive the tutor's *behaviour* is essentially the same in
PARTIAL_CORRECT vs COMPLETE_WRONG — ask the student to walk
through their setup or continue the working. The state distinction
is a *labelling* signal for the LLM (helps it phrase its question
specifically) but not a behaviour fork.

**Decision**: ship the cheap heuristic in v1 (default to
PARTIAL_CORRECT when math is clean and last_claim ≠ expected). The
ACTION blocks for PARTIAL_CORRECT and COMPLETE_WRONG both lead to
"ask the student to walk through it" — false categorisation of
COMPLETE_WRONG as PARTIAL_CORRECT just means a slightly less
specific question, not a wrong direction.

**Implication for ACTION blocks**: even on COMPLETE_CORRECT we do
not shortcut — the tutor confirms but also asks the student to
explain WHY their approach works, or asks them to verify the
answer makes sense in context. The pedagogical stance is constant:
active engagement regardless of correctness.

### 9. Layer S — should it run on non-math lessons?

**RESOLVED (2026-04-30):** math-only for v1, but the architecture
must be abstractable so we can apply the same pattern to other
subjects later. The shared pattern is:

  **extract claims → verify deterministically (or against a corpus)
  → inject a structured signal block telling the LLM which claims
  passed/failed and what action to take.**

Subject-specific instances:
- Math (v1) — extract arithmetic equations, verify with safe-eval
- Geography — extract date/place/name claims, verify against
  curriculum KB + structured fact tables
- Science — extract numeric values + named entities (chemical
  formulas, biological terms), verify against curriculum KB
- Language — extract conjugation / translation claims, verify
  against grammar rules

For v1, build `MathWorkingAnalyzer` as one concrete class with a
clean interface (`analyze(student_input, expected) -> Analysis`,
`build_block(analysis) -> str`). Don't prematurely formalise a
`ClaimAnalyzer` Protocol — defer the abstraction until we add a
second subject. The interface emerges from the second
implementation.

See "Reusable pattern for non-math subjects" subsection under
Layer S design.

### 10. Separator request — should the tutor negotiate format with the student?

**RESOLVED (2026-04-30):** yes, when extraction confidence is low.

When Layer S extracts 0 steps from a long student reply (the
student wrote prose without explicit equations, or the input has
ambiguous structure), the tutor's response should explicitly
request a clean format:

> "I want to make sure I check each of your steps carefully.
>  Can you write each step on its own line, like:
>      95 + 70 = 165
>      165 + 110 = 275
>      360 - 275 = 85"

This is a UX improvement to the NO_WORKING action block. It
teaches students to format their working in a way that's both
pedagogically clear AND machine-verifiable.

**Out of scope for v1**: per-session separator preference (the
tutor remembering "from now on use `;`"). Layer S's extractor is
already separator-agnostic, so this is unnecessary — we just need
SOMETHING parseable.

## Risks

1. **False positives in non-math contexts.** Geography lessons mention
   numbers ("Seychelles has 115 islands"). The verifier might match
   them as bad arithmetic. Mitigation: `is_math` gate; period review
   of audit logs to tune false-positive rate.

2. **Layer 3 latency.** Each retry adds 5-15s. Generating a 20-lesson
   course with average 1 retry per lesson = 5 minutes added latency.
   Mitigation: parallel generation already in place
   (`generate_content_for_unit/course`); retries within a lesson are
   serial but lessons run in parallel.

3. **Audit log explosion.** A bad LLM run on a 10-step lesson could
   produce 50+ corrections. Mitigation: cap audit entries to first 20
   per lesson; truncate with a "..." marker.

4. **Layer 4 template rigidity.** The LLM may try to use a template
   for a question that doesn't quite fit the pattern, producing weird
   forced numbers. Mitigation: ship Layer 4 patterns one at a time;
   review the first 50 templated questions per pattern manually before
   wide rollout.

5. **`READY_WITH_WARNINGS` flag noise.** If 30% of generated lessons
   end up flagged, teachers will ignore the flag (alert fatigue).
   Mitigation: tune the warning threshold (only flag uncorrectable
   issues, not corrected-during-Layer-1 ones); aim for <5% of lessons
   flagged.

6. **Parametric renderer security.** AST walker for safe-eval is a
   common XSS vector if implemented carelessly. Mitigation: whitelist
   approach (allow specific node types + names; deny by default);
   fuzz-test with adversarial inputs; never `eval()` raw strings.

## What NOT to do

- **Don't** auto-rewrite question stems or answer keys silently in
  Layer 1 — Layer 3 retry is the only safe way to handle stem/key
  disagreement.
- **Don't** add a second LLM call per lesson just for verification —
  the deterministic checks should catch enough; a second LLM-judge
  call doubles cost.
- **Don't** ship Layer 4 across all question types at once — opt in
  per pattern, validate each before extending.
- **Don't** block content generation on Layer 1+2 failures — soft-fail
  with `ready_with_warnings`. Teachers need lessons even when the
  audit is imperfect.
- **Don't** introduce a new `ContentVerificationLog` table in v1 —
  metadata JSON is enough for the visibility we need.

## Next step

Confirm the open questions above (especially #5 — stem/key conflict
rule, and #8 — PARTIAL vs COMPLETE_WRONG heuristic).

Then begin **S1 — Layer S extractor + chain analyzer**. It's the
highest user-visible impact (fixes the "tutor finishes the problem"
bug live in production), self-contained, no migrations, unblocks
S2-S5 in sequence.

Layer 1 (A1) is the natural next phase after Layer S ships.
