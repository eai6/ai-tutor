# Pose Question Two-Phase Commit — Implementation Plan

**Status**: Approved direction; spec ready to implement.
**Owner**: Roy Manzi (review) / Claude (implementation).
**Created**: 2026-05-27.
**Cutover**: rolling — three independent fixes, each ships when ready. No flag-gated migration.
**Related**:
- `design/tasks/move-router-implementation-plan.md` — the sibling router redesign; this plan lands in the same release window but is independent.
- `test-reports/MATHS-S1-evaluation-2026-05-27-run7.md` — P1 evidence (`mcq_options_missing` masquerading as the safe-template fallback).
- `test-reports/GEO-S5-evaluation-2026-05-27-run7.md` — P1 evidence (canned fallback twice in a row, root cause includes Phase-A silent rejection).
- `apps/tutoring/v2/tools/pose_question.py` — Phase A `validate_pose`.
- `apps/tutoring/v2/services/student_tutor.py` — `_handle_pose_tool_use`, `_render_bank_stem_with_options`, `_extract_mcq_letters`, `_call_with_tools`.
- `apps/tutoring/v2/services/tutor_engine.py` — the conformance retry loop at `:432-491`.

---

## 1. Why this plan exists

Three independent bugs in the `pose_question` two-phase commit pipeline that surfaced as P1 errors in the 2026-05-27 run-7 evaluation:

### Bug 1 — MCQ-options data-format mismatch

The bank schema does not standardize `LessonStep.choices` formatting. Some lessons author choices as `["A) evaporates", "B) condenses", ...]`; others as `["evaporates", "condenses", ...]` (no letter prefix). The pipeline assumes the letter-prefixed form:

- `_extract_mcq_letters()` at `student_tutor.py:917-937` uses `_MCQ_LETTER_RE = r"^\s*([A-Da-d])\s*[).:\-]"` — when choices lack a prefix this returns `[]`, leaving `mcq_option_order` empty downstream.
- `_render_bank_stem_with_options()` at `student_tutor.py:885-904` happily appends the bare choices (no letters).
- Phase A safety floor `_looks_like_mcq_stem_without_options()` at `pose_question.py:475-488` looks for inlined options via `r"(?m)^\s*[A-Da-d]\s*[).:\-]\s+\S"`. Bare choices don't match.
- Result: a stem reading "Which of the following…" with bare choices appended is **refused with `mcq_options_missing`** even though the choices ARE in the visible text.

The student sees the safe-template fallback. This is one of the upstream causes of the canned-fallback P1 in both run-7 reports.

### Bug 2 — Phase A rejection feedback is dropped

`_handle_pose_tool_use()` at `student_tutor.py:399-479` resolves a tool_use block into either a `PendingPose` or `None`. On `ToolRejection` the function returns `(None, "")` (line 472) and logs a warning. The LLM never sees why its tool call was refused. On retry, the LLM has no information to do anything different — it re-attempts the same slot (gets the same rejection) or invents prose (caught by conformance, retry loop completes, escalation fires, safe template ships).

The Anthropic tool-use protocol natively supports `tool_result` blocks with structured error content — the LLM can read the rejection reason and pick a different slot. This affordance is unused today.

### Bug 3 — `PendingPose` dropped silently on conformance failure

`tutor_engine.py:432-491` is the conformance retry loop. On first-attempt failure, the loop calls `_invoke_tutor_or_fallback` again with full pipeline re-execution — same prompt, same Phase A, same Phase B. The first attempt's `PendingPose` is discarded; the retry must reproduce it from scratch.

Two consequences:

1. **Wasted Phase A work.** Phase A is read-only and produced a valid result; throwing it away is pure inefficiency.
2. **Failure shape on prose-only conformance violations.** If conformance rejected for a prose-side reason (stickiness violation, answer leak, prose-question) but Phase A had passed, the retry re-renders prose AND re-attempts Phase A. The LLM may pick a different slot the second time, OR fail to pose at all, OR succeed but with different framing. The retry behaves like a fresh turn instead of like a focused render-fix.

Net: on prose-only failures, the pose can vanish between attempt and retry. The student sees a fallback when a valid question was already prepared.

---

## 2. Target architecture

