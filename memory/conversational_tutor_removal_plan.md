# Remove the legacy ConversationalTutor engine — Plan (2026-07-27)

## Problem

`apps/tutoring/conversational_tutor.py` is 13,190 lines of engine that
`simple_tutor` was built to replace. `memory/simple_tutor_engine_plan.md` said
the old engine "stays untouched until simple_tutor proves out on the eval
benchmark." It has proved out — simple_tutor is the default (`is_enabled()`
returns True unless explicitly disabled, `simple_tutor/__init__.py:34-43`) and
every session in the local DB is `engine='simple'`.

Keeping it costs more than disk. Today it broke session start outright on this
Jetson: `views.py:685` imported `ConversationalTutor` at the top of
`chat_start_session`, and the legacy module does not compile on Python 3.10
(`conversational_tutor.py:4187` puts a backslash inside an f-string expression,
which PEP 701 only legalised in 3.12). simple_tutor was going to handle the
request; it never got the chance, because an import of a module it does not use
crashed the view first. Deploy targets 3.12 so CI and prod never saw it. That is
the shape of the problem — a dead engine that is still load-bearing at import
time, and still the *only* engine on some routes.

## Current state (from audit)

**The enabling fact: `simple_tutor` has zero code dependency on the legacy
engine.** The only mention across `apps/tutoring/simple_tutor/*.py` is a prose
docstring reference (`grader.py:27`). Nothing to untangle.

**Six production entry points call the legacy engine with no `simple_tutor`
gate** — these run v1 unconditionally, today, in prod:

| Entry point | Legacy call |
|---|---|
| `apps/tutoring/views.py:1285` `chat_difficulty_signal` | `respond()` |
| `apps/tutoring/views.py:1385` `chat_answer_bank_question` | `respond()` |
| `apps/api/views/sessions.py:47` `start_session` | `start()` / `resume()` |
| `apps/api/views/sessions.py:123` `respond` | `respond()` |
| `apps/api/views/sessions.py:142` `submit_exit_ticket` | `submit_exit_ticket()` |
| `apps/api/views/sessions.py:176` `start_review` | `start_review()` |

The entire `/api/v1/` surface (`config/urls.py:43`, routed at
`apps/api/urls.py:39`) is legacy-only. Its own module docstring says it "wraps
the existing tutor engine logic … so we don't duplicate the ConversationalTutor
wiring." That wrapper was never revisited when simple_tutor landed.

**Three entry points are already gated**, with legacy as an error fall-through
only: `chat_start_session` (`views.py:683`), `chat_start_review`
(`views.py:1236`), `chat_exit_ticket` (`views.py:1607`).

**Shared symbols still living in the legacy module** (these must move, not
die):

- `SessionState` (`conversational_tutor.py:110`) — CLAUDE.md's canonical
  session-state enum, imported by 9 test files.
- `TutorMessage`, `ConceptCoverageResult` — dataclasses; `TutorMessage` is what
  `_serialize_tutor_message` (`apps/api/views/sessions.py:18`) flattens.
- `_THINKING_LEAK_RE`, `_strip_probe_sentences`, `_looks_like_authored_question`
  — helpers consumed outside the module.
- `TUTOR_SYSTEM_PROMPT_TEMPLATE` (`conversational_tutor.py:103`) is *already* a
  re-export shim; the real definition is `apps/tutoring/prompts/anthropic.py:34`.
  Only `apps/llm/prompts.py:27` still imports it via the legacy path.

**Response shape mismatch is the real porting work.** Legacy returns a
`TutorMessage` dataclass (`.content`, `.phase`, `.media`, `.show_exit_ticket`,
`.is_complete`, `.step_number`, …); simple_tutor returns a plain dict
(`engine.py:626` — `content` / `tool_calls` / `fallback` / `step_advanced`),
with view-shaped payloads built by `respond_for_view` (`engine.py:2273`) and
`start_for_view` (`engine.py:2668`).

**Test blast radius:** 31 of 72 files in `apps/tutoring/tests/` reference the
legacy engine.

**Stale default:** `TutorSession.engine` defaults to `Engine.V1`
(`models.py:139-146`) even though simple_tutor is the production engine.
simple_tutor overwrites it to `SIMPLE` at `engine.py:346`, so the default only
applies to rows no engine ever touched.

