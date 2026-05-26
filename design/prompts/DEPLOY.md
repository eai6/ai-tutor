# Deploy Guide — v2 Engine Routing + Per-Purpose Model Overrides

When merging a PR to `main`, you can pick which **LLM model** drives each
ModelConfig purpose the v2 engine dispatches, and whether new sessions
should route to the v2 engine at all — without editing any code. The deploy
workflow reads a small set of environment variables and writes them onto
the new Container App revision:

| Knob | Env var | What it controls |
|---|---|---|
| **v2 routing** | `NEW_TUTOR` | Routes *new* sessions to the v2 engine (`on`) or back to legacy `ConversationalTutor` (`off` — kill switch only). |
| **Tutor move** | `TUTOR_MOVE_MODEL_OVERRIDE` | Model for the per-move tutor response (`StudentTutor`). |
| **Grader (math)** | `GRADER_MATH_MODEL_OVERRIDE` | Model for the math-DSL grading path. |
| **Grader (grounded)** | `GRADER_GROUNDED_MODEL_OVERRIDE` | Model for the grounded-adjudication path. **Gemini-only** — Google-grounding is provider-required. |
| **Conformance classifier** | `CONFORMANCE_CLASSIFIER_MODEL_OVERRIDE` | Model for the 9-binary-label structural conformance classifier. |
| **Tutor-claim adjudicator** | `TUTOR_CLAIM_ADJUDICATOR_MODEL_OVERRIDE` | Model for the conformance tutor-claim adjudicator. **Gemini-only.** |
| **Profiler summary** | `PROFILER_SUMMARY_MODEL_OVERRIDE` | Model for the end-of-session profile summarizer. |

Phase 3 §3.6.1 of `design/refactor/refactor-implementation-plan.md` replaced
the single `TUTOR_MODEL_OVERRIDE` legacy knob with these six. The legacy
env var is still read for the (now-deprecated) `tutoring` purpose so
in-flight legacy sessions keep their dispatch surface, but new sessions
no longer touch it.

---

## TL;DR

1. **For the next push to `main`** — set the relevant repo variables once
   in **Settings → Secrets and variables → Actions → Variables**. Every
   subsequent merge to `main` deploys with those values.
2. **For an ad-hoc deploy from any commit on `main`** (e.g. flipping
   `NEW_TUTOR` off as the kill switch, swapping a model for a single
   purpose) — go to **Actions → Deploy → Run workflow** and fill in the
   relevant override fields.

---

## Valid values

### `NEW_TUTOR`

| Value | Meaning |
|---|---|
| `on` *(default)* | New sessions route to the v2 engine. |
| `off` | New sessions route back to the legacy `ConversationalTutor`. **Reserved for student-facing safety incidents** — not benchmark drift, not dashboard noise. In-flight v2 sessions continue on v2 (sticky-per-session via `TutorSession.engine_version`). |

### Per-purpose model overrides

All six follow the same format: `provider/model_name`. Empty / unset = use
whatever the DB has marked as the active config for that purpose (current
behaviour).

| Value | Provider | Notes |
|---|---|---|
| *(empty)* *(default)* | Whatever the DB says | Use the active `ModelConfig` row for the purpose. |
| `anthropic/claude-sonnet-4-6` | Anthropic | Standard tier. |
| `anthropic/claude-opus-4-7` | Anthropic | Premium tier; ~5× cost — reserve for high-stakes purposes. |
| `openai/gpt-4.1` | OpenAI | Strict-mode structured outputs friendly. |
| `google/gemini-3-flash-preview` | Google | Cheapest; required for grounded purposes. |
| `google/gemini-3.1-pro-preview` | Google | Premium Gemini tier; required for grounded purposes. |

**Grounding-required purposes** (`GRADER_GROUNDED`, `TUTOR_CLAIM_ADJUDICATOR`)
accept only Gemini providers. A non-Gemini override is logged as a deploy
warning and the runtime falls through to the DB-active config — Google's
Search grounding is a Gemini-native feature with no cross-provider
equivalent, so silently disabling it would break the grounded path.