```
                ┌──────────────────────────────────────────────────────┐
                │             StudentTutor._call_with_tools             │
                │                                                       │
   tool_dict ──►│                                                       │
                │  Multi-turn tool loop (NEW — Fix 2):                  │
                │    1. LLM emits tool_use(slot=N)                      │
                │    2. validate_pose(...) — Phase A                    │
                │       ├─ pass: PendingPose                            │
                │       └─ fail: tool_result(is_error=True,             │
                │                            content=rejection_reason)  │
                │    3. on rejection, loop back to LLM with             │
                │       tool_result appended; LLM picks                 │
                │       different slot. Max 2 tool-use turns per        │
                │       turn (slot N, slot M).                          │
                │                                                       │
                │  Returns (PendingPose | None, response_text)          │
                └──────────────────────────────────────────────────────┘
                                       │
                                       ▼
                ┌──────────────────────────────────────────────────────┐
                │           TutorEngine conformance retry loop          │
                │                                                       │
                │  Fix 3 — categorize conformance failures:             │
                │    ├─ PROSE_ONLY (stickiness, answer_leak,            │
                │    │   prose_question, state_coherence)               │
                │    │     → re-render prose ONLY                       │
                │    │     → HOLD PendingPose from first attempt        │
                │    │     → Phase A does NOT re-run                    │
                │    │                                                  │
                │    └─ POSE_RELATED (Phase A reject surfaced as        │
                │        extractor violation, missing tool call)        │
                │          → full retry with violation hints            │
                │          → Phase A may re-run                         │
                │                                                       │
                │  On retry pass: commit PendingPose (held or new)      │
                └──────────────────────────────────────────────────────┘
```

### 2.1 Fix 1 — MCQ-options renderer normalization

Two layers, defense-in-depth:

**Layer A — Authoring-time normalization** (durable fix).

A new save hook on `LessonStep` (`apps/curriculum/models.py::LessonStep.save`) normalizes `choices` when `answer_type == "multiple_choice"`:

```python
def _normalize_mcq_choices(self):
    if self.answer_type != "multiple_choice" or not self.choices:
        return
    if not isinstance(self.choices, list):
        return
    letters = ["A", "B", "C", "D", "E", "F"]
    normalized = []
    for i, choice in enumerate(self.choices):
        if not isinstance(choice, str):
            normalized.append(choice)
            continue
        stripped = choice.strip()
        if _MCQ_LETTER_RE.match(stripped):
            normalized.append(stripped)
        else:
            prefix = letters[i] if i < len(letters) else f"Option{i+1}"
            normalized.append(f"{prefix}) {stripped}")
    self.choices = normalized
```

A one-off data-migration `apps/curriculum/migrations/0029_normalize_mcq_choices.py` runs this normalizer over every existing `LessonStep` row with `answer_type=multiple_choice`. Idempotent — re-running is safe.

**Layer B — Renderer fallback** (immediate fix, also catches edge cases the save hook misses).

`_render_bank_stem_with_options()` at `student_tutor.py:885-904` gains a synthesis branch: when choices lack letter prefixes, synthesize them at render time:

```python
if answer_type == "multiple_choice":
    choices = getattr(step, "choices", None) or []
    if not isinstance(choices, list) or not choices:
        return stem
    rendered_choices = []
    for i, c in enumerate(choices):
        if not isinstance(c, str):
            continue
        cs = c.strip()
        if not cs:
            continue
        if _MCQ_LETTER_RE.match(cs):
            rendered_choices.append(cs)
        else:
            letter = chr(ord("A") + i)
            rendered_choices.append(f"{letter}) {cs}")
    if not rendered_choices:
        return stem
    return f"{stem}\n\n" + "\n".join(rendered_choices)
```

`_extract_mcq_letters()` at `student_tutor.py:917-937` gains a fallback: when no choice matches `_MCQ_LETTER_RE`, return `[chr(ord("A") + i) for i in range(len(non_empty_choices))]` — synthetic letters consistent with what the renderer just produced.

**Layer C — Safety floor sharpening** (reduces false positives if the upstream fixes are bypassed).

`_looks_like_mcq_stem_without_options()` at `pose_question.py:475-488` accepts an expanded "options inlined" pattern. Today it only matches `r"(?m)^\s*[A-Da-d]\s*[).:\-]\s+\S"`. Extend to also accept:

- Bullet-prefixed lines: `r"(?m)^\s*[-*•]\s+\S"`
- Numbered lines: `r"(?m)^\s*\d+\s*[).:\-]\s+\S"`

