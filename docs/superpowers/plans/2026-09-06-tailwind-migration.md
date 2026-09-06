# Tailwind v4 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every hand-written stylesheet and inline style in the platform with Tailwind v4 utilities, pixel-faithfully, one surface per pull request.

**Architecture:** One `@theme` block ports the existing design tokens verbatim, so Tailwind's utilities compile to the same `var(--color-*)` the database-driven institution theming already overrides. A committed build artifact keeps all three ship pipelines Python-only. Conversion is mechanical where it can be — a declaration→utility helper with exact arbitrary-value fallback guarantees pixel fidelity — and hand-judged where selectors are structural.

**Tech Stack:** Tailwind CSS v4.3.3 (`@tailwindcss/cli`), Django 5 templates, Python 3.13, pytest-django, Chromium over raw CDP for screenshot verification.

**Spec:** `docs/superpowers/specs/2026-09-06-tailwind-migration-design.md`

## Global Constraints

- **Tailwind v4 only.** No `tailwind.config.js`. All configuration in `@theme` inside `ai_tutor/static/css/app.css`.
- **Pixel-faithful.** Any visual difference between the before and after screenshot of a page is a defect. Where the token scale does not match a hand-written value exactly, use an arbitrary value (`p-[0.6rem]`), never the nearest token.
- **No literal colour outside `app.css`.** `grep -rE '#[0-9A-Fa-f]{3,8}\b'` over `ai_tutor/static/css/` and `ai_tutor/templates/` must match only `app.css`, the two documented exceptions, and `email/verify_email.html`.
- **No runtime class-name concatenation.** Python and JavaScript select a complete literal utility string from a lookup. Never `f'badge--{tone}'`.
- **Never touch text inside `{% trans %}` / `{% blocktrans %}`.** 1,519 msgids exist across `locale/fr` and `locale/pt_MZ`; 46 contain markup. `makemessages` must produce an empty diff.
- **Never modify** `ai_tutor/templates/email/verify_email.html` (HTML email; inline styles are load-bearing) or `ai_tutor/templates/accounts/password_reset_email.html` (plain text).
- **Every inline `<script>` keeps `nonce="{{ request.csp_nonce }}"`.** Enforced by `apps/safety/tests/test_assessment_findings.py`.
- **Test command:** `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest <path> -q` — bare `pytest` cannot work on this box.
- **Build command:** `npm run css`. The output `ai_tutor/static/css/app.build.css` is committed.
- **Commit trailer:** every commit ends with `Claude-Session: https://claude.ai/code/session_0116UfmwGDtPWYnzoZdNXSdp`.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `package.json` | one dev dependency (`@tailwindcss/cli`), one `css` script |
| `ai_tutor/static/css/app.css` | the source: `@import`s plus the whole `@theme` |
| `ai_tutor/static/css/app.build.css` | committed build output, the only stylesheet the app links |
| `scripts/css_to_tailwind.py` | declaration→utility helper used by every conversion task |
| `scripts/shoot.py` | Chromium/CDP screenshot harness |
| `tests/design/test_no_raw_css.py` | guard tests: no stray hex, no concatenated class names, no orphan stylesheet links |
| `tests/design/test_css_build_current.py` | drift check — rebuild must equal the committed artifact |
| `.github/workflows/css.yml` | runs the drift check in CI |

**Deleted, by phase:** `marketing/{landing,docs,legal}.css` (phase 1) → `dashboard/**` (phase 2) → `student/**` (phase 3) → `shared/**` (phase 4).

---

## Phase 0 — Foundation

### Task 1: Toolchain and theme

**Files:**
- Create: `package.json`, `.gitignore` entry for `node_modules/`
- Create: `ai_tutor/static/css/app.css`
- Create: `ai_tutor/static/css/app.build.css` (generated, committed)