An unknown provider/model combo on any purpose logs a warning at runtime
(`[ModelConfig] <env>=... did not resolve…; falling back to DB-active`) and
falls through to the DB-active config — fail-soft so a typo never breaks a
dispatch path.

---

## Method 1 — Set repo variables (steady-state, applies to every push to main)

Use this when you've decided on a model combination that should ship on
every merge until further notice.

1. Go to the repo on GitHub.
2. Click **Settings → Secrets and variables → Actions → Variables**.
3. Click **New repository variable**. Set the name to whichever knob you
   want to pin (e.g. `TUTOR_MOVE_MODEL_OVERRIDE`) and the value to a
   `provider/model_name` from the table above. Click **Add variable**.
4. Repeat for each purpose you want to pin. Leave the rest unset to fall
   through to the DB-active config.
5. (Optional) Set `NEW_TUTOR=off` only if you need to keep the legacy
   engine serving new sessions — this is the kill switch path.
6. Merge the PR. The deploy workflow reads these values automatically.
7. **Verify** by checking the deploy run's `Resolve deploy-time config`
   notice line for the resolved values.

To change later, edit the variable values in the same place. The change
takes effect on the next push to `main`.

---

## Method 2 — Use workflow_dispatch (ad-hoc, one-off)

Use this for the kill switch, a hotfix, or to deploy the current `main`
with a different per-purpose model combo without changing repo variables.

1. Go to the repo on GitHub.
2. Click **Actions → Deploy** (in the left sidebar).
3. Click **Run workflow** (top-right).
4. Pick the overrides you want to apply for this deploy. Leave fields
   empty to inherit repo variables (or DB-active when those are unset).
5. Branch: leave on `main` (or pick another).
6. Click **Run workflow** again to confirm.

---

## Rollback

Three independent rollback levers, in increasing severity:

1. **Per-purpose model rollback.** Re-run the deploy workflow with the
   problem purpose's override blanked out — the runtime falls back to
   DB-active for that purpose; every other purpose stays at its current
   setting. Use this when one purpose is misbehaving but the rest of the
   pipeline is fine.
2. **All-purposes rollback.** Re-run the deploy workflow with all
   overrides blank — every purpose falls back to DB-active. Use this
   when you suspect cross-purpose interaction issues.
3. **v2 kill switch.** Set `NEW_TUTOR=off` (workflow_dispatch input or
   repo variable). New sessions route back to legacy
   `ConversationalTutor`; in-flight v2 sessions keep running on v2.
   Use this **only** for student-facing safety incidents — not for
   benchmark drift or dashboard noise.

---

## How this works under the hood

- `apps/tutoring/v2/config/flags.py::is_new_tutor_enabled()` reads
  `NEW_TUTOR`. Default is `on`; only an explicit `off` flips it.
- `apps/llm/models.py::ModelConfig.get_for(purpose)` reads the matching
  `<PURPOSE>_MODEL_OVERRIDE` env var (e.g. `TUTOR_MOVE_MODEL_OVERRIDE`
  for `purpose='tutor_move'`). Empty / unset / malformed / non-Gemini-on-
  grounded → falls through to the DB-active row.
- On app boot the deploy workflow logs a `Per-purpose dispatch` notice;
  the running app's `ModelConfig.get_for(...)` resolution is visible in
  per-call traces (`apps/tutoring/tracing.py`).
- The deploy workflow always writes every override env var on the new
  revision (even when empty) so the new revision does not inherit a
  stale value from a previous deploy.

---

## Common scenarios

| Scenario | What to do |
|---|---|
| Kill-switch back to legacy for a safety incident | Workflow_dispatch with `new_tutor=off` |
| Swap one purpose to a cheaper model for cost-tuning | Workflow_dispatch with only that purpose's override filled |
| Pin a model combo permanently | Set the relevant repo variables; every push to `main` deploys with them |
| Roll back a bad deploy | Workflow_dispatch with every override blank (returns to DB-active) |
| Test a new Gemini Flash on the conformance classifier | Workflow_dispatch with `conformance_classifier_model_override=google/gemini-3-flash-preview` |
