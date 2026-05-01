# Figure Facts Plan (2026-05-01)

## Problem

The tutor LLM cannot see figures. Today it only sees text titles in the media catalog. When a student names an angle relationship from a diagram ("are 1 and 5 corresponding angles?"), the tutor reasons from its own (often wrong) mental model of where the labelled points sit.

Observed: tutor confidently told a student angles 1 and 5 were *alternate interior* when the figure's own legend panel labelled them *corresponding*. The bank-pull architecture doesn't catch this because it's an explanation, not an authored question.

## Locked decisions

1. **Option B is the primary path.** Each figure ships with structured ground-truth metadata (`figure_facts`). Tutor consults this as data, never reasons spatially.
2. **Option A (vision) is context-only fallback.** Used to enrich the tutor's prose when `figure_facts` is missing. Never feeds into evaluation, grading, or the L3/L4/L5 validators. Hard rule: vision can shape PROSE, never VERDICTS.
3. **Lesson `is_published` IS the figure approval gate.** No separate `figure_facts_verified` flag, no separate review UI. Reviewing the lesson reviews everything in it.
4. **Backfill via one-time vision extraction.** Existing figures get facts pulled by a vision-capable LLM, stored, then carried forward by the existing publish workflow.

## Schema

### `MediaAsset.figure_facts` (JSONField, nullable)

Richer schema — facts AND pedagogical anchors so the tutor can reference the figure actively, not just consult it for verification.

```json
{
  "type": "parallel_lines_with_transversal",
  "scene_description": "Two horizontal parallel lines, l (top) and m (bottom), are cut by a diagonal transversal t. Eight angles are labelled 1-8 at the two intersection points. A 'Key' panel on the left shows the symbols. A bottom panel summarises the three angle-relationship rules with example pairs.",
  "labelled_features": [
    {"label": "1", "location": "top-left of the upper intersection", "color": "blue"},
    {"label": "2", "location": "top-right of the upper intersection", "color": "yellow"},
    {"label": "5", "location": "top-left of the lower intersection", "color": "blue"},
    {"label": "6", "location": "top-right of the lower intersection", "color": "yellow"},
    {"label": "l", "location": "the upper horizontal line"},
    {"label": "m", "location": "the lower horizontal line"},
    {"label": "t", "location": "the diagonal transversal"}
  ],
  "angle_relationships": [
    {"pair": [1, 5], "relationship": "corresponding", "equal": true},
    {"pair": [3, 6], "relationship": "alternate_interior", "equal": true},
    {"pair": [3, 5], "relationship": "co_interior", "sum": 180}
  ],
  "extra_facts": [
    "lines l and m are parallel",
    "line t is the transversal",
    "the bottom panel of the figure lists three rules with worked examples"
  ],
  "anchor_prompts": [
    "Look at angles 1 and 5 in the figure — what do you notice about their position at each intersection?",
    "Find angle 3 in the figure. Now find angle 6. What is the relationship between them?",
    "The bottom-right panel shows '3 + 5 = 180°'. What does this tell you about co-interior angles?"
  ]
}
```

Field roles:

- `type` — short LLM-friendly tag for the figure category.
- `scene_description` — what's visible, written for the tutor (1-3 sentences). Replaces "imagine two parallel lines" with concrete reality.
- `labelled_features` — every labelled point/line/region with its position and (when present) color. Lets the tutor say *"the blue angle on the top-left"* with confidence.
- `angle_relationships` — structured equalities/sums for verification.
- `extra_facts` — free-form for content not fitting the structured fields (axis labels, map landmarks, panel callouts).
- `anchor_prompts` — pre-authored scaffolding questions tied to specific labelled features. The tutor can pose any of these directly; they're verified at content-time, so they bypass the no-authoring rule.

For non-geometry figures, `angle_relationships` is empty and the meat lives in `scene_description` + `labelled_features` + `extra_facts` + `anchor_prompts`. The schema generalises to charts, maps, photos.

No `figure_facts_verified` field. The lesson's `is_published` is the only gate.

## Backfilling existing figures

### Management command: `python manage.py backfill_figure_facts`

