# Benchmark JSONL Export — Plan (2026-05-14)

## Problem

The current CSV export (`apps/benchmark/views.py:66 benchmark_export_annotations`) emits 20 columns but loses the **richest signal in the data**: the full conversation history, lesson objective, and the step-level structured material (script + question + answer key) that distinguishes our system from existing tutor-eval benchmarks.

User wants the exported data to be:
1. **Drop-in compatible with the SIG-EDU shared tasks** (BEA 2023 + 2025) so external evaluators can consume it
2. **Carry our unique signal**: the lesson objective, the current step's teacher script, and the question + expected answer when the step is an assessment. This is what justifies our system being a competency-based platform; external evaluators benchmarking us against MathDial / TutorBench should see this context.

## Current state (from audit)

**Already snapshotted** (`apps/benchmark/sampling.py:67-195` `build_item_snapshot`):
- `conversation_history`: list of `{turn_id, role, text}` (lines 107-114)
- `lesson_id`, `lesson_title`, `lesson_objective` (lines 170-172)
- `current_step_id`, `current_step_type` (lines 173-174)
- `student_turn`: `{turn_id, text}` (lines 176-179)
- `production.tutor_response` (line 189)
- `production.pipeline_trace` (full judge_outputs etc., lines 120-155)

**NOT in the snapshot, but reachable via FK at export time:**
- `LessonStep.teacher_script` (`apps/curriculum/models.py:314`)
- `LessonStep.question` (line 317)
- `LessonStep.expected_answer` (line 333)
- `LessonStep.choices` (line 328 — JSON for MCQ)
- `LessonStep.rubric` (line 337 — for free-text grading)
- `LessonStep.answer_type` (line 323)

Hydrate from `BenchmarkItem.source_turn → session.lesson.steps.filter(id=current_step_id).first()`. If the step has been deleted (course regen) → null fields.

## Decisions confirmed (user, 2026-05-14)

- **JSONL, not CSV**, for the new export. Nested data (utterances + tutor responses + annotations) doesn't fit CSV cleanly.
- Keep existing CSV download untouched — it's good for spreadsheet review.
- Mirror BOTH BEA 2023 AND BEA 2025 schemas in one JSONL row (cheap; consumers pick what they need).
- Carry our extra signal under non-conflicting keys (`lesson_context`, `extra_annotation`) so external evaluators ignore them cleanly.

## Target design

### A. New endpoint

`GET /dashboard/benchmark/export.jsonl` — same filter semantics as the CSV endpoint (`?subject=`, `?stratum=`, `?status=`, optional `?lesson_id=`). Streams `application/x-ndjson` so it scales beyond a few thousand rows.

Same super-admin gate as the CSV endpoint.

Filename: `annotations-<subject>-<stratum>-<status>-YYYYMMDD-HHMMSS.jsonl`.

### B. Per-row schema

One JSON object per line. Each line corresponds to one `(BenchmarkItem, BenchmarkAnnotation)` pair (so a single item with multiple annotations becomes multiple lines — the conversation history is duplicated per line, which is the BEA-2025 pattern).

```json
{
  "conversation_id": "MATH_S18_T456:production_v1:human:42",
  "item_id": "MATH_S18_T456",
  "subject": "math",

  "utterances": [
    {"text": "Let's start by defining a fraction.", "speaker": "Tutor"},
    {"text": "What's a fraction?", "speaker": "Student"}
  ],
  "conversation_history": "Tutor: Let's start by defining a fraction.\nStudent: What's a fraction?",

  "tutor_responses": {
    "production_v1": {
      "response": "A fraction is a part of a whole...",
      "annotation": {
        "Mistake_Identification": "Yes",
        "Mistake_Location": "Yes",
        "Providing_Guidance": "To some extent",
        "Actionability": "No"
      },
      "extra_annotation": {
        "actual_labels": ["explanation_clear", "missing_check_for_understanding"],
        "expected_labels": ["explanation_clear", "asks_followup_question"],
        "failure_categories": ["no_check_for_understanding"],
        "safety_concern": false,
        "rationale": "Tutor explained well but didn't check if student understood",
        "passes": false,
        "annotator_role": "human",
        "annotator_user_id": 1,
        "annotator_model": "",
        "system_variant": "production_v1",
        "stratum": "missing_check"
      }
    }
  },

  "lesson_context": {
    "lesson_id": 638,
    "lesson_title": "Introduction to Fractions",
    "lesson_objective": "Students will define a fraction and identify numerator and denominator.",
    "current_step": {
      "step_id": 1842,
      "step_type": "quiz",
      "teacher_script": "Now let's check your understanding...",
      "question": "What's the numerator in 3/4?",
      "answer_type": "short_numeric",
      "expected_answer": "3",
      "choices": null,
      "rubric": ""
    }
  },

  "metadata": {
    "session_id": 18,
    "turn_id": 456,
    "created_at": "2026-05-11T14:23:00Z",
    "updated_at": "2026-05-12T09:15:00Z"
  }
}
```

