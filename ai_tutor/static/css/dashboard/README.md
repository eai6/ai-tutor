# Teacher dashboard design system

Everything the dashboard renders comes from these stylesheets plus one
page-level sheet per page that needs one. Load order matters and is fixed in
`templates/dashboard/base.html`:

```
../shared/tokens.css       the vocabulary — colour, type, space, radius, motion
../shared/base.css         document defaults, focus policy, a11y, skeletons
../shared/password-field.css  reveal toggle + requirement checklist
layout.css                 the app shell — rail, topbar, canvas, drawer
components/surfaces.css    card, section head, tile, attention row, empty, footnote
components/controls.css    button, segmented, badge, form field, alert
components/data.css        table, progress, pagination, layout utilities
charts.css                 the div-based charts
legacy.css                 compatibility shim for un-migrated pages (temporary)
pages/*.css                only what exists because of one page's shapes
```

## The one rule

**No literal colour outside `tokens.css`.** Not in a stylesheet, not in a
template, not in JavaScript. If you need a colour that isn't there, add a
semantic token and say what role it plays. `grep '#[0-9A-Fa-f]\{6\}'` over
this directory should only ever match `tokens.css`.

Two deliberate exceptions, both documented at the line:

* `components/controls.css` — the `.select` chevron, because an SVG data URI cannot
  read a CSS custom property.
* `base.html` — the per-institution theme block, which is DB-driven.

## Colour

The palette is one warm-neutral ramp plus five status roles.

**Warm, not cool.** The old sheet used Tailwind's blue-grey ramp under an
orange brand colour. Orange on a cool grey vibrates; on a warm grey (hue ~30,
very low chroma) it reads as ink on paper. That is the whole reason for the
`--warm-*` ramp.

**The brand orange is a signal, not wallpaper.** `--primary` marks the things
a teacher acts on — the active nav item, the focus ring, the session bars.
It does not decorate.

**Three orange tokens, three jobs.** Getting this wrong is the single most
common mistake in this codebase:

| token | value | use | contrast on white |
|---|---|---|---|
| `--primary` | `#E8590C` | bars, focus ring, active edge, tint source | 3.6:1 — non-text only |
| `--primary-fill` | `#C4460A` | filled control carrying white text | 5.0:1 with white |
| `--primary-ink` | `#A83B00` | orange **text** and standalone icons | 6.4:1 |

`--primary` cannot carry text, in either direction. The old sheet used it for
`.nav-link.active` colour, for the "mean 74%" figure, and as the primary
button fill with white on top; all three failed AA.

**Status colours are deliberately desaturated.** A teacher scans this page
dozens of times a day, and saturated red/green at that frequency is
fatiguing. Each role ships as a trio — ink (AA on white), surface, border:

```
--success / --success-surface / --success-border / --success-solid
--warning / --danger / --info / --accent   (same shape)
```

Components reference the **role**, never the hue, so "needs attention" can be
re-hued in one place.

**Colour is never the only signal.** The active nav item has a left edge as
well as a tint. Attention rows carry severity in the edge, the icon and the
wording. The distribution chart splits at the pass mark and captions both
halves in a legend.

### Contrast

Every foreground/background pair the dashboard actually renders is verified
against WCAG 2.2 AA — 4.5:1 for text, 3:1 for UI components and graphical
objects. Two tokens are **not text colours** and are commented as such:

* `--text-faint` (2.6:1) — decorative glyphs only: the chevron beside a link
  that already has a label, an empty-state illustration, a marker fill.
* `--warm-300` (1.6:1) — hairline dividers. Form controls take
  `--border-field` (3.3:1), because a control's boundary is a UI component.

## Typography

Three faces, each doing exactly one job.

| role | face | used for |
|---|---|---|
| display | Space Grotesk | headings, and every figure read as a number |
| body | Nunito | everything a person reads as prose |
| mono | Space Mono | eyebrow labels and timestamps, as texture |

Space Grotesk against Nunito is a deliberate pairing, not a default: angular
technical display over a soft humanist body. Nunito is also the
student-facing app's face, so the two halves of the product feel related.
Space Mono appears only in `.eyebrow`, `.tile__label`, `.nav-group__title`
and table headers — it is the page's signature texture and stops labels
competing with the figures beneath them.

Both are self-hosted in `static/vendor/` (SIL OFL). Production has no
external font source under CSP and pilot schools are frequently offline.

Numerals use `font-variant-numeric: tabular-nums` everywhere, so a column of
figures can be compared without reading each one.

## Spacing

A dense 4px scale (`--space-1` … `--space-12`). The old sheet used `1.5rem`
for nearly every gap, which is why the page felt airy but carried little
information. Vertical rhythm between page sections comes from `.stack`, not
from a `margin-bottom` on each card.

