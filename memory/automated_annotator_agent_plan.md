# Automated Annotator Agent — Plan (2026-05-13)

## Problem

Iteration on the tutor and content-gen pipelines is bottlenecked by **human annotation**. Edward labels 3 items at a time; benchmark scoring lags every prompt change by days. The simulator plan (`memory/llm_student_simulator_plan.md`) generates synthetic *traffic*, but the metrics still need a human to label each turn against the 30-label rubric.

We want to close the loop: **an LLM agent annotates the synthetic traffic the same way a teacher would, via the live dashboard UI in a real browser** — so the metric is computed exactly the way a human admin would compute it (clicking through, filling forms, reading the rendered Run-detail page), and then surfaces those metrics back to the developer (Edward, future Claude) on every push.

The hard constraint Edward set: the agent **interacts via Chrome like a real user**, not via API or DB shortcuts. Reason: the rendered UI is the source of truth that humans see; an agent that drives the UI catches template bugs (the class shipped 6× per `feedback_django_template_comments`), exercises auth/CSRF, and validates that admin metrics match what teachers will rely on.

The hard constraint Edward also set: **runs on a real server**, not just locally.

The synthesis that resolves both without tradeoff:

> **The runner *is* the server.** Each GitHub Actions workflow run boots an ephemeral Django container (built from the same `Dockerfile` production uses) plus headless Chromium + xvfb in the same runner. chrome-devtools-mcp drives that Chromium. The annotator agent (Claude via Agent SDK) talks to the dashboard like a teacher would. After the run, the container dies. No Pulumi staging stack. No prod-data pollution. The "real server" is genuinely real (same code as Azure) — it's just throwaway.

This plan composes with `memory/llm_student_simulator_plan.md` (data source) and `memory/eval_benchmark_v2_simplified.md` (label rubric the agent uses). It does NOT replace human annotation — it runs in parallel, and Edward's labels remain the ground-truth cohort the LLM agent's accuracy is measured against.

## Current state (from audit)

Citations are file:line throughout.

### CI/CD already in place

- `.github/workflows/deploy.yml` — push to `main` → Docker build → push to ACR → Container App update. The runner is `ubuntu-latest` with Docker available.
- `.github/workflows/audit_math_content.yml` — manual trigger; uses `az containerapp exec` to run a script INSIDE the live prod container. Sets the precedent for "GitHub Actions interacts with prod content," but read-only.
- `.github/workflows/regenerate_math_content.yml` — same pattern, write-side. Already authorized to mutate prod data.
- No existing workflow runs the app as an ephemeral instance inside the runner.

### Dockerfile is CI-friendly

- `Dockerfile` (multi-stage; `python:3.12-slim` base) builds an image that runs `gunicorn config.wsgi:application --bind 0.0.0.0:8000`. The same image runs in production (Azure) and would run in the runner.
- Startup CMD does `migrate && seed_gamification && backfill_progress && classify_unit_grades && seed_help_assistant_model && cp vectordb && generate_recent_updates && build_help_index && gunicorn`. ~30s cold start observed locally.

### Anthropic SDK already a dependency

- `requirements.txt:3` — `anthropic==0.79.0` is in the runtime image. The Claude Agent SDK is a separate package (`claude-agent-sdk`) — would need adding for the orchestration shell, OR we can use the bare `anthropic` SDK + a thin loop. **Recommend Agent SDK** for tool-use loop ergonomics.
- `google-genai==1.65.0` is also present. Either provider can run the annotator.

### chrome-devtools-mcp shape

- Local install path: `~/.cache/chrome-devtools-mcp/chrome-profile`. Spawns Chrome + chromium binary; talks via Chrome DevTools Protocol.
- Today the user runs Claude Code CLI and chrome-mcp is registered as an MCP server in `~/.claude.json`. In CI we'd need a different harness because the Claude Code CLI is not the right shape for headless automation.
- Linux container needs: chromium, xvfb (or `--headless=new` mode), the `@modelcontextprotocol/chrome-devtools-mcp` npm package, node 20+. ~200 MB of additional layers — acceptable for a CI image.

