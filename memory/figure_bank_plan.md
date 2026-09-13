# Figures in tutoring — audit, and a bank the server draws from (2026-09-13)

**Status: PROPOSAL. Nothing in the engine or the prompts has been changed.**
Written for review at Edward's request. Numbers are measured on the local DB
(3,387 lesson steps, 11,067 tutor turns) unless marked otherwise.

---

# Part 1 — Audit

## A1. The tutor almost never shows a figure

`request_figure` fired **25 times in 11,067 tutor turns — 0.23%**. For scale,
`pose_question` fired 6,642 times and `record_answer` 6,477.

| tool | calls |
|---|---|
| pose_question | 6,642 |
| record_answer | 6,477 |
| auto_pose_fallback | 414 |
| **request_figure** | **25** |
| auto_pivot | 19 |
| auto_grade_fallback | 14 |

Meanwhile **63% of steps carry a figure** (2,144 of 3,387) and 716 of those have
a real image file on disk. Figures are authored by the content generator, drawn
by gpt-image-2 at real cost, judged for alignment — and then shown to a student
roughly never.

This is the headline finding. Everything below explains it.

## A2. Nothing in the prompt says WHEN to show one

The entire figure instruction in the system prompt is a guardrail:

```
- Reference pre-generated figures only via request_figure(figure_id) using ids
  from <figure_catalog>. Do not invent figure ids or describe figures that
  aren't in the catalog.
```

(`prompts.py:585-590`, rendered into Block 0.) It constrains *how* to reference
a figure and says nothing about *when* — no pedagogical trigger, no tie to the
current question, no tie to the objective. The tool description adds only
mechanics ("the platform inserts the image inline beside your text").

A model given a capability, a prohibition, and no reason to use it will not use
it. 0.23% is the predictable result, not a tuning failure.

## A3. The "catalog" it selects from has never had more than one entry

2,144 image entries across 2,144 steps-with-images: **exactly one figure per
step, always**. `request_figure(figure_id)` exists to choose among alternatives,
and there have never been alternatives. The id is decoration on a decision with
one option.

## A4. Two thirds of authored figures do not exist

Only **716 of 2,144 (33%)** figure entries carry a `url`. The other 1,428 are
figure *intents* — a `description`, `alt_text` and `caption` the generator wrote
— where image generation failed or never ran. `_build_figure_catalog` correctly
skips them, so those steps present an empty catalog and the tutor could not show
anything even if it wanted to.

Distribution by step phase (with-url / total):

| phase | with url | total |
|---|---|---|
| explain | 245 | 765 |
| practice | 165 | 529 |
| explore | 132 | 394 |
| engage | 163 | 348 |
| evaluate | 9 | 105 |

Note `evaluate`: 105 figures authored for assessment steps, **9 rendered**.
Those are the steps where a missing figure is not cosmetic — if a question says
"using the diagram", an absent diagram makes it unanswerable.

## A5. A figure is not linked to anything it is supposed to teach

A figure entry's keys, across all 2,144: `type`, `description`, `alt_text`,
`caption`, and on the 716 generated ones `url`, `source` (+ `model`/`provider`
on 20). There is **no objective, no concept tag, no question link**.

Compare `ExitTicketQuestion`, which is the bank this proposal is modelled on:

| | question bank | figures today |
|---|---|---|
| own DB row | ✅ `ExitTicketQuestion` | ❌ a dict in `LessonStep.media` JSON |
| stable id | ✅ pk | ❌ list position, recomputed per turn |
| objective link | ✅ `concept_tag` | ❌ none |
| difficulty | ✅ `difficulty` | ❌ none |
| quality gate | ✅ `content_quality_status` + `reviewed_by` | ❌ none |
| judge verdicts | ✅ `judge_outputs` | ⚠️ on `MediaAsset`, see A6 |
| server selects it | ✅ since `catalog_only_questions_plan.md` | ❌ LLM-selected |

So the platform cannot answer "which figure supports objective EO3?" — the
question the whole proposal turns on.

## A6. The rich figure metadata is dead at runtime

`MediaAsset.figure_facts` exists, with a documented purpose:

> Used by the tutor at runtime to anchor scaffolding in real labelled features
> instead of asking the student to imagine.

**`simple_tutor` never reads `figure_facts`.** Zero references in the whole
package. It is the same class of loss as the `|||MEDIA:N|||` handler that died
with the legacy engine: built, tested, then orphaned by an engine swap.

Worse, `MediaAsset` has **no foreign key to Lesson or LessonStep**. The only
join is file-URL string matching (`image_service.py:513`, `file__endswith`). So
there are two parallel records of the same figure — the JSON the tutor reads,
and the row that holds the facts and the judge verdicts — related by a string.
69 MediaAsset rows exist for 716 generated URLs; 22 have figure_facts.

## A7. Two judges exist to detect failures a deterministic design cannot have

- `judges/figure_ref.py` — flags the tutor saying "looking at the diagram" when
  no figure was attached. Its docstring: *"Production transcripts showed the
  tutor saying 'Looking at the diagram, you can see…' with no diagram attached —
  frequent and confusing for students."*