#### Field choice rationale

- `conversation_id` includes the system_variant + annotator + annotation_id so multi-rater / multi-system annotations get unique row ids without collisions
- `utterances` (list of dicts) → BEA-2023-shaped, but speaker labels are `"Tutor"` / `"Student"` (capitalized) per user spec — not BEA's lowercase `"teacher"` / `"student"`. Matches our internal terminology and the inline labels in the `conversation_history` string. External BEA-2023 evaluators will need a one-line mapping if they're case-sensitive.
- `conversation_history` (string) → BEA 2025 shape, derived by joining utterances with `\n`
- `tutor_responses` is a dict keyed by `system_variant` → BEA 2025 supports multi-candidate; we only have one candidate per (item, system_variant, annotator) so the dict has one key per row. (Future: emit one row per item with all variants merged into the dict; defer until we have multiple variants in production.)
- `annotation` block: maps our `actual_labels` → BEA 2025's 4-dim ordinal via a deterministic table (see C below). Carries no information loss because `extra_annotation` has the full label set.
- `lesson_context` is the block that distinguishes us — external evaluators get the lesson objective + step script + answer key so they can score "did the tutor honour the lesson plan?" not just "did the tutor produce a plausible response?"
- `metadata` block: provenance for re-derivation if needed (session_id, turn_id, timestamps)

### C. Label mapping: ours → BEA 2025 4-dim

`apps/benchmark/labels.py` already defines our rubric. Map each BEA dimension by inspecting `actual_labels` + `expected_labels`:

```python
# apps/benchmark/bea_mapping.py (new)

BEA_DIM_RULES = {
    'Mistake_Identification': {
        # "Yes": tutor recognised the student's error
        'yes_if_actual':  {'identifies_error', 'corrects_error'},
        'partial_if':     {'partial_acknowledgment'},
        # "No": expected to but didn't
        'no_if_expected_minus_actual': {'identifies_error', 'corrects_error'},
    },
    'Mistake_Location': {
        'yes_if_actual':  {'pinpoints_error_location'},
        'partial_if':     {'general_error_acknowledgment'},
        'no_if_expected_minus_actual': {'pinpoints_error_location'},
    },
    'Providing_Guidance': {
        'yes_if_actual':  {'gives_hint', 'asks_socratic_question', 'demonstrates_method'},
        'partial_if':     {'gentle_nudge'},
        'no_if_expected_minus_actual': {'gives_hint', 'asks_socratic_question'},
    },
    'Actionability': {
        'yes_if_actual':  {'gives_clear_next_step', 'asks_followup_question'},
        'partial_if':     {'open_ended_invitation'},
        'no_if_expected_minus_actual': {'gives_clear_next_step', 'asks_followup_question'},
    },
}
```

Function `map_to_bea_2025(actual_labels, expected_labels) -> Dict[str, Literal['Yes','To some extent','No']]` applies the rules. Don't fabricate labels we don't have — if our rubric doesn't cover a BEA dimension, the value falls through to `"No"` rather than being absent (BEA evaluators expect all 4 keys).

**Important**: actual label names in `apps/benchmark/labels.py` may not exactly match my placeholder names above (`identifies_error` etc.). The mapping table needs to use the real names. Audit `apps/benchmark/labels.py` first; pick the closest 1-3 labels per BEA dimension; document the mapping in the bea_mapping.py docstring.

### D. Hydration helper