The safety floor still fires when the stem looks like an MCQ AND none of the patterns match — i.e., when choices are genuinely missing.

### 2.2 Fix 2 — Phase A rejection feedback via `tool_result`

`_call_with_tools()` at `student_tutor.py:270-397` is rewritten to support a bounded multi-turn tool loop within a single tutor turn:

```python
def _call_with_tools(self, *, client, system_prompt, user_prompt,
                    tool_dict, slot_map, context, move):
    """Multi-turn tool loop with Phase A feedback.

    Up to MAX_POSE_ATTEMPTS_PER_TURN tool_use rounds. On Phase A
    rejection, append a tool_result(is_error=True, content=reason)
    block and let the LLM pick a different slot. Stop on first
    successful PendingPose, on max attempts, or on no tool_use block.
    """
    MAX_POSE_ATTEMPTS_PER_TURN = 2
    messages = [{"role": "user", "content": user_prompt}]
    posed_step_ids_used: set[int] = set()

    for attempt in range(MAX_POSE_ATTEMPTS_PER_TURN):
        tool_choice = (
            {"type": "auto"}
            if attempt == 0
            else {"type": "any"}  # if first attempt failed, force a retry call
        )
        message = client.generate_with_tools(
            messages=messages, system_prompt=system_prompt,
            tools=[tool_dict], max_tokens=900, tool_choice=tool_choice,
        )
        text_chunks, tool_use_block, pending_pose, rejection = (
            self._process_tool_message(message, slot_map, context)
        )
        if pending_pose is not None:
            return _assemble_response(text_chunks, pending_pose,
                                      rendered_stem=pending_pose.rendered_stem)
        if tool_use_block is None:
            # No tool call — text-only response or refusal. Return as-is.
            return _assemble_response(text_chunks, None, "")
        # Phase A rejected. Append assistant message + tool_result and loop.
        messages.append({"role": "assistant", "content": message.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_block.id,
                "is_error": True,
                "content": _format_rejection_for_llm(rejection,
                                                    posed_step_ids_used),
            }],
        })
        posed_step_ids_used.add(_slot_from_block(tool_use_block, slot_map))

    # Exhausted attempts — return whatever text we got, no pose.
    return _assemble_response(text_chunks, None, "")
```

`_format_rejection_for_llm()` is a new helper that converts the `ToolRejection.reason` into actionable guidance:

| `ToolRejection.reason` | LLM-facing tool_result content |
|---|---|
| `mcq_options_missing` | *"Slot N rejected: the stem reads like a multiple-choice question but no options were rendered. Pick a different slot."* |
| `in_session_repeat` | *"Slot N rejected: this question has already been posed this session. Pick a different slot."* |
| `cross_session_repeat` | *"Slot N rejected: this question was asked recently in a prior session. Pick a different slot."* |
| `ref_unresolved` / `schema_invalid` | *"Slot N rejected: tool arguments invalid (`{detail}`). Pick a different slot."* |
| `not_derivable` | *"Slot N rejected: the canonical answer cannot be derived from the visible stem. Pick a different slot."* |
| `token_invalid` | *"Token rejected: invalid or already consumed. Use a bank slot instead."* |

The tool_result content includes the set of slots already attempted this turn, so the LLM doesn't loop on the same slot:

> *"Slots already attempted and rejected this turn: [3]. Choose from the remaining available slots."*

**MAX_POSE_ATTEMPTS_PER_TURN = 2** keeps the loop bounded — at most one retry within a single tutor turn. If both attempts fail, the conformance layer takes over via the existing retry mechanism (which now sees the tool path exhausted and the chosen_move likely needs to change downstream, e.g., to `close_topic`/`pivot`).

### 2.3 Fix 3 — Sticky PendingPose on prose-only conformance failures

`tutor_engine.py:432-491` is rewritten with conformance-failure categorization:

```python
PROSE_ONLY_VIOLATIONS = frozenset({
    "stickiness", "answer_leak", "state_coherence",
    "figure_ref", "praise_filter",
    "verdict_no_affirm", "verdict_no_refute", "verdict_no_uncertainty",
    "all__no_assessment_in_prose",  # debatable — see §2.3.1
})

POSE_RELATED_VIOLATIONS = frozenset({
    "missing_tool_call_when_expected",
    "extractor_assessment_question_in_prose",
})

def _classify_conformance_failure(violations: list[str]) -> str:
    """Return 'prose_only' | 'pose_related' | 'mixed'."""
    has_prose = any(v in PROSE_ONLY_VIOLATIONS for v in violations)
    has_pose = any(v in POSE_RELATED_VIOLATIONS for v in violations)
    if has_pose and not has_prose:
        return "pose_related"
    if has_prose and not has_pose:
        return "prose_only"
    return "mixed"
```