1. Iterate every `MediaAsset` row with `figure_facts IS NULL` (idempotent — re-running skips already-extracted rows).
2. For each, send the image to a vision-capable LLM (Sonnet 4.6 default; configurable via `ModelConfig.get_for('figure_extraction')`).
3. Structured-output prompt (Pydantic schema for the full `figure_facts` shape):
   > *"This is a figure used in tutoring. Extract: (1) `scene_description` — 1-3 sentences describing what's visible to a student looking at this figure. (2) `labelled_features` — every labelled point, line, or region with its position and color when present. (3) `angle_relationships` — for geometry figures, every equality/sum the figure asserts. Copy from any 'key' or legend panel verbatim. (4) `extra_facts` — anything else the figure tells the student (axis labels, panel callouts, captions). (5) `anchor_prompts` — three short questions a tutor could use to direct the student's attention to specific labelled features ('Look at angle X — what colour is it?' / 'Find label Y on the figure'). Return JSON matching the schema only. If the figure is unstructured (a photo, a map without overlays), set `type: 'unstructured'` and put descriptive facts in `extra_facts`."*
4. Store as `figure_facts`. Lesson is unchanged — when the teacher next reviews/republishes, the new metadata flows through the existing approval gate.
5. Log counts: extracted / errored / skipped (no labels detected).

### Cost / time

~200-400 figures across the platform. ~$0.02-0.08 per Sonnet vision call (richer extraction, longer output). **One-time backfill total: $10-30.** Runs in ~15-25 minutes serially; parallelise to <5 min if needed.

### Re-extraction

If a teacher edits a figure's source PNG, the management command can be re-run with `--force` to re-extract that asset's facts.

## Runtime injection

### `_build_figure_facts_block()` in `conversational_tutor.py`

Mirrors `_build_question_bank_block()`. For each figure attached to the current step where `figure_facts` is non-null, render a structured block:

```
<figure_facts source="parallel-lines-diagram.png">
  Scene: Two horizontal parallel lines, l (top) and m (bottom),
    are cut by a diagonal transversal t. Eight angles are labelled
    1-8 at the two intersection points.

  Labelled features:
    - "1" — top-left of the upper intersection (blue)
    - "5" — top-left of the lower intersection (blue)
    - "3" — bottom-left of the upper intersection (pink)
    - "6" — top-right of the lower intersection (yellow)
    - "l" — the upper horizontal line
    - "m" — the lower horizontal line
    - "t" — the diagonal transversal

  Verified relationships:
    - Angles 1 and 5 are CORRESPONDING (equal)
    - Angles 3 and 6 are ALTERNATE INTERIOR (equal)
    - Angles 3 and 5 are CO-INTERIOR (sum to 180°)

  Anchor prompts you may use VERBATIM to direct attention:
    - "Look at angles 1 and 5 in the figure — what do you notice
       about their position at each intersection?"
    - "Find angle 3 in the figure. Now find angle 6. What is the
       relationship between them?"
</figure_facts>
```

Followed by the usage rule (also in the system prompt):

```
RULES FOR USING FIGURES:
1. PROMPT VISUALISATION, NOT IMAGINATION. When a figure is attached,
   say "look at the figure" / "find angle 5 on the diagram" — NEVER
   "imagine two parallel lines" or "picture this". The student is
   looking at the figure; the tutor must too.
2. ANCHOR YOUR SCAFFOLDING. Reference labelled features by their
   actual labels and positions ("the blue angle at the top-left of
   the upper intersection"), not vague gestures ("an angle up here").
3. VERIFY CLAIMS AGAINST <figure_facts>. When the student names a
   relationship ("are 1 and 5 corresponding?"), consult the
   verified_relationships list before answering. Do not interpret
   the geometry yourself.
4. PREFER ANCHOR PROMPTS. When you need to direct attention to a
   feature, the anchor_prompts above are pre-verified scaffolds
   you can use verbatim — no authoring required.
5. HONEST UNCERTAINTY. If the student asks about something not in
   <figure_facts>, say so. Do not guess.
```

Injected after `<media_catalog>`, before `<question_bank>`.

### Auto-attach the figure on every turn of a step that has one

Already mostly true via the media catalog + LLM-driven `|||MEDIA:N|||`. We tighten it: when the current step has a figure with `figure_facts`, the catalog block tells the LLM **the figure is already visible to the student** — so the tutor doesn't need to "ask to show it", just reference it. This eliminates an entire class of "let me show you the diagram" filler turns.