## Target design

Delete `apps/tutoring/conversational_tutor.py` and the `SIMPLE_TUTOR_ENGINE`
kill-switch. One engine, no dispatch, no fall-through.

Getting there is **not** a delete — it is a port of six endpoints, a relocation
of five shared symbols, and a test migration. Sequenced so that every phase
leaves `main` shippable and no phase mixes a behaviour change with a move.

The ordering principle: **make the legacy module unreachable before making it
absent.** Every phase up to 5 is reversible by re-adding a call site; only
phase 5 is destructive, and by then nothing points at it.

## Data model changes

One migration, in phase 5 only:

- `TutorSession.Engine` — remove the `V1 = 'v1'` choice, change `default` to
  `Engine.SIMPLE`. Keep the **field**: it is `db_index=True` and historical rows
  legitimately record which engine ran them.
- Backfill: leave existing `engine='v1'` rows as-is. They are an accurate
  historical record. Removing the choice does not invalidate stored values
  (Django validates choices at form/serializer level, not at the DB).
- **Do not** drop `engine` entirely, and do not rewrite history rows.

Nothing else changes. `engine_state`, `SessionTurn`, `InFlightQuestion` are all
simple_tutor-native already.

## Backend changes

### Phase 1 — relocate shared symbols (no behaviour change)

New module `apps/tutoring/session_state.py` holding `SessionState`,
`TutorMessage`, `ConceptCoverageResult`. New/extended
`apps/tutoring/text_utils.py` for `_THINKING_LEAK_RE`,
`_strip_probe_sentences`, `_looks_like_authored_question` (drop the leading
underscore — they are cross-module API, not private).

- `conversational_tutor.py` imports them back from the new homes, so it keeps
  working untouched during the transition.
- Retarget `apps/llm/prompts.py:27` to `from apps.tutoring.prompts import
  TUTOR_SYSTEM_PROMPT_TEMPLATE`; delete the shim at
  `conversational_tutor.py:103`.
- Update the 9 test files importing `SessionState` from the legacy path.

### Phase 2 — port the six ungated entry points

Build one adapter, `apps/tutoring/simple_tutor/view_adapters.py`, exposing
`to_legacy_message_shape(payload: dict) -> dict` that maps a simple_tutor
payload onto the key set `_serialize_tutor_message` emits. This keeps the
`/api/v1/` JSON contract byte-identical for any mobile client already shipped —
the engine swaps underneath, the wire format does not move.

- `apps/api/views/sessions.py` — all four endpoints route to
  `simple_tutor.engine.{start_for_view, respond_for_view, …}` through the
  adapter. Delete `_serialize_tutor_message` once nothing returns a
  `TutorMessage`.
- `views.chat_difficulty_signal` — the synthetic-turn injection is engine-
  agnostic; only the `tutor.respond(...)` call changes to `respond_for_view`.
- `views.chat_answer_bank_question` — same; note it already grades
  deterministically via `bank_grader` before calling the engine, so only the
  narration turn moves.

### Phase 3 — remove the fall-through branches

Delete the legacy `except → fall through to legacy` arms in
`chat_start_session`, `chat_start_review`, `chat_exit_ticket`, plus the
`simple_tutor.is_enabled()` conditionals. An error in simple_tutor must surface
as a 500, not silently re-run a different engine — CLAUDE.md's no-silent-skip
rule. Remove `is_enabled()` and `SIMPLE_TUTOR_ENGINE` from
`simple_tutor/__init__.py`, `offline_eval/run_matrix.sh:34`, and
`evals/runner.py:338`.

At the end of phase 3 the legacy module has **zero** production callers.

### Phase 4 — test migration

Of the 31 legacy-touching test files, triage into:
- **Port** — tests asserting pedagogy/behaviour that simple_tutor must also
  satisfy (remediation, mastery transitions, safety wiring, probe-strip).
  These are the valuable ones; rewrite against `simple_tutor.engine`.
- **Delete** — tests asserting v1-internal mechanics with no simple_tutor
  analogue (phase-based flow, v1 safety valves — `simple_tutor_engine_plan.md`
  is explicit that those valves were workarounds for bugs the new engine
  eliminates by construction).

