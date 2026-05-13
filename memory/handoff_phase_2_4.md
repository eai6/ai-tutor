# Handoff brief — picking up after Phase 2.4 (2026-05-13, very long session)

Hi future Claude. This brief is the pointer; durable plans live in `memory/` and `CLAUDE.md`. Read this top-to-bottom before touching anything. The previous handoff is `memory/handoff_phase_2_3.md` — that one's still useful for the remediation/probe context, but everything below supersedes the "in-flight" sections in it.

---

## TL;DR — where you are

**Everything from 2_3's "in-flight" list shipped + a lot more.** Prod (`origin/main`) is at `2a83835`. 16 commits landed since `0f6db96` (the last commit before that handoff). The pilot now has:

1. A full LLM-as-student simulator that drives real `respond()` end to end
2. An automated annotator agent (Claude+chrome-mcp) that labels via the rendered dashboard
3. A GitHub Actions runner workflow for CI annotation (smoke-validated; full-mode never run end-to-end yet)
4. Per-blank fill-in-blank grading + per-pair matching grading, both judging in whole-question context
5. Multi-select failure categories, sampler narrow filters (subject/since/until), run-detail subject-filter pills
6. **A new production domain — `https://www.seselai.sc/`** alongside the old `ai-tutor.wbg.edwardamoah.com`

Working tree is clean (no uncommitted changes). The only "untracked" stuff is autosaved noise (`.DS_Store`, `db.sqlite3`, `.claude/scheduled_tasks.lock`, `ops/annotator_agent/metrics_history.jsonl`, three pre-existing untracked memory files from Phase 2_2/2_3 era — leave them).

---

## What shipped this session (chronological)

| Commit | Theme |
|---|---|
| `73b643a` | Benchmark UI: on-demand scoring button + annotation counts |
| `16e8537` | Three plans (simulator, content-eval, automated annotator) |
| `f6ceefa` | Simulator: STRUGGLER persona + driver + sampler integration (Phases 1, 2, 4 of `llm_student_simulator_plan.md`) |
| `f594c04` | Annotator agent v0: Claude Agent SDK + chrome-devtools-mcp loop validated locally |
| `b405cd9` | Annotator role override (agent annotations land as `llm_judge`, not `human`) |
| `1620df8` | Containerization (Dockerfiles, docker-compose) — unverified at commit time, disk was full |
| `5a39c97` + `30c9e35` + `d8fc471` | CI workflow: smoke + full mode + screenshot + git-based metrics history |
| `0531203` | Multi-select `failure_categories` + `run_full_pipeline` local command + lesson_angles fixture |
| `18f95f5` | Per-blank fill-in-blank grading + tutor-focus directive + per-input frontend coloring |
| `863f19a` | Sampler: narrow filters by `lesson_id` / `since` / `until` |
| `fbe97c2` | Reset exit-modal submit button on second open (mid-remediation re-quiz bug) |
| `4d62653` | Per-pair matching grading + sentence-context fill-in-blank prompt |
| `d74e542` | Drop `lesson_id` input from sampler form (user feedback) |
| `0fb013a` | Matching grader: judge each pair in whole-question context |
| `43c3981` | Subject filter actually narrows + run-detail UI cleanup (single slice, multi-tag clarifier) |
| `1451e2a` + `7f2023b` | Pulumi: support multiple custom domains + fix straggler |
| `2a83835` | Run-detail subject filter pills |

All on `origin/main`, all deployed to Azure.

---

## What's live in prod

**URLs:**
- `https://www.seselai.sc/` (NEW — primary canonical going forward)
- `https://ai-tutor.wbg.edwardamoah.com/` (OLD — kept during transition)
- `https://aitutor-pixel-app.niceground-67d5237f.centralus.azurecontainerapps.io` (Azure-generated, always works)

**Behavioral changes the user might raise:**
- Exit-ticket review modal now colours blanks/pairs INDIVIDUALLY (not all-red when one is wrong)
- Fill-in-blank grader is way more lenient (judges sentence meaning, not blank-string match)
- Submit button on the second exit ticket (mid-remediation) is no longer missing
- Annotation form has CHECKBOXES (not a dropdown) for failure categories — multiple categories per item allowed
- Run-detail page hides `by_stratum` / `by_eval_layer` / `by_history` slices; only `by_subject` shown
- Run-detail page has subject-filter pills above the summary
- Sampler form has `subject` / `since` / `until` filters; `lesson_id` was removed at user request

---

## Open work (priority order)

### 1. Annotator agent CI: full mode never run end-to-end on GitHub Actions

**State:** smoke mode is GREEN (chromium+node+chrome-mcp boot inside Linux runner — load-bearing question answered). Full mode plumbing exists (`seed_ci_fixture` loads `ops/annotator_agent/fixtures/lesson_angles.json` → BenchmarkItem, agent annotates, metrics history pushed to `metrics-history` branch) but **was cancelled mid-flight** earlier this session because we caught that it was annotating fake data instead of real curriculum. After we rebuilt the fixture from real lesson 638, the user pivoted to local-pipeline iteration and never re-ran full mode in CI.

