# Converting the platform's CSS to Tailwind v4

**Status:** approved design, not yet planned
**Date:** 2026-09-06
**Branch at time of writing:** `ui-ux-improvements` (`c05e95b`)

## Goal

Replace every hand-written stylesheet and every inline style in the platform
with Tailwind v4 utilities, leaving one generated stylesheet in place of
twenty-three hand-maintained ones.

The rewrite is invisible to users. Pixel-faithful is the acceptance bar: same
layout, same spacing, same colour on every page. Redesign is a separate
project, made cheaper by this one.

## What is being replaced

Measured on `c05e95b`:

| | Amount |
|---|---|
| Stylesheets in `ai_tutor/static/css/` | 23 files, ~6,400 lines (excluding `vendor/`) |
| Inline `<style>` blocks | ~5,450 lines across 51 of 178 templates |
| Static `style=""` attributes | 2,034 |
| Dynamic `style=""` attributes | 49 |
| Templates composing class names at runtime | 20 |
| Class names constructed in JavaScript | ~30, across 5 files |

## What survives

"No custom CSS" has four documented exceptions. Nothing else is exempt.

| Survives | Why |
|---|---|
| `static/css/app.css` | the `@import "tailwindcss"` + `@theme` entry point |
| `static/vendor/css/{google-fonts,space-grotesk,katex.min}.css` | vendored third-party; served locally because the Jetson build runs offline with no external network |
| The `<style>` block at `templates/base.html:57-66` | per-institution brand colour, read from the database at request time. A runtime value cannot be a build-time utility. |
| 49 inline `style="width: …%"` attributes | progress-bar widths interpolated by Django (`{% widthratio %}`, `{{ row.pass_pct }}`). Tailwind has no runtime arbitrary values. |
| `templates/email/verify_email.html` | an HTML email. Mail clients strip `<link>` and most strip `<style>`, so its 12 inline `style=` attributes and its table layout are the only thing that renders. Excluded from the migration entirely. |

## Design

### 1. The theme

Tailwind v4 is configured in CSS. There is no `tailwind.config.js`. The whole
configuration is one `@theme` block in `static/css/app.css`:

```css
@import "tailwindcss";

@source "../../templates/**/*.html";
@source "../js/**/*.js";
@source "../../apps/**/templatetags/*.py";

@theme {
  /* warm neutral ramp, status trios, brand orange — verbatim from tokens.css */
  --color-warm-25: #FCFBF9;
  --color-primary: #E8590C;
  --color-primary-fill: #C4460A;
  --color-primary-ink: #A83B00;
  --color-green-ink: #0F7B5F;
  /* … */

  /* the breakpoints the stylesheets actually use, not Tailwind's defaults */
  --breakpoint-sm: 30rem;
  --breakpoint-md: 56rem;
  --breakpoint-lg: 60rem;
  --breakpoint-xl: 62rem;

  /* the four @keyframes: fade, slide, slide-down, skeleton-sheen */
  --animate-skeleton: skeleton-sheen 1.4s infinite;
}
```

**Why v4 specifically.** v4 utilities compile to `var(--color-*)`; v3 compiled
to literal hex. `templates/base.html:57-66` overrides `--coral`, `--coral-fill`,
`--coral-ink` and `--coral-tint` at `:root` from the database so each school
keeps its brand colour. Under v4 that override continues to work through the
generated utilities with no change. Under v3 it would compile away and school
theming would silently stop working.

**Browser floor.** v4 requires Safari 16.4. The existing stylesheets already
use `color-mix()` in 18 places (`student/shell.css`, `student/auth.css`,
`dashboard/layout.css`, `marketing/landing.css`), which is Safari 16.2. The
pilot iPads are therefore already assumed to be on a 2023-era Safari; the
delta is two point releases.

**The accessibility tiers survive as names.** `css/dashboard/README.md`
documents three oranges with three jobs, and the trap that `--primary` (3.6:1)
cannot carry text in either direction. After migration the names carry the
same meaning — `text-primary-ink` is the only orange permitted on text,
`bg-primary-fill` the only one permitted under white — and the invariant is
enforceable the same way: `grep '#[0-9A-Fa-f]\{6\}'` must match `app.css` and
nothing else.