**Interfaces:**
- Produces: the utility vocabulary every later task consumes — colour names identical to the token names in `shared/tokens.css` (`primary-fill`, `green-ink`, `text-muted`, `border-field`, …), `--spacing: 0.25rem` reproducing `--space-1`…`--space-12` as `p-1`…`p-12`, breakpoints `xs`/`sm`/`md`/`lg`/`xl` = `30rem`/`40rem`/`56rem`/`60rem`/`62rem`.

- [ ] **Step 1: Create `package.json`**

```json
{
  "name": "ai-tutor-css",
  "private": true,
  "scripts": {
    "css": "tailwindcss -i ai_tutor/static/css/app.css -o ai_tutor/static/css/app.build.css --minify"
  },
  "devDependencies": {
    "@tailwindcss/cli": "4.3.3"
  }
}
```

- [ ] **Step 2: Install and confirm the CLI runs**

Run: `npm install && npx tailwindcss --help | head -3`
Expected: usage text, exit 0.

- [ ] **Step 3: Add `node_modules/` and lockfile policy to `.gitignore`**

`node_modules/` is ignored; `package-lock.json` is committed so CI installs the same CLI.

- [ ] **Step 4: Write `app.css` porting every token verbatim**

Ported from `shared/tokens.css` with no value changes. Preflight is included from the start and the sheet is linked *before* the legacy stylesheets, so during phases 1–3 the old sheets keep winning wherever they disagree.

