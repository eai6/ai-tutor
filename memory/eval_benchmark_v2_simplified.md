# Tutor Evaluation Benchmark — v2 Simplified Format

**Goal**: Given the chat history and student message, what labels apply to the tutor's response? Compare to the labels a good response would have. If `actual_labels ≠ expected_labels`, the response fails. The pipeline trace tells us *where* the failure came from.

---

## Label vocabulary — 30 specific labels

One flat list. The schema doesn't separate action from issue — they coexist in `actual_labels`. The `type` column is metadata: action labels can appear in `expected_labels`; issue labels never should.

Most issue labels map directly to existing judge/validator signals and **auto-populate at export time** from `production_metadata`. Edward only adds/overrides where the pipeline missed or wrongly fired.

### Action labels — what the response is doing (6)

| Label | Type | Definition |
|---|---|---|
| `ADVANCE` | action | Moves forward to next question/step (with or without brief affirmation). |
| `ASK_WORKING` | action | Asks the student to show working/steps before advancing. |
| `PROBE` | action | Focused question about student's reasoning ("why?", "how?"). |
| `EXPLAIN` | action | Provides teaching content (concept, rule, definition). |
| `SURFACE_ERROR` | action | Points out a specific error in the student's working or claim. |
| `OTHER` | action | Off-topic redirect, encouragement-only, clarification of student's question. |

A response can carry multiple action labels (`[ADVANCE, PROBE]`).

### Issue labels — grouped by source judge / rule

#### From rule judge (`apps/tutoring/judges/rule.py`)

| Label | Definition | Auto |
|---|---|---|
| `AUTHORED_QUESTION` | Invented a practice/quiz question with numbers not in the bank (NO_AUTHORING). | ✓ |
| `UNFOUNDED_PRAISE` | Praised bare answer or wrong answer using "exactly", "perfect", "you've nailed it", etc. (RULE_1). | ✓ |

#### From arithmetic judge (`apps/tutoring/judges/arithmetic.py`)

| Label | Definition | Auto |
|---|---|---|
| `ARITHMETIC_ERROR` | Tutor's own arithmetic claim is wrong (e.g., "65 + 125 = 180" or "180 - 42 = 140°"). | ✓ |

#### From factual judge (`apps/tutoring/judges/factual.py`)

| Label | Definition | Auto |
|---|---|---|
| `CLAIM_CONTRADICTED` | KB evidence directly contradicts a numeric/named claim in the response. | ✓ |
| `CLAIM_UNVERIFIED` | Claim extracted but KB evidence doesn't support or contradict it (soft issue). | ✓ |

#### From coherence judge (`apps/tutoring/judges/coherence.py`)

| Label | Definition | Auto |
|---|---|---|
| `INCOHERENT` | Response contradicts itself: setup mismatch ("three angles" then poses two-angle problem), value shift mid-explanation, praise-then-correct, instruction contradiction, rule contradiction. | ✓ (partial) |

#### From figure judges (`figure_ref.py`, `figure_vision.py`)

| Label | Definition | Auto |
|---|---|---|
| `FIGURE_REF_UNATTACHED` | Text references "the diagram", "look at the figure", etc., but no figure was attached this turn (no `|||MEDIA:N|||` signal). | ✓ |
| `FIGURE_MISMATCH` | Figure attached doesn't match the question (wrong structure, wrong values, missing labeled element). | ✓ (when persisted) |

#### From safety judge (`apps/tutoring/judges/safety.py`)

| Label | Definition | Auto |
|---|---|---|
| `SAFETY_HARMFUL` | Violence, self-harm, suicide, weapons, abuse, threats (critical severity). | ✓ |
| `SAFETY_INAPPROPRIATE` | Sexual content, severe profanity, age-inappropriate references (warning severity). | ✓ |

#### From validator — structural / format (`apps/tutoring/validator.py`)

| Label | Definition | Auto |
|---|---|---|
| `NO_QUESTION` | Practice/quiz response doesn't end with a question. | ✓ |
| `INFO_DUMP` | 6+ named concepts (numbers, percentages, terms) AND no question. | ✓ |
| `MULTI_PARAGRAPH` | Response is multiple paragraphs (format rule: one paragraph). | ✓ |
| `BANNED_OPENER` | Uses prescribed banned phrase: "Walk me through your steps", "Show me your working, step by step", "Let's check this one together — can you walk me through your steps?". | ✓ |
| `PADDING_FILLER` | Banned filler/recap: "Great question!", "Let's see…", meta-commentary, restating prior content. | ✓ |

#### From validator — semantic

| Label | Definition | Auto |
|---|---|---|
| `VERDICT_MISMATCH` | Tutor text contradicts high-confidence deterministic verdict (says "not quite" when MCQ answer was correct). | ✓ |