On first-attempt failure:

- **`prose_only`**: retry path **holds the first attempt's `PendingPose`**. The retry call to `_invoke_tutor_or_fallback` passes a new kwarg `hold_pending_pose=first_resp.pending_pose`. Inside `StudentTutor.respond`, when this kwarg is non-None, the tool path is **skipped entirely** — the LLM is called text-only with violation hints, and the held `PendingPose` is reattached to the returned `TutorResponse` for the retry. Phase A does not re-run.

- **`pose_related`** OR **`mixed`**: full retry as today. Phase A re-runs from scratch. The first attempt's `PendingPose` is discarded.

The Phase B commit at `tutor_engine.py:595-604` is unchanged — it commits whichever `PendingPose` ends up on the final attempt that conformance passes.

#### 2.3.1 The `all__no_assessment_in_prose` edge case

This violation fires when the LLM emitted an assessment question in prose AND the tool was available but not called. Two sub-cases:

- Sub-case A: LLM emitted prose-question AND no tool_use block (the pose-related shape).
- Sub-case B: LLM emitted both prose-question AND a tool_use (over-eager LLM).

In sub-case B, the `PendingPose` from the tool_use IS valid; the prose question is a duplicate. Re-rendering prose with the held `PendingPose` is the right behavior.

The classification rule: `all__no_assessment_in_prose` counts as `prose_only` IF `first_resp.pending_pose is not None`, else `pose_related`.

### 2.4 New runtime metrics for observability

Three new spans / fields in the v2 observability dashboard:

| Span / field | When emitted | Useful for |
|---|---|---|
| `pose_question.phase_a_rejection` (span) | Every Phase A rejection inside the tool loop | Track rejection rates by reason (early signal of authoring bugs) |
| `pose_question.tool_loop_attempts` (span field, on the per-turn pose span) | Counts attempts per turn (0/1/2) | Monitor that the multi-turn loop is working |
| `conformance.retry_classification` (span field) | `prose_only` / `pose_related` / `mixed` on every conformance retry | Validate that sticky retry is taking the right path |

---

## 3. Files touched

### Fix 1 — MCQ-options renderer

| File | Change | Lines |
|---|---|---|
| `apps/curriculum/models.py` | **Add** `LessonStep._normalize_mcq_choices()` + call from `save()`. | ~25 new |
| `apps/curriculum/migrations/0029_normalize_mcq_choices.py` | **New** data migration running the normalizer over existing rows. | ~50 new |
| `apps/tutoring/v2/services/student_tutor.py` | **Extend** `_render_bank_stem_with_options` with letter-synthesis branch. **Extend** `_extract_mcq_letters` with synthetic-letter fallback. | ~30 net |
| `apps/tutoring/v2/tools/pose_question.py` | **Extend** `_looks_like_mcq_stem_without_options` to recognize bullet-prefixed and numbered options. | ~10 net |
| `apps/curriculum/tests/test_lesson_step_mcq_normalize.py` | **New** unit tests for the save hook + migration. | ~80 new |
| `apps/tutoring/v2/tests/test_pose_question_schema.py` | **Add** tests for bullet/numbered options in `_looks_like_mcq_stem_without_options`. | ~40 new |

### Fix 2 — Phase A `tool_result` feedback

| File | Change | Lines |
|---|---|---|
| `apps/tutoring/v2/services/student_tutor.py` | **Rewrite** `_call_with_tools` as a bounded multi-turn loop. **Extract** `_process_tool_message()`, `_format_rejection_for_llm()`, `_assemble_response()` as helpers. | ~180 net |
| `apps/tutoring/v2/tools/pose_question.py` | **No changes to validate_pose itself.** The function already returns `ToolRejection` with `reason` + `detail`. | 0 |
| `apps/tutoring/v2/tests/test_pose_question_schema.py` | **Add** tests that the LLM receives a `tool_result(is_error=True)` block on rejection, and that the second attempt uses a different slot from the first. | ~120 new |

### Fix 3 — Sticky PendingPose retry

