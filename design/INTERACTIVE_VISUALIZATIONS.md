# Interactive Visualizations — Design Notes

## Context

Today, lesson visuals are static images: pre-authored during curriculum
generation, stored in `LessonStep.media` (JSONField), and selected at chat time
by the LLM via a `|||MEDIA:N|||` signal referencing a numbered catalog. See
`apps/curriculum/models.py:274-304`, `apps/tutoring/conversational_tutor.py:2660-2775`,
and `templates/tutoring/chat_tutor.html:1416-1481`.

This note describes what it would take to extend the pipeline so that
*data-driven* concepts get **interactive widgets** — e.g., an HDI explorer
with sliders for income, life expectancy, and schooling that computes the
composite score and shows the developed/developing band in real time.

Use case that motivated this doc: the HDI lesson (`/tutor/lesson/1051/`) asks
*"why would a country with high oil sales but poor healthcare not be considered
truly developed?"*. A slider-based widget lets the student move inputs and
observe the band change, and the tutor can pose precise prompts like
*"set income to $8k and life expectancy to 45 — which band?"*.

Scope is intentionally subject-agnostic: math and geography today, possibly
more (physics, economics, civics) later.

---

## 1. A declarative widget spec (not LLM-generated code)

The single biggest design decision: **widgets are declarative JSON specs that
map to pre-built frontend components**, not LLM-generated HTML/JS. Letting
the LLM emit arbitrary code is a security and correctness risk — injection,
broken formulas, layout chaos, no review surface.

Define a small, growing library of widget *types*, each with a typed schema:

- `composite_index_explorer` — HDI, Gini, literacy index, BMI; any weighted
  multi-input formula
- `function_plotter` — math: `y = f(x, params)` with parameter sliders
- `map_overlay` — geography: base map + data-driven choropleth tied to a
  year/indicator slider
- `simulation_2d` — later, for physics/chemistry
- `data_comparator` — bar/line chart with toggleable series

Each type has fixed "slots": inputs, formula/data, display bands, reference
points (e.g., *Norway = 0.966*). The LLM or human author only fills slots.

## 2. Authoring pipeline changes

Extend the curriculum authoring path in `apps/curriculum/content_generator.py:27-41`:

- Add a `MediaWidget` Pydantic schema alongside `MediaImage`: `widget_type`,
  `params` (typed per type), `caption`, `alt_text`, optional `reference_points`.
- Update `StepMedia` to allow `widgets: [...]` in addition to `images: [...]`.
- Update the LLM generation prompt (`content_generator.py:505-526`) so that,
  when a step's concept is data-driven or parametric, it emits a widget spec
  instead of (or alongside) an image description.
- Store in the same `LessonStep.media` JSONField — no schema migration beyond
  JSON-internal evolution.

**Key judgment call:** let the LLM *propose* widget specs, but require
human/teacher review before publish. This matches the existing
teacher-reviewed design stated at `conversational_tutor.py:1694-1697`.
Auto-publishing AI-authored widgets with incorrect formulas would cause real
learning harm.

## 3. Runtime catalog + selection (reuse the signal mechanism)

The current `|||MEDIA:N|||` catalog mechanism already solves the selection
problem. Extend it:

- In `_build_media_catalog()` (`conversational_tutor.py:2660-2775`), enumerate
  widgets alongside images: `[3] HDI Explorer (widget): 3 sliders → composite
  score with dev-status bands`.
- Add a parallel signal `|||WIDGET:N|||`, or keep unified `|||MEDIA:N|||` and
  let the `type` field drive rendering.
- The LLM decides pedagogical fit using the same "only show if listed in
  catalog" instruction at `conversational_tutor.py:358-362`. No new selection
  logic needed.

**"Where appropriate" gating is the LLM's job, constrained by what's in the
catalog.** Appropriateness is controlled at authoring time — if a step has no
widget attached, none will appear.

## 4. Frontend rendering

The existing artifact side panel in `templates/tutoring/chat_tutor.html:1416-1481`
is the right home. Today it hosts a zoomable image; for widgets it hosts an
interactive component.

Requirements:

- A widget registry on the frontend that maps `widget_type` → component.
- Each component is a small JS module (React/Preact/Lit — whichever is adopted)
  that takes the spec's `params` as props.
- A whitelisted formula evaluator (e.g., `mathjs` with operator/function
  whitelisting) so the spec can carry formulas declaratively without `eval`.
- Mobile behavior: widgets collapse gracefully — e.g., stacked sliders under
  the chat, not blocking input.
