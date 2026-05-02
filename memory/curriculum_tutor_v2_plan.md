# Curriculum + Tutor v2 Plan (2026-05-02)

## Problem

The platform's curriculum and runtime tutor have several rough edges that surfaced during the Seychelles pilot:

1. Some lessons end up with no `enabling_objectives` populated, so exit-ticket EO normalisation drops every emission and remediation can only target the broad learning objective.
2. Math exit tickets are 100% `short_numeric` because Layer 4 strict mode requires every math question to be templated, but only that one type has a templated/computable form. MCQ / matching / fill / short_answer math questions have no path to a code-verified answer.
3. The tutor has no awareness of WHICH sub-objectives a student is weak on. Bank questions are sampled uniformly per session.
4. The tutor still occasionally evaluates math answers from the LLM's judgement instead of the bank's stored correct answer.
5. Remediation after exit-ticket failure is generic re-teaching of "the lesson" rather than targeted review of the specific sub-objectives the student missed.
6. Figure metadata describes what's in the figure but not what it was MEANT to depict, so the tutor can't reason about the original generation intent.

## Locked decisions

### Platform-wide architectural rule (the spine of this plan)

**The LLM never calculates correct answers. It only compares a student's answer to a verified, approved schema answer that already exists on the question record.**

This applies EVERYWHERE on the platform:
  - Lesson-step practice grading
  - Exit-ticket grading
  - Summative-exam grading
  - Remediation re-quizzes
  - Quick-check probes during tutoring
  - Any other math evaluation surface added in future

The "schema answer" is a code-computable field on the question:
  - `LessonStep.expected_answer` (already exists)
  - `ExitTicketQuestion.correct_answer` (MCQ option letter)
  - `ExitTicketQuestion.answer_data['computed']` / `['blanks']` / `['pairs']` (typed answer payload — populated by Layer 4 templates)

The LLM's role around math is strictly:
  - Pose questions from the bank (not author them)
  - Diagnose where the student went wrong using the verified explanation as scaffolding
  - Encourage and guide, never confirm correctness

### Other locked decisions

1. **EO normalisation stays as-is at the field level** (concept_tag = broad, enabling_objective = specific lesson EO, snapped to canonical via `_normalize_enabling_objective`). The fix is upstream: ensure every lesson has its `enabling_objectives` populated before exit-ticket gen runs.
2. **All math questions must have a computable form.** Templates extend beyond `short_numeric` to MCQ + matching + fill_in_blank. Short_answer is out of scope (open-ended responses).
3. **Exit-ticket attempts produce a per-EO competency map** that drives both step selection at session start AND the remediation flow.
4. **Vision (Option A) stays out of evaluation.** Figure metadata is for context only — never grades.
5. **Math exit-ticket format mix** (per item 1 decision): 7 fill / 6 match / 7 short_numeric / 15 MCQ = 35 total.
6. **Sampling weights** (per item 4 decision): failed EOs = 5x, unattempted EOs = 3x, mastered EOs = 1x.
7. **Remediation walkthrough scope**: ALL failed questions, walked through in lesson-EO order (the order the EOs appear on the lesson, not ad-hoc grouping). Each EO's failed questions are reviewed before moving to the next EO.
8. **Show-working policy during tutoring**: the tutor does NOT pester for working-show on every turn. The student attempts the answer; the deterministic grader judges; if wrong, the tutor uses the exit-ticket explanation as the canonical "how to get there" reference for scaffolding. Show-working is requested for all failed question during exit tickets.
9. **Per-lesson EO coverage gaps** (per item 2 decision) are logged to `lesson.metadata.verification_audit.eo_coverage_gaps`.

## Per-item design

### 1. EO attachment parity for exit-ticket questions

**Current state.** Lesson steps have `LessonStep.enabling_objective` populated reliably because step generation has access to `lesson.enabling_objectives` (which was just expanded by `_expand_to_granular_subskills`). Exit-ticket questions hit the same normaliser but the canonical list can be empty if the lesson predates the EO expansion.

**Target.** Before `generate_exit_ticket_for_lesson` runs the LLM call, ensure `lesson.enabling_objectives` is populated. If empty, run the same `_expand_to_granular_subskills` helper that step generation uses. The exit-ticket prompt then has canonical EOs to copy from, and the post-hoc normaliser has canonical EOs to snap against.

**Code changes.**
- `apps/curriculum/content_generator.py::generate_exit_ticket_for_lesson` — early in the function, if `lesson.enabling_objectives` is empty, call `_expand_to_granular_subskills(lesson, curriculum_context)`. Pass the curriculum context the function needs (lesson title + objective + parent unit objectives).
- Surface `enabling_objectives` count in the exit-ticket regen log so teachers can see the expansion fired.
- Test: regen an exit ticket on a lesson with empty `enabling_objectives` and assert the lesson now has 5-8 expanded EOs and the exit ticket questions are tagged with them.