| File | Change | Lines |
|---|---|---|
| `apps/tutoring/v2/services/conformance/check.py` | **Add** `PROSE_ONLY_VIOLATIONS` / `POSE_RELATED_VIOLATIONS` constants + `classify_conformance_failure(violations) -> str` helper. | ~50 new |
| `apps/tutoring/v2/services/tutor_engine.py` | **Rewrite** the conformance retry block at `:432-491`: classify failure, branch on `prose_only` vs other. | ~80 net |
| `apps/tutoring/v2/services/student_tutor.py` | **Extend** `respond` with `hold_pending_pose: Optional[PendingPose] = None` kwarg. When non-None: skip tool path, call `generate` text-only with violation hints, reattach the held `PendingPose` to the returned `TutorResponse`. | ~40 net |
| `apps/tutoring/v2/tests/test_conformance_orchestrator.py` | **Add** tests for the sticky-pose retry path. | ~150 new |

### Cross-cutting

| File | Change | Lines |
|---|---|---|
| `apps/tutoring/v2/contracts/tutoring.py` | **Extend** `TutorResponse` no changes needed (existing `pending_pose` field is reused). | 0 |
| `apps/tutoring/tracing.py` | **Add** `pose_question.phase_a_rejection` span name to the v2 dashboard registry. | ~10 |
| `apps/dashboard/views.py` (v2 observability) | **Surface** the three new spans/fields on the per-turn detail view. | ~30 |
| `CLAUDE.md` | **Update** the "Grader-driven correctness + structural conformance" paragraph to describe the Phase A feedback loop + sticky retry. | ~15 |

Net: ~850 lines added across three feature areas, ~30 lines deleted. All three fixes are independent and can ship in any order.

---

## 4. Implementation order

Each fix is independent. Recommended ordering by impact / risk:

### Phase 1 — Fix 1 (MCQ renderer)

Lowest risk, highest immediate impact (resolves the misclassified P1 shape).

1. **Add save hook + data migration**. Apply migration locally and verify count of normalized rows.
2. **Extend renderer + extractor**. Unit tests cover both letter-prefixed and bare-choice cases.
3. **Sharpen the safety floor**. Test the bullet/numbered cases.
4. **Live-test in dev**: pick a LessonStep with bare-choice MCQ, drive a tutoring session via `chrome-devtools-mcp`, confirm the question reaches the student with synthesized letters.

### Phase 2 — Fix 3 (sticky retry)

Medium risk (changes conformance retry control flow). Highest leverage on the "canned fallback twice in a row" shape.

1. **Add classification helper** in `conformance/check.py`. Unit tests cover all violation-set permutations.
2. **Extend `StudentTutor.respond`** with `hold_pending_pose` kwarg. Unit tests verify text-only path skips tool dict.
3. **Rewrite conformance retry block** in `tutor_engine.py`. Integration tests cover `prose_only` and `pose_related` paths.
4. **Live-test in dev**: trigger a stickiness violation (e.g., force a `scaffold_hint` to drift), confirm the retry re-renders prose without losing the original pose.

### Phase 3 — Fix 2 (Phase A `tool_result` feedback)

Highest risk (changes the multi-turn shape of every tutor LLM call that uses tools). Lower immediate impact than Fix 3 because the conformance retry now (with Fix 3) has the right shape to recover from Phase A rejections at the next turn boundary; this fix tightens it to the same-turn boundary.

1. **Implement the multi-turn loop helper** + extract the three sub-functions. Unit tests with stubbed Anthropic Messages.
2. **Live-test in dev**: force a Phase A rejection (`in_session_repeat` on a deliberately re-posed slot), confirm the LLM picks a different slot on the second tool call within the same turn.
3. **Monitor `pose_question.tool_loop_attempts` distribution** for a week post-cutover. Expected: 95%+ at 1, ≤5% at 2. If higher, the LLM is struggling to pick valid slots and authoring may need attention.

### Validation

After all three fixes ship, run:

- `apps/tutoring/v2/tests/` full suite.
- MATHS-S1 and GEO-S5 evaluation scenarios end-to-end.
- Full benchmark per `memory/eval_benchmark_v2_simplified.md`.

Target: 0 P1 errors on the four run-7 P1 cases (math 5/7/9 prose proof, math 5/12/13 resume affirm, geo meta-input grading, geo guess-B advance).

---

## 5. Test plan

### 5.1 Fix 1 — MCQ-options tests