```css
@import "tailwindcss";

@source "../../templates/**/*.html";
@source "../js/**/*.js";
@source "../../apps/**/templatetags/*.py";

@theme {
  /* primitive — warm neutral ramp */
  --color-warm-25: #FCFBF9;
  --color-warm-50: #F7F5F2;
  --color-warm-100: #EFECE7;
  --color-warm-200: #E3DFD8;
  --color-warm-300: #CFC9C0;
  --color-warm-350: #948C81;
  --color-warm-400: #A9A199;
  --color-warm-500: #6F6860;
  --color-warm-600: #5E574E;
  --color-warm-700: #453F38;
  --color-warm-800: #2E2A25;
  --color-warm-900: #1B1815;

  /* brand — three oranges, three jobs. See css/dashboard/README.md. */
  --color-primary: #E8590C;        /* 3.6:1 — NON-TEXT only */
  --color-primary-fill: #C4460A;   /* 5.0:1 under white */
  --color-primary-dark: #A83B00;
  --color-primary-light: #FFF3EC;
  --color-primary-tint: #FDE3D3;
  --color-primary-ink: #A83B00;    /* 6.4:1 — the only orange for text */

  /* status trios */
  --color-green-ink: #0F7B5F;   --color-green-surface: #E7F4EF;
  --color-green-border: #B8E0D0; --color-green-solid: #12996F;
  --color-amber-ink: #9A6207;   --color-amber-surface: #FBF1DE;
  --color-amber-border: #EFD9AE; --color-amber-solid: #B0720F;
  --color-rose-ink: #B3261E;    --color-rose-surface: #FBEAE8;
  --color-rose-border: #F2C4C0;  --color-rose-solid: #D6382E;
  --color-blue-ink: #2D5B8E;    --color-blue-surface: #E8F0F8;
  --color-blue-border: #C4D8EC;  --color-blue-solid: #3E7CBF;
  --color-violet-ink: #6B3FA0;  --color-violet-solid: #7C4DBE;

  /* semantic */
  --color-canvas: var(--color-warm-25);
  --color-surface: #FFFFFF;
  --color-surface-sunken: var(--color-warm-50);
  --color-surface-hover: var(--color-warm-100);
  --color-surface-inverse: var(--color-warm-900);
  --color-text: var(--color-warm-900);
  --color-text-secondary: var(--color-warm-700);
  --color-text-muted: var(--color-warm-500);
  --color-text-faint: var(--color-warm-400);   /* 2.6:1 — NOT a text colour */
  --color-text-on-accent: #FFFFFF;
  --color-text-link: var(--color-primary-ink);
  --color-border: var(--color-warm-200);
  --color-border-strong: var(--color-warm-300);
  --color-border-field: var(--color-warm-350);
  --color-success: var(--color-green-ink);
  --color-success-surface: var(--color-green-surface);
  --color-success-border: var(--color-green-border);
  --color-success-solid: var(--color-green-solid);
  --color-warning: var(--color-amber-ink);
  --color-warning-surface: var(--color-amber-surface);
  --color-warning-border: var(--color-amber-border);
  --color-warning-solid: var(--color-amber-solid);
  --color-danger: var(--color-rose-ink);
  --color-danger-surface: var(--color-rose-surface);
  --color-danger-border: var(--color-rose-border);
  --color-danger-solid: var(--color-rose-solid);
  --color-info: var(--color-blue-ink);
  --color-info-surface: var(--color-blue-surface);
  --color-info-border: var(--color-blue-border);
  --color-info-solid: var(--color-blue-solid);
  --color-accent: var(--color-primary-ink);
  --color-accent-surface: var(--color-primary-light);
  --color-accent-border: var(--color-primary-tint);
  --color-accent-solid: var(--color-primary);
  --color-accent-fill: var(--color-primary-fill);

  /* type */
  --font-display: 'Space Grotesk', 'Nunito', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --font-body: 'Nunito', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  --font-mono: 'Space Mono', ui-monospace, 'SF Mono', 'Cascadia Mono', Menlo, monospace;

  --text-2xs: 0.6875rem;  --text-xs: 0.75rem;   --text-sm: 0.8125rem;
  --text-base: 0.875rem;  --text-md: 1rem;      --text-lg: 1.25rem;
  --text-xl: 1.625rem;    --text-2xl: 2.125rem; --text-3xl: 2.75rem;

  --leading-tight: 1.15;  --leading-snug: 1.35;  --leading-normal: 1.55;
  --tracking-tight: -0.018em;  --tracking-wide: 0.08em;
  --font-weight-normal: 400;  --font-weight-medium: 600;  --font-weight-bold: 700;

  /* 4px base reproduces --space-1 … --space-12 exactly as p-1 … p-12 */
  --spacing: 0.25rem;

  --radius-xs: 4px;  --radius-sm: 6px;  --radius-md: 10px;
  --radius-lg: 14px; --radius-full: 9999px;

  --shadow-xs: 0 1px 2px rgba(27, 24, 21, 0.05);
  --shadow-sm: 0 1px 3px rgba(27, 24, 21, 0.06), 0 1px 2px rgba(27, 24, 21, 0.04);
  --shadow-md: 0 4px 12px rgba(27, 24, 21, 0.07), 0 1px 3px rgba(27, 24, 21, 0.05);
  --shadow-lg: 0 12px 32px rgba(27, 24, 21, 0.10), 0 2px 8px rgba(27, 24, 21, 0.05);
  --shadow-focus: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-primary);
  --shadow-halo: 0 0 0 3px var(--color-primary-light);
  --inset-shadow-focus: inset 0 0 0 2px var(--color-primary);

  --ease-out: cubic-bezier(0.2, 0, 0, 1);
  --ease-spring: cubic-bezier(0.32, 0.72, 0, 1);
  --dur-fast: 120ms;  --dur-base: 200ms;  --dur-slow: 360ms;

  --breakpoint-xs: 30rem;  --breakpoint-sm: 40rem;  --breakpoint-md: 56rem;
  --breakpoint-lg: 60rem;  --breakpoint-xl: 62rem;

  --rail-width: 260px;  --rail-width-compact: 212px;
  --topbar-height: 60px; --content-max: 1440px;

  --animate-skeleton: skeleton-sheen 1.4s linear infinite;
  --animate-fade: fade var(--dur-base) var(--ease-out);
  --animate-slide: slide var(--dur-base) var(--ease-out);
  --animate-slide-down: slide-down var(--dur-base) var(--ease-out);
}

@keyframes fade { from { opacity: 0 } to { opacity: 1 } }
@keyframes slide { from { transform: translateY(6px); opacity: 0 } to { transform: none; opacity: 1 } }
@keyframes slide-down { from { transform: translateY(-6px); opacity: 0 } to { transform: none; opacity: 1 } }
@keyframes skeleton-sheen { from { background-position: -200% 0 } to { background-position: 200% 0 } }
```