**Effort.** ~1 hr.

### 2. Computable templates for all question types

**Current state.** `apps/curriculum/parametric_renderer.py` defines `ParametricQuestionTemplate` for `short_numeric` only — single answer formula, single rendered question. Layer 4 strict mode forces math questions through this, so math is `short_numeric` only.

**Target.** Extend the template schema to cover the four format types we use in exit tickets, all with a computable + verifiable answer:

```python
class ParametricMCQTemplate(BaseModel):
    """Templated MCQ. Correct answer + 3 distractors all computed
    from the same parameter sample."""
    template_text: str            # "Three angles around a point are {a}°, {b}°, and x°. Find x."
    parameters: Dict[str, ParameterSpec]
    correct_formula: str          # "360 - a - b"
    distractor_formulas: List[str]  # ["a + b", "180 - a - b", "360 - a"] — 3 wrong-answer formulas
    answer_unit: str = ""
    explanation_template: str
    constraints: List[str] = []
    # Server randomises which letter the correct answer lands at per render

class ParametricFillBlankTemplate(BaseModel):
    """Templated fill-in-blank with 1-3 numeric or named blanks."""
    template_text: str            # "Two angles are {a}° and {b}°. The third angle is ___°."
    parameters: Dict[str, ParameterSpec]
    blank_formulas: List[str]     # ["360 - a - b"] — one per blank, in order
    answer_unit: str = ""
    explanation_template: str
    constraints: List[str] = []

class ParametricMatchingTemplate(BaseModel):
    """Templated matching where each pair is a (left, right) computed
    from the same parameter sample. Tutor renders 4-6 pairs."""
    template_text: str            # framing prose: "Match each angle pair to its sum."
    parameters: Dict[str, ParameterSpec]
    pair_count: int               # 4, 5, or 6
    left_formula: str             # "{a}° + {b}°"  (rendered N times with different samples)
    right_formula: str            # "a + b"        (rendered N times, computed)
    distractor_count: int = 2     # extra wrong-side options
    explanation_template: str
    constraints: List[str] = []
```

`short_numeric` keeps its existing schema (already works).

`short_answer` stays free-form (no computable form possible without LLM). (Human Notes: We can ask for final answer in one box and working in another box. That way we can deterministically evaluation final answer and use LLM to check working on the working using the ticket explaination)

**Renderer additions** in `parametric_renderer.py`:
- `render_mcq(template, seed)` → returns `{stem, options: [{letter, text, is_correct}], correct_letter, computed_correct_value, params}`
- `render_fill_blank(template, seed)` → `{stem_with_blanks, blank_values, params}`
- `render_matching(template, seed)` → `{stem, pairs, distractor_rights, params}`

**Validator extensions** in `validate_template`:
- For MCQ: every distractor formula must produce a value DIFFERENT from the correct formula across all samples (ambiguous distractors rejected). Magnitude checks per slot.
- For fill_in_blank: each blank's formula must reference declared params and produce a stable type (int / float / string).
- For matching: pair_count between 4-6, left/right formulas reference declared params, no two pairs collide on left or right (otherwise ambiguity).

**Content-gen prompt.** Update `MATH_EXIT_TICKET_PROMPT` to allow + require a mix of templated formats, not just short_numeric. New worked examples for MCQ / fill / matching templates. Explicit format mix for math (locked): **7 fill / 6 match / 7 short_numeric / 15 MCQ = 35 total**, all templated.

**Persistence.** `ExitTicketQuestion.template_data` already exists; it stores the template JSON. Extend the save path to handle the new types — set `question_type` correctly, populate `option_a/b/c/d` from rendered MCQ output, populate `answer_data.blanks` for fill-in-blank, populate `answer_data.pairs` for matching.

**Test strategy.** Per-template-type unit tests + an integration test that generates a math exit ticket and asserts the format mix matches the prompt's specification.

**Effort.** Largest item. ~1.5-2 days:
- Schema + Pydantic models: 4 hrs
- Render functions per type: 4 hrs
- Validator extensions: 3 hrs
- Content-gen prompt update + worked examples: 3 hrs
- Persistence path: 2 hrs
- Tests: 4 hrs

### 3. Figure generation prompt in figure_facts metadata

**Current state.** `MediaAsset.figure_facts` stores scene_description, labelled_features, angle_relationships, extra_facts, anchor_prompts. The generation prompt (the text the LLM was given to produce the image) is discarded after `_save_generated_image_bytes` runs.

**Target.** Add `generation_prompt: Optional[str]` to `FigureFacts`. When `image_service` saves a generated image, pass the prompt to `extract_and_save_for_asset` and merge it into the saved `figure_facts` dict. The runtime tutor's `_build_figure_facts_block` includes it as additional context:

```
<figure_facts source="parallel-lines.png">
  Original generation prompt: "Schematic diagram of two parallel lines
    cut by a transversal, with all 8 angles labelled 1-8…"
  Scene: Two horizontal parallel lines, l (top) and m (bottom)…
  ...
```

**Code changes.**
- `apps/curriculum/figure_facts_schema.py` — add `generation_prompt: Optional[str] = None`
- `apps/curriculum/figure_facts_extractor.py::extract_and_save_for_asset` — accept optional `generation_prompt` kwarg, merge into the saved dict
- `apps/tutoring/image_service.py::_save_generated_image_bytes` — pass `prompt` (already in scope) to the helper
- `apps/tutoring/conversational_tutor.py::_build_figure_facts_block` — render `Original generation prompt: ...` line before `Scene:` if present

For backfill, the existing `backfill_figure_facts` command can extract scene-description from the image; it doesn't have the original prompt. Backfilled figures keep `generation_prompt=None`. Only new generations get it.

**Effort.** ~30 min.

### 4. EO-aware bank sampling for the tutor

**Current state.** `apps/tutoring/question_bank.py::sample_session_pool` samples uniformly from the lesson's published bank. `pick_candidates_for_step` already prefers `enabling_objective` matches at runtime (we shipped this).

**Target.** Bias the per-session pool sampling toward sub-objectives the student is weak on, based on the student's competency map (item 6). Bank still draws from the same pool of published questions, but weights:
- High weight: questions tagged with EOs the student has FAILED in past attempts
- Medium weight: questions tagged with EOs the student has NOT YET attempted
- Low weight: questions tagged with EOs the student has already mastered

**Code changes.**
- New helper `compute_student_eo_competency(student, lesson)` → `Dict[eo_text, status]` where status is in `{'mastered', 'failed', 'unattempted'}`. Reads from past `ExitTicketAttempt.answers` for this lesson.
- `sample_session_pool(lesson, seed, student=None)` — when `student` is provided, weight the random.choices by EO competency. When None (existing callers), uniform sampling preserved.
- `_build_question_bank_block` in conversational_tutor.py — pass `self.student` when calling sample_session_pool.

**Step selection at session start.** The session's lesson-step ordering can also use the EO map: skip / deprioritise practice steps whose EO the student has already mastered. Out of scope for v1 — defer.

**Effort.** ~1 day:
- Competency-map helper: 3 hrs (queries past attempts, scores per EO)
- Weighted sampling: 2 hrs
- Wire into engine + tests: 3 hrs

### 5. Deterministic grading for all bank-pulled questions

**Current state.** The deterministic math grader (`_deterministic_math_check`) compares student input to `LessonStep.expected_answer` for short-numeric math practice steps. For other question types and for runtime-pulled bank questions, the LLM still has discretion.

**Target.** When a bank entry is rendered to the student via `|||QUESTION:N|||`, the server records the entry's `correct_answer` (option letter for MCQ, or `answer_data.computed`, or `answer_data.blanks`, depending on type) on the session turn metadata. When the student responds, a deterministic grader runs FIRST, regardless of subject:

```python
def grade_bank_response(question, student_input):
    """Compare the student's input to the bank entry's stored answer.
    Returns True/False/None (None when the comparison can't be done
    confidently — e.g. short_answer free-text)."""
    qt = question.question_type
    if qt == 'mcq':
        # Match the option letter or the option's full text
        return _grade_mcq(question, student_input)
    if qt == 'short_numeric':
        return _grade_numeric(question, student_input)  # already exists
    if qt == 'fill_in_blank':
        return _grade_blanks(question, student_input)
    if qt == 'matching':
        return _grade_matching(question, student_input)
    return None  # short_answer — fall through to LLM evaluator
```

Result feeds the existing `<evaluation_signal>` block injected before the tutor's response generation. Tutor sees `Verdict: CORRECT` or `Verdict: INCORRECT` deterministically, with NO room to disagree.

**Code changes.**
- New module `apps/tutoring/bank_grader.py` with `grade_bank_response`
- `conversational_tutor.respond()` — before LLM generation, if the previous turn rendered a bank question, run `grade_bank_response` on the student input, attach result to `_pending_math_check` (or a new `_pending_bank_grade` attribute), inject into prompt as the verdict signal.
- Persist the verdict on `SessionTurn.metadata` for forensic review.
- Show-working request capped at one ask per failed question (per locked decision 8). Once the student attempts the final answer, grade and move on.

**Effort.** ~half day, but depends on item 2 schema (need to know how MCQ correct answer is encoded post-template).

### 6. EO-driven remediation

**Current state.** After exit-ticket failure, `_start_remediation` re-runs the lesson's TUTORING state from step 0. Generic re-teaching, not tied to the specific failed questions.