```python
# ─── Save-hook normalizer ─────────────────────────────────────

def test_lessonstep_save_hook_synthesizes_letters_on_bare_choices():
    step = LessonStep.objects.create(
        ..., answer_type="multiple_choice",
        choices=["evaporates", "condenses", "precipitates"],
    )
    assert step.choices == [
        "A) evaporates", "B) condenses", "C) precipitates",
    ]

def test_lessonstep_save_hook_idempotent_on_prefixed_choices():
    step = LessonStep.objects.create(
        ..., answer_type="multiple_choice",
        choices=["A) foo", "B. bar", "C: baz"],
    )
    # Pre-existing letter prefixes pass through unchanged.
    assert step.choices == ["A) foo", "B. bar", "C: baz"]

def test_lessonstep_save_hook_skips_non_mcq():
    step = LessonStep.objects.create(
        ..., answer_type="short_answer",
        choices=["foo", "bar"],
    )
    # No normalization for non-MCQ.
    assert step.choices == ["foo", "bar"]

def test_migration_0029_normalizes_existing_rows():
    """Apply migration over a fixture row with bare choices.
    Post-migration, the row has letter-prefixed choices."""

# ─── Renderer + extractor fallback ───────────────────────────

def test_render_bank_stem_synthesizes_letters_when_missing():
    step = SimpleNamespace(
        question="Which of the following describes condensation?",
        answer_type="multiple_choice",
        choices=["evaporates", "condenses", "precipitates"],
    )
    rendered = _render_bank_stem_with_options(step)
    assert "A) evaporates" in rendered
    assert "B) condenses" in rendered
    assert "C) precipitates" in rendered

def test_extract_mcq_letters_synthesizes_when_no_prefixes():
    step = SimpleNamespace(
        answer_type="multiple_choice",
        choices=["evaporates", "condenses", "precipitates"],
    )
    assert _extract_mcq_letters(step) == ["A", "B", "C"]

# ─── Safety floor sharpening ──────────────────────────────────

def test_looks_like_mcq_accepts_bullet_options():
    stem = "Which of the following is true?\n\n- foo\n- bar\n- baz"
    assert _looks_like_mcq_stem_without_options(stem) is False

def test_looks_like_mcq_accepts_numbered_options():
    stem = "Which of the following is true?\n\n1. foo\n2. bar\n3. baz"
    assert _looks_like_mcq_stem_without_options(stem) is False

def test_looks_like_mcq_still_refuses_genuinely_missing_options():
    stem = "Which of the following describes the hydrological cycle?"
    assert _looks_like_mcq_stem_without_options(stem) is True
```

### 5.2 Fix 2 — Phase A feedback tests

```python
# ─── Multi-turn tool loop ─────────────────────────────────────

def test_tool_loop_returns_first_pending_pose_immediately():
    """First tool_use passes Phase A. No second LLM call."""

def test_tool_loop_retries_on_phase_a_rejection():
    """First tool_use(slot=3) → in_session_repeat rejection.
    Second LLM call sees tool_result(is_error=True). LLM emits
    tool_use(slot=5). Phase A passes. Returns PendingPose for slot=5."""

def test_tool_loop_includes_attempted_slots_in_rejection_message():
    """After first rejection, the tool_result content lists
    'Slots already attempted and rejected this turn: [3]'.
    Verified by inspecting the messages list passed to the
    second LLM call."""

def test_tool_loop_max_attempts_is_2():
    """Both tool calls rejected. Loop exits without committing
    a pose. Returns (None, response_text). No third LLM call."""

def test_tool_loop_handles_no_tool_use_on_retry():
    """First tool_use rejected. Second LLM call returns text only,
    no tool_use. Loop exits cleanly with text response."""

# ─── Per-reason formatting ────────────────────────────────────

@pytest.mark.parametrize("reason,detail,expected_substring", [
    ("mcq_options_missing", "...", "multiple-choice question but no options"),
    ("in_session_repeat", "...", "already been posed this session"),
    ("cross_session_repeat", "...", "asked recently in a prior session"),
    ("ref_unresolved", "no canonical for ...", "tool arguments invalid"),
    ("not_derivable", "...", "canonical answer cannot be derived"),
    ("token_invalid", "...", "invalid or already consumed"),
])
def test_format_rejection_per_reason(reason, detail, expected_substring):
    """Each rejection reason maps to actionable LLM-facing text."""

# ─── Span instrumentation ─────────────────────────────────────

def test_phase_a_rejection_span_emitted_per_rejection():
    """Two consecutive rejections → two phase_a_rejection spans."""

def test_tool_loop_attempts_span_field_correct():
    """Single-attempt success → tool_loop_attempts=1.
    One rejection then success → tool_loop_attempts=2."""
```