- `judges/figure_vision.py` — a **vision** call (5–10× the cost of a text call)
  verifying the attached figure matches the question. Its docstring names the
  failure: *"the student sees a 2-angle diagram while being asked about 3
  angles."*

Both are post-hoc detectors for a coupling the engine does not enforce. If the
figure is bound to the question by construction, the first cannot happen and the
second has nothing to check.

## A8. The type vocabulary the generator writes is not the one the page renders

Authored `type` values: `diagram` (1,769), `map` (204), `illustration` (103),
`chart` (48), `photograph` (12), `schematic map` (7), `infographic` (1).

The chat page renders only `image | diagram | chart | illustration`
(`chat_tutor.html`). `map`, `photograph`, `schematic map` and `infographic` —
**224 figures** — would be dropped silently. Today's payload fix sidesteps this
by emitting `type: 'image'` unconditionally, which is safe but discards the
authored type. The two vocabularies were never reconciled.

## A9. It did not render at all until today

`respond_for_view` emitted `{'url': …}` with no `type`, and the page's filter
requires one — so every figure the tutor did request produced an empty
`<div class="message-media">`. Fixed in `15a4a15` this morning, along with the
lost `alt`/`caption` and the missing `attached_media` persistence. Which means
**the 25 calls above produced approximately zero visible figures**, and no
production evidence exists yet about figures actually helping.

## A10. A per-course kill switch, half on

`Course.tutoring_images_enabled`, teacher-editable. 4 of 8 local courses have it
off. It is a legitimate control but it is currently the only lever, and it is
all-or-nothing per course.

## What the audit concludes

The figure path is not underperforming; it is **structurally unable to perform**.
The decision to show a figure sits with the model, the model is given no reason
to make it, the thing it selects from has one element, two thirds of the
elements are missing, and nothing connects a figure to the objective or the
question it was drawn for.

---

# Part 2 — Proposal: a figure bank the server draws from

## The principle, and its precedent

`auto-memory/feedback_server_owns_question_state.md` lays out the rule this
platform already follows:

> The LLM's tool calls are *useful hints*, not load-bearing actions. The server
> owns authoritative state.

That memory's own table assigns **"Which figure to display → Tutor LLM via
`request_figure`"**. That was a deliberate v1 call. The measurements above are
the evidence for revisiting it.

`memory/catalog_only_questions_plan.md` already did exactly this move for
questions: the model stopped authoring stems and started *selecting an index*,
and the server renders from the bank. Its argument transfers verbatim:

> This is a structural fix, not a validation one: there is no path by which a
> mangled stem can reach a student.

The figure equivalent: **there is no path by which a figure appears without the
item it belongs to, or fails to appear when that item needs it.**

## The learning science, stated so it can be argued with

Three of Mayer's multimedia principles bear directly, and each maps to a
mechanism rather than a prompt instruction:

- **Spatial/temporal contiguity** — a figure helps when it is present *at the
  moment* the student reasons about it, not summoned later. → the figure is
  attached to the question, and rendered with it.
- **Signalling** — the student must know what to look at. → `figure_facts`'
  `labelled_features` and `anchor_prompts` become prompt context so the tutor
  can say "find the 40° angle at the centre", not "look at the diagram".
- **Coherence** — extraneous images cost attention. → a figure appears when it
  is *for* the current objective, and not otherwise. Decorative figures are a
  content problem the bank makes visible.

None of these can be enforced by asking a 4B nicely. They are properties of when
the platform chooses to render.

## D1. `LessonFigure` — the bank

A first-class row, mirroring `ExitTicketQuestion`'s shape:

```
LessonFigure
  lesson            FK Lesson          (required)
  step              FK LessonStep      (nullable — a figure may serve a lesson)
  media_asset       FK MediaAsset      (nullable — replaces URL string-matching)
  url               CharField          (resolved; kept for the no-asset case)
  enabling_objective CharField         (the concept_tag convention, see below)
  role              CharField          anchor | worked_example | stimulus
  alt_text, caption, description
  figure_facts      JSONField          (mirrored from MediaAsset for one read)
  content_quality_status  pending | approved | rejected
  judge_outputs     JSONField          (figure_alignment, …)
  order_index       int
```

`enabling_objective` follows `feedback_step_objective_linkage`'s existing
string-field convention — the same one that links `ExitTicketQuestion.concept_tag`
to a step's objective. **No new linkage mechanism**; the figure joins the same
join.

`role` is the part that makes selection deterministic:

- **`stimulus`** — the question cannot be answered without it. Must render with
  the question. This is where `evaluate`-phase figures belong, and where a
  missing file is a blocking content error rather than a cosmetic one.
- **`anchor`** — supports the explanation of an objective. Renders once when the
  objective is first taught.
- **`worked_example`** — accompanies a worked solution.

## D2. Selection moves to the server

The rule, stated completely:

1. When the server writes an `InFlightQuestion`, it looks up the
   `LessonFigure` bound to that question (or to its objective, `role=stimulus`).
   If one exists and is approved and has a file, **it renders with the question**.
   No tool call, no model decision.
2. On a teaching turn, if the current step's objective has an approved
   `role=anchor` figure that has not been shown in this session, the server
   attaches it once.
3. `session.engine_state` records shown figure ids, so "once" means once.

This is `_auto_pose_fallback`'s pattern — the server path that already produced
51% of questions with none of the corruption.

## D3. What the prompt becomes

This is the part to scrutinise, because it inverts the current instruction.

Today the prompt offers a capability the model must decide to invoke. It would
instead **state a fact about the screen**, which is a far easier thing for a 4B
to use correctly:

```xml
<figure_on_screen>
  The student is looking at: a clock face at 3:00 with four angles marked
  around the centre point.
  Labelled: angle 1 (top right), angle 2 (bottom right), angle 3, angle 4.
  Refer to these labels by name when you point at something.
</figure_on_screen>
```

Rendered only when a figure is actually attached this turn. When none is, the
block is absent and — per `prompting-fundamentals` on positive framing over
prohibition — there is **no "do not mention figures" instruction to follow**,
because there is nothing on screen to mention and no tool to tempt the model.

Consequences:
- `request_figure` leaves the tool schema. Tool surface drops from 3 to 2,
  continuing `tool_surface_reduction_plan.md`'s direction.
- The `FIGURE_RULE` guardrail in Block 0 is deleted; it guards a tool that no
  longer exists.
- `figure_facts` becomes load-bearing — the thing it was built for (A6).
- `judges/figure_ref` keeps its job (the tutor can still reference a figure that
  is not there) but should now fire near-zero; it becomes a regression alarm.
- `judges/figure_vision` can be **retired**: it checks a binding the engine now
  makes by construction. That removes a 5–10× vision call from the judge path.

## D4. Authoring and backfill

The bank is only as good as its coverage, and coverage today is 33%.

1. **Migration** — one `LessonFigure` per existing `media['images']` entry,
   `content_quality_status='pending'`, `enabling_objective` left blank.
2. **Backfill the objective** — the step's own objective is the honest default
   for an `anchor`; a `stimulus` needs the question link and cannot be guessed.
   This is the real work, and it is a content task, not an engine one.
3. **`LessonStep.media` stays** as the authoring surface and the source of
   truth for generation; `LessonFigure` is the derived, queryable, reviewable
   projection. Two writers to one JSON blob is what produced A6.
4. **The 1,428 figure-less intents** become visible as
   `LessonFigure(url='', status='pending')` — a queue, where today they are
   invisible.

## D5. What this does NOT do

- It does not add a new model call. Selection is a DB lookup.
- It does not let the tutor pick between figures. If a step ever has two, the
  `role` + objective decides; if that is ambiguous, it is a content bug.
- It does not change image generation.
- It does not touch `tutoring_images_enabled`.

## Open questions for review

1. **Is `role` the right taxonomy?** Three values is a guess from the phase
   distribution in A4. A fourth (`summary`) may be warranted.
2. **Should an unrendered `stimulus` block publication?** A question that says
   "using the diagram" with no diagram is broken content. Making it a publish
   gate is strong medicine and would block real lessons today.
3. **Anchor figures: once per session, or once per objective?** A student who
   returns after a week may want it again.
4. **Do we want a figure at all on `practice` steps?** 529 authored, 165
   rendered. Coherence says a practice item either needs its stimulus or should
   have no picture.
5. **Retire `figure_vision` immediately, or run it shadow for one pilot cycle**
   to confirm the binding holds?

## Verification, before any of this ships

Per `prompting-fundamentals` — evals before prompts:

1. A held-out set of lessons where a figure is genuinely load-bearing
   (`evaluate`-phase stimulus figures with files: 9 today — this set has to be
   grown first, and that is the gating task).
2. Measure figure-shown rate before/after. Current baseline: 0.23% of turns,
   ~0 visible (A9).
3. `figure_ref` judge issue rate must go to ~0, not merely down.
4. The local 4B arm must be measured separately — the whole point is that it no
   longer has to decide.

---

## Files this would touch

| file | change |
|---|---|
| `apps/curriculum/models.py` | new `LessonFigure` |
| `apps/media_library/models.py` | FK from MediaAsset, or leave and point the other way |
| `simple_tutor/engine.py` | `_build_figure_catalog` → bank lookup; attach at pose time |
| `simple_tutor/prompts.py` | `<figure_catalog>` → `<figure_on_screen>`; drop FIGURE_RULE + tool |
| `simple_tutor/tools.py` | remove `handle_request_figure` |
| `apps/tutoring/judges/figure_vision.py` | retire |
| dashboard | a review queue for `content_quality_status` |

Refs: memory/catalog_only_questions_plan.md,
auto-memory/feedback_server_owns_question_state.md,
auto-memory/feedback_step_question_linkage.md,
auto-memory/project_offline_first_priority.md