The four `@keyframes` are copied verbatim from the sheets they replace; confirm each against `shared/base.css` and `student/components.css` before writing, and correct the values here if they differ.

- [ ] **Step 5: Build and confirm the artifact exists**

Run: `npm run css && wc -c ai_tutor/static/css/app.build.css`
Expected: a non-empty file.

- [ ] **Step 6: Confirm the tokens survived the build**

Run: `grep -c 'E8590C\|C4460A\|A83B00' ai_tutor/static/css/app.build.css`
Expected: at least 3 — the theme variables are emitted even before any utility uses them.

- [ ] **Step 7: Commit**

```bash
git add package.json package-lock.json .gitignore ai_tutor/static/css/app.css ai_tutor/static/css/app.build.css
git commit -m "build: add Tailwind v4 with the existing tokens as its theme"
```

---

### Task 2: Screenshot harness and the baseline

**Files:**
- Create: `scripts/shoot.py`
- Create: `scripts/pages.txt` (the URL inventory)

**Interfaces:**
- Produces: `python scripts/shoot.py --out <dir>` writes one PNG per URL in `scripts/pages.txt`; `python scripts/shoot.py --compare <before> <after>` prints a per-page pixel-difference count and exits non-zero if any page differs.

- [ ] **Step 1: Write the harness**

Drives `/usr/bin/chromium` headless over raw CDP with the venv's `websockets` (16.0). Cache disabled via `Network.setCacheDisabled`, one screenshot per URL at 1440×900 and again at 390×844, full-page.

- [ ] **Step 2: Enumerate the pages**

Every URL reachable from `ai_tutor/config/urls.py` that renders a template in scope, one per line, with the fixture user each needs. Teacher pages need a staff login; student pages need a student login; marketing and docs pages are anonymous.

- [ ] **Step 3: Capture the baseline on the pre-migration tree**

Run: `git stash && python scripts/shoot.py --out .screens/baseline && git stash pop`
Expected: one PNG per page, no errors.

- [ ] **Step 4: Verify the harness detects a change**

Introduce a one-line colour change in `shared/tokens.css`, re-shoot to `.screens/probe`, compare against baseline.
Expected: non-zero diff, non-zero exit. Revert the change.

- [ ] **Step 5: Commit the harness** (`.screens/` is gitignored)

---

### Task 3: Link the stylesheet, verify nothing moved

**Files:**
- Modify: `ai_tutor/templates/base.html:42`, `ai_tutor/templates/dashboard/base.html:38`, `ai_tutor/templates/docs/base.html:16`
- Modify: `ai_tutor/templates/desktop/setup.html:22`, `ai_tutor/templates/desktop/server.html:19`, `ai_tutor/templates/safety/csrf_failure.html:10`

- [ ] **Step 1: Add the link above every existing stylesheet link**

```django
<link rel="stylesheet" href="{% static 'css/app.build.css' %}">
```

It goes *first* so the legacy sheets override Tailwind's preflight wherever they disagree. This is the whole reason phases 1–3 can run without the old pages breaking.

- [ ] **Step 2: Re-shoot and compare against the baseline**

Run: `python scripts/shoot.py --out .screens/step0 && python scripts/shoot.py --compare .screens/baseline .screens/step0`
Expected: zero differing pages.

- [ ] **Step 3: If any page differs, fix it here, not later**

The likely causes are preflight rules the old sheets do not re-assert: `ol,ul { list-style: none }`, `img { display: block }`, `table { border-collapse: collapse }`. Fix by adding the missing declaration to the page's own sheet — which is being deleted later anyway — and re-compare. Do not proceed with a non-zero diff. If more than five pages differ, fall back to `@import "tailwindcss/theme"` + `@import "tailwindcss/utilities"` without preflight, and move preflight to Task 20.