### 5.3 Fix 3 — Sticky retry tests

```python
# ─── Classification helper ────────────────────────────────────

def test_classify_prose_only_violations():
    assert classify_conformance_failure(["stickiness"]) == "prose_only"
    assert classify_conformance_failure(["answer_leak", "praise_filter"]) == "prose_only"

def test_classify_pose_related_violations():
    assert classify_conformance_failure(
        ["extractor_assessment_question_in_prose"]
    ) == "pose_related"

def test_classify_mixed_violations():
    assert classify_conformance_failure(
        ["stickiness", "missing_tool_call_when_expected"]
    ) == "mixed"

def test_classify_no_assessment_with_pending_pose_is_prose_only():
    """all__no_assessment_in_prose + pending_pose != None →
    sub-case B from §2.3.1 → prose_only."""

def test_classify_no_assessment_without_pending_pose_is_pose_related():
    """all__no_assessment_in_prose + pending_pose == None →
    sub-case A → pose_related."""

# ─── Retry path branching ─────────────────────────────────────

def test_sticky_retry_holds_pending_pose_on_prose_only_failure():
    """First attempt: PendingPose committed, conformance fails on
    stickiness violation. Retry call to StudentTutor.respond
    receives hold_pending_pose=first_pose. Retry response.text is
    re-rendered prose; returned pending_pose IS the held one."""

def test_sticky_retry_skips_tool_path_when_holding_pose():
    """StudentTutor.respond(hold_pending_pose=...) does NOT bind
    the pose_question tool to the LLM call. Verified by patching
    generate_with_tools to raise (it shouldn't be called)."""

def test_pose_related_retry_runs_full_pipeline():
    """First attempt: no PendingPose, conformance fails on
    missing_tool_call_when_expected. Retry runs full
    _invoke_tutor_or_fallback pipeline (no hold_pending_pose)."""

def test_mixed_retry_runs_full_pipeline():
    """First attempt: PendingPose + violations include both
    prose_only and pose_related. Retry runs full pipeline,
    held pose discarded."""

# ─── Phase B commit behavior ──────────────────────────────────

def test_phase_b_commits_held_pose_after_prose_only_retry_passes():
    """End-to-end: first attempt drift → prose_only retry with held
    pose → retry passes conformance → runtime_state.open_question
    matches the FIRST attempt's PendingPose (not re-validated)."""

def test_phase_b_no_commit_when_retry_also_fails():
    """First attempt prose_only fail → retry also fails →
    escalation fires. No pose committed. runtime_state.open_question
    is unchanged."""

# ─── Observability ────────────────────────────────────────────

def test_retry_classification_span_field_emitted():
    """conformance.retry_classification field present on every
    retry span: prose_only / pose_related / mixed."""
```

### 5.4 Integration scenarios (the four run-7 P1 cases)

Run after all three fixes are landed. Marked `@pytest.mark.integration`.

```python
def test_geo_meta_input_does_not_canned_fallback():
    """GEO-S5 run-7 P1: student says 'i dont understand. what is
    condensation'. With the redesigned grader (already shipped) +
    the move-router (sibling plan) + Fix 3 holding the pose on
    any prose-only conformance issue, the student receives an
    explain or worked_example move, NOT the canned safe template."""

def test_maths_pythagoras_resume_does_not_re_emit_engage():
    """MATHS-S1 run-7 P1-1: resume turn with three fully-shown
    correct solutions. With the redesigned grader, sticky open_question
    rehydration (sibling plan §2.6 reframe), and Fix 3, the tutor
    confirms-and-extends or closes; does NOT re-emit the engage
    paragraph."""

def test_mcq_with_bare_choices_reaches_student():
    """LessonStep with answer_type=multiple_choice and bare-choice
    list. Fix 1 normalizes at save time AND at render time. The
    visible response shipped to the student contains 'A) foo',
    'B) bar', 'C) baz'. Phase A does NOT refuse with
    mcq_options_missing."""

def test_phase_a_rejection_on_repeat_recovers_within_turn():
    """LLM picks slot already in the in_session_repeat ledger.
    Phase A refuses with in_session_repeat. Fix 2 surfaces the
    rejection back to the LLM as tool_result. LLM picks a
    different slot. Same turn ships with a valid PendingPose."""
```

