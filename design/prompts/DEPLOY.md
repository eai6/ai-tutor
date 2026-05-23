# Deploy Guide — Picking Prompt Variant & Tutor Model

When merging a PR to `main`, you can pick which **tutor prompt** and which
**tutor model** the freshly-deployed revision will use, without editing any
code. The deploy workflow reads two environment variables and writes them
onto the new Container App revision:

| Knob | Env var | What it controls |
|---|---|---|
| **Prompt variant** | `TUTOR_PROMPT_VARIANT` | Which prompt the tutor uses on every turn |
| **Tutor model** | `TUTOR_MODEL_OVERRIDE` | Which LLM the tutoring purpose calls |

This guide shows the two ways to set them.

---

## TL;DR

1. **For the next push to `main`** — set the repo variables `TUTOR_PROMPT_VARIANT` and `TUTOR_MODEL_OVERRIDE` once in **Settings → Secrets and variables → Actions → Variables**. Every subsequent merge to `main` deploys with those values.
2. **For an ad-hoc deploy from any commit on `main`** (e.g. rollback, hotfix, prompt-variant flip without a code change) — go to **Actions → Deploy → Run workflow** and pick from the dropdowns.

---

## Valid values

### `TUTOR_PROMPT_VARIANT`

| Value | Prompt | When to use |
|---|---|---|
| `baseline` *(default)* | Current per-provider production prompt | Safe default. Preserves behaviour before any prompt-iteration work landed. Use when you want zero change from the previously-deployed prompt. |
| `v6` | Unified rewrite, highest measured composite quality | Recommended for production once you've decided to ship the prompt improvements. Highest measured score (Sonnet 4 = 3.27 / 5 vs baseline 2.88). |
| `v7` | Branch-template restructure (smallest, cleanest) | Recommended for production if you prefer the cleaner state-machine prompt. Same quality as v6 within noise; easier to maintain. |

Empty / unset / `v3` are all aliases for `baseline`. An unknown value (typo) logs a warning and falls through to baseline — your deploy will not break, but the variant you wanted won't take effect, so verify after.

### `TUTOR_MODEL_OVERRIDE`

Format: `provider/model_name` (e.g. `anthropic/claude-sonnet-4-6`).
Empty / unset = use whatever the database has marked as the active config for the `tutoring` purpose (current production behaviour).

| Value | Provider | Notes |
|---|---|---|
| *(empty)* *(default)* | Whatever the DB says | Use the active `ModelConfig` row for `purpose=tutoring`. |
| `anthropic/claude-sonnet-4-20250514` | Anthropic | Standard tier (Sonnet 4). Best measured quality on v6. |
| `anthropic/claude-sonnet-4-6` | Anthropic | Standard tier (Sonnet 4.6, newer). Same pricing as Sonnet 4. |
| `anthropic/claude-opus-4-7` | Anthropic | Premium tier. Use only for high-stakes lessons; ~5× cost. |
| `google/gemini-3-flash-preview` | Google | Cheapest option (~5% the cost of Claude). Slower per turn. |
| `google/gemini-3.1-pro-preview` | Google | Google's premium tier. Comparable quality to Sonnet 4.6 in our testing. |

The override applies **only** to `purpose=tutoring`. Other purposes (`judge`, `regen`, `generation`, etc.) continue to read from the DB-active config — a typo in `TUTOR_MODEL_OVERRIDE` cannot accidentally retarget the post-response judge or content generator.

---

## Method 1 — Set repo variables (steady-state, applies to every push to main)

Use this when you've decided on a prompt + model combination that should ship on every merge until further notice.

1. Go to the repo on GitHub.
2. Click **Settings → Secrets and variables → Actions → Variables**.
3. Click **New repository variable**, name it `TUTOR_PROMPT_VARIANT`, value `v6` (or `v7` or `baseline`). Click **Add variable**.
4. Repeat for `TUTOR_MODEL_OVERRIDE` — value either empty (uses DB-active), or a `provider/model_name` from the table above.
5. Merge the PR. The deploy workflow reads these values automatically.
6. **Verify** (see "How to verify" below).