```python
# apps/benchmark/hydration.py (new)

def hydrate_step_context(snapshot_item: dict) -> Optional[dict]:
    """Look up LessonStep at export time. Returns None if step deleted."""
    step_id = snapshot_item.get('current_step_id')
    if not step_id:
        return None
    from apps.curriculum.models import LessonStep
    step = LessonStep.objects.filter(id=step_id).first()
    if not step:
        return None
    return {
        'step_id': step.id,
        'step_type': step.step_type,
        'teacher_script': step.teacher_script or '',
        'question': step.question or '',
        'answer_type': step.answer_type,
        'expected_answer': step.expected_answer or '',
        'choices': step.choices,
        'rubric': step.rubric or '',
    }


def normalise_role(role: str) -> str:
    """Internal 'tutor' label → BEA 'teacher'; 'student' stays."""
    return 'teacher' if role == 'tutor' else role


def utterances_from_history(history: list) -> list:
    """[{turn_id, role, text}, ...] → [{text, speaker}, ...] in BEA 2023 shape."""
    return [{'text': h['text'], 'speaker': normalise_role(h['role'])} for h in history]


def conversation_history_string(utterances: list) -> str:
    """[{text, speaker}, ...] → 'Tutor: ...\nStudent: ...' BEA 2025 shape."""
    return '\n'.join(
        f"{'Tutor' if u['speaker'] == 'teacher' else 'Student'}: {u['text']}"
        for u in utterances
    )
```

### E. View

```python
# apps/benchmark/views.py — new view

@super_admin_required
def benchmark_export_jsonl(request):
    """Stream BEA-compatible JSONL of annotated benchmark items."""
    annotations = _filtered_annotations(request)  # share filter logic with CSV view
    response = StreamingHttpResponse(
        _emit_jsonl_lines(annotations),
        content_type='application/x-ndjson',
    )
    filename = _build_export_filename(request, 'jsonl')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _emit_jsonl_lines(annotations) -> Iterable[bytes]:
    for ann in annotations.iterator(chunk_size=100):
        row = _build_jsonl_row(ann)
        yield (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')


def _build_jsonl_row(annotation) -> dict:
    item = annotation.item
    snapshot = item.snapshot or {}
    item_block = snapshot.get('item', {})
    production = snapshot.get('production', {})

    utterances = utterances_from_history(item_block.get('conversation_history', []))
    student_turn = item_block.get('student_turn') or {}
    if student_turn.get('text'):
        utterances.append({'text': student_turn['text'], 'speaker': 'student'})

    conversation_history_str = conversation_history_string(utterances)

    bea = map_to_bea_2025(annotation.actual_labels, annotation.expected_labels)

    return {
        'conversation_id': f"{item.item_id}:{annotation.system_variant}:"
                           f"{annotation.annotator_role}:{annotation.id}",
        'item_id': item.item_id,
        'subject': item.subject,
        'utterances': utterances,
        'conversation_history': conversation_history_str,
        'tutor_responses': {
            annotation.system_variant: {
                'response': production.get('tutor_response', ''),
                'annotation': bea,
                'extra_annotation': {
                    'actual_labels': annotation.actual_labels or [],
                    'expected_labels': annotation.expected_labels or [],
                    'failure_categories': annotation.failure_categories or [],
                    'safety_concern': annotation.safety_concern,
                    'rationale': annotation.rationale or '',
                    'passes': annotation.passes,
                    'annotator_role': annotation.annotator_role,
                    'annotator_user_id': annotation.annotator_user_id,
                    'annotator_model': annotation.annotator_model or '',
                    'system_variant': annotation.system_variant,
                    'stratum': item.stratum,
                },
            },
        },
        'lesson_context': {
            'lesson_id': item.lesson_id,
            'lesson_title': item_block.get('lesson_title', ''),
            'lesson_objective': item_block.get('lesson_objective', ''),
            'current_step': hydrate_step_context(item_block),
        },
        'metadata': {
            'session_id': item_block.get('session_id'),
            'turn_id': item_block.get('turn_id'),
            'created_at': annotation.created_at.isoformat() if annotation.created_at else None,
            'updated_at': annotation.updated_at.isoformat() if annotation.updated_at else None,
        },
    }
```

