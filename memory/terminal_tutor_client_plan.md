# Terminal tutor client — Plan (2026-07-27)

Status: **Phase 1-3 shipped 2026-07-27**, working on-box. Phase 4 (non-interactive
`--script`) outstanding. Two findings during implementation corrected the design —
see "Corrections from implementation" below.

## Problem

Development testing of the tutor currently requires a browser. On the Jetson Orin
Nano Super that is untenable: the box has 7.4 GB of *unified* CPU/GPU memory, the
local Qwen 4B needs ~4.0 GB resident, and Firefox alone was measured holding
~1.3 GB across its processes on 2026-07-27. The two do not fit. The observed
failure is not subtle — with the browser open, available memory fell to 3.9 GB and
the Ollama fit preflight (`apps/llm/client.py::_ollama_fit_preflight`) correctly
refused to load the model, so the tutor could not answer at all.

We need a way to chat with the tutor exactly as a student would, from a terminal,
driving the **real engine** rather than a mock, with a memory footprint measured in
megabytes rather than gigabytes.

## Deployment context — decision recorded 2026-07-27

**Concurrent WiFi AP+STA is dropped.** The Jetson's Realtek RTL8822CE runs NVIDIA's
out-of-tree `rtl8822ce` driver (binds `cfg80211` directly, not `mac80211`). `iw list`
reports verbatim:

```
interface combinations are not supported
```