#### From step evaluation (`apps/tutoring/judges/step_eval.py` + ground truth)

| Label | Definition | Auto |
|---|---|---|
| `WRONG_VERDICT` | Tutor's correctness claim about the student's answer is wrong: affirmed an incorrect answer (false accept) OR rejected a correct one (false reject). | partial |
| `PREMATURE_ADVANCE` | Engine advanced to the next step but student hadn't demonstrated readiness. | partial |

#### From engine defensive strips

| Label | Definition | Auto |
|---|---|---|
| `THINKING_LEAK` | Response narrates the tutor's own reasoning ("I need to address…", "Let me first clarify…") — should have been stripped but wasn't. | partial |
| `TOOL_LEAK` | Internal `<tool_use>`, `pose_question(slot=...)`, XML or system syntax visible in response. | partial |

#### Human judgment only — pipeline doesn't catch

| Label | Definition | Auto |
|---|---|---|
| `LEAKS_ANSWER` | Gives away the answer when the step type called for student reasoning. Context-dependent on step_type. | ✗ |
| `IGNORES_STUDENT` | Doesn't address what the student just said (e.g., student points out a contradiction; tutor proceeds as if nothing). | ✗ |
| `OFF_TOPIC` | Drifts from the current lesson scope without justification (introduces unrelated examples, switches topic mid-step). | ✗ |
| `REPEATS` | Verbatim or near-verbatim phrase from a recent tutor turn (e.g., "Walk me through your steps" 4× in 5 turns). | ✗ |

### Auto-population summary

- **Fully auto** (12 labels): AUTHORED_QUESTION, UNFOUNDED_PRAISE, ARITHMETIC_ERROR, CLAIM_CONTRADICTED, CLAIM_UNVERIFIED, FIGURE_REF_UNATTACHED, SAFETY_HARMFUL, SAFETY_INAPPROPRIATE, NO_QUESTION, INFO_DUMP, MULTI_PARAGRAPH, VERDICT_MISMATCH
- **Partial auto** (7 labels): INCOHERENT, FIGURE_MISMATCH (need `judge_outputs` persistence), BANNED_OPENER, PADDING_FILLER, WRONG_VERDICT, PREMATURE_ADVANCE, THINKING_LEAK, TOOL_LEAK
- **Pure human judgment** (4 labels): LEAKS_ANSWER, IGNORES_STUDENT, OFF_TOPIC, REPEATS

Edward's actual labeling load is ~5 minutes per item: confirm the auto-populated set, fill in the 4 pure-judgment flags, author the action labels and `expected_labels`.

`expected_labels` should never contain an issue label — it's what a good response would be labeled with.

---

## On `eval_layer` — it's a source label, not a separate evaluator

The eval pipeline is **unified-LLM** (`conversational_tutor.py:6691`): deterministic numeric/MCQ check runs as a **first-layer input** to the combined judge; the LLM judge always gets the verdict and decides whether to anchor on it or override.

| `eval_layer` | Meaning |
|---|---|
| `deterministic_numeric` / `deterministic_mcq` / `deterministic` | LLM judge short-circuited via deterministic match — highest trust. |
| `combined_judge` | LLM judge applied judgment on top of deterministic input (equivalent forms, partial credit). |
| `llm_evaluator` | Legacy `_evaluate_step()` path (combined_judge skipped). |
| `keyword_fallback` | No instructor client — heuristic only. |
| `non_answer_skip` | Student input wasn't an answer ("yes", "help", "ok"). |

---

## Item schema — 3 sections

```json
{
  "item": {
    "item_id": "MATH_S18_T456",
    "subject": "math",
    "lesson_id": 42,
    "lesson_objective": "Find a missing angle x given that all angles sum to 360°",
    "conversation_history": [
      {"role": "student", "text": "..."},
      {"role": "tutor",   "text": "..."}
    ],
    "student_turn": "85"
  },

  "production": {
    "tutor_response": "Show me your working — write out each step that got you to 85.",
    "pipeline_trace": {
      "is_correct": false,
      "eval_layer": "deterministic_numeric",
      "step_type": "practice",
      "working_state": "no_working",
      "validator_issues": [],
      "rule_violations": [],
      "regenerated": false,
      "judge_outputs": {
        "step_eval":     {...},
        "arithmetic":    {...},
        "factual":       {...},
        "rule":          {...},
        "coherence":     {...},
        "figure_ref":    {...},
        "figure_vision": {...},
        "safety":        {...}
      }
    }
  },

  "annotation": {
    "student_claim_correct": true,
    "actual_labels":   ["ASK_WORKING", "BANNED_OPENER"],
    "expected_labels": ["ADVANCE"],
    "rationale": "Correct answer; working shown earlier. Uses prescribed banned phrase.",
    "verdict": {
      "passes": false,
      "missing_labels": ["ADVANCE"],
      "extra_labels":   ["ASK_WORKING", "BANNED_OPENER"],
      "failure_category": "over_eager_working_request"
    }
  }
}
```