Triage list produced in phase 4, reviewed before any test is deleted.

### Phase 5 — delete

`git rm apps/tutoring/conversational_tutor.py`. Migration for the `Engine`
choice. Sweep docs: `CLAUDE.md`, `README.md`, `PLATFORM_DOC.md`,
`apps/tutoring/README.md`, `docs/developer_guide.md`, and the eight
`.claude/skills/*/SKILL.md` files that still describe v1 as live —
`tutoring-engine-expert` is entirely about the legacy engine and needs
rewriting or retiring.

## Frontend/mobile changes

None required if the phase-2 adapter holds the `/api/v1/` contract. The web chat
UI already talks to `/tutor/api/chat/*`, which is simple_tutor-gated today.

Flagged for verification, not assumed: whether any shipped mobile build depends
on `phase` (a 5E display string simple_tutor derives per-step rather than
tracking as flow state). If a client branches on it, the adapter must synthesise
it from the current step's `phase` field.

## Out of scope

- Rewriting `simple_tutor` itself. This plan moves callers; it does not touch
  the engine's logic.
- The Python 3.10 f-string fix (`conversational_tutor.py:4187`). Phase 5 deletes
  the file. Until then the lazy-import fix already applied to `views.py` is
  sufficient, and prod is on 3.12 regardless.
- Removing the `TutorSession.engine` field.
- Any judge / eval-harness restructuring.
- The deprecated specialist judges in `apps/tutoring/judges/` — separate
  cleanup, tracked in CLAUDE.md against the unified-judge rollout.

## Phased delivery

| Phase | Work | Est. (solo days) |
|---|---|---|
| 0 | Lazy-import unblock in `views.py` | **done** (2026-07-27) |
| 1 | Relocate `SessionState` + helpers; retarget prompt import | 0.5 |
| 2 | Port 6 ungated entry points + `/api/v1/` adapter | 2–3 |
| 3 | Delete fall-through branches + kill-switch | 0.5 |
| 4 | Test triage + migration (31 files) | 2–3 |
| 5 | Delete module, migration, doc/skill sweep | 1 |
| | **Total** | **6–8 days** |

Phase 2 is the only phase with real unknowns, and its size depends entirely on
open question 1.

## Open questions

1. **Is `/api/v1/` consumed by a live client?** This forks phase 2 hard. If a
   mobile build is in the field, we port four endpoints behind a frozen contract
   (2–3 days). If nothing consumes it — the RN mobile plan is in
   `memory/archives/` — we **delete** the four endpoints instead (~2 hours) and
   the total drops to 4–5 days. *Recommend: check deploy logs for `/api/v1/`
   traffic and confirm no App Store / Play build is live before starting phase
   2.* This is the one answer needed before work begins.

2. **Prod session distribution.** Local DB is 100% `engine='simple'`, but that
   proves nothing about Seychelles. *Recommend: run
   `TutorSession.objects.values('engine').annotate(n=Count('id'))` against a prod
   dump before phase 3.* If meaningful recent `v1` traffic exists, something is
   still routing there and phases 2–3 are incomplete.

3. **Where should `SessionState` live?** *Recommend `apps/tutoring/session_state.py`*
   over folding it into `models.py` — it is a pure enum with no ORM dependency,
   and `models.py` is already large. Cheap to change later.

4. **Keep or retire `tutoring-engine-expert`?** It documents v1 exclusively.
   *Recommend: retire it and fold anything still true into
   `codebase-architecture-expert`,* rather than rewriting a skill for an engine
   that no longer exists.

## Next step

Answer open question 1 — confirm whether any live client consumes `/api/v1/`.
Everything else is mechanical; that answer sets phase 2's size.

## Related

- `memory/simple_tutor_engine_plan.md` — the original replacement plan; this is
  its final phase. Its "old engine stays untouched" clause is what this
  supersedes.
- `memory/simple_tutor_engine_milestones.md` — build milestones M1–M13.
- `memory/simple_tutor_m12_pose_question_milestones.md` — the tool architecture
  the ported endpoints will run on.
- `memory/jetson_qwen_tool_compliance_plan.md` — where the 3.10 import crash
  surfaced; its blocker 2 is resolved by phase 5.