AP mode and station mode are each supported, but **not simultaneously**. Options
considered and rejected: swapping to mainline `rtw88_8822ce` (not built for
5.15.148-tegra, and even on success pins the AP to the router's channel), and a USB
second radio (defer until there is a reason to buy hardware).

**Accepted shape:** the box is *either* a station *or* a hotspot, never both.

Long-term target — **no browser ever runs on the Jetson**:

```
  Students' phones  ))) hotspot )))  Jetson  ──> Django + local Ollama
                                       (offline, no uplink required)
```

Development target — the same engine, driven from a terminal on the box itself.
This plan covers the development half. The hotspot half is out of scope here and
needs its own plan when field deployment is scheduled.

## Current state (from audit)

The codebase already has the seam this feature needs — it does not need inventing.

**Two entry points, deliberately layered:**

- `apps/tutoring/simple_tutor/engine.py:324` — `respond(session, user_input) -> dict`.
  The engine's internal contract. Returns `{'content': str, 'tool_calls': list, ...}`.
  Documented as never raising; falls back to `_FALLBACK_REPLY` on internal failure.
- `apps/tutoring/simple_tutor/engine.py:2286` — `respond_for_view(session, user_input) -> dict`.
  A **thin adapter** over `respond()` that projects the result into the JSON shape
  the chat UI consumes: derives step display fields from `session.current_step_index`,
  extracts `is_correct` from any `record_answer` verdict, pulls `media_url` from
  `request_figure`, and handles the exit-ticket transition.
- `start(session)` at `engine.py:281` and `start_for_view` follow the same pairing.

**Who calls which:**

- Browser: `apps/tutoring/views.py:1214` calls `respond_for_view`.
- Eval harness: `evals/runner.py:341` calls `respond` (the raw engine).

That divergence matters. The eval harness has never exercised the adapter the
browser actually uses.

**Session bootstrap already exists** at `evals/runner.py:320`:
`TutorSession.objects.create(student=, lesson=, institution=, status=ACTIVE, is_synthetic=True, engine_state={...})`,
with fixtures resolved by `_eval_institution_and_user()` (`evals/runner.py:205`) against
`EVAL_INSTITUTION_PK = EVAL_USER_PK = 999001` (`evals/runner.py:61`).

**What the view layer adds that the engine does not:**
`apps/tutoring/views.py:1016` imports `ContentSafetyFilter`, `RateLimiter`,
`SafetyAuditLog`; `ContentSafetyFilter.check_content(message, context="student_input")`
runs a PII redaction pass before the engine sees the text. Plus auth/ownership
checks via `get_object_or_404(TutorSession, ..., student=request.user)`, CSRF, and
HTTP serialisation. **None of these are in the engine.** A direct-call CLI skips them.

**Engine flag:** `apps/tutoring/simple_tutor/__init__.py:34` — `is_enabled()` reads
`SIMPLE_TUTOR_ENGINE` from `os.environ` on *every call*, so it can be flipped without
a restart. simple_tutor is the default.

**Management-command precedent exists**: `generate_content.py`, `seed_sample_data.py`,
`run_eval`, `score_benchmark.py`, etc. under `apps/*/management/commands/`.

**`rich` is NOT installed.** Confirmed 2026-07-27.

## Target design

A Django management command, `python manage.py tutor_chat`, that opens a real
`TutorSession` and loops on stdin, calling the same adapter the browser calls.

```
manage.py tutor_chat --lesson 42
  │
  ├── bootstrap: resolve institution + student + lesson, create TutorSession
  ├── opening turn:  start_for_view(session)
  └── loop:          respond_for_view(session, input())
                        └── respond()  ← the real engine, real LLM, real tools
```

**Five decisions this plan commits to:**

1. **A management command, not a standalone script.** Django setup, settings
   resolution, and argument parsing come free, and it matches the ~8 existing
   commands. A standalone script would re-derive `DJANGO_SETTINGS_MODULE` and
   repo-root handling — the exact class of bug that made
   `offline_eval/seed_ollama_configs.py` unrunnable on this machine (hardcoded
   `/home/daniel/...`, fixed in commit `1da1282`).

2. **Drive `start_for_view` / `respond_for_view`, not `start` / `respond`.**
   Those adapters *are* the browser's contract. Calling the raw engine would test a
   different surface than production serves, and would silently skip the step-progress,
   `is_correct`, `media_url` and exit-ticket-transition logic. This also closes a real
   gap: nothing currently exercises `respond_for_view` outside a live browser.

3. **In-process, not HTTP.** No dev server, so no extra ~200-400 MB — the entire
   point of the exercise. The cost is explicit and documented: the safety filter,
   rate limiter, and auth checks listed above are **not** exercised. An `--http` mode
   that drives the real endpoints is a deliberate v2 (see Out of scope).

4. **Sessions are marked synthetic.** `is_synthetic=True` plus
   `engine_state={'cli_session': True}`. Without this, every dev chat pollutes
   session analytics and any future benchmark draw. The eval runner already sets
   `is_synthetic=True`; this follows it.

5. **Standard library only.** No `rich`, no `textual`, no `prompt_toolkit`. Plain
   `input()` and ANSI escapes. `rich` is not currently a dependency and adding one
   to a memory-constrained box for colour is a bad trade.

**The value-add over the browser** is visibility. The browser hides the plumbing;
this client surfaces it, behind flags:

- `--show-tools` — each `tool_calls` entry: tool name, args, verdict
- `--show-judge` — the turn's `SessionTurn.judge_outputs`
- `--show-timing` — wall-clock per turn, plus Ollama prefill/decode when present
- `--show-state` — `current_step_index`, `SessionState`, step transitions

Default output is plain conversation, so it reads like the chat UI.

## Backend changes

No changes to the engine. This is additive and read-only with respect to existing code.

| File | Change |
|---|---|
| `apps/tutoring/management/commands/tutor_chat.py` | **New.** Thin `Command.handle` — arg parsing and the I/O loop only. |
| `apps/tutoring/cli/session.py` | **New.** `bootstrap_session(lesson_id, student=None) -> TutorSession`. Holds the create-and-mark logic. |
| `apps/tutoring/cli/render.py` | **New.** Formatting for tool calls, judge output, timings. Pure functions, no I/O — unit-testable without a TTY. |
| `apps/tutoring/tests/test_tutor_chat_cli.py` | **New.** Tests the render helpers and bootstrap against fixtures; mocks the LLM so it runs in CI without a model. |

**Deliberately NOT extracting a shared session-bootstrap helper yet.** `evals/runner.py:320`
would be the second call site, and CLAUDE.md's Rule of Three says wait for the third.
Noted here so the next person to touch session creation knows the extraction is
pending, not forgotten — `offline_eval/student_sim.py` is the likely third.

**Why the logic lives in `apps/tutoring/cli/` and not in `Command.handle`:** a fat
`handle()` is untestable without a subprocess. Keeping `handle` to arg-parsing plus a
loop, with the real work in importable modules, is the same separation the codebase
already applies via the `*_for_view` adapters — transport at the edge, domain
underneath. The CLI becomes a third adapter alongside the view and the eval runner.

## Out of scope

- **The hotspot / field-deployment build.** Separate plan when scheduled.
- **`--http` mode** driving the real endpoints (would exercise safety filter, rate
  limiter, auth). Real value, but it needs the dev server running, which reintroduces
  the memory cost this plan exists to avoid. v2.
- **Exit-ticket flow.** v1 detects the transition and prints a notice, then stops.
- **Teacher / dashboard surfaces.** Student chat only.
- **Multi-session or replay/transcript-import.** One interactive session per invocation.
- **Any change to engine behaviour.** If the CLI reveals a bug, that is a separate fix.

## Phased delivery

Estimates are days of focused solo work.

| Phase | Work | Est. |
|---|---|---|
| 1 | `tutor_chat` command + `bootstrap_session`; opening turn and reply loop via `*_for_view`; plain output | 0.5 d |
| 2 | `--show-tools` / `--show-judge` / `--show-state` / `--show-timing` renderers | 0.5 d |
| 3 | Tests: render helpers, bootstrap, one mocked end-to-end turn | 0.5 d |
| 4 | Non-interactive mode (`--script file` / piped stdin) for reproducible runs and CI | 0.5 d |
| | **Total** | **~2 d** |

Phase 1 alone unblocks browser-free development on the Jetson. Phases 2-4 are
quality-of-life and can follow.

## Corrections from implementation (2026-07-27)

**1. `simple_tutor` does not run a per-turn combined judge at all.** Open question 3
below asked whether the CLI should run "the judges" and warned about doubled latency.
That premise was wrong. `run_combined_judge` / `run_unified_judge` are called only by
the legacy `apps/tutoring/conversational_tutor.py` (:2295, :8104). simple_tutor grades
through `apps/tutoring/simple_tutor/grader.py` tiers instead — deterministic, then an
embedding gate, then a verifier LLM. So:

- `--no-judge` is moot for the per-turn path and was not built.
- `SessionTurn.judge_outputs` is usually empty for simple_tutor turns; `--show-judge`
  says so explicitly rather than rendering a blank.
- The ~30 s/turn cost is the tutor's own two-call tool protocol, **not** tutor+judge.
  Measured: 89.7 s first turn (includes ~40 s cold model load), then ~19 s/turn once
  resident. Better than the 30 s the plan budgeted for.

**2. The fit guard broke multi-turn conversation, and this client found it.**
`_ollama_fit_preflight` checked projected footprint against MemAvailable without first
asking whether the model was *already loaded*. Turn 1 loaded the model; the model's own
~4 GB then made available memory look too small; every turn after was refused for
memory the model itself was holding (observed: "4.0 GB projected against 1.4 GB
available" for a model that was loaded and answering). Fixed by checking `/api/ps`
first — residency means no new allocation, so the fit question does not apply.
Deliberately uncached, since `OLLAMA_KEEP_ALIVE` expiry changes residency mid-process.

**4. The fit guard reads a SERVER setting from the CLIENT's environment.**
`_ollama_fit_preflight` sizes the KV cache from `OLLAMA_KV_CACHE_TYPE`, but the value
that actually governs allocation is whatever `ollama serve` was launched with. A client
process that cannot see it assumes Ollama's `f16` default and projects **double** the
real KV — measured 2.2 GB instead of 1.1 GB, total 5.1 GB, refusing a model that fits
comfortably. Reproduced by running `chat.py` from a non-interactive shell, where
`~/.bashrc` returns early and never exports the vars.

Worked around in `chat.py` by `setdefault`-ing the same values, so the script is
self-contained. **Not properly fixed.** The guard cannot currently learn the server's
KV type — `/api/ps` does not report it, and it appears only in the server's startup
log. A client/server mismatch in the other direction (server on f16, client assuming
q8_0) would make the guard *under*-estimate, which is the dangerous direction. Options
if this bites again: parse the server log once at startup, or have the guard treat KV
as unknown unless the model is already resident (where `/api/ps` gives the real size).

**3. Cross-family grading vs offline — unresolved, needs a decision.**
`grader.py:1284` resolves its verifier with
`get_judge_provider_chain('judge', exclude_provider=tutor_provider)` to avoid
self-preference bias (rationale at `grader.py:1078-1090`). When the tutor is
`local_ollama`, the new `_local_judge_provider` honours that exclusion and returns
None, so the verifier falls through to the **cloud** chain. With network that works;
genuinely offline it means no verifier at all (`GradeResult(PARTIAL, needs_followup)`).
Cross-family independence and offline operation are mutually exclusive on a one-model
box. Not resolved here — see Open question 5.

## Open questions

1. **Which lesson does it default to?**
   *Recommend:* require `--lesson <id>`, with `--list-lessons` to discover.
   *Reason:* an implicit default silently tests whatever lesson happens to be first,
   which is how eval draws become unreproducible.

2. **Which student identity?**
   *Recommend:* reuse the eval fixture user (`EVAL_USER_PK = 999001`), overridable
   with `--student <username>`.
   *Reason:* the fixture already exists, is institution-scoped correctly, and keeps
   CLI traffic out of real student records.

3. **Should the CLI run the judges?**
   *Recommend:* yes — judges are part of the real turn, and on the Jetson they now
   run on the same local model (uncommitted work in
   `apps/curriculum/content_judges/_providers.py::_local_judge_provider`). Add
   `--no-judge` to skip when iterating on tutor voice alone.
   *Reason:* a client that skips the judges is not reproducing a production turn.
   **Caveat:** doubles per-turn latency on a 4B — tutor call plus judge call, both
   local. Measure before assuming it is usable interactively.

4. **Resume an existing session, or always start fresh?**
   *Resolved:* always fresh. `--session <id>` deferred.

5. **Offline grading: accept self-preference bias, or lose the verifier?** (new)
   When tutor and verifier must be the same local model, `grader.py`'s cross-family
   exclusion cannot be satisfied.
   *Recommend:* an explicit offline mode where `_local_judge_provider` ignores
   `exclude_provider`, logging a warning that cross-family independence is
   unavailable. A biased verifier beats no verifier — Tier-2 grading otherwise
   returns PARTIAL/needs_followup for every free-text answer offline.
   *Reason:* the alternative silently degrades grading precisely when the box has no
   network, which is the deployment target.
   *Alternative worth measuring first:* a **small dedicated judge model** (qwen3.5:2b
   or 0.8b) resident alongside the 4B. That restores genuine model independence AND
   stays offline. Requires `OLLAMA_MAX_LOADED_MODELS=2` and a combined footprint under
   budget — 4.0 GB (tutor) + ~1.5-2 GB (2b) against ~5.5 GB available with no browser.
   Tight but plausible; 0.8b more comfortably. This supersedes the plan's earlier
   claim that the judge *must* share the tutor's tag — that held only under
   `MAX_LOADED_MODELS=1`.

## Risks

- **Judge latency on-box.** A local tutor turn measured ~13 s decode at 16 tok/s on
  qwen3-4b-jetson. Adding a local judge call on the same 4B could push a turn past
  30 s, which is poor interactivity. Open question 3 exists because of this; the
  `--no-judge` escape hatch is the mitigation.
- **Unverified judge quality on 4B.** The local-judge wiring resolves correctly
  (verified 2026-07-27) but no live verdict has ever been produced — the browser was
  holding the memory. Whether a 4B emits parseable multi-axis judge output is
  **unknown** and must be measured before the CLI depends on it.
- **Fidelity gap is real, not theoretical.** Skipping `ContentSafetyFilter` means the
  CLI will not reproduce PII-redaction behaviour. Anyone debugging a safety issue must
  use the browser or wait for `--http` mode. This belongs in the command's `--help`.

## Next step

Build Phase 1: `apps/tutoring/management/commands/tutor_chat.py` plus
`apps/tutoring/cli/session.py`, driving `start_for_view` / `respond_for_view` against
a `--lesson` on the eval fixture user, with plain conversational output.

## Related

- `memory/jetson_qwen_tool_compliance_plan.md` — which model runs on this box; the 4B
  ceiling this client will be driving.
- `memory/simple_tutor_engine_plan.md` — the engine being driven.
- `memory/eval_harness_plan.md` — the other programmatic driver (`evals/runner.py`),
  which calls raw `respond()` rather than the view adapter.
- `auto-memory/jetson_crash_memory_config.md` — the memory arithmetic behind
  "no browser on the Jetson", and the out-of-repo system config.