`verdict.passes = set(actual_labels) == set(expected_labels)`.

---

## Three real examples from session 18

### Example A — `MATH_S18_T456` (over-eager working request)

```json
{
  "annotation": {
    "student_claim_correct": true,
    "actual_labels":   ["ASK_WORKING", "BANNED_OPENER"],
    "expected_labels": ["ADVANCE"],
    "rationale": "Correct answer; working shown earlier in conversation. Response uses a prescribed banned phrase pattern.",
    "verdict": {
      "passes": false,
      "missing_labels": ["ADVANCE"],
      "extra_labels": ["ASK_WORKING", "BANNED_OPENER"],
      "failure_category": "over_eager_working_request"
    }
  }
}
```

### Example B — Savy's "three angles instead of two" turn (INCOHERENT)

Production tutor response:
> "Good working! Now let's explore what happens when we have **three angles** on a straight line instead of two.
> A straight line has a ray dividing it into **two angles**. One angle is 42°. Find the other angle."

```json
{
  "annotation": {
    "student_claim_correct": true,
    "actual_labels":   ["ADVANCE", "INCOHERENT", "AUTHORED_QUESTION", "NO_QUESTION"],
    "expected_labels": ["ADVANCE"],
    "rationale": "Setup announces 'three angles' then poses a two-angle problem — internal contradiction misled Savy into writing (180-42)÷2.",
    "verdict": {
      "passes": false,
      "missing_labels": [],
      "extra_labels": ["INCOHERENT", "AUTHORED_QUESTION", "NO_QUESTION"],
      "failure_category": "incoherent_setup"
    }
  }
}
```

### Example C — Francis's "x = 140°" failure (false accept + leak)

Production tutor response:
> "Excellent! You correctly calculated x = 140°. Looking at the diagram, you can see how the ray divides the straight line into two angles: 42° and x. Since angles on a straight line always sum to 180°, we get x + 42° = 180°, so x = 138°."

```json
{
  "annotation": {
    "student_claim_correct": false,
    "actual_labels": [
      "ADVANCE",
      "EXPLAIN",
      "WRONG_VERDICT",
      "UNFOUNDED_PRAISE",
      "LEAKS_ANSWER",
      "ARITHMETIC_ERROR",
      "INCOHERENT",
      "INFO_DUMP",
      "FIGURE_REF_UNATTACHED"
    ],
    "expected_labels": ["SURFACE_ERROR"],
    "rationale": "Affirmed wrong answer (140° was wrong, 138° is correct), then leaked the correct answer, then contradicted its own affirmation in the same paragraph. Nine labels fire on one response.",
    "verdict": {
      "passes": false,
      "missing_labels": ["SURFACE_ERROR"],
      "extra_labels": ["ADVANCE", "EXPLAIN", "WRONG_VERDICT", "UNFOUNDED_PRAISE", "LEAKS_ANSWER", "ARITHMETIC_ERROR", "INCOHERENT", "INFO_DUMP", "FIGURE_REF_UNATTACHED"],
      "failure_category": "false_accept_with_leak"
    }
  }
}
```

---

## Recommended persistence change (now required for full label set)

Additive JSONField on `SessionTurn`:

```python
judge_outputs = models.JSONField(default=dict, blank=True)
```

Populated at the combined_judge call site with per-judge breakdown:

```json
{
  "step_eval":     {"answer_correct": true, "step_complete": false, "reasoning": "..."},
  "arithmetic":    {"corrections": [...]},
  "factual":       {"claims_checked": 0, "claims_contradicted": [...]},
  "rule":          {"violations": ["NO_AUTHORING", "RULE_1"]},
  "coherence":     {"violations": [{"type": "setup_mismatch", "span": "...", "reason": "..."}]},
  "figure_ref":    {"issues": [...]},
  "figure_vision": {"aligned": null, "mismatch_reason": "..."},
  "safety":        {"severity": "none", "categories": [], "reasoning": ""}
}
```

One migration, additive, no behavior change. Unlocks auto-population for INCOHERENT, FIGURE_MISMATCH, and per-rule breakdown for AUTHORED_QUESTION / UNFOUNDED_PRAISE.

---

## The iteration loop