- [ ] **Step 4: Run the test suite**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest ai_tutor/apps/safety/tests/ -q`
Expected: pass.

- [ ] **Step 5: Commit**

---

### Task 4: The conversion helper

**Files:**
- Create: `scripts/css_to_tailwind.py`
- Test: `tests/design/test_css_to_tailwind.py`

**Interfaces:**
- Produces: `decls_to_utilities(css: str) -> str` — takes a declaration block (`"padding:0.6rem 0.85rem;border-radius:8px"`) and returns a utility string (`"px-[0.85rem] py-[0.6rem] rounded-[8px]"`). Prefers a theme token when the value matches one exactly, otherwise emits an arbitrary value so the result is pixel-identical. Raises `Unconvertible` for declarations with no utility form, so the caller must handle them explicitly rather than silently dropping them.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from scripts.css_to_tailwind import decls_to_utilities, Unconvertible

def test_exact_token_match_uses_the_token():
    assert decls_to_utilities("padding: 1rem") == "p-4"          # --space-4
    assert decls_to_utilities("color: var(--text-muted)") == "text-text-muted"

def test_non_token_value_uses_an_arbitrary_value_not_the_nearest_token():
    assert decls_to_utilities("padding: 0.6rem") == "p-[0.6rem]"

def test_shorthand_splits_into_axes():
    assert decls_to_utilities("padding: 0.6rem 0.85rem") == "py-[0.6rem] px-[0.85rem]"

def test_unconvertible_raises_rather_than_dropping():
    with pytest.raises(Unconvertible):
        decls_to_utilities("counter-reset: section")
```

- [ ] **Step 2: Run to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python -m pytest tests/design/test_css_to_tailwind.py -q`
Expected: FAIL, `ModuleNotFoundError: scripts.css_to_tailwind`.

- [ ] **Step 3: Implement**

A property→utility-prefix table, a value→token reverse map built from the `@theme` block, and shorthand expansion for `padding`/`margin`/`border-radius`/`inset`.

- [ ] **Step 4: Run to verify they pass.** Expected: PASS.

- [ ] **Step 5: Commit**

---

### Task 5: Guard tests and the CI drift check

**Files:**
- Create: `tests/design/test_no_raw_css.py`, `tests/design/test_css_build_current.py`
- Create: `.github/workflows/css.yml`

- [ ] **Step 1: Write the guards**

```python
ALLOWED_HEX_FILES = {
    "ai_tutor/static/css/app.css",
    "ai_tutor/templates/email/verify_email.html",
}

def test_no_literal_colour_outside_the_theme():
    """The one rule from css/dashboard/README.md, kept after the migration."""
    offenders = [p for p in css_and_template_files()
                 if HEX.search(p.read_text()) and str(p) not in ALLOWED_HEX_FILES]
    assert offenders == []

def test_no_runtime_class_name_concatenation():
    """Tailwind's scanner cannot see a class name Django or JS assembles.

    A concatenated class often works in development, because it was in an
    earlier build's scan, then ships with no styles at all.
    """
    offenders = grep_templates(r'class="[^"]*[\w-]-\{\{')
    assert offenders == []

def test_only_the_built_stylesheet_is_linked():
    linked = grep_templates(r"static 'css/([^']+)'")
    assert set(linked) <= {"app.build.css"}