## Components

Each is a compound: a root class plus named parts scoped by their own class,
never by descendant selectors.

```html
<div class="card">
  <header class="card__header">
    <h2 class="card__title">…</h2>
    <p class="card__meta">…</p>
  </header>
  <div class="card__body">…</div>
</div>
```

A card nested in a card does not inherit the outer card's padding, and no
selector has to out-specify another.

Repeated leaf components are template tags rather than copied markup — see
`apps/dashboard/templatetags/dashboard_ui.py`:

```django
{% load dashboard_ui %}
{% icon "flag" %}                                    {# decorative #}
{% icon "flag" label=_("Flagged") size="lg" %}       {# meaningful #}
{% badge student.grade tone="info" %}
{% stat_tile label=_("Sessions") value=n note=note hint=hint %}
{% progress pct tone="success" count=done total=all label=_("Mastered") %}
{% empty_state title=… body=… icon_name="students" %}
{% attention_item item %}
```

`tone` is validated against an allow-list in Python; a typo falls back to the
neutral skin rather than emitting a class that matches nothing.

## Icons

One inlined sprite, `templates/dashboard/_components/icon_sprite.html`,
included once at the top of `<body>`. External `<use href="sprite.svg#id">`
does not work in Safari and the pilot runs iPads.

Symbols are drawn on a 24×24 grid with **no** `fill`, `stroke`, or
`stroke-width` — `.icon` supplies all of it from `currentColor`, so one
symbol serves every colour and size. Draw new ones the same way.

## Progressive disclosure

`title=""` is not a disclosure: keyboard users never see it, touch users
cannot reach it, and it vanishes on a timer. The "how is this counted" notes
use `<details class="footnote">` instead — focusable, announced, and it stays
open while the teacher reads it. Collapsible nav groups are `<details>` too,
which gives keyboard operation and correct semantics with no JavaScript.

## Motion

Two durations (`--dur-fast` 120ms, `--dur-base` 200ms) and two curves. Every
animation is inside a `prefers-reduced-motion` guard in `base.css`. Skeletons
keep a static tint when motion is reduced, so they still read as "not loaded".

## Adding a page

1. Extend `dashboard/base.html`; fill `page_title`, and `topbar_eyebrow` /
   `topbar_actions` if the page has them.
2. Wrap sections in `.stack` for vertical rhythm.
3. Reach for existing components first. Only add a `pages/*.css` when a shape
   genuinely belongs to that page alone — and put it in the page's
   `extra_css` block, not in `components/`.
4. Never open a `style=""` attribute except to pass **data** to CSS: a width
   percentage, or a custom property like the gain scale's `--pre` / `--post`.
5. Run the contrast and a11y checks before shipping.

## Where things live

```
css/shared/     tokens, base, password-field   — all three surfaces
css/dashboard/  layout, components/, charts, legacy
css/student/    brand, shell, components, auth, catalog, legacy
css/marketing/  landing, legal
templates/_includes/icon_sprite.html + icon.html + feedback_button.html
```

`_includes/feedback_button.html` renders on every page in the product. It used
to carry its own violet — the one colour a teacher and a student both saw that
belonged to neither palette — and now uses the semantic roles, so it takes
whichever skin the page loaded.

## Legacy pages

~20 dashboard templates still use the class names the old `base.html` `<style>`
block defined — `.card-header`, `.form-control`, `.btn-primary`, `.stat-value`
and friends. Two things keep them working:

* `tokens.css` aliases the old `--gray-*` variables into the new warm ramp.
* `legacy.css` re-implements the old **class names** on the new tokens.

Both are temporary. The colour aliases alone were not enough: deleting the old
`<style>` block took the component rules with it, and Settings and Student
Groups shipped with unstyled form fields and a card title sitting on the
card's top border until `legacy.css` was added. Screenshot an un-migrated page
before assuming a shell change is safe.

**New code must not use these names.** Migrate a page when you have another
reason to open it; delete `legacy.css` and its `<link>` when the last one is
done.

### Migrated so far

* Dashboard: `base`, `home`, `students/list`, `classes/list`,
  `flagged_sessions`, `settings`.
* Student: the `base.html` shell, `catalog`, and all twelve auth pages —
  those had each duplicated the same `.auth-*` block in their own `<style>`,
  about 780 lines of copy that had drifted apart page by page. `student/auth.css`
  is now the single definition.
* Public: `landing`, `terms`.

The rest keep their own `<style>` blocks but no longer carry hardcoded
colours: a pass over `templates/dashboard/` replaced every literal hex with
the semantic role it was standing in for, so those pages track the palette
even before they are properly migrated. Check with:

```sh
grep -rhoE '#[0-9a-fA-F]{3,6}\b' ai_tutor/templates/dashboard/ | wc -l
```