### F. UI

`templates/benchmark/list.html` — alongside the existing "⬇ download annotations (CSV)" button, add **"⬇ download JSONL (BEA format)"**. Same filter context links through.

## Out of scope (deferred)

- Server-side **filter for "BEA-2023 only"** (one row per item with single response, omitting our `extra_annotation`) — easy add later if external evaluators ask
- **Multi-candidate aggregation**: emitting one row per item with multiple `tutor_responses` keyed by `system_variant`. Currently we emit one row per (item, annotation), which means duplicate context per row. Aggregation is cheap to retrofit; defer until we have multiple variants in production
- **Adding step_script + answer_key to the snapshot itself** (so deleted steps still export fully). Defer; the FK lookup is cheap and most steps survive
- **PII anonymisation for step content**: assumes no student names in `teacher_script`. If teachers add personalised material with student names in the future, add the same `anonymize()` pass that the conversation_history already gets
- **CSV format extension** — could add BEA-mapped 4-dim columns to the existing CSV, but JSONL is the better target for ML eval
- **Streaming validation**: doesn't validate the BEA mapping at export time; if labels don't match the rules, falls through to "No". Manual spot-check on first 10 rows is sufficient

## Phased delivery

| Phase | Work | Hours |
|---|---|---|
| **R1.1 — Hydration helpers + BEA mapping** | (1) `bea_mapping.py` with mapping table calibrated to actual labels in `apps/benchmark/labels.py`; (2) `hydration.py` with step lookup + role normalisation; (3) inline tests in shell | 2 |
| **R1.2 — JSONL view + URL + button** | (1) `benchmark_export_jsonl` view; (2) URL route; (3) refactor shared filter logic with CSV view; (4) UI button on list page | 2 |
| **R1.3 — Spot-check + paper integration** | (1) Download a 10-row sample, manually verify shape against BEA examples; (2) pipe into `research/paper_v2.md` workflow if needed | 1 |

Total: ~5 hours.

## Risks

- **Label mapping mismatch**: my placeholder label names (`identifies_error` etc.) likely don't match `apps/benchmark/labels.py` exactly. Audit step before R1.1; calibrate against real labels. Safe default: any unmapped BEA dim returns `"No"` rather than missing key.
- **Large export memory**: JSONL streams via `iterator(chunk_size=100)` so memory bounded at ~100 rows in flight. ~70 prod annotations is fine; scales to 10K easily.
- **Step deletion**: course regenerations delete LessonStep rows. Hydration returns null `current_step` field — external evaluators must handle missing step gracefully. Document in the schema description.
- **PII**: `anonymize()` is applied to conversation history at sample time but NOT to step content (script/question/answer). If teachers ever embed personalised content, this leaks. Out of scope for v1; flag in CLAUDE.md for future authors.

## Open questions

1. **Conversation_id uniqueness**: include `annotation.id` to guarantee uniqueness (recommended) vs concatenate `(item_id, variant, annotator)` (more semantic but requires unique constraint to be airtight). Recommend annotation.id.
2. **Should we include `pipeline_trace` (judge outputs, regen history) in the JSONL row?** Recommend YES under a `pipeline_trace` key — gives external evaluators visibility into our orchestration, supports the paper's claim about post-hoc judges. Adds ~2KB per row, fine.
3. **Anonymisation of student names in `teacher_script`?** Recommend NO for v1 — scripts are LLM-generated, no real student names. Add if pilot generates personalised lesson content.
4. **Map our labels to BEA's 4-dim, or leave the 4-dim empty and let external evaluators apply their own?** Recommend MAP — even an imperfect mapping is more useful than empty fields. Document the mapping in the row's `extra_annotation.bea_mapping_notes` for transparency.
5. **Emit one row per (item, system_variant, annotator)?** OR one row per item with all variants merged? Recommend per-row for v1 (simpler streaming, no bulk-aggregation logic). Aggregate later if external evaluators ask.

## Next step

Confirm open questions (especially #4 — do we map to BEA 4-dim?). Audit `apps/benchmark/labels.py` to populate the actual label names in the mapping table. Then R1.1.

Refs: memory/eval_benchmark_v2_simplified.md