```

The third guard is expected to fail until Task 20 and is marked `xfail(strict=True)` until then, so it flips to a real assertion the moment the last sheet dies.

- [ ] **Step 2: Write the drift check**

Rebuilds `app.css` to a temporary path and asserts byte-equality with the committed `app.build.css`. Skipped when `npx` is unavailable so a fresh checkout without Node still runs the suite.

- [ ] **Step 3: Run both.** Expected: hex and concatenation guards fail with the current tree's real offenders listed; drift check passes.

- [ ] **Step 4: Record the offender counts in the commit body** as the number each phase must drive down.

- [ ] **Step 5: Add the CI workflow** — `npm ci && npm run css && git diff --exit-code ai_tutor/static/css/app.build.css`

- [ ] **Step 6: Commit**

---

## Phase 1 — Marketing and documentation

### Task 6: The playbook generator

**Files:**
- Modify: `scripts/build_playbook_docs.py`
- Regenerate: `ai_tutor/templates/docs/sections/**` (69 files)
- Test: `ai_tutor/apps/docs/tests/test_playbook_docs.py`

- [ ] **Step 1: Read `marketing/docs.css`** and write down the exact declarations behind `.doc-table`, `.doc-table-wrap`, `.doc-list`, `.doc-note`, `.doc-note__label`, `.doc-code`, plus the bare `p`/`h3`/`li`/`strong` rules the fragments rely on.

- [ ] **Step 2: Extend the existing generator test** to assert the emitted HTML carries utility strings and no `doc-` class remains.

- [ ] **Step 3: Run it.** Expected: FAIL.

- [ ] **Step 4: Replace the class constants in the generator** with the utility strings from Step 1, adding explicit utilities to every bare `<p>`, `<h3>` and `<li>` it emits, since preflight zeroes their margins.

- [ ] **Step 5: Regenerate all three languages**

Run: `venv/bin/python scripts/build_playbook_docs.py && venv/bin/python scripts/build_playbook_translations.py`
Expected: 69 files change; `git diff --stat` shows no prose changes, only class attributes.

- [ ] **Step 6: `makemessages` diff must be empty.** Expected: no change to `locale/fr` or `locale/pt_MZ`.

- [ ] **Step 7: Screenshot-compare the docs pages.** Expected: zero diff.

- [ ] **Step 8: Commit**

### Task 7: Marketing templates and the deletion of their sheets

**Files:**
- Modify: `docs/base.html`, `docs/_card.html`, `accounts/landing.html`, `safety/{privacy_policy,terms_of_service,privacy_dashboard}.html`, `accounts/terms_accept.html`, `downloads/{index,self_hosting}.html`, `_includes/_marketing_{header,footer}.html`
- Delete: `marketing/{landing,docs,legal}.css`

- [ ] **Step 1: Convert each template's inline `<style>` block** using `decls_to_utilities`, moving each rule onto the elements that carry its selector.
- [ ] **Step 2: Convert each `style=""` attribute** on those templates.
- [ ] **Step 3: Remove the three `<link>` tags and delete the three sheets.**
- [ ] **Step 4: Screenshot-compare.** Expected: zero diff. Iterate until it is zero.
- [ ] **Step 5: `makemessages` diff empty; `pytest` green.**
- [ ] **Step 6: Commit**

---

## Phase 2 — Teacher dashboard

### Task 8: Utility lookups in `dashboard_ui.py`

**Files:**
- Modify: `ai_tutor/apps/dashboard/templatetags/dashboard_ui.py`
- Modify: `ai_tutor/templates/dashboard/_components/{badge,stat_tile,progress,empty_state,attention_item}.html`, `_includes/icon.html`
- Test: `ai_tutor/apps/dashboard/tests/test_dashboard_ui.py`

**Interfaces:**
- Produces: `tone_class(prefix, tone)` returns a complete literal utility string looked up from `TONE_UTILITIES[prefix][tone]`. `TONES` stays `('neutral','success','warning','danger','info','accent')`; `_tone()` keeps normalising unknown values to `neutral`.

- [ ] **Step 1: Write the failing test**

```python
def test_tone_class_returns_literal_utilities_not_a_modifier():
    out = tone_class("badge", "success")
    assert "--" not in out
    assert "bg-green-surface" in out and "text-green-ink" in out

def test_every_tone_has_an_entry_for_every_prefix():
    for prefix in TONE_UTILITIES:
        assert set(TONE_UTILITIES[prefix]) == set(TONES)

def test_unknown_tone_falls_back_to_neutral():
    assert tone_class("badge", "banana") == tone_class("badge", "neutral")
```

- [ ] **Step 2: Run to verify it fails.** Expected: FAIL on the `--` assertion.
- [ ] **Step 3: Add `TONE_UTILITIES`** — a nested dict, every value a literal string, in a file `@source` already scans.
- [ ] **Step 4: Run to verify it passes.**
- [ ] **Step 5: Convert the six component templates** to utilities, keeping every `aria-*` attribute exactly as it is.
- [ ] **Step 6: Commit**

### Task 9: The dashboard shell

**Files:** Modify `dashboard/base.html`; delete `dashboard/layout.css` at the end of the task.

- [ ] **Step 1:** Convert the rail, topbar, canvas and drawer, using `w-[var(--rail-width)]` and `h-[var(--topbar-height)]` for the component knobs.
- [ ] **Step 2:** Port the three `@media (min-width: 62rem)` blocks to `xl:` and the `max-width: 61.99rem` blocks to `max-xl:`.
- [ ] **Step 3:** Update `dashboard-shell.js` — `rail-open` and `is-open` become literal utility strings from a `const` map.
- [ ] **Step 4:** Screenshot-compare every dashboard page. Expected: zero diff.
- [ ] **Step 5:** Delete `layout.css` and its `<link>`. Re-compare. Commit.

### Task 10: Dashboard components

**Files:** the 44 templates; delete `components/{surfaces,controls,data}.css`.

- [ ] **Step 1:** Build the class→utility map for every selector in the three sheets, including descendant selectors, which become explicit utilities on the child element.
- [ ] **Step 2:** Apply the map across the 44 templates.
- [ ] **Step 3:** Convert the `.select` chevron data URI, the one documented exception that cannot read a custom property, to a `bg-[url(...)]` arbitrary value with the literal colour — and add it to `ALLOWED_HEX_FILES` in the guard test with the reason at the line.
- [ ] **Step 4:** Screenshot-compare. Zero diff. Delete the three sheets. Commit.

### Task 11: Charts

**Files:** `dashboard-charts.js`, `dashboard/charts.css`

- [ ] **Step 1:** Replace the constructed `chart__bar` / `chart__col` / `chart__gridline` / `chart__x-label` names with literal utility strings from a `const` map at the top of the file.
- [ ] **Step 2:** Confirm Tailwind's scanner picks them up: `grep -c 'chart' ai_tutor/static/css/app.build.css` after a rebuild should be 0 while the utilities themselves are present.
- [ ] **Step 3:** Screenshot-compare the pages with charts. Delete `charts.css`. Commit.

### Task 12: The 44 pages' own styles

**Files:** the inline `<style>` blocks and `style=""` attributes across the dashboard templates, including the 8 `benchmark/` pages; delete `dashboard/legacy.css` and `dashboard/pages/home.css`.

- [ ] **Step 1:** Convert page by page, screenshot-comparing each before moving on.
- [ ] **Step 2:** Leave the 49 dynamic `style="width: …%"` attributes exactly as they are — they are a documented exception.
- [ ] **Step 3:** Delete `legacy.css` and `pages/home.css`. This retires the teacher-side compatibility shim carried since 2026-08-31.
- [ ] **Step 4:** Zero screenshot diff, `makemessages` empty, `pytest` green. Commit.

---

## Phase 3 — Student and tutoring

### Task 13: The student shell and brand

**Files:** `base.html`; delete `student/{brand,shell}.css`.

- [ ] **Step 1:** Convert the shell. **Leave the `<style nonce>` block at `base.html:57-66` untouched** — it is the database-driven institution theme and a documented exception.
- [ ] **Step 2:** Confirm institution theming still works: set `theme_primary` on a test institution and check the rendered page picks it up.
- [ ] **Step 3:** Screenshot-compare. Delete the two sheets. Commit.

### Task 14: Student components, catalog and auth

**Files:** the 8 templates extending `base.html`, the `accounts/` auth pages, `student-shell.js`, `password-field.js`, `flash.js`; delete `student/{components,catalog,auth,legacy}.css` and `shared/{flash,password-field}.css`.

- [ ] **Step 1:** Convert the templates.
- [ ] **Step 2:** Replace every constructed class name in the three JS files with literal utility strings from `const` maps — `pw-meter`, `pw-rule`, `pw-match`, `alert__close`, `alert__text`, `is-open`, `is-done`, `is-failed`, `is-leaving`.
- [ ] **Step 3:** Exercise the password field and a flash message in the browser, not just the DOM: screenshot the meter at weak, medium and strong.
- [ ] **Step 4:** Screenshot-compare. Delete the sheets. Retires the student-side shim. Commit.

### Task 15: The chat tutor

**Files:** `tutoring/chat_tutor.html`, `tutoring/pretest.html`, `tutoring/summative/{take,review}.html`, `tutoring/_partials/exit_modal.html`

- [ ] **Step 1:** Convert, taking particular care with the `100dvh` container — `base.html:76-81` records that an element added above it once pushed the typing bar below the fold.
- [ ] **Step 2:** Screenshot at 390×844 as well as desktop, and confirm the typing bar is on screen.
- [ ] **Step 3:** Send a real message in a local session and confirm the media signal still renders and turns still style correctly.
- [ ] **Step 4:** Zero diff. Commit.

---

## Phase 4 — Shared foundation

### Task 16: Delete the last sheets and tighten CSP

**Files:** delete `shared/{tokens,base}.css`; modify `apps/safety/csp.py`; modify `tests/design/test_no_raw_css.py`

- [ ] **Step 1:** Remove the last `<link>` tags and delete `tokens.css` and `base.css`.
- [ ] **Step 2:** Flip the third guard test from `xfail` to a plain assertion — only `app.build.css` may be linked.
- [ ] **Step 3:** Confirm the `--gray-*` legacy aliases have no remaining consumers before deleting them: `grep -rn 'var(--gray-' ai_tutor/` must be empty.
- [ ] **Step 4:** Split `style-src` in `apps/safety/csp.py:73`

```python
('style-src-elem', "'self'"),
# 49 progress-bar widths are interpolated by Django and cannot be utilities.
# Clearing them means moving the width to a data- attribute set from JS,
# since CSSOM writes are not CSP-restricted. Out of scope here.
('style-src-attr', "'unsafe-inline'"),
```

- [ ] **Step 5:** Update the comment at `csp.py:8-11` — the counts it cites (55 style blocks, 2226 style attributes) are now 1 and 49.
- [ ] **Step 6:** Full screenshot comparison across every page, full `pytest`, `makemessages` empty.
- [ ] **Step 7:** Delete `css/dashboard/README.md`'s stylesheet inventory and replace it with the `@theme` contract, keeping the three-orange table — it is the most useful thing in the file.
- [ ] **Step 8:** Commit.

---

## Self-Review

**Spec coverage.** §1 theme → Task 1. §2 build → Tasks 1, 5. §3 runtime class names → Tasks 8, 11, 14, plus the guard in Task 5. §4 component vocabulary → Task 8. §5 docs fragments → Task 6. §6 standalone templates → Tasks 3 (links), 7 (marketing), 12 (dashboard partials), 14–15 (student partials). §7 verification → Tasks 2, 5, and a gate in every conversion task. §8 order → phases 1–4. Consequences/CSP → Task 16. Survivors: `app.css` (Task 1), vendor sheets (never touched), the DB theme block (Task 13 Step 1), the 49 width attributes (Task 12 Step 2), the HTML email (global constraint).

**Placeholder scan.** No TBD/TODO. Task 6 Step 1 and Task 10 Step 1 are read-then-write steps rather than literal code, because the utility strings depend on declarations that must be read from the sheet at execution time; both name the exact file and the exact selectors to extract.

**Type consistency.** `decls_to_utilities` is the name in Task 4's tests, its implementation, and Tasks 7/10/12. `TONE_UTILITIES` and `tone_class(prefix, tone)` match between Task 8's tests and its steps. `ALLOWED_HEX_FILES` is defined in Task 5 and extended in Task 10 Step 3. `scripts/shoot.py --out` / `--compare` match between Task 2 and every later gate.