**The accessibility media queries port, not drop.** `@media print` (3),
`prefers-reduced-motion: reduce` (4), `forced-colors: active` (2) and
`prefers-contrast: more` (1) become the `print:`, `motion-reduce:`,
`forced-colors:` and `contrast-more:` variants.

### 2. The build

`package.json` carries one dev dependency (`@tailwindcss/cli`) and one script.
`npm run css` compiles `static/css/app.css` to `static/css/app.build.css`,
and **that output is committed to git**.

The platform ships three ways — a Docker image (Azure and AWS), a Python wheel
whose `pyproject.toml` packages `static/` inside it, and a frozen
desktop/kiosk build. Committing the artifact means none of the three needs a
Node toolchain; they continue to run `collectstatic` against a file that is
already there.

The cost of a committed artifact is drift. A CI job rebuilds the stylesheet
and fails if the result differs from what is in git, so a stale build cannot
reach production.

**`app.build.css` is linked from all three base templates in step 0**, before
any page consumes it. Utilities are therefore available on every surface from
the first step onward, and the per-surface steps only ever *delete* old
stylesheets. This is what decouples the shared partials — `_includes/icon.html`,
`_includes/feedback_button.html`, `_includes/_baseline_recommend_banner.html`
are rendered by more than one surface, and can migrate with whichever surface
reaches them first without breaking the others.

### 3. Runtime-composed class names

Tailwind's scanner matches literal strings in source text. It never sees a
class name assembled at runtime, and this codebase assembles them in two
places.

**In templates (20 occurrences).** `pill-{{ row.item.status }}`,
`turn-{{ turn.role }}`, `alert--{{ message.level_tag }}`,
`attention-item--{{ item.tone }}`, `badge-{{ s.status }}`,
`step-phase {{ step.phase }}`. `dashboard_ui.py:138`'s `tone_class` is the
sanctioned form of the pattern.

**In JavaScript (~30 names, 5 files).** `dashboard-charts.js` constructs
`chart__bar`, `chart__col`, `chart__gridline`, `chart__x-label`;
`password-field.js` constructs the whole `pw-meter` / `pw-rule` tree;
`dashboard-shell.js` toggles `rail-open` and `is-open`; `flash.js` builds
`alert__close` and `alert__text`.

**Rule, identical in both:** runtime code selects a *complete literal utility
string* from a lookup. It never concatenates a fragment. `tone_class` returns
`"bg-green-surface text-green-ink border-green-border"` from a dict whose six
values are literal in a `.py` file covered by `@source`. JS reads from a
`const` map of the same shape.

This is the failure mode most likely to reach production, because a
concatenated class often *works in development* — it was present in the scan
from an earlier build — and ships with no styles at all.

### 4. Component vocabulary

The six `dashboard_ui` inclusion tags — `icon`, `badge`, `stat_tile`,
`progress`, `empty_state`, `attention_item` (`apps/dashboard/templatetags/dashboard_ui.py`)
— become the single home for every repeated utility string. Any shape used on
three or more pages goes through a tag rather than being copied.

Without this, "no custom CSS" degenerates into the same forty-utility string
pasted across 44 dashboard pages, which is materially worse than the
stylesheet it replaced.

### 5. The 69 documentation fragments

`templates/docs/sections/{,fr/,pt_mz/}*.html` is 69 files — 23 sections in
three languages — and uses exactly six classes: `doc-table`, `doc-table-wrap`,
`doc-list`, `doc-note`, `doc-note__label`, `doc-code`.

They are generated by `scripts/build_playbook_docs.py` from
`docs/AI_Tutor_Country_Adoption_Playbook.docx`, with translations produced by
`scripts/build_playbook_translations.py`. The migration changes the class
strings in the generator and re-runs it; all 69 regenerate together.

The generator must also start emitting utilities on bare `<p>`, `<h3>` and
`<li>`, because Tailwind's preflight zeroes their margins. No typography
plugin — the generator owns every element it emits, so explicit utilities are
deterministic and keep the "no custom CSS" rule intact.

### 6. Templates that extend nothing

54 templates extend a base and inherit their surface from it. The remaining
non-generated templates are standalone — partials, error pages and the desktop
shells — and each loads surface stylesheets directly, so each needs an explicit
step. None of them is exempt.