---

## 6. Cutover

Three independent fixes, no flag-gating per directive. Each ships on its own PR when ready:

1. **PR 1 — Fix 1 (MCQ renderer + save hook + migration)**. Lowest risk; ship first.
2. **PR 2 — Fix 3 (sticky retry)**. Ships after PR 1 is in main and stable.
3. **PR 3 — Fix 2 (Phase A feedback loop)**. Ships last; benefits from the prior two being live.

**Rollback path**: each fix is independent; revert the specific PR if a regression surfaces. The `NEW_TUTOR=off` kill switch remains as the engine-level fallback to legacy `ConversationalTutor`.

**Pre-cutover checklist (per PR)**:

- [ ] Unit tests for that fix green.
- [ ] One MATHS-S1 + one GEO-S5 dry run in dev does not regress.
- [ ] Observability span(s) for that fix emitting on dev sessions.

**Post-cutover validation (within 24h of each PR)**:

- [ ] Production dashboard shows the new span(s) on representative sessions.
- [ ] No P1-class incident reports tied to the changed code path.
- [ ] Rejection-rate / loop-attempt distributions in expected ranges (PR 2/3 only).

---

## 7. Risks

1. **Save-hook + migration race condition.** If a deploy lands the migration but not the save hook (or vice versa), bare-choice rows could re-appear via the dashboard editor. Mitigated by shipping both in the same PR and gating the migration on the model code being present.

2. **`tool_choice={"type":"any"}` on retry inside the tool loop may conflict with the sibling router redesign.** The router plan deletes the force-mode for `pose_question` move; the same force-mode is reintroduced here as a per-retry mechanism. Resolution: this force-mode is internal to `_call_with_tools` and applies only to the second tool-loop attempt (not the move-selection layer). The router layer is unaffected.

3. **Multi-turn tool loop adds latency on Phase A rejections.** Worst case: 2 LLM calls per tutor turn when only 1 was expected. Per the 2026-05-27 directive (quality over latency), acceptable. Monitored via `tool_loop_attempts` distribution.

4. **Sticky-pose retry could mask a real LLM hallucination.** If the held pose was for question X and the LLM re-renders prose pretending to discuss question Y, the conformance retry passes and the student sees pose-for-X with prose-for-Y. Mitigated by the existing state-coherence and figure-ref gates which run on the retry — if the retry prose claims X is Y, they reject. Worst case is one wasted retry, not a P1.

5. **Classification helper drift.** Conformance violation names live in two places now: `verdict_matrix.py` (the source) and `PROSE_ONLY_VIOLATIONS` / `POSE_RELATED_VIOLATIONS` (the classifier). New violations added to the classifier require corresponding test coverage. Mitigated by a "violation names exhaustive" test that asserts every active rule name in the conformance pipeline appears in one of the two sets (or is explicitly listed as "ignored").

---

## 8. Out of scope

- Replacing the conformance classifier or any of its existing rules — all stay as-is.
- Changing the `pose_question` tool schema or backend provenance enforcement (`bank_question_id` / `pre_pose_token`) — unchanged.
- Replacing the cross-session repeat guard — unchanged.
- Changes to the `pose_inline_question` token path — unchanged. The `tool_result` feedback (Fix 2) applies uniformly to both bank and token paths.
- Frontend changes — none. All fixes are server-side.
- The move-router redesign — sibling plan, lands independently.

---

## 9. Definition of done

- [ ] All three PRs (Fix 1, Fix 2, Fix 3) merged to main.
- [ ] Migration `0029_normalize_mcq_choices` applied in production.
- [ ] All §5.1-5.3 unit tests green in CI.
- [ ] §5.4 integration scenarios green against the cutover engine (router + grader + pose-question fixes all live).
- [ ] MATHS-S1 + GEO-S5 evaluation scenarios run end-to-end with 0 P1 errors.
- [ ] v2 observability dashboard shows the three new spans/fields on representative production sessions.
- [ ] Post-cutover monitoring stable for 7 days: no P1-class incident reports tied to pose-question pipeline.
- [ ] `CLAUDE.md` updated to describe Phase A feedback loop + sticky retry behavior.