- Accessibility: keyboard control on sliders, live ARIA announcements of
  computed values.

## 5. Tying questions to widget state

This is the pedagogically interesting part. *"If oil sales are X and life
expectancy is Y, move the sliders to see status"* requires the tutor to
**read back the widget state**.

Three levels of coupling, in increasing order of complexity:

- **Level A — explore-only (start here):** Widget is a free exploration tool;
  the LLM continues to grade free-text answers as today. Cheap, low risk,
  already ~80% of the value.
- **Level B — state-aware questions:** On turn submit, the frontend forwards
  current widget state in the chat POST body. The LLM gets
  `[widget state: income=$8k, life_exp=45, current_HDI=0.412]` in context and
  can grade accordingly. Store widget-state snapshots on `TutorMessage` or
  `Session` for replay.
- **Level C — programmatic check questions:** The widget spec carries a
  `checkpoint` (e.g., *student must land in the "low development" band*). The
  client detects when conditions are met and flags the LLM, similar to quiz
  scoring today. Highest fidelity, tackle last.

## 6. Subject extensibility

Math/geography today, more possibly later. The widget-type library should
stay **small and subject-agnostic at its core**; add subject-specific
templates as thin configurations:

- `composite_index_explorer` serves HDI, GDP breakdowns, BMI with no code
  change — just different slot values.
- `function_plotter` covers most high-school math (linear, quadratic, trig,
  exponential).
- `map_overlay` covers most thematic geography.

For physics/chemistry later, add `simulation_2d`. Resist the urge to ship
one-off widgets per lesson; each new type is a long-term maintenance
commitment.

## 7. Authoring quality controls

Widgets are higher-stakes than a decorative image — a wrong formula teaches
the wrong thing. Required:

- **Unit tests per widget type** verifying the formula matches the
  authoritative source (UN HDI definition, etc.).
- **Spec validator** during curriculum generation rejecting malformed or
  out-of-range params.
- **Teacher preview** in the curriculum authoring UI so someone can interact
  with the widget before publishing.
- **Telemetry**: log widget interactions (anonymized) to see which widgets
  students actually engage with vs. ignore — kill the duds.

## 8. Rollout path (incremental, lowest-risk order)

1. Ship **one widget type** (`composite_index_explorer`) hard-wired to the
   HDI lesson — prove end-to-end with a hand-authored spec.
2. Add widget rendering to the artifact panel and the catalog/signal
   plumbing.
3. Open up to **teacher-authored** widgets across a few more lessons.
4. Enable **LLM-proposed** widget specs during content generation, still
   behind teacher review.
5. Add Level B state-aware questions.
6. Add the next widget type (`function_plotter` for math) only once #1–#5
   are stable.

## Summary of where changes land

| Area | File(s) | Change |
|---|---|---|
| Schema | `apps/curriculum/content_generator.py`, `apps/curriculum/models.py` | Extend `StepMedia` to include `widgets[]`; add `MediaWidget` Pydantic model |
| Authoring prompt | `apps/curriculum/content_generator.py:505-526` | Teach the LLM when to propose widgets vs. images |
| Catalog/selection | `apps/tutoring/conversational_tutor.py:2660-2775`, `:358-362` | Include widgets in the numbered catalog |
| Signal parsing | `apps/tutoring/conversational_tutor.py:4316-4344` | Handle `\|\|\|WIDGET:N\|\|\|` (or reuse MEDIA + `type`) |
| Rendering | `templates/tutoring/chat_tutor.html:1416-1481` | Widget registry + components in the artifact panel |
| Chat payload | `apps/tutoring/views.py` (POST handler) | Optionally forward widget state with student messages |
| Teacher review | `apps/dashboard/` curriculum views | Preview + approve widget specs before publish |

## Risks and open questions

- **Authoring quality at scale** — every widget must be pedagogically and
  mathematically correct. The LLM-proposed path needs robust review.
- **Restraint** — not every lesson benefits from a widget. The catalog
  should stay sparse; otherwise they become noise.
- **Security** — declarative specs plus a whitelisted formula evaluator
  avoid arbitrary code execution, but any expansion (e.g., custom JS hooks)
  reopens the attack surface.
- **Responsive design** — widgets on small screens need careful layout;
  artifact panel is desktop-first today.
- **Offline/low-bandwidth** — Seychelles/Africa deployment constraints mean
  widgets must be lightweight and usable on 3G.

The two hardest parts aren't technical. They're authoring quality at scale
and restraint in deciding where widgets belong. The technical plumbing
largely parallels what's already built for images.