### Local browser verification already a project rule

- `auto-memory/feedback_chrome_devtools_default_verification.md` (saved this session) commits Edward to chrome-mcp as the default UI verification path. This plan is the natural extension: same tool, same loop, automated.

### No agent-driven UI test exists today

- `git grep` for `mcp__chrome-devtools` in `.github/`, `scripts/`, and source: 0 hits. The MCP usage lives in user-driven Claude Code sessions, never in automation.

### Existing benchmark surface the agent will drive

- Annotation form: `apps/benchmark/views.py:244` `benchmark_annotate(request, item_id)`. Locked to `system_variant='production_v1'`, `annotator_role='human'` for human use. Has an `'llm_judge'` role choice in `BenchmarkAnnotation.Annotator` enum but no current UI for it — this plan adds the agent-as-llm_judge persona.
- Score-now: `/dashboard/benchmark/scores/run-now/` — POST creates a `BenchmarkRun` from existing annotations (just shipped in `73b643a`).
- Run detail: `/dashboard/benchmark/scores/<id>/` — renders pass rate, slices, failure categories.

## Target design

### Components

```
.github/workflows/
└── annotator_agent.yml          # workflow definition

ops/annotator_agent/
├── README.md
├── Dockerfile.runner            # Django+chromium+node multi-stage; one image to start the runner instance
├── docker-compose.yml           # boots django + chromium-side-by-side for dev parity
├── orchestrator.py              # Python entrypoint — boots django, seeds data, launches agent
├── agent_prompt.md              # the system prompt the LLM annotator receives
├── seed_simulator_data.py       # invokes `simulate_session` (from llm_student_simulator_plan)
├── publish_metrics.py           # post run summary to PR / commit / Slack
└── tests/
    └── test_orchestrator.py
```

Plus one tiny Django change:

- `apps/benchmark/views.py` — `benchmark_annotate` already supports `annotator_role` field; verify the form actually submits it (today it's locked to `'human'` per `views.py:252`). Add `annotator_role` as an honored POST param when the requesting user has a flag (`is_agent`) on their User profile, or scoped by a header (`X-Annotator-Role: llm_judge`). Detail in Phase 2 below.

### Workflow shape (annotator_agent.yml)

Triggered by:
- `workflow_dispatch` (manual button — Edward can run it any time)
- `pull_request` opened/synchronize (every PR gets a metric snapshot)
- `schedule: cron('0 6 * * *')` (nightly drift tracker)

Steps inside the runner:
1. Checkout repo at the PR/main commit.
2. Build the runner image (Django + chromium + node + chrome-devtools-mcp). Cached via `actions/cache` keyed on Dockerfile.runner hash.
3. `docker compose up -d` — boots django on `:8000`, chromium with `--remote-debugging-port=9222`. Waits for `/healthz`.
4. Seed data: `python ops/annotator_agent/seed_simulator_data.py --personas struggler,average,capable --lessons 5,7,11 --turns-per 10`. Uses the simulator from `memory/llm_student_simulator_plan.md`. ~50 SessionTurns produced.
5. Run benchmark sampler against the seeded data: `python manage.py sample_benchmark --count 50 --include-synthetic`. Produces 50 BenchmarkItems.
6. **Run the annotator agent** (`python ops/annotator_agent/orchestrator.py`):
   - Spins up the Claude Agent SDK with chrome-devtools-mcp registered as a tool.
   - System prompt: `agent_prompt.md` — describes the rubric, the dashboard layout, the legend, when to mark `passes=true`, etc. ~3000 tokens. Cached via prompt caching.
   - The agent's first tool call: `mcp__chrome-devtools__new_page url=http://localhost:8000/admin/login/`.
   - Logs in (creds injected as env var; ephemeral).
   - Walks `/dashboard/benchmark/` → for each unannotated item → `take_snapshot` → reasons about labels → `fill` → `click submit`.
   - When the queue is empty, navigates to `/dashboard/benchmark/scores/`, fills the notes field with `"agent run @ ${commit_sha}"`, clicks "Score now".
   - Reads pass rate + slice breakdown off the rendered run-detail page via `take_snapshot`.
   - Returns a structured JSON: `{"pass_rate": ..., "by_subject": {...}, "by_failure_category": {...}, "items_annotated": 50}`.
7. Post the JSON as a workflow run summary + PR comment.
8. (Optional, Phase 5) If pass rate dropped vs `main`, open a draft PR proposing the previous prompt as a rollback.

### Why each constraint resolves without tradeoff

**"Real server, not laptop":** The runner is real; same Docker image as Azure; same gunicorn; same migrations. The only difference from prod is the data (synthetic) and the lifetime (one workflow run).

**"Like a user, via Chrome":** The agent literally types into form fields and clicks buttons in a real Chromium process. No `client.post()`. No `BenchmarkAnnotation.objects.create()`. The annotation lands the same way Edward's would.

**"No prod data pollution":** The Django container is ephemeral. No connection to Azure Postgres. The vectordb is fresh. Real teacher annotations are untouched.

**"No staging infra to maintain":** No new Container App, no new Postgres, no Pulumi changes. The runner image is the staging.

**"Self-iteration":** Phase 5 adds a "propose-fix" loop where the agent can open a draft PR with a prompt diff that the next workflow run scores. The diff is reviewed by Edward before merge — automation proposes, human disposes.

**"Metrics integrity":** Pass rate is read off the rendered page. If the template breaks, the agent's snapshot includes visible Django comment text, the agent reports "I can't find the pass-rate value," and the workflow fails loudly.

### Annotator agent — key design choices

The agent's system prompt (`agent_prompt.md`) is the load-bearing part. Three rules:

1. **Cite the rubric.** Every label decision references which rule from `memory/eval_benchmark_v2_simplified.md` triggered it. If the agent can't cite, it can't tag. This forces grounding and gives Edward auditable rationale per-item.

2. **Defer when uncertain.** Confidence threshold per label. Below 0.6 confidence, the agent picks `'safety_concern' = false` and writes `rationale: "low confidence — defer to human"`. These items count as `failed` for run pass-rate (so the agent can't pad its score by skipping hard cases), but Edward can filter for them later.

3. **Match the auto-populated set, then add human-judgment labels.** The agent reads the `production.pipeline_trace.judge_outputs` from the snapshot (already auto-populated for 12 of 30 labels per `eval_benchmark_v2_simplified.md`). It adds the 4 pure-human-judgment labels (`LEAKS_ANSWER`, `IGNORES_STUDENT`, `OFF_TOPIC`, `REPEATS`) and authors `expected_labels`. This minimizes the surface where the agent could go wrong — it's only authoring the smallest layer.

### Cost envelope (per workflow run)

Rough order-of-magnitude for one run with 50 items:

| Item | Cost |
|---|---|
| GitHub Actions (Linux, 8 min @ $0.008/min) | $0.06 |
| Anthropic API — agent reasoning (~50 items × ~3K input + 1K output × Sonnet 4.6) | $0.45 |
| Synthetic-student traffic (Gemini 2.5 Flash, ~150 turns) | $0.15 |
| Tutor calls (Opus 4.7 — same lesson runs the real model) | $4.50 |
| **Total** | **~$5.20 per run** |

Tutor cost dominates. Hosted-runner minutes are negligible. **The cost-of-iteration ceiling is the tutor LLM, not the annotator.**

Mitigation: cache lesson generations between runs (the lessons themselves don't regenerate every run), and use a smaller item count (10–20) for per-PR runs; only nightly does the full 50.

## Out of scope (explicitly deferred)

These are NOT v1. Calling them out so they don't sneak in:

1. **Driving the live Azure URL.** The "ephemeral container in the runner" pattern explicitly avoids this. If we ever need to validate prod itself, that's a separate workflow with read-only chrome-mcp and no annotation submission.
2. **Replacing human annotation.** The LLM-judge cohort runs in parallel; pass rate computed separately. `BenchmarkRun.annotator_role` already discriminates `human` vs `llm_judge`. Edward keeps labelling.
3. **Auto-merge of agent-proposed prompt PRs.** Every prompt change is human-reviewed. The agent labels the PR `agent-proposed-improvement` and assigns Edward; that's it.
4. **Vision-only / pure-screenshot agent.** chrome-devtools-mcp's `take_snapshot` is a structured a11y tree — keeps the agent grounded in DOM elements with stable selectors. Pure-screenshot vision-loop is more robust to template changes but ~5× more expensive and slower; revisit only if DOM stability becomes a problem.
5. **Multi-tenant agent annotators.** One agent persona for v1 (Sonnet 4.6, conservative). v2 could run multiple agents (different models, different temperatures) and report inter-agent agreement as a quality signal.
6. **Agent-driven content review** (the content-gen plan from `memory/content_generation_benchmark_plan.md`). Defer until the tutor-side annotator is shipping clean. Same architectural shape extends naturally.
7. **Slack/email notification of run results.** Workflow run summary + PR comment is enough for v1. Slack webhook is a 30-min add-on.
8. **Cost-budget enforcement at the agent level.** The cost ceiling lives in the workflow YAML (`timeout-minutes: 30`) and the simulator's `--max-cost-usd` flag. The agent itself doesn't track cost.

## Phased delivery

Each phase ships value standalone. Stop after any phase if signal not worth next.

| Phase | Goal | Days | Key files | Success metric | Risk |
|---|---|---:|---|---|---|
| **1. Runner image + django+chrome boot** | One workflow run boots Django on :8000 + chromium on :9222 in the same runner; healthcheck passes; agent can `take_snapshot` of the login page. | 3 | `ops/annotator_agent/Dockerfile.runner`, `docker-compose.yml`, `.github/workflows/annotator_agent.yml` (skeleton), test workflow logs | Manual `workflow_dispatch` produces a snapshot of `/admin/login/` in artifacts | Image size — chromium adds ~150 MB. **Mitigate:** use `chromium-headless-shell` not full chrome; cache layers via `actions/cache`. |
| **2. Agent skeleton — login + walk + read** | Agent logs into the dashboard, navigates to one BenchmarkItem, reads its content, navigates to runs page. **No annotation yet** — read-only loop end to end. | 4 | `ops/annotator_agent/orchestrator.py`, `agent_prompt.md` (initial), tests against seeded fixture data | Workflow run posts agent's narration of "what I saw on each page" as artifact; matches expected page structure | Agent loops/gets confused on real DOM. **Mitigate:** strict step budget (max 50 tool calls per item); abort if same page visited 3× in a row. |
| **3. Annotation submission + score-now** | Agent fills annotation form, submits, then clicks Score now and reads pass rate. POST handler honors `annotator_role='llm_judge'`. | 4 | `apps/benchmark/views.py` (loosen role-locking), agent prompt extended with rubric, `ops/annotator_agent/seed_simulator_data.py` | One full run produces N annotations + 1 BenchmarkRun + posts pass rate to workflow summary | Agent annotates wrong (low quality). **Mitigate:** Phase 4's agreement metric quantifies it; until then, all agent runs tagged `annotator_role='llm_judge'` so they don't pollute Edward's `human` cohort. |
| **4. Agreement vs Edward's human cohort** | Run scoring once over Edward's labels and once over the agent's labels for the SAME items; compute Cohen's kappa per label + overall agreement. Surface in the run-detail page. | 3 | `apps/benchmark/scoring.py` (cross-check_annotations already exists), `templates/benchmark/run_detail.html` (agreement panel — already structured) | After 30 items annotated by both, agreement panel renders kappa values; agent vs human pass-rate diff < 10% | Persistent label-class disagreement (e.g., agent never fires `LEAKS_ANSWER`). **Mitigate:** Phase 4 deliverable IS measuring the gap; v2 iterates the prompt to close it. |
| **5. Agent-proposed prompt PRs** *(conditional)* | When pass rate drops vs `main`, agent opens a draft PR with a 1-line prompt rollback or a textual diff suggestion. Human reviews. | 6 | `ops/annotator_agent/propose_fix.py`, GitHub PR via `actions/github-script`, label workflow | One real prompt regression triggers a draft PR with a diff that Edward judges sensible | Garbage PRs. **Mitigate:** label `agent-proposed`; require human review; no auto-merge ever. |
| **6. Content-eval agent extension** *(conditional, depends on `content_generation_benchmark_plan.md` Phase 1)* | Same agent shape, different rubric, different surface — agent reviews generated content via chrome-mcp. | 4 | Reuse runner image; new agent prompt for content eval; new orchestrator entry point | Workflow can run either `--mode tutor` or `--mode content` and emit appropriate metrics | Same as Phase 3. |

**Total Phases 1–4: ~14 focused days. Phases 5–6 are conditional on Phases 1–4 producing reliable, trusted metrics.**

## Testing

| Phase | New tests |
|---|---|
| 1 | `test_runner_boot.sh` — workflow self-test: `docker compose up`, curl `/healthz`, `curl http://localhost:9222/json/version` (Chrome DevTools endpoint). Asserts both up within 60s. |
| 2 | Mock the LLM agent (replay canned tool calls); assert orchestrator wires snapshot output → next tool call correctly. Run against fixture Django data. |
| 3 | `test_agent_role_lock.py` — non-staff users cannot POST `annotator_role='llm_judge'`; agent's bot user can. `test_full_run.py` — boot, seed, agent runs, BenchmarkRun row created with correct counts. |
| 4 | `test_agreement_calc.py` — given a fixed pair of label sets (same items, agent vs human), agreement panel computes the right kappa. Mirrors `test_scoring.py::test_agreement_overlap_only`. |
| 5 | `test_propose_fix.py` — given a regression scenario (pass rate dropped 20%), propose_fix opens a PR with the right title format and labels (no actual GitHub call — mock). |

Per `auto-memory/feedback_chrome_devtools_default_verification.md`: agent run-detail page rendering must be browser-verified locally before any UI change ships. The agent's view of that page IS the metric, so a template bug is a metric bug.

Per `feedback_concurrency_testing_patterns.md`: orchestrator launches the agent in a single thread; no parallelism in v1. If we ever parallelize per-item annotation, mocked LLM calls need a 50ms sleep to surface races.

## Composition with related plans

- **`memory/llm_student_simulator_plan.md`** — generates the synthetic data this agent annotates. The simulator runs in Phase 3's `seed_simulator_data.py`. If the simulator isn't built yet, Phase 1 can run against a fixture SQL dump committed to the repo as a placeholder.
- **`memory/content_generation_benchmark_plan.md`** — Phase 6 extends this agent shape to content review. Same runner image, different agent prompt.
- **`memory/eval_benchmark_v2_simplified.md`** — the rubric the agent grounds against. The 30 labels live in `apps/benchmark/labels.py`; agent prompt cites by name.
- **`memory/agentic_platform_architecture_plan.md`** — Phase 1 trace logging would give per-run cost / latency observability. Independent shippable but synergistic.
- **`auto-memory/feedback_chrome_devtools_default_verification.md`** — codifies the "via chrome like a user" principle this whole plan is built on. The plan IS the operationalization of that memory.
- **`cicd-expert` skill** — consult during Phase 1 for runner image patterns + caching strategies.
- **`azure-cloud-expert` skill** — NOT relevant to v1 (we're not touching Azure). Becomes relevant if Phase 5 grows into "agent runs against staging Container App."

## Open questions

Resolve before Phase 1 starts:

1. **Agent model: Sonnet 4.6 or Haiku 4.5?** **Recommend: Sonnet 4.6.** Reason: per-item reasoning quality matters more than per-item cost. Haiku tends to over-fire safety labels and miss subtle pedagogy issues. Sonnet at $3/$15 per 1M is fine at 50 items/run.

2. **Agent role: dedicated `agent-bot` Django user, or anonymous staff token?** **Recommend: dedicated user.** Reason: every annotation is FK'd to `request.user`. Having `agent-bot@aitutor.dev` makes filtering trivial in admin. One-off `Lesson.fixture` migration creates the user.

3. **Trigger frequency.** Every PR? Only on prompt-pack-touching PRs? Nightly? **Recommend: nightly + manual-only on PRs initially.** Reason: ~$5/run, every PR doubles to $10/PR if both PR-open and merge trigger. Edward triggers manually when he wants signal during a prompt iteration; nightly catches drift.

4. **Cohort separation in scoring.** Should agent annotations and human annotations score INTO THE SAME `BenchmarkRun` (cross-checked) or SEPARATE runs? **Recommend: separate runs by `annotator_role`.** The scoring function already accepts a `cross_check_annotations` arg (`apps/benchmark/scoring.py::compute_metrics`); use it for the agreement panel. Pass rates stay distinct so we don't confuse "is the tutor better" with "do humans and the agent agree."

5. **What's the fallback if chrome-mcp fails mid-run?** **Recommend: hard-fail the workflow with the artifact + screenshot.** Reason: silent partial-completion would corrupt metrics. Better to have a red workflow than a green-but-wrong one.

6. **Agent-proposed PR scope (Phase 5).** Prompt-pack diffs only, or any code change? **Recommend: prompt-pack only for v1.** Reason: prompt diffs are recoverable (one-line revert); code diffs aren't. Expand only after we trust the agent's suggestions.

7. **Where do agent's per-item rationale + chain-of-thought get stored?** **Recommend: `BenchmarkAnnotation.rationale` + `BenchmarkAnnotation.notes` extended.** The rationale field already exists; agent writes its label-by-label justification there. Notes (~5 KB each × 50 items = ~250 KB/run; negligible).

## Risks

1. **Agent quality cliff.** If Sonnet 4.6 turns out to disagree with Edward >40% of the time, the metric is noise. **Mitigate:** Phase 4 IS measuring this; if agreement is poor, iterate the prompt before Phase 5. Don't ship Phase 5 (auto-PRs) until kappa > 0.6 on the labels Edward cares about.

2. **DOM drift breaks the agent.** Template changes to the annotation form silently break agent submission. **Mitigate:** stable `data-testid` attributes on the form fields agent interacts with. Phase 2 includes adding these in `templates/benchmark/annotate.html` (small, additive).

3. **chrome-mcp on Linux container — unproven for this codebase.** Local Mac usage works; CI Linux is new ground. **Mitigate:** Phase 1's success metric IS the boot test. If it doesn't work, we discover it cheap.

4. **Cost runaway.** A pathological agent loop could burn $50 in one run. **Mitigate:** workflow `timeout-minutes: 30`; agent has a max-tool-calls budget per item; nightly cap of 1 run/day enforced by cron + concurrency group.

5. **Agent over-trusts auto-populated labels.** If `judge_outputs` is wrong (a judge bug), agent rubber-stamps the wrong labels. **Mitigate:** agent prompt explicitly instructs "verify auto-populated labels against the actual response text; flag disagreement as a label override with rationale."

6. **Synthetic-data distribution skew makes the metric meaningless.** If the simulator mostly produces struggling-student turns, the agent's pass rate doesn't predict real-student pass rate. **Mitigate:** the simulator plan stratifies by persona; the run summary breaks pass rate down by persona. Pooled number is supplementary, not primary.

7. **Production prompts read from `apps/llm/prompts/` — agent has the same access as Claude in dev.** No actual risk (these are not secrets) but worth noting for any future Phase 6 where the agent writes prompts.

## Next step

Build Phase 1 in isolation: write `ops/annotator_agent/Dockerfile.runner`, `docker-compose.yml`, and a minimal `annotator_agent.yml` workflow that just boots both services and posts a screenshot of the login page as an artifact. No agent yet. If chrome boots in the runner and Django serves a login page, the load-bearing infra question is answered.