**Target.** Two changes:

**(6a) Build a per-attempt EO competency map.** When a student submits an exit ticket, compute per-EO performance:

```python
# In _grade_exit_ticket
eo_results: Dict[str, Dict] = {}  # {eo_text: {asked: int, correct: int, failed_qs: List[QuestionId]}}
for q, student_answer, is_correct in graded_questions:
    eo = q.enabling_objective or q.concept_tag or ''
    if not eo: continue
    bucket = eo_results.setdefault(eo, {'asked': 0, 'correct': 0, 'failed_qs': []})
    bucket['asked'] += 1
    if is_correct:
        bucket['correct'] += 1
    else:
        bucket['failed_qs'].append(q.id)
```

Persist on `ExitTicketAttempt.answers['eo_competency']`. Also feeds item 4 (sampling).

**(6b) Targeted remediation flow.** Replace `_start_remediation` with a flow that:

1. **Opening message.** Tutor names the EOs the student GOT and the EOs they MISSED:
   > "Good work — you mastered: 'Calculate angles around a point' and 'Identify reflex angles'. We need to revisit these:
   >   - **Define cost price, selling price, profit, loss, and discount** (you missed 2 questions)
   >   - **Apply profit and loss concepts in reverse** (you missed 1 question)
   > Let me walk through every question you missed so we can fix the gap."

2. **Per-failed-question review, in lesson-EO order.** Iterate the lesson's EOs in their published order; for each EO, walk through ALL failed questions tagged to that EO before moving to the next EO. For each failed question:
   - Re-pose the question (verbatim from the bank entry)
   - Show how to solve it using the bank's stored `explanation` (which carries the canonical step-by-step from the Layer 4 template)
   - Ask the student to attempt it themselves (deterministic grading against the schema answer)
   - Confirm before moving to the next question

3. **Re-quiz the weak EOs.** After all walkthroughs done, draw 2-3 fresh bank questions per failed EO (using item 4's weighted sampling) and have the student attempt them. Each attempt is graded deterministically. If a student passes the re-quiz for an EO, that EO promotes to mastered in the competency map.

4. **Safety valve.** Same 30-exchange total cap on remediation (already shipped).

**Code changes.**
- `_grade_exit_ticket` builds + persists `eo_competency` map (~30 min)
- `_start_remediation` rewritten to walk through failed questions one at a time (~3 hrs)
- New helper `_remediation_step_plan(failed_questions)` produces the ordered list of (question, replay action) tuples
- Frontend chat already supports the rendered question via `|||QUESTION:N|||` — no UI work
- Update remediation safety valve to allow longer sessions (30 cap is already in place)

**Effort.** ~1 day for the flow + ~30 min for the competency-map persistence.

## Out of scope (v3+)

- Cross-lesson competency map (per-course rollup beyond a single attempt) — useful for the dashboard but not for per-session targeting in this round
- Replacing the LLM evaluator entirely with deterministic grading on short_answer questions
- Adaptive difficulty escalation within a session driven by competency
- Vision in evaluation paths (forbidden — context only, already locked)
- Wrapping non-math lessons in templates — math-only mandate, geography stays free-form

## Phased delivery

Order by dependency + risk:

| Phase | Work | Effort | Why this order |
|---|---|---|---|
| **P1** | Item 1 (EO attachment parity) + Item 3 (figure prompt in metadata) | ~1.5 hrs | Small foundational fixes; unblocks proper EO data + adds context for tutor. Independent. |
| **P2** | Item 2 (computable templates for all types) | ~1.5-2 days | Largest item. Enables item 5 to grade all types. Math exit tickets get format variety back. |
| **P3** | Item 5 (deterministic bank grading) | ~half day | Depends on P2 schema. Closes the "tutor confidently states wrong math" failure mode. |
| **P4** | Item 6 (EO-driven remediation with question walkthrough) | ~1 day | Depends on P1's canonical EOs. Major UX upgrade for the post-fail flow. |
| **P5** | Item 4 (EO-aware bank sampling) | ~1 day | Depends on P4's competency-map persistence. Bank questions skew toward weak sub-skills. |

**Total: ~5-6 days of focused work.**

## Open questions

All five answered + locked above:
1. Format mix → **7 fill / 6 match / 7 short_numeric / 15 MCQ** ✓
2. EO coverage gaps → **logged to audit** ✓
3. Sampling weights → **failed=5, unattempted=3, mastered=1** ✓
4. Walkthrough scope → **ALL failed questions, walked in lesson-EO order** ✓
5. Grading principle → **platform-wide rule: LLM never calculates, only compares against approved schema answer; show-working asked at most once per failed question** ✓

## Next step

Plan locked. Start P1 (items 1 + 3, ~1.5 hrs).