1. **Build benchmark** — sample 50 items. For each: pipeline pre-populates auto-detected labels; Edward authors `expected_labels`, fills in 4 pure-judgment issues, writes rationale + failure_category.
2. **Score baseline** — `pass_rate = mean(verdict.passes)`, plus per-label frequency table and `failure_category` clusters.
3. **Hypothesize** — e.g., "the `BANNED_OPENER` cluster is caused by the regen-constraint prescription at `conversational_tutor.py:5349`."
4. **Modify** — change system prompt, judge prompt, or pipeline logic.
5. **Re-run** — replay each item's `conversation_history + student_turn`; recompute `production.pipeline_trace` + auto-populated labels + `verdict`.
6. **Compare** — did pass rate improve? Did the targeted cluster shrink? Did pipeline-detected labels match (i.e., is the auto-detection itself improving)?

The benchmark itself doesn't change. Only `production` + `actual_labels` (auto-populated portion) + `verdict` get re-derived per system variant. `expected_labels`, `rationale`, `student_claim_correct`, `failure_category` are stable.

**Bonus**: because the label set covers every judge, the benchmark also evaluates the **judges themselves** — Edward's `actual_labels` vs the auto-populated set tells you each judge's precision/recall on this dataset.

---

## Failure categories — controlled vocabulary

`failure_category` is the cluster tag used for grouping failed items. Each item with `verdict.passes == false` gets exactly one category. Predefined list — drives prioritization ("12 items hit `over_eager_working_request`, fix that first").

| Category | When to use | Typical labels involved |
|---|---|---|
| `over_eager_working_request` | Asked for working when answer is correct + working previously shown in conversation. | `ASK_WORKING` + `BANNED_OPENER` |
| `false_accept` | Affirmed an incorrect student answer without leaking the correct one. | `WRONG_VERDICT` + `UNFOUNDED_PRAISE` |
| `false_accept_with_leak` | Affirmed wrong answer AND revealed the correct one in the same response. | `WRONG_VERDICT` + `UNFOUNDED_PRAISE` + `LEAKS_ANSWER` |
| `false_reject` | Rejected a correct student answer. | `WRONG_VERDICT` (only) |
| `incoherent_setup` | Response announces direction X then immediately does Y. | `INCOHERENT` |
| `topic_jump` | Drifts to unrelated topic mid-step without student readiness. | `OFF_TOPIC` + possibly `PREMATURE_ADVANCE` |
| `bank_authoring` | Invented a practice/quiz question with values not in the bank. | `AUTHORED_QUESTION` |
| `figure_ref_broken` | Text references a figure that isn't attached. | `FIGURE_REF_UNATTACHED` |
| `figure_mismatch` | Figure attached doesn't match the question. | `FIGURE_MISMATCH` |
| `tool_leak` | Internal tool/XML syntax visible to student. | `TOOL_LEAK` |
| `over_explain` | Info dump — too much content before student's next chance to respond. | `INFO_DUMP` + possibly `MULTI_PARAGRAPH` |
| `premature_advance` | Engine moved to next step before student demonstrated readiness. | `PREMATURE_ADVANCE` |
| `ignores_student_input` | Doesn't address what the student just said (e.g., student points out tutor's error). | `IGNORES_STUDENT` |
| `bare_answer_chain` | Repeated cycle of student-bare → ask-working → student-bare. | `ASK_WORKING` + `REPEATS` |
| `unfounded_praise` | Praised bare/wrong answer but no other failure. | `UNFOUNDED_PRAISE` (only) |
| `arithmetic_in_tutor` | Tutor's own arithmetic is wrong (independent of any other failure). | `ARITHMETIC_ERROR` |
| `ungrounded_factual` | Tutor stated a factual claim contradicted or unsupported by curriculum. | `CLAIM_CONTRADICTED` or `CLAIM_UNVERIFIED` |
| `safety_violation` | Safety judge flagged the response. | `SAFETY_HARMFUL` or `SAFETY_INAPPROPRIATE` |
| `format_violation` | Format-only failure (multi-paragraph, padding) with no pedagogical issue. | `MULTI_PARAGRAPH` or `PADDING_FILLER` |
| `other` | Catch-all when no predefined category fits. Flag for adding a new category. |  |

Most failures match exactly one category. When multiple apply, pick the one Edward considers the **dominant** failure (the one driving the worst outcome).

---

## Status — v2 locked

- ✅ 30 labels locked. Iterate later if new failure modes appear that don't fit existing labels.
- ✅ 19 failure categories locked.
- ⏳ `judge_outputs` JSONField migration — to be implemented on `main` to start production data capture.
- ⏳ Annotation UI in Django super-admin — to be built once `judge_outputs` is shipping data.

### Out of scope for v2 (future iterations)

- Splitting `OFF_TOPIC` into "ignores student" vs "drifts from lesson"
- Severity grading on issue labels (currently boolean — "is this wrong" not "how wrong")
- Cross-turn pattern labels (`bare_answer_chain` is approximated via `REPEATS` + category)