**To resume:** `gh workflow run annotator_agent.yml -f mode=full`. Watch with `gh run watch`. ~10 min, ~$1-2 Anthropic spend. Expect to find `metrics-history` branch created with the first JSONL line.

**Risk:** the workflow's `pulumi up` step doesn't exist (annotator workflow doesn't touch Pulumi); but the existing chicken-and-egg with `az containerapp hostname add` for new domains DOES NOT apply here — the workflow runs Django + chromium inside the runner, no Azure mutation.

### 2. `run_full_pipeline` local command — bump default `max_steps`

User ran `python manage.py run_full_pipeline --lesson 638 --max-turns 8 --max-items 2 --max-steps 60`. The agent hit `max_steps=60` partway through 2 items (only 1 of 2 scored). For multi-item runs, default `max_steps=100` (currently 80) is more honest. One-line change in `apps/tutoring/management/commands/run_full_pipeline.py`.

### 3. Investigate "small island nations" remediation MCQ — possible NO_AUTHORING violation

User reported: during remediation on a geography lesson, the tutor served an MCQ ("Which of the following best explains why small island nations like Seychelles depend heavily on international trade?") that wasn't in the exit-ticket they took. **Local search returned 0 matches in any ExitTicketQuestion bank.** Strong suspicion: the tutor authored a new MCQ during remediation walkthrough → `NO_AUTHORING` rule violation.

**Diagnostic path:**
- Pull the prod session via the user (need session ID)
- Check the relevant turn's `judge_outputs.rule.violations` — if `NO_AUTHORING` is there, the judges already caught it but the regen ensemble couldn't clean it within the cycle cap
- Check the turn's `metadata.bank_offered` — if `false`, no bank tool was offered, so authoring was the only path

**If confirmed authoring violation:** the fix is to ensure the bank-tool guard is enforced during remediation walkthrough specifically (not just during normal tutoring turns). Look at how `_get_next_uncovered_concept` injects the REMEDIATION FOCUS directive — does it also inject the bank-tool requirement?

### 4. More math lessons in local DB

`run_full_pipeline` rotates through `[638 math, 543 geography, 546 geography]` because lesson 638 is the only math lesson in local SQLite. To rotate properly:
- Generate more locally: `python manage.py generate_lesson_content --lesson-id <new>` (~5 min, ~$1 each)
- OR pull math curriculum dump from Azure prod into local SQLite

### 5. Content-gen benchmark — full plan exists, ZERO code shipped

`memory/content_generation_benchmark_plan.md` — parallel structure to the tutor benchmark, scores teacher edits to generated content (lesson scripts, exit-ticket questions, figures) with 16-tag legend (factual/arithmetic/safety/etc.) + severity. Plan is ~14 days for Phases 1–4. **NOT started.** Lower priority than the annotator agent track right now.

### 6. Agentic platform Phase 1 (TurnSpan logging) — not started

`memory/agentic_platform_architecture_plan.md` — adds a `TurnSpan` model with one row per LLM call / judge fire / regen decision. Foundation for cost-per-turn analytics + agent decomposition decisions. Plan is ~30 focused days for Phases 1–5. Phase 1 alone is 4–6 days.

### 7. Optional polish that came up but wasn't requested

- Filter-by-stratum on sampling form (matches the existing list-filter UI)
- Annotator agent prompt could push further into synonym-aware annotation
- Email domain (`mail.ai-tutor.wbg.edwardamoah.com`) is still tied to the OLD primary custom domain. If the user wants email to come from `mail.seselai.sc`, that's a Pulumi config change + new ACS email-domain DNS records.

---

## Pulumi custom-domain gotcha worth recording

**Adding a new custom domain to Container Apps has a chicken-and-egg.** `pulumi up` will fail with `RequireCustomHostnameInEnvironment` when creating the ManagedCertificate — Azure wants the hostname BOUND to the Container App first, but the binding references the cert.

**Workaround:** add the hostname out-of-band BEFORE `pulumi up`:

```bash
az containerapp hostname add --hostname <newdomain> \
    --resource-group aitutor-pixel-rg \
    --name aitutor-pixel-app \
    --location centralus
```

Hostname goes in with `bindingType: Disabled` (HTTP-only). Then `pulumi up` succeeds — the cert provisions and the binding flips to `SNI_ENABLED`.

This applies to EVERY new custom domain; the existing ones are fine because Pulumi already manages their state.

---

## Files / locations to NOT touch without asking

Per CLAUDE.md "Before risky actions":
- `config/settings.py`, `Dockerfile`, `.github/workflows/`, `infra/__main__.py`, `infra/Pulumi.pixel.yaml` — load-bearing for production
- `pulumi destroy` or any destructive Azure operation
- `git push origin main` (triggers Azure deploy via deploy.yml)
- Migrations against prod (use the dump-and-test-on-copy pattern in CLAUDE.md)

`infra/` files were modified this session (multi-domain refactor) — that change is committed + applied. Future Pulumi edits still need confirmation.

---

## Decisions locked this session (don't relitigate)

These were chosen by Edward via AskUserQuestion or direct instruction. Don't propose reversing without explicit direction:

- **Failure categories are multi-select** (was single-select). Migration `benchmark.0003` backfilled existing data to one-element lists.
- **Per-blank verdicts for fill-in-blank, per-pair for matching** — frontend colors each input/select individually. Tutor's REMEDIATION FOCUS directive surfaces per-blank verdicts so it can target the specific gap.
- **Sentence-context grading** for fill-in-blank (judge meaning when student's word is substituted, not exact-string match).
- **Whole-question context grading** for matching (consider semantic equivalents on the right side; accept partially-correct matches reflecting real attributes; reject factually wrong).
- **Sampler form simplified** — `lesson_id` removed (kept in backend for `run_full_pipeline` + management command); `subject` / `since` / `until` exposed.
- **Subject filter on `/dashboard/benchmark/scores/<id>/`** uses `?subject=` query param + recompute live (not pre-stored per-subject metrics).
- **`www.seselai.sc` is the new canonical domain** — old `ai-tutor.wbg.edwardamoah.com` kept during transition. Both bound + responding.
- **Pulumi supports multiple custom domains** — `custom-domains` list config (legacy `custom-domain` string still honored, merged + deduped).
- **CI annotator full mode runs against frozen `lesson_angles.json` fixture** (not live simulator) — keeps cost predictable. The `run_full_pipeline` LOCAL command runs the simulator fresh; the CI workflow does not.

---

## Architectural state — quick scan

- Simulator's STRUGGLER persona is too capable on simple arithmetic (~30-40% wrong vs target 70%). Voice is authentic. Surfaced a real `WRONG_VERDICT` / false-reject bug in production tutor on session 20 (turns 481, 483) — bank-grade accepted student answers as wrong when they were arithmetically correct. **Investigation deferred** but worth noting.
- Annotator agent uses `?annotator_role=llm_judge&annotator_model=claude-sonnet-4-5` query string to land in the cohort separated from human annotations. The save-and-next redirect carries the override forward.
- `BenchmarkRun.metrics` schema unchanged — `{overall, slices, failure_categories, agreement?}`. `failure_categories` is now derived from MULTI-tag annotations (one annotation can contribute to multiple counters), so the sum-of-counts > failed-item-count. Run-detail page shows a clarifier line about this.
- Custom-domain plumbing in Pulumi: `custom_domains` list at top of `infra/__main__.py`, looped through `ManagedCertificate` creation (with `LEGACY_CERT_NAMES` lookup so the existing cert isn't recreated) and `CustomDomainArgs` on the ingress. CSRF env var concats all bound domains.

---

## How to verify a fresh start in 60 seconds

```bash
# 1. Both prod domains responding
curl -I https://www.seselai.sc/health/                  # → 200
curl -I https://ai-tutor.wbg.edwardamoah.com/health/    # → 200 (old)

# 2. Pulumi state matches main
cd infra && PULUMI_CONFIG_PASSPHRASE='...' pulumi preview
# Expect: no changes (clean state)

# 3. Tests pass
venv/bin/python manage.py test apps.benchmark           # 69 tests OK
venv/bin/python manage.py test apps.tutoring.tests      # 824 tests OK

# 4. Local annotator works
venv/bin/python manage.py runserver  # in one shell
venv/bin/python -m ops.annotator_agent.orchestrator \
    --base-url http://127.0.0.1:8000 --persona struggler --max-items 1
# Expect: agent labels one item via chrome-mcp, ~$0.90, ~30 steps
```

UI tests: per `auto-memory/feedback_chrome_devtools_default_verification.md`, drive the dashboard via chrome-devtools-mcp before claiming any UI change is done. Restart Chrome (not Claude Code) if MCP is stuck — see `auto-memory/feedback_chrome_devtools_mcp_recovery.md`.

---

## Where to start

If the user gives no explicit direction, the highest-leverage next move is **#1 — run annotator-agent full mode in CI end-to-end and confirm the metrics-history branch gets its first JSONL line.** That closes the loop the whole session has been building toward and tells you whether the GitHub Actions infra path is real or theoretical.

If they DO give direction: read it carefully. The user iterates fast, often surfaces a new bug mid-task, and prefers ship-fix-ship over big batches. Don't auto-commit during active iteration. Confirm before destructive or shared-state actions (push to main, pulumi up, az resource changes).