| Step | Standalone templates |
|---|---|
| 1 · marketing + docs | `accounts/landing.html`, `downloads/{index,self_hosting}.html`, `docs/base.html`, `docs/_card.html`, `_includes/_marketing_{header,footer}.html` |
| 2 · dashboard | `dashboard/base.html`, `dashboard/_components/*.html` (5), `dashboard/materials/_confirm_card.html` |
| 3 · student | `base.html`, `tutoring/_partials/exit_modal.html`, `accounts/_email_verify_banner.html`, `_includes/{feedback_button,icon,icon_sprite,_baseline_recommend_banner}.html`, `desktop/{setup,server}.html`, `safety/csrf_failure.html` |
| never | `email/verify_email.html` (HTML email), `accounts/password_reset_email.html` (plain text, no CSS) |

`desktop/{setup,server}.html` and `safety/csrf_failure.html` look like they
might need to be self-contained, but all three `{% static %}`-link the shared
stylesheets today and are served by a running Django with collected static.
They are ordinary pages.

### 7. Verification — three gates, per surface

1. **Screenshot comparison, before and after, for every page in the surface.**
   `chrome-devtools-mcp` is not installed on this machine. The gate drives
   `/usr/bin/chromium` over raw CDP using the venv's `websockets`, with the
   browser cache disabled and `runserver` restarted after template edits.
   Any visual difference is a defect, not an accepted change.

2. **`makemessages` produces an empty diff.** There are 1,519 msgids across
   `locale/fr` and `locale/pt_MZ`, and **46 of them contain markup**. Reflowing
   a `<strong>` inside a `{% blocktrans %}` silently orphans both translations.
   Zero msgid churn is a hard gate.

3. **`pytest` passes**, in particular
   `apps/safety/tests/test_assessment_findings.py`, which asserts on CSP
   headers and on every inline `<script>` carrying `nonce="{{ request.csp_nonce }}"`.
   Templates in scope carry both.

### 8. Order

Each step is one pull request, independently verified and independently
deployable. `main` is shippable at every point.

| Step | Surface | Deletes | Pages |
|---|---|---|---|
| 0 | theme + build | nothing — additive, nothing consumes it yet | 0 |
| 1 | marketing + docs | `marketing/{landing,docs,legal}.css` | 2 templates + 69 generated fragments |
| 2 | teacher dashboard | `dashboard/**`, including `dashboard/legacy.css` | 44 (34 dashboard, 8 benchmark, 2 accounts) |
| 3 | student + tutoring | `student/{brand,shell,components,catalog,auth,legacy}.css` | 8 + partials |
| 4 | shared foundation | `shared/{tokens,base,flash,password-field}.css` | — |

**The dashboard precedes the student surface deliberately.** The student
surface is the live Seychelles pilot, including the chat tutor. Migrating the
44 teacher-facing pages first proves the approach across the largest surface
before anything reaches students mid-term.

Steps 2 and 3 retire the two `legacy.css` compatibility shims that the design
system has been carrying since 2026-08-31.

## Consequences

**CSP tightens, partially.** `apps/safety/csp.py:71` records that `style-src`
is untightenable because of 2,226 inline `style=""` attributes. Afterwards
there are 49, all of them progress-bar widths. `style-src-elem` can therefore
move to `'self'` — the half that blocks injected `<style>` elements — while
`style-src-attr` still requires `'unsafe-inline'`.

Clearing the final 49 means moving widths to a `data-` attribute set from
JavaScript, since CSSOM writes are not CSP-restricted. That is a follow-up,
deliberately out of scope here.

**The design system is preserved, not discarded.** The token vocabulary and
its WCAG-AA verified tiers move into `@theme` intact. What is discarded is the
hand-written component layer that consumed them.

## Out of scope

- Any visual redesign. Pixel-faithful is the bar.
- Dark mode.
- Clearing the last 49 inline width attributes.
- A relaxed fidelity bar anywhere. The 8 `templates/benchmark/*.html` files
  are internal annotation tools rather than a product surface, but they extend
  `dashboard/base.html` and are 8 of step 2's 44 pages. Step 2 deletes the
  stylesheets they depend on, so they migrate with the rest; only their
  screenshot review may be less exacting.

Refs: auto-memory/dashboard-design-system.md, auto-memory/browser-verification-on-this-box.md