### Math-only

Gated on `Course.is_math` for v1. Non-math lessons keep current behaviour.

## Vision fallback (Option A)

### `BaseLLMClient.supports_vision` capability flag

Add to the abstraction. Anthropic / OpenAI / Gemini clients return True; Ollama (text-only) returns False.

### Where it fires

In `_generate_contextual_response`, when the current step has figures AND any of:
- `figure_facts` is null
- The student asked a question the facts don't address (heuristic: student message contains spatial words like "left", "top", "show me" without a labelled-point reference)

…the tutor's main generation call includes the figure as an image content block. The model sees the figure and can describe it more accurately for prose explanations.

### Hard constraint

The prompt for the vision call MUST include:

> *"This figure is shown to the student for context. You may describe what you see in your prose explanation. You MAY NOT use what you see to evaluate whether the student's answer is correct — that is determined by the deterministic grader, not by you."*

The validator's L3 correctness layer remains unchanged: it grades against `expected_answer` / `correct_answer` from the published bank, not against vision output.

### Provider-aware degradation

When `supports_vision=False` (Ollama text-only):
- `figure_facts` present → tutor uses it
- `figure_facts` missing → tutor responds honestly: *"I can't see the figure clearly — can you describe what you're looking at?"*

## Going forward (new figures)

Content-generation pipeline already produces lesson figures (`apps/curriculum/content_generator.py`). After a figure is generated, immediately call the same vision-extraction pass and store `figure_facts` on the new `MediaAsset`. Teacher reviews both the figure and the facts in the existing publish workflow. No new UI.

## Out of scope (v2+)

- Per-figure `figure_facts_verified` flag (rejected — `is_published` is the gate)
- Separate teacher review UI for figures (rejected — lesson review covers it)
- Vision in evaluation paths (forbidden — context only)
- Auto-correcting `figure_facts` when a teacher edits the figure (manual `--force` re-run)
- Extracting facts from already-published figures (re-publish flow handles it)

## Phased delivery

| Phase | Work | Estimate |
|---|---|---|
| F1 | `MediaAsset.figure_facts` JSONField + migration + Pydantic schema | 45 min |
| F2 | `extract_figure_facts()` helper using vision LLM with structured output (full rich schema, not just relationships) | 3 hrs |
| F3 | `backfill_figure_facts` management command (idempotent, batchable) | 1.5 hrs |
| F4 | `_build_figure_facts_block()` + system-prompt injection + 5-rule usage block | 2 hrs |
| F5 | Tests: extractor against fixture images, prompt-injection assertions, anchor-prompt rendering | 1.5 hrs |
| F6 | Integrate vision-extraction into `content_generator.py` for new figures | 1 hr |
| **F7 (optional)** | Vision fallback (Option A) — `supports_vision` flag + multimodal call for figures without facts | 4 hrs |

**Core (F1-F6): ~1.5 days.** Vision fallback (F7) is optional.

## Test strategy

- Replay the angles-1-and-5 transcript. Assert that with `figure_facts` injected, the prompt contains the literal `"Angles 1 and 5 are CORRESPONDING"` line and the rule `"PROMPT VISUALISATION, NOT IMAGINATION"`.
- Schema validation tests: malformed `figure_facts` (missing required keys, wrong types) are caught by Pydantic before storage.
- Anchor-prompt rendering: when `anchor_prompts` is non-empty, the bank block lists them as verbatim-usable scaffolds.
- Vision-extractor tests using stable fixture images (a known parallel-lines diagram from the existing media library) — assert the extractor returns expected `scene_description`, `labelled_features`, and `angle_relationships`.
- Negative test: an unstructured figure (a photo, a map) returns `type: "unstructured"` with sensible `extra_facts` rather than crashing or fabricating relationships.
- Anti-imagination test: assert that when `figure_facts` is present, the system prompt contains the explicit ban on "imagine X" / "picture this" phrasing.

## Next step

Confirm the plan and I start with F1 (schema + migration) + F2 (extractor helper). Both are pure additions; no behaviour change until F4 wires injection into the prompt.