To change later, edit the variable values in the same place. The change takes effect on the next push to `main`.

---

## Method 2 — Use workflow_dispatch (ad-hoc, one-off)

Use this for a rollback, a hotfix, or to deploy the current `main` with a different prompt/model combo without changing repo variables.

1. Go to the repo on GitHub.
2. Click **Actions → Deploy** (in the left sidebar).
3. Click **Run workflow** (top-right).
4. Pick:
   - **Tutor prompt variant**: dropdown — `baseline` / `v6` / `v7`
   - **Tutor model override**: dropdown — empty or a `provider/model_name`
   - Branch: leave on `main` (or pick another).
5. Click **Run workflow** again to confirm.
6. **Verify** (see below).

This deploys the current commit on the selected branch, with the env vars set per your dropdown choices. It does NOT change the repo variables — the next push to `main` reverts to whatever the repo variables say.

---

## How to verify the deploy worked

### 1. Confirm the workflow set the env vars

In the Actions run log, look for the step **"Resolve deploy-time config"**. It prints a notice line at the top of the run:

```
::notice title=Deploy config::TUTOR_PROMPT_VARIANT=v6  TUTOR_MODEL_OVERRIDE=anthropic/claude-sonnet-4-6
```

These should match what you intended.

### 2. Confirm the Container App revision has the env vars

After the deploy step completes, run from your terminal:

```bash
az account set --subscription "Pixel Design Labs LLC"
az containerapp show \
  --name $APP_NAME \
  --resource-group $RG_NAME \
  --query 'properties.template.containers[0].env[?name==`TUTOR_PROMPT_VARIANT` || name==`TUTOR_MODEL_OVERRIDE`]'
```

Should output something like:

```json
[
  { "name": "TUTOR_PROMPT_VARIANT", "value": "v6" },
  { "name": "TUTOR_MODEL_OVERRIDE", "value": "anthropic/claude-sonnet-4-6" }
]
```

### 3. Confirm the running tutor is using them

Hit the running app and watch the logs:

```bash
az containerapp logs show \
  --name $APP_NAME --resource-group $RG_NAME --follow
```

Then drive one tutor session via the dashboard. You should see:

- For a non-baseline prompt variant: no `[prompts] unknown TUTOR_PROMPT_VARIANT=` warning (one of these means the variant name didn't match anything; baseline is being served silently).
- For a model override: `[QuestionTool] llm_call: ... model=<your-override-model>` lines in the tutor turn log.

If the override didn't take effect, see "Troubleshooting" below.

---

## Rollback

If the deployed combination misbehaves, you have three ways to revert, in order of speed:

### Fastest: re-deploy with workflow_dispatch + baseline

1. Actions → Deploy → Run workflow
2. Set `Tutor prompt variant` = `baseline` and `Tutor model override` = (empty).
3. Run. ~4 minute deploy.

This pins the new revision to the original production behaviour. Repo variables are NOT changed; the next push to `main` would re-apply them.

### Medium: edit the repo variables, then re-run the last deploy

1. Settings → Variables → set `TUTOR_PROMPT_VARIANT` back to `baseline`, clear `TUTOR_MODEL_OVERRIDE`.
2. Actions → most recent Deploy run → **Re-run all jobs**.

Same effect as above, but persists across future merges.

### Manual (if Actions itself is down): set env vars directly on the Container App

```bash
az containerapp update \
  --name $APP_NAME \
  --resource-group $RG_NAME \
  --set-env-vars TUTOR_PROMPT_VARIANT=baseline TUTOR_MODEL_OVERRIDE=
```

This creates a new revision in seconds. Use only when GitHub Actions itself can't run.

---

## Troubleshooting

### "I set the variant but the tutor's behaviour didn't change"

- Check the Container App env vars (verification step 2 above) — did the env var actually land on the new revision?
- Check the app logs for `[prompts] unknown TUTOR_PROMPT_VARIANT=...` — this means you typed an invalid value (typo); the app fell back to baseline silently.
- Confirm the revision is healthy: `az containerapp revision list ...` — the new revision should show `Active = True`.

### "The model override seems to have been ignored"

- Confirm you used a valid `provider/model_name` from the table above.
- Check app logs for `[ModelConfig] TUTOR_MODEL_OVERRIDE=... did not resolve` — this means the provider's API key env var (e.g. `GOOGLE_API_KEY`) is missing from the Container App's env. The override falls back to the DB-active config in this case.
- Verify the override is scoped correctly: it ONLY affects `purpose=tutoring`. If you're looking at judge / generation / regen logs, those still use the DB-active config by design.

### "I want to leave the prompt variant alone but change only the model" (or vice versa)

That's fine. The two knobs are independent. Set one, leave the other empty. Empty `TUTOR_PROMPT_VARIANT` = baseline. Empty `TUTOR_MODEL_OVERRIDE` = DB-active model.

### "My PR needs the variant to change AT THE SAME TIME as the code change"

Two options:

1. **Recommended**: merge the code PR first (with `TUTOR_PROMPT_VARIANT` = `baseline` / unchanged), confirm green, then flip the variant via workflow_dispatch or repo variable.
2. **Coupled**: set the repo variable to the target variant BEFORE merging. The post-merge auto-deploy will pick up both the code change and the new env var in the same revision.

Option 1 is safer because it lets you isolate "did the code change break anything?" from "did the variant flip break anything?".

---

## How this works under the hood

(Skip this section if you don't need the implementation detail.)

- **Prompt**: `apps/tutoring/prompts/variants.py` holds `V6_TUTOR_SYSTEM_PROMPT_TEMPLATE` and `V7_TUTOR_SYSTEM_PROMPT_TEMPLATE` as Python constants. `get_active_variant_template(baseline)` reads `TUTOR_PROMPT_VARIANT` at each tutor turn and returns either a variant template or the unchanged baseline. The Anthropic and Gemini prompt builders call this helper just before the per-turn injection.
- **Model**: `apps/llm/models.py::ModelConfig.get_for('tutoring')` checks `TUTOR_MODEL_OVERRIDE` first; if set + valid, it returns a runtime config built via `resolve_runtime(provider, model_name)`. Other purposes never consult the env var.
- **Workflow**: `.github/workflows/deploy.yml` resolves both env vars from (1) `workflow_dispatch` inputs, (2) repo variables, (3) defaults. The deploy step always writes both env vars onto the new revision via `az containerapp update --set-env-vars` so a previous revision's stale value cannot leak forward.

Tests covering all four code paths live in `apps/tutoring/tests/test_prompt_variant_selection.py`.

---

## Quick reference card

| I want to... | Do this |
|---|---|
| Keep current production prompt + model (zero change) | Repo variables empty (or both set to `baseline` / empty) |
| Roll out the v6 prompt to all classrooms | Set `TUTOR_PROMPT_VARIANT=v6` (repo variable). Merge. |
| A/B v6 vs v7 in production | Set `v6` for two weeks, switch to `v7` via workflow_dispatch, compare teacher feedback |
| Test v7 on one revision only | Workflow_dispatch with `v7`; repo variable unchanged |
| Switch tutor to Sonnet 4.6 | Set `TUTOR_MODEL_OVERRIDE=anthropic/claude-sonnet-4-6` (repo variable). Merge. |
| Switch tutor to Gemini Flash to cut cost | Set `TUTOR_MODEL_OVERRIDE=google/gemini-3-flash-preview` (repo variable) |
| Roll back a bad deploy | Workflow_dispatch with `baseline` + empty model override |
| Pin variant + model permanently | Both repo variables set; every push to `main` deploys with them |
