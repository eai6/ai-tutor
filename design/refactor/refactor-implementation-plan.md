# Conversational Tutor Refactor — Implementation Plan

Companion to:
- `design/refactor/refactor-analysis.md` — recommended design (R1–R10, §3 service surface, §7 resolved decisions).
- `design/refactor/current-conversational-tutor.md` — the as-is engine.
- `design/science-principles.md` — the 13-principle pedagogical source (Math Academy Way, Chapters 10–23). **Load-bearing reference for every `StudentTutor` move prompt** in Phase 2 §2.2; the "Most testable imperatives" column maps directly into per-move prompt directives.

This document sequences the refactor into **three phases**. Each phase is independently shippable to a non-production branch and produces measurable artifacts. Phase 3 is the cutover point; everything before that runs alongside the legacy engine without changing student-facing behavior.

## Verification of the as-is (codebase spot-check)

Confirmed against current code on `refactor/conversational-tutor-redesign`:

| Claim from as-is | Verified at |
|---|---|
| ~12,600-line `ConversationalTutor` | `apps/tutoring/conversational_tutor.py` is 12,631 LOC |
| Unified judge + 10 specialists in `judges/` | `apps/tutoring/judges/{unified,factual,arithmetic,coherence,figure_ref,figure_vision,handoff,history,rule,safety,step_eval}.py` |
| Regen ensemble | `apps/tutoring/regen/{prompt,score,self_retry}.py` |
| Untyped 40-key engine state | `TutorSession.engine_state = JSONField(default=dict)` at `apps/tutoring/models.py:68` |
| `judge_outputs` per-turn JSON | `SessionTurn.judge_outputs = JSONField(default=dict)` at `apps/tutoring/models.py:250` |
| `pose_question` accepts raw `correct_answer` | `apps/tutoring/conversational_tutor.py:525, 735–736` — tool schema currently passes the LLM-supplied canonical through to the backend |
| `StudentProfile.profile_summary` does NOT exist | `apps/accounts/models.py:201` — only `skills_snapshot` JSON is present today; the TEXT column R2 calls for must be added |
| Supporting utilities to keep (Question, bank_grader, working analyzer, repeated_question, praise_filter, answer_leak) | All present at module-level in `apps/tutoring/` |
| Three entry points wired through views | `apps/tutoring/views.py:656, 940, 1198` (`chat_start_session`, `chat_respond`, `chat_start_review`) |

No surprises that invalidate the refactor analysis. The plan below references file paths in the verified shape.

## Operating principles for the plan

1. **Build alongside, cut over once.** New code lands under a new namespace (proposed: `apps/tutoring/v2/`) so the legacy engine keeps serving sessions until the new engine clears the benchmark. `views.py` chooses engine by `NEW_TUTOR` env flag — default flips in Phase 3.
2. **Lift forward, do not rewrite, the deterministic utilities.** The pieces R6 calls out (numeric ±0.01, MCQ letter match, Jaccard repeats via `apps/tutoring/repeated_question.py`, praise filter, bare-answer detection as a signal from `apps/tutoring/student_working_analyzer.py` — not a standalone module, media-signal parser, the `ast`-based working analyzer itself, `Question` abstraction + `bank_grader`) keep their existing modules and get imported by the new services. They are not re-implemented.
3. **Each phase has a benchmark gate.** Phase 1 gates on contract tests + unit tests of utilities (no behavior change yet). Phase 2 gates on offline benchmark parity against `memory/eval_benchmark_v2_simplified.md` for the conversational path. Phase 3 gates on end-to-end benchmark parity + safety floor coverage before the default flips.
4. **No multi-agent decomposition beyond what `refactor-analysis.md` §3 specifies.** Grader / Tutor / Profiler / Engine + tools. Don't introduce additional agents during implementation.
5. **CLAUDE.md is rewritten in Phase 3**, when the routing flips. The unified-judge default and the "no multi-agent without measured bottleneck" rule are removed at that point — not before, because the legacy engine is still serving traffic during Phases 1–2 and the existing rules still apply to it. **During Phases 1–2, `refactor-analysis.md` §1 is the authoritative override**: it explicitly states that the cancelled Seychelles pilot is the bottleneck-measurement the CLAUDE.md rule required, and that CLAUDE.md "will be rewritten as part of this refactor." Any Claude session working on Phase 1 or 2 should cite `refactor-analysis.md` §1 + this operating principle when the CLAUDE.md rules appear to forbid the in-flight grader/tutor/profiler decomposition.

## Out of scope (do not touch in this refactor)

Listed here so we don't grow the work in flight. Each is called out in `refactor-analysis.md` §1 or §7 as deliberately excluded.

- Authoring-time pipelines: `ImageGenerationService`, `ContentGenerator` JSON-repair, `figure_facts_extractor`.
- Cross-session spacing / interleaving scheduler (R7 note in §4).
- Authoring-time question-derivability gate (deferred per §7 item 11; `BANK_PREPOSE_RECHECK=on` is the pilot mitigation).
- Multi-tenancy scoping (already correct; do not change `Q(institution=inst) | Q(institution__isnull=True)` usage).
- Frontend rendering of media or exit-ticket modal — backend contracts (response shape, `|||MEDIA:N|||`) preserved verbatim.

## Preserved runtime surfaces (UX/UI passthrough)

This refactor targets the tutoring runtime; user-experience and surface concerns that the legacy engine already handles correctly are preserved verbatim. The new `TutorEngine` / `StudentTutor` must thread these through unchanged:

- **Mobile response format** — when the request is from a mobile client, the legacy engine injects a mobile-specific response shape into the prompt. The new per-move prompts must include the same shape directive when the client is mobile.
- **`Course.tutoring_images_enabled`** — per-course gate that suppresses the media catalog entirely. `MediaService` honors this flag; when disabled, no media block is injected.
- **`|||MEDIA:N|||` signal parser** — lifted forward unchanged (§3 deletion table).
- **Persona / locale / institution name / grade level** — assembled into the new shared move-prompt preamble exactly as today (`Institution.name`, `StudentProfile.tutor_personality`, language code).
- **`SessionTurn.judge_outputs` and `SessionTurn.metadata`** shape — preserved. New trace fields land under a `v2_trace` key inside `judge_outputs` (consistent with CLAUDE.md "new judge fields go to `judge_outputs` only") and additionally as spans via the existing `apps/tutoring/tracing.py` surface (see Phase 3, §3.3).
- **Frontend chat / artifact-panel / exit-ticket-modal contracts** — request and response JSON shapes unchanged.

---

# Phase 1 — Foundation: typed state, service skeleton, schema-tight tools

**Goal.** Stand up the new module structure with typed contracts, ship the kill switch, and migrate the tool-call provenance — all without changing what students see. The legacy engine still serves every session at the end of Phase 1. The new code path exists and is unit-tested but not wired into production routing.

**Why this comes first.** Two of the design's load-bearing claims — "stateless services with typed Pydantic contracts" and "backend-enforced tool answer-key provenance" — must be in place before `StudentGrader` or `StudentTutor` can be written against them. The typed state also unblocks the conformance check's `state-coherence` rule, which references `open_question.visible_context_at_pose` (§7 item 6) that does not exist today. Doing the schema work first also lets us extract and unit-test the forward-lifted utilities behind a clean import surface, so Phase 2 doesn't pay the integration cost twice.

## Deliverables

1. **New module layout under `apps/tutoring/v2/`** (or equivalent — name TBD; convention should be obvious in the codebase).
   - `v2/contracts/` — Pydantic models: `TutoringContext`, `GradingRequest`, `GradingResult`, `ProfileUpdate`, `SessionRuntimeState`.
   - `v2/services/` — service skeletons: `StudentGrader`, `StudentTutor`, `StudentProfiler`, `TutorEngine`, `ContextManager`, `MediaService`, `ExitTicketService`. Skeleton = signature + docstring + `raise NotImplementedError`.
   - `v2/tools/` — `MathVerificationTool` (full implementation; not blocked on Phase 2), `QuestionTool` and `InlineQuestionTool` shims that wrap the new tool-schema (see deliverable 4).
   - `v2/utilities/` — re-exports of the lifted-forward utilities (no duplication; `from apps.tutoring.bank_grader import …`). This is the import surface Phase 2 codes against.
2. **`SessionRuntimeState` as a Pydantic model persisted to a new JSONB column on `TutorSession`.**
   - New column: `TutorSession.runtime_state` (JSONB on Postgres, JSON on SQLite dev). Migration `00NN_add_runtime_state.py`. Default `{}`.
   - Pydantic schema fields per §7 item 6 of the analysis: `open_question` (incl. `visible_context_at_pose`), `attempts_on_open_question`, `posed_question_ledger`, `objective_progress`, `media_shown`, `remediation_state`, `current_move`, `move_history`, `unverified_run_length`, `safety_valve_counters`, `resume_marker`. Plus one additive field beyond §7 item 6 to support bare-answer detection (Phase 2 §2.1.1): `bare_answer_counts_by_objective: dict[str, int]` (default `{}`). The field is additive — the analysis's §7 item 6 listing is presented as "explicit fields (additive — new fields land as schema migrations, not free-form keys)," so extending it within the typed schema is consistent with that spec.
   - **Do not migrate or backfill `engine_state`.** Legacy column stays for legacy sessions; new sessions write only to `runtime_state`. Engine dispatch reads/writes the column it owns.
3. **`StudentProfile.profile_summary` TEXT column + `StudentProfile.asked_questions` JSONB column** (R2).
   - Migration adds two columns on `apps/accounts/models.py:StudentProfile`:
     - `profile_summary` (TEXT, default `''`) — qualitative recall (strengths, struggles, examples shown).
     - `asked_questions` (JSONB on Postgres / JSON on SQLite, default `{}`) — structured map keyed by `"{source}:{id}"` composite per §4.1, values `{last_asked_at: iso8601}`. Drives cross-session repeat avoidance. See §4.3 for the read mechanism (`cross_session_repeat_guard()`) and Phase 3 §3.1 for the write mechanism (profiler end-of-session).
   - Indexed only on `student` (already the PK relation). No code writes to either yet — `StudentProfiler` lands in Phase 3; `cross_session_repeat_guard()` reads `asked_questions` in Phase 2 (reading an empty `{}` is a no-op until Phase 3 writes to it).
4. **Tool-schema tightening with backend-enforced provenance + two-phase commit.**
   - `pose_question` and `pose_inline_question` schemas in the new `v2/tools/` layer accept **only** `question_ref` (a typed reference — see deliverable 4.1 below) **or** `pre_pose_token` (opaque). They refuse `correct_answer` outright.
   - **Two-phase commit for tool side effects** (resolves the state-consistency hole identified in review: token consumption, ledger append, and `open_question` write must not persist if conformance rejects the candidate response):
     - **Phase A — dry-run validation** during candidate generation. The tool call runs the full validation pipeline (token signature check + derivability check + repeat guards — see deliverables 4.2 and 4.3) but does **not** consume the token, does **not** append to the ledger, and does **not** write `open_question`. Validation either returns a "valid" outcome with the canonical resolved (used only to render the question text to the candidate response) or refuses the tool call.
     - **Phase B — commit** runs only after the candidate response passes structural conformance and is about to be persisted/shown. The commit step consumes the token from the cache (marking single-use), appends the question signature to `SessionRuntimeState.posed_question_ledger`, and writes `SessionRuntimeState.open_question` with `visible_context_at_pose` populated from the just-validated tool inputs.
     - **On conformance retry**, the candidate is discarded — including its dry-run tool calls. The retry re-validates from scratch; the token is still unconsumed and re-presentable, the ledger is unchanged, and the same question may legitimately be reproposed (the retry's re-validation will pass again on the same inputs).
     - **On second conformance failure → safe template**, no tool side effects ever committed. The safe template path doesn't pose new questions; it surfaces verdict-keyed feedback (§2.5) without consuming `pre_pose_token`s or mutating the ledger.
     - Implementation: validation returns a `PendingPose` object (typed Pydantic); persistence layer (`ContextManager`) consumes it only at turn-commit. No mutations on `SessionRuntimeState` are written through any path other than the commit hook.
   - **Per-path validation flow** (Phase A only):
     - **`pre_pose_token` path** — backend validates the signed token against the in-memory cache (read-only check, doesn't mark consumed), retrieves the canonical the token stamped, and confirms the token's `visible_context_at_pose` snapshot. The derivability check already ran when the token was issued; no re-validation needed.
     - **`question_ref` (bank) path under `BANK_PREPOSE_RECHECK=on`** — backend resolves the canonical from the table indicated by `question_ref.source`, then runs the same visible-derivability check `pre_pose_check()` runs for token-path questions: the canonical must be derivable from the student-visible prompt + attached figure + recent transcript, with hidden KB chunks suppressed. **Failure refuses the tool call** (the question is not posed; `TutorEngine` selects an alternate). Success captures the snapshot for use at Phase B commit.
     - **`question_ref` (bank) path under `BANK_PREPOSE_RECHECK=off`** (post-MVP, when the authoring-time gate has landed) — bank path skips the derivability check but **still runs the repeat guards** (see deliverable 4.3). Still captures a `visible_context_at_pose` snapshot from the live tool-call inputs.
   - **Legacy tools untouched.** The legacy `ConversationalTutor` still has its raw-`correct_answer` schema and keeps serving sessions until Phase 3.

4.1. **Typed `question_ref` for cross-table provenance.** The plan-level term `bank_question_id` resolves ambiguously across `ExitTicketQuestion` and `LessonStep` (integer IDs collide). Replace with a typed reference at every layer:
   - Tool argument schema: `question_ref: { source: "lesson_step" | "exit_ticket_question" | "inline_generated", id: int }`. Backend resolves to the correct row by `source`.
   - `StudentProfile.asked_questions` (Phase 1 §3) is keyed by a string-composite of source + id, e.g. `"lesson_step:42"`, `"exit_ticket_question:7"`. Values remain `{last_asked_at: iso8601}`. The composite key trivially serializes to JSON and makes table-of-origin auditable without joins.
   - `SessionRuntimeState.posed_question_ledger` entries carry the same `{source, id}` structure plus the Jaccard signature.
   - `open_question.source` from §7 item 6 takes the same enum: `"lesson_step" | "exit_ticket_question" | "pre_pose_token"`.

4.2. **Single-use HMAC token cache.** The cache is a process-local LRU keyed by `(session_id, token)`; tokens are HMAC-signed with a per-process secret. **Validation (Phase A) is read-only; consumption (Phase B) marks single-use atomically.** Phase 2 wires `StudentGrader.pre_pose_check()` to produce tokens; Phase 1 ships the cache + verifier + tests for both Phase A read-only and Phase B mark-consumed semantics.

4.3. **Repeat guards as standalone always-on checks** (decoupled from `pre_pose_check`). Both repeat guards run at the tool boundary as Phase A validation steps, **independently of `BANK_PREPOSE_RECHECK`**:
   - **`in_session_repeat_guard()`** — canonicalizes the visible stem, computes a Jaccard signature via the lifted-forward `apps/tutoring/repeated_question.py`, compares against `SessionRuntimeState.posed_question_ledger`. Match → refuse.
   - **`cross_session_repeat_guard()`** — checks `StudentProfile.asked_questions` for the composite `"{source}:{id}"` key with `last_asked_at` inside the avoidance window (default 14 days). Match → refuse.
   - Both run on **every** tool-posed assessment question (bank-path under either `BANK_PREPOSE_RECHECK` setting, token-path, inline). The derivability check `pre_pose_check()` is a separate, independent gate — it can be skipped when `BANK_PREPOSE_RECHECK=off`; repeat guards cannot. This resolves the coupling bug where turning the derivability flag off would have silently disabled repeat prevention.
5. **`MathVerificationTool` full build** (§7 item 5 + 8).
   - Constrained JSON DSL with whitelisted opcodes (`add, sub, mul, div, pow, sqrt, log, sin/cos/tan, eq, solve, …`) + variables block.
   - Small Python interpreter (single-file, no `exec`) that walks the DSL and emits a step-by-step trace.
   - DSL-validation pass: structured variable-binding check (DSL variables must map to numbers/quantities named in the visible problem text) + a focused LLM call for free-form cases the structured check can't decide.
   - Composed grading pipeline: `MathVerificationTool` (problem → canonical) + existing `student_working_analyzer.py` (student prose → value) + comparator (SymPy / Pint / ±0.01) for equivalence. The comparator is a thin wrapper; pick libraries in implementation.
   - Unit tests: fixture problems from the existing benchmark, plus deliberate adversarial cases (algebraic equivalents, unit mismatch, mixed notation).
6. **Routing kill switch and runtime flags** (R10).
   - `views.py` `chat_start_session` / `chat_respond` / `chat_start_review` read `os.environ.get('NEW_TUTOR', 'off')`. `off` → legacy `ConversationalTutor` (current behavior). `on` → new `TutorEngine`. Phase 1 only adds the read + dispatch; Phase 2 makes `on` actually do something useful.
   - The flag value is sticky per session: a session that started on legacy stays on legacy across resumes, and vice versa. Implementation: `TutorSession.engine_version` text field (migration), defaulting to `'legacy'`. Resume reads the field, not the env var.
   - **`BANK_PREPOSE_RECHECK` env var read** also lands in Phase 1, alongside `NEW_TUTOR`. Default `'on'` per §7 item 11; consumed by the new tool layer (deliverable 4). Both `NEW_TUTOR` and `BANK_PREPOSE_RECHECK` live behind a single centralized accessor module in `v2/config/` so flag reads aren't scattered through call sites and so the test suite can patch them in one place.
7. **`ModelConfig` purposes for new services.**
   - Extend `apps.llm.ModelConfig.Purpose` (`apps/llm/models.py`) with **six** new entries: `GRADER_MATH`, `GRADER_GROUNDED`, `TUTOR_MOVE`, `CONFORMANCE_CLASSIFIER`, `TUTOR_CLAIM_ADJUDICATOR`, `PROFILER_SUMMARY`.
   - Migration adds the enum values and seeds default `ModelConfig` rows so each purpose is dispatchable from day one.
   - **Provider constraints driven by path coverage** (not blanket analysis mandate; rationale below):
     - `GRADER_GROUNDED` covers two branches per analysis §3: **KB-grounded** adjudication for curriculum content (provider-agnostic in principle) and **Gemini Google-grounding** for general world knowledge (provider-required — Google Search grounding is a Gemini-native feature with no cross-provider equivalent). Because a single dispatched call may need either branch and the path is selected at call time by question type, the primary **must be Gemini** to guarantee Google-grounding is available when needed. Splitting into two purposes (`GRADER_KB_GROUNDED` + `GRADER_GOOGLE_GROUNDED`) is a viable post-MVP refactor; for MVP, the single Gemini-pinned purpose is the simpler path. Fallback drops to `unverified` rather than route to a non-grounding model on the Google-grounded branch.
     - `TUTOR_CLAIM_ADJUDICATOR` primary **must be Gemini** for the same reason (analysis: "same grounded-adjudication machinery as the non-math branch"). Same rationale and same post-MVP split option.
   - **Architecture-required temperature constraints** (CLAUDE.md invariants):
     - `CONFORMANCE_CLASSIFIER`, `GRADER_MATH`, `GRADER_GROUNDED`, `TUTOR_CLAIM_ADJUDICATOR` → temperature `0` (JUDGE-class verification consistency).
     - `TUTOR_MOVE` → clamp `[0.1, 0.3]` (TUTORING-class).
     - `PROFILER_SUMMARY` → temperature `0` (deterministic summarization; the profile is a memory artifact read by future sessions, not a creative output).
   - **`PROFILER_SUMMARY` role**: drives `StudentProfiler`'s end-of-session summarization of strengths, struggles, misconceptions named, and examples shown into the `profile_summary` TEXT column. Provider choice is an implementation sub-decision; fast/cheap tier is appropriate (summarization is bounded, runs once per session, async). The structured `asked_questions` map is written deterministically and does **not** use this purpose.
   - **Provider selection for `GRADER_MATH`, `TUTOR_MOVE`, `CONFORMANCE_CLASSIFIER`, `PROFILER_SUMMARY` is an implementation sub-decision, not a plan commitment** (§7 framing: dispatch surfaces in place, model selection tuned later). Default rows seeded during Phase 1 implementation, swappable via admin without code changes (mirrors today's `tutoring`/`judge`/`regen` rows).
   - Phase 1 ships schema, enum values, default rows, fallback-chain plumbing, and the admin registry only; consumers land in Phase 2 (`GRADER_MATH`, `GRADER_GROUNDED`, `TUTOR_MOVE`, `CONFORMANCE_CLASSIFIER`, `TUTOR_CLAIM_ADJUDICATOR`) and Phase 3 (`PROFILER_SUMMARY`).

## Tests

- **Contract round-trip tests** for every Pydantic model (serialize → DB → deserialize → equality).
- **Tool-schema rejection tests** (schema-layer only, no derivability logic — that requires Phase 2's grader):
  - A tool call with `correct_answer` is rejected at schema validation.
  - A tool call with an invalid `question_ref` (unknown `source`, or no matching row for the resolved `source`/`id`) is rejected at FK resolution.
  - A tool call with an expired or replayed `pre_pose_token` is rejected at token-cache verification (Phase A read-only check returns "invalid").
  - A tool call with a valid `question_ref` resolves the canonical from the table indicated by `question_ref.source` and routes to `pre_pose_check()` — which in Phase 1 is a `NotImplementedError` stub; the test asserts the routing call happens, not its outcome. Full derivability tests live in Phase 2 §2.1 once the grader is real.
  - A tool call with a valid `pre_pose_token` retrieves the stamped canonical from the cache (without consuming it — Phase A is read-only) and routes to the same `pre_pose_check()` stub.
- **Two-phase commit semantics tests** (Phase 1 §4 + §4.2): a Phase-A-validated tool call leaves the token uncommitted and the ledger unchanged; calling the commit hook explicitly marks the token consumed and appends to the ledger; a second commit attempt on the same token is rejected.
- **Repeat-guard isolation tests** (Phase 1 §4.3): `in_session_repeat_guard()` and `cross_session_repeat_guard()` are called directly with both `BANK_PREPOSE_RECHECK=on` and `BANK_PREPOSE_RECHECK=off` — both guards run identically in both modes. (Behavioral repeat tests with real fixtures land in Phase 2 §2.1.1.)
- **`BANK_PREPOSE_RECHECK` flag wiring test**: with the env var set to `off`, the tool boundary still resolves the canonical and still runs both repeat guards but skips the routing to `pre_pose_check()` derivability; with `on`, the derivability routing happens.
- **`MathVerificationTool` unit tests**: a tiered fixture set (arithmetic, single-variable algebra, units, equivalence). Coverage target: every opcode exercised at least once, both DSL-validation pass branches (structured + LLM-mediated) exercised.
- **Utility re-export tests**: import each lifted-forward utility through the new path and run its existing test suite — confirms no behavior drift.
- **Routing test**: with `NEW_TUTOR=off`, the existing tutoring test suite passes unchanged.

## Exit criteria

- Migrations applied locally and against a dev clone of prod data without errors.
- All Phase 1 tests pass, full existing test suite still passes.
- `NEW_TUTOR=on` boots a new session and writes a valid `SessionRuntimeState` snapshot to `runtime_state`. (The session itself won't be useful yet — Phase 2 supplies the conversation behavior.)
- `MathVerificationTool` clears its fixture suite at ≥ 95% accuracy on the math fixtures from `memory/eval_benchmark_v2_simplified.md`.

## Phase 1 risks and mitigations

- **JSONB column for `runtime_state` is a schema change on `TutorSession`.** Migration runs on a hot table. Mitigation: column is nullable / defaulted; no backfill; legacy sessions ignore it; reversible.
- **HMAC token cache is process-local.** Acceptable because Azure Container Apps runs a small replica count and tokens are single-turn-scoped. If we ever scale out replicas, move to Redis. Documented as a known boundary in the module docstring.
- **DSL design decisions slip.** Opcode set and comparator-library choice are §7-item-5 sub-decisions. Time-box at one week; default to "smallest set that covers the benchmark fixtures" and grow as needed.
- **Phase 1 is a multi-migration phase.** Six schema changes land: `TutorSession.runtime_state`, `TutorSession.engine_version`, `StudentProfile.profile_summary`, `StudentProfile.asked_questions`, `ModelConfig.Purpose` enum extension (6 new entries), and the 6 default `ModelConfig` rows seeded via data migration. Mitigation: ship them as a tight sequence in a single deploy (or two back-to-back deploys at most) to avoid mixed-schema runtime states. All migrations are additive — no column drops, no row-level edits to existing rows — and individually reversible.
- **`ModelConfig.Purpose` enum extension touches a production table.** Mitigation: additive enum + new default rows only; existing rows (`tutoring`, `judge`, `regen`, `generation`, etc.) are not modified or read differently. Reversible by reverting the migration; the legacy engine never reads the new purposes.

---

# Phase 2 — Conversational engine: grader, tutor, conformance

**Goal.** Make `NEW_TUTOR=on` actually conduct a tutoring conversation, with grader-driven correctness and the conformance layer in place. By the end of Phase 2 the new engine should match or beat the legacy engine on the eval benchmark for the conversational path. Still not the default in production.

**Why this is one phase, not two.** Grader and Tutor are co-designed: the Tutor's move prompts consume the grader's redacted `student_safe_feedback`, and the conformance check references both the verdict shape and the tutor's intent labels. Splitting them creates an awkward intermediate state where one side has to stub the other. Keeping them in the same phase lets a single benchmark run validate the joined pipeline.

## Deliverables

### 2.1 — `StudentGrader` (the central service)

Implements all three responsibilities from `refactor-analysis.md` §3:

- **Student-answer grading.**
  - **Math path.** LLM emits constrained JSON DSL → DSL-validation pass → `MathVerificationTool` (Phase 1) executes → comparator. Mismatch in DSL validation → verdict `unverified`. Output shape extends analysis §3 by one additive boolean flag from §2.1.1: `{ verdict, private_canonical, student_safe_feedback, student_value, reasoning, citation, bare_answer }`. `bare_answer` defaults to `false`; the math path sets it to `true` when the deterministic pre-pass detects a numeric-only response. Non-math paths leave it `false`. **`bare_answer` is consumed by the move *prompt*, not by move *selection*:** move selection still fires from the standard inputs in §2.3 (wrong verdict → `scaffold_hint`, correct → `confirm_and_advance`); the `bare_answer` flag is passed through to the `scaffold_hint` move prompt as a template variable that biases its content toward "show your working" diagnostic phrasing. This keeps the move state machine pure (no new branches, no new moves) and isolates bare-answer behavior to prompt content where it belongs.
  - **Non-math path.** Tiered: deterministic `bank_grader` first when a canonical exists; KB-grounded adjudication (curriculum-content questions); Gemini Google-grounding (general world knowledge). All three feed a single confidence-thresholded judgment that escalates to `unverified` below threshold. Tunable threshold; start conservative (§7 item 1 sub-decision).
  - **`unverified` is first-class**, not an error. The downstream conformance check has explicit `unverified` rules.
  - **Output redaction**: produces parallel `private_canonical` (never passed to `StudentTutor` on wrong/partial moves) and `student_safe_feedback` (rubric-shaped: `what_right`, `what_missing`, `first_misconception_redacted`). The redaction step is part of the grader output, not the conformance layer.
- **Pre-pose check** producing signed `pre_pose_token`. Enforces the student-visible derivability invariant: canonical must be derivable from `open_question.visible_context_at_pose` (visible prompt + attached figure + recent conversation), **with hidden KB chunks suppressed during derivation**. Returns either a token (pass) or a refusal reason (fail). The token cache lives in Phase 1; this phase populates it.
- **Tutor-claim adjudication.** Same grounded-adjudication machinery as the non-math student-grading path, called by the conformance layer on surfaced tutor prose claims. Returns `{ supported | contradicted | unverified, citation }`.

### 2.1.1 — Deterministic safety floors at the tool boundary and inside `StudentGrader`

Per analysis §3 deletion table, these utilities are lifted forward as deterministic gates. The repeat guards live at the **tool boundary** (Phase 1 §4.3 — independent of `pre_pose_check` so they remain active under `BANK_PREPOSE_RECHECK=off`); the bare-answer detector lives **inside the grader**. All run before any LLM call.

- **In-session and cross-session repeat guards — at the tool boundary, not inside `pre_pose_check`.** The implementation is fully specified in Phase 1 §4.3. Phase 2 wires them into the tool-call validation pipeline (Phase A of the two-phase commit per §4):
  - `in_session_repeat_guard()` compares the Jaccard signature of the candidate stem against `SessionRuntimeState.posed_question_ledger` — match → refuse the tool call; `TutorEngine` selects an alternate.
  - `cross_session_repeat_guard()` checks `StudentProfile.asked_questions` for the `"{source}:{id}"` composite key with `last_asked_at` inside the avoidance window (default 14 days) — match → refuse.
  - The signature/key is appended to `posed_question_ledger` only at Phase B commit (after conformance passes), per §4's two-phase commit semantics. This means a rejected candidate's tool calls do **not** poison the ledger.
  - Both guards run on **every** assessment-question tool call regardless of `BANK_PREPOSE_RECHECK`. The derivability check is the only validation that the flag can disable; repeat prevention is always on.
- **Bare-answer detection on the math path** (analysis §3 deletion table; CLAUDE.md math-tutoring rule). When the student input on a math turn is a numeric-only response with no working, the grader sets a `bare_answer=true` flag on the `GradingResult`. The verdict itself is unchanged — bare-answer is not a verdict, it's a signal. The flag is consumed at the **move-prompt** layer, not the move-selection layer (per §2.1's GradingResult shape note and §2.3's inputs contract):
  - **Correct bare answer** → verdict=`correct` → move selection picks `confirm_and_advance` from the standard verdict→move mapping. The `bare_answer=true` flag biases that move's prompt content toward a brief "because…" affirmation, matching the CLAUDE.md rule of "confirm + advance, no probing." Move selection is unchanged.
  - **Wrong bare answer** → verdict=`wrong` → move selection picks `scaffold_hint` from the standard mapping. The `bare_answer=true` flag biases that move's prompt content toward "show your working" diagnostic phrasing, matching the CLAUDE.md rule of "wrong bare answer triggers a single ask-for-working as diagnosis." Move selection is unchanged.
  - Counters land in `SessionRuntimeState.bare_answer_counts_by_objective` (new field added to the Pydantic model in Phase 1 — additive). No move-table threshold changes in MVP; the counter exists for future tuning (e.g., bare-answer rate as a signal for difficulty adjustment).
- **Numeric ±0.01 tolerance and MCQ letter match** stay where the lifted-forward `bank_grader` already implements them. The grader's math path uses them as the comparator's plain-numeric branch.

These three gates are the deterministic floor analysis §3 promised to "keep as deterministic gates inside `StudentGrader`." They run regardless of LLM availability.

### 2.2 — `StudentTutor` and the move table

- One service entry point: `StudentTutor.respond(context: TutoringContext, verdict: GradingResult | None, move: Move) → str`.
- One **focused per-move prompt** per move from §4 of the analysis (`pose_question`, `confirm_and_advance`, `confirm_and_extend`, `scaffold_hint`, `name_misconception`, `worked_example`, `explain`, `pivot`, `close_topic`). 200–400 tokens each.
- **Per-move prompt content is grounded in `design/science-principles.md` (Math Academy Way, Chapters 10–23).** This is the load-bearing pedagogical source — not the deprecated legacy prompts, and not free-form authoring. Implementation rule when writing or tuning a move prompt:
  1. Open `design/science-principles.md` and identify which principles `refactor-analysis.md` §4 attributes to that move (the "Principles baked in" column of the move table). For example:
     - `pose_question` → Active Learning (Ch. 10) + Retrieval Practice (Ch. 20).
     - `confirm_and_advance` → immediate feedback + Cognitive Load (Ch. 14, "don't over-teach").
     - `scaffold_hint` → faded scaffolding (Ch. 14, expertise-reversal effect).
     - `name_misconception` → Targeted Remediation (Ch. 21, "diagnose root cause").
     - `worked_example` → Cognitive Load (Ch. 14, "worked example before practice; subgoal labelling").
     - `pivot` → Productive-struggle limit (Ch. 21, "scaffold rather than lower the bar").
     - `close_topic` → Mastery Learning (Ch. 13, "hold the same bar; vary the path").
  2. For each attributed principle, lift its **"Most testable imperatives"** column from `science-principles.md` directly into the move prompt as behavioural directives. The imperatives are written to be testable, so they translate cleanly into prompt language ("the student is *doing* on this turn", "name the method first, then ask", "fade the scaffold this attempt", etc.).
  3. The move prompt does **not** restate principles abstractly ("apply active learning") — that's the legacy 460-line prompt's failure mode. It states the imperatives directly as turn-shaping instructions.
  4. **Universal preamble principles** (Growth Mindset / Direct Instruction's framing tone — Ch. 22 + 11) live in the shared preamble, not in every move prompt. Don't duplicate.
  5. **Cross-session principles** explicitly named in `science-principles.md` (Automaticity Ch. 15, Non-Interference Ch. 17, Spaced Repetition Ch. 18, Interleaving Ch. 19, Gamification Ch. 22) are **out of scope for MVP** per analysis §4. The move prompts must not pretend to deliver them. If the principle requires cross-session scheduling, leave it for the follow-up scheduler (analysis §4 note); if the principle is per-turn (Ch. 10, 11, 14, 20, 21), include it.
  6. Each move prompt's docstring cites the exact `science-principles.md` row(s) it draws from (chapter number + principle name) so future tuning has the trail. This is the prompts-to-source bidirectional link analogous to CLAUDE.md's "memory ↔ commit cross-citations."
- The 460-line legacy system prompt is **not ported**. Replace with a small shared preamble (growth-mindset / effort-praise framing — preamble convention carried forward from the legacy CLAUDE.md "Universal preamble principles" guidance and `refactor-analysis.md` §4's "universal preamble principles" note, **not** sourced from `science-principles.md`'s 13-principle table; language/locale; persona) + the per-move prompt. Total stable prefix for a single turn lands around 1–2 KB, not 10 KB.
- **Prompt skills are non-negotiable per CLAUDE.md.** Before writing or tuning any move prompt, consult `prompting-fundamentals-expert` then the provider-specific skill (`claude-prompting-expert`, `openai-prompting-expert`, or `gemini-prompting-expert`) matching the model `TUTOR_MOVE` resolves to. The `science-principles.md` source defines *what* the prompt should make the tutor do; the prompting-expert skills define *how* to structure the prompt so the target model follows it reliably.
- The tutor receives the **full transcript** (§7 item 10) — no windowing in MVP.
- The tutor receives `student_safe_feedback` on wrong/partial moves, **never** `private_canonical`. Plumbing-level invariant: the move prompt template has no slot named `canonical_answer` for those moves.
- The tutor receives a media catalog from `MediaService` (Phase 3 makes this a proper extracted service; Phase 2 calls a thin inlined version). Dual-coding directives in `science-principles.md` Ch. 14 ("verbal + visual throughout") inform the per-move prompt language around when to emit the `|||MEDIA:N|||` signal.

### 2.3 — `TutorEngine` deterministic move selection

- Move selection is a pure function of inputs — not an LLM call. Inputs: `verdict.kind` (one of `correct | wrong | partial | unverified`), `attempts_on_open_question`, `objective_progress`, `unverified_run_length`, `current_move`, `move_history`, `profile_summary` (last-persisted text). **`verdict.bare_answer` is explicitly NOT a move-selection input** — it's passed through to the selected move's prompt template as a content bias (§2.1.1). Move selection sees only `verdict.kind`; the move prompt sees the full `verdict` object including `bare_answer` and `student_safe_feedback`.
- **What feeds move selection vs. what doesn't.** Move selection reads `profile_summary` (free-text qualitative recall) as its only profile input. It does **not** read `StudentProfile.skills_snapshot` or `StudentSkillMastery` rows. Per analysis §4, the move table's triggers are all attempt-count / verdict / objective-coverage signals; cross-session spacing and mastery-level-driven question selection are explicitly out of MVP scope. The mastery-level lookahead the legacy engine used (e.g., "≥70% mastered → keep it tight") is dropped in MVP; bring it back as a follow-up once spacing/interleaving lands.
- Decision logic is a small state machine. Move table from §4 — implement exactly the triggers shown; do not add or invent moves.
- Safety valves (§7 item 3): max 40 turns / session, max 12 turns / objective, force-close objective after 6 verdict-less turns. These are caps; `pivot` and `close_topic` should fire first under normal conditions.
- **No predefined lesson script.** Engine may skip, reorder, expand, or substitute `LessonStep` rows based on the move state machine and the profile. Teacher authoring is preserved (R1) — the engine reads `LessonStep` as a hint, not as a flowchart.
- **`StudentSkillMastery` write hook — dashboard-only side effect, not a tutoring-flow input.** After `StudentGrader` returns a `correct` or `wrong` verdict (not `partial`, not `unverified`), `TutorEngine` invokes `apps.tutoring.personalization.SkillAssessmentService.record_practice(student, skill_id, was_correct, hints_used, session)`. `skill_id` resolves from the current `LessonStep` → objective → skill mapping that the legacy engine already uses. ~20 LOC, no new schema. **This write keeps the teacher dashboard, prerequisite gating, and per-objective competency aggregates live — it is NOT consumed by `TutorEngine` move selection or by `StudentTutor` move prompts** (see the inputs bullet above). It's a downstream side effect for reporting surfaces, isolated from the runtime path. Not in scope: the broader `personalization.py` surface (`RetrievalService`, `InterleavedPracticeService`, `RemediationService`, `SessionPersonalizationService`) — those are subsumed by the move table or deferred per analysis §4.

### 2.4 — Structural conformance check

This is the runtime replacement for the regen ensemble + 10-axis unified judge. It is one fast-LLM classifier call + a stack of deterministic gates.

- **Fast-LLM classifier** returns the **nine binary labels** from analysis §3:
  `{ affirms_correctness, refutes_correctness, surfaces_uncertainty, contains_assessment_question_in_prose, hands_floor_back_or_transitions, contains_partial_feedback_shape, contains_factual_claim, contains_arithmetic_claim, student_claim_present }`. Each is a narrow binary decision; `student_claim_present` reads from the student's prior turn, the rest from the candidate tutor response. Use a fast/cheap tier model — provider selected during implementation against the benchmark per analysis §7 (no plan-level commitment, per the "does NOT do" list at the end of this plan).
- **Deterministic answer-leak check** scoped to `verdict=wrong`, `verdict=partial`, and any turn with an unanswered open question. Lifts `apps/tutoring/answer_leak.py` forward unchanged in role.
- **Deterministic safety pre-screen.** Lifts `apps/tutoring/judges/safety.py` forward as the safety floor. P1 doesn't replace child-safety guards (deletion table).
- **Deterministic state-coherence check.** New code. Validates the response against `SessionRuntimeState`: `open_question` still matches, `current_move` is one the engine selected, last verdict referenced is the verdict in hand. Replaces the old `history` judge with a cheap deterministic check.
- **Tutor-claim adjudication route.** If `contains_factual_claim` or `contains_arithmetic_claim` is true, route the surfaced claim(s) through `StudentGrader.adjudicate_tutor_claim()`. `contradicted` or persistent `unverified` → reject. Arithmetic claims also route through `MathVerificationTool`.
- **No-verdict student-claim handling — safety property only, not full adjudication.** Per analysis §3 "No-verdict student claims," when `student_claim_present=true` on the prior student turn and no grader verdict was produced this turn, the conformance verdict-matrix forbids `affirms_correctness` and `refutes_correctness` (covered by the rule matrix below). This enforces the safety property: the tutor cannot adjudicate without a grader verdict. The analysis additionally describes an optional pre-generation route — `TutorEngine` detects `student_claim_present`, calls `StudentGrader.adjudicate_tutor_claim()` on the student's claim to produce a *synthetic* verdict, then `StudentTutor` generates with that verdict in hand. **This pre-generation route is explicitly deferred post-MVP.** The MVP default is the analysis's "Default behavior when neither path fires: treat as `unverified` — the tutor must reflect/probe, not adjudicate." Conformance enforces this; no P1 hole results from the deferral.
- **Praise filter.** Lift `apps/tutoring/praise_filter.py` forward; runs under every non-`correct` verdict.
- **Extended figure_ref check.** Lift `apps/tutoring/judges/figure_ref.py` forward, extended per §3 deletion table: when a figure is attached and the tutor makes a quantitative/spatial claim, the claim must be present in `MediaAsset.figure_facts`.
- **Numeric mutation + authored-example provenance rule check.** The thin deterministic surface that `rule` judge keeps (§3 deletion table). Lift from `apps/tutoring/judges/rule.py` (numeric mutation guard) and `rule_compliance.py`; cut the LLM-judge surface.
- **Verdict-keyed rule matrix** exactly per analysis §3. All four verdicts (`correct`, `wrong`, `partial`, `unverified`) plus the no-verdict-with-claim case have explicit rules. Implementation = small table, not nested if/else.
- **One retry on rejection** with violated rules surfaced to `StudentTutor`. On second failure, route to safe terminal template (§2.5).

### 2.5 — Safe terminal templates

- Five templates per analysis §3, keyed to verdict (and the no-verdict-with-claim case).
- Templates draw content from `student_safe_feedback` fields (canonical never leaks). The "next action" slot is filled from the next action `TutorEngine` would have selected.
- Emits a `template.fallback` span via `apps/tutoring/tracing.py` (Phase 2 owns span emission per §3.3) and sets `SessionTurn.metadata.fallback_used = true` for the denormalized rollup. Phase 3 layers dashboards/alerts on top of the trigger rate.

### 2.6 — `ExitTicketService` wired to the new grader

- Selects a subset of `ExitTicketQuestion` rows from the lesson's `ExitTicket` bank, excluding recently-attempted ones via `ExitTicketAttempt` history.
- Renders as a separate quiz UI (not posed via `QuestionTool` — see §3). Frontend contract unchanged.
- Each response routes through `StudentGrader` (bank_grader first; grounded adjudication fallback for free-text rubric items).
- Aggregate pass/fail is a derived count compared against `ExitTicket.passing_score`. No "exit-ticket hold gate" and no "force-clear after N hold cycles" — `TutorEngine` transitions straight to exit ticket when objective evidence is sufficient (R6).
- `BANK_PREPOSE_RECHECK=on` default (§7 item 11) — exit-ticket bank questions get the full Phase A validation pipeline (derivability check via `pre_pose_check` + both repeat guards) until the authoring-time gate ships. When the flag flips off post-MVP, **only** the derivability check is skipped; `in_session_repeat_guard()` and `cross_session_repeat_guard()` still run (see §4.3).

### 2.7 — `ContextManager`

- Single owner of the typed Pydantic contracts and the read/write boundary against `SessionRuntimeState`.
- Assembles `TutoringContext` for each service call (transcript, profile snapshot, objective, KB chunks, verdict if any).
- Service calls receive **frozen snapshots**, not live state — this is what the "stateless services" claim means in practice.

## Tests

- **Move state-machine fixture tests**: one fixture per move from §4, demonstrating the trigger conditions and the engine's deterministic selection.
- **Move-prompt provenance audit** (one-time, not automated): each per-move prompt has a docstring citing the `design/science-principles.md` chapter(s) + principle(s) it embeds, matching the attribution in `refactor-analysis.md` §4. The audit is a written checklist run once per move during implementation; renewed only when a move prompt is materially revised. Not a CI gate (prompt content is too qualitative for assertion-based testing); a reviewer's eyes confirm each move prompt's directives trace back to "Most testable imperatives" rows in the source.
- **Grader unit tests**:
  - Math: every comparator path (SymPy / Pint / ±0.01) exercised by fixture problems.
  - DSL-validation: deliberate decomposition-vs-problem mismatches must produce `unverified`.
  - Non-math grounded adjudication: confidence threshold honored (mocked grounding source, low-confidence input → `unverified`).
  - Pre-pose check: hidden KB chunks suppressed during derivation; "answer requires hidden context" cases refused.
  - Tutor-claim adjudication: `contradicted` and `unverified` outputs both produce rejection in the conformance layer.
- **Bank-path derivability tests (`BANK_PREPOSE_RECHECK=on`)** — moved here from Phase 1 because they require a real grader:
  - One passing fixture per source: curriculum `LessonStep` bank question, `ExitTicketQuestion` bank question, `pose_inline_question` call — each verifies the canonical is derivable from the visible context (visible prompt + attached figure + recent transcript).
  - One **failing** fixture: a bank question whose canonical depends on a hidden KB chunk (not in visible prompt, not in attached figure, not in recent transcript) — `pre_pose_check` refuses; tool call rejected; `TutorEngine` selects an alternate.
- **Bank-path bypass test (`BANK_PREPOSE_RECHECK=off`)**: same fixtures pose without derivability check; `open_question.visible_context_at_pose` is still populated from the tool-call inputs so the conformance state-coherence check has a valid snapshot.
- **In-session repeat-guard tests (§2.1.1)**: pose question Q; attempt to pose Q again in the same session (exact match, paraphrase, template-repeat with surface-number swap); each is refused at the tool boundary; `TutorEngine` selects a different question. The ledger is verified to contain Q's signature exactly once after the rejected repeat attempts.
- **Bare-answer behavior tests (§2.1.1)**: math fixture where the student replies with a numeric-only answer matching the canonical → grader produces verdict=`correct` + `bare_answer=true` → `TutorEngine` selects `confirm_and_advance`; same fixture but student answer is wrong → verdict=`wrong` + `bare_answer=true` → `TutorEngine` selects `scaffold_hint` and the move prompt biases toward asking for working. The `bare_answer_counts_by_objective` counter increments in both cases.
- **Conformance verdict-matrix tests**: one fixture per (verdict × violated-rule) combination. Total ~25 cases — exhaustive on the rule matrix, not just smoke.
- **Safe-template fallback tests**: deliberate two-strike failures route to the verdict-keyed template; canonical never appears in the template output (assertion: `private_canonical not in rendered_template`).
- **End-to-end conversational benchmark**: run the full new engine against `memory/eval_benchmark_v2_simplified.md` and compare against the legacy engine's recent benchmark numbers. **Phase 2 gates on ≥ parity, with specific attention to the three P1 categories.**

## Exit criteria

- New engine handles full sessions under `NEW_TUTOR=on` on a dev environment.
- Benchmark parity (or better) vs. legacy engine on the 19 failure categories. Improvement expected on P1; the cancelled pilot is the prior.
- Safe-template fallback rate measured on the benchmark; documented as a baseline so Phase 3 trace logging can monitor for drift.
- No regression on safety floor categories (safety pre-screen + answer-leak + extended figure-ref).
- Legacy engine still serves production traffic untouched.

## Phase 2 risks and mitigations

- **Conformance classifier becomes the new error-correlation surface.** Mitigation: its decision space is narrow (binary intent labels on a short response) and the rules it informs are explicit, not learned. Watch the safe-template trigger rate as a quality signal (analysis §3); a sustained high rate means classifier or move prompts are mistuned.
- **Move-table doesn't cover a real session shape.** Mitigation: §4 is the contract — no new moves added in implementation. If a pattern doesn't fit a move, document it; do not silently invent a new move (that's exactly the meta-LLM principle-selection failure mode we're avoiding).
- **DSL-validation pass false positives push too many turns to `unverified`.** Mitigation: the threshold is tunable; start conservative and pull data from the benchmark. `unverified` is a legitimate verdict, but if the rate is much higher than the legacy engine's "uncertain" outcomes, the validation pass is too strict.
- **Pre-pose check on bank questions adds latency.** Quantified mitigation: the recheck runs the same DSL/grounding path as runtime questions, so worst case is one extra grader call per posed question. The flag `BANK_PREPOSE_RECHECK=on` allows turning it off in benchmarking to isolate the cost; production keeps it on (§7 item 11).

---

# Phase 3 — Cutover: profiler, observability, default flip, legacy deprecation

**Goal.** Flip `NEW_TUTOR` to default-on for new sessions, finish the observability layer, deprecate the legacy modules read-only, and update CLAUDE.md. Legacy stays loaded for resume of in-flight sessions and as the kill-switch fallback (R10).

**Why these together.** The profiler is the last piece needed for cross-session learning carry-over, but it doesn't affect single-session correctness — so it's safely the last addition. Observability (trace logging, dashboards, safe-template trigger-rate alerts) is a hard prerequisite for flipping the default, but isn't useful before Phase 2's conformance layer exists. Deprecation can't happen until cutover is stable. Bundling them avoids three small phases for what is one go-live event.

## Deliverables

### 3.1 — `StudentProfiler`

- Reads/writes `StudentProfile.profile_summary` TEXT (column shipped in Phase 1).
- Write cadence (§7 item 2): **async, end-of-session only for MVP.** Add mid-session batching only if profile drift demonstrably hurts within-session adaptation. Single setting, not a tuning knob.
- Generates a session summary: what was learned, strengths and challenges, examples shown. Uses the `PROFILER_SUMMARY` model purpose (Phase 1 §7) for the LLM summarization call.
- **Read window vs. physical archival — explicit amendment to R2.** Analysis R2 calls for "10-session retention" implemented as "a row-archival job on `TutorSession`," which conflates two concerns: what the runtime *reads* and what physically lives in the hot DB. The plan splits them and ships only the first in MVP. **This is a deliberate deviation from R2 as literally written, justified by MVP simplicity** (per the user's clarification that features not directly affecting the three P1 errors can be deferred):
  - **Read window (in scope, implemented in MVP).** When the tutor reads the student's prior history, the profiler exposes only the most-recent 10 `TutorSession` rows for that student — a simple `ORDER BY ended_at DESC LIMIT 10` filter at the read boundary. This delivers the runtime-visible behavior R2 was protecting (the tutor doesn't load unbounded history); no data is deleted.
  - **Physical archival (deferred — explicit post-MVP follow-up).** Moving `TutorSession` + `SessionTurn` rows older than 10-per-student to cold storage is the operational housekeeping concern R2 names. It does not affect runtime correctness, the P1 errors, or any in-scope deliverable; a management command or scheduled job lands later. Tracked explicitly here as an amendment to R2, not silently dropped.
- **Cross-session repeat avoidance — write side.** The read side is fully specified in Phase 1 §4.3 (`cross_session_repeat_guard()` runs at the tool boundary, reads `StudentProfile.asked_questions` keyed by `"{source}:{id}"`, refuses on hits inside the avoidance window). Phase 3 owns the **write** side:
  - At end-of-session, `StudentProfiler` extracts the `posed_question_ledger` entries from `SessionRuntimeState` (committed entries only — rejected candidates never reached commit per §4 two-phase semantics), maps each `{source, id}` to its composite key string, and writes `{ "{source}:{id}": {last_asked_at: <session end timestamp>} }` into `StudentProfile.asked_questions`.
  - The map is capped at the last N entries (default 500) with LRU eviction.
  - The free-text `profile_summary` carries qualitative recall (strengths, struggles, misconceptions named); the structured `asked_questions` map carries the verifiable repeat-avoidance signal. Two columns, two cadences, no conflation.
- The tutor reads the **last persisted** snapshot, not a live one — `ContextManager` enforces this.

### 3.2 — `MediaService` extracted

- Thin selector: lesson-scoped figures + KB-similarity-ranked top-N catalog injected per turn (R8).
- Lifts the `|||MEDIA:N|||` parser forward unchanged.
- Scoped at `Lesson` level, **not per step** (R8 — explicit simplification from today's per-step + per-lesson + KB-figures three-source catalog).
- The `<figure_facts>` block injection moves here too; the extended figure_ref conformance check reads from `MediaAsset.figure_facts` populated at authoring time.

### 3.3 — Observability dashboards and alerts

This is the layer the analysis calls out as a prerequisite for any future agent decomposition ("Instrument before splitting" — CLAUDE.md architecture rules) but it also gates the default flip.

**Use the existing tracing surface, do not invent a new one.** `apps/tutoring/tracing.py` already provides `emit_span`, `start_span_buffer`, `reset_span_buffer`, `flush_spans`, and the `TurnSpan` model (`apps/tutoring/models.py:279`) is already wired to persist spans against `SessionTurn`. The new engine emits spans through the same API the legacy engine uses.

**Phase ownership split:**
- **Phase 2 emits the per-stage spans + the per-turn rollup.** This is required for Phase 2's benchmark gate — without spans, the team can't see per-stage latencies, conformance reject rates, or which stage produced an `unverified` outcome. Adding spans only after benchmark parity is established would mean debugging blind.
- **Phase 3 adds the dashboards/alerts layer on top.** Spans exist; this phase visualizes them.

**Phase 2 deliverable:**
- **Per-stage span emission** from `TutorEngine.respond()`. Each LLM hop and each gate becomes one span: `grader.math` / `grader.grounded` / `grader.pre_pose_check` / `grader.tutor_claim_adjudication` / `tutor.move_call` / `conformance.classifier` / `conformance.state_coherence` / `conformance.answer_leak` / `conformance.safety` / `conformance.figure_ref` / `conformance.rule_check` / `tutor.retry` / `template.fallback` / `tool.commit`. Span payload carries: stage name, model used (when an LLM is called), input/output token counts, latency, outcome (pass / reject / skipped + reason).
- **Per-turn rollup** alongside spans: selected move, grader verdict, conformance classifier label vector, conformance violations list, retry-used flag, fallback-template used. Persisted to `SessionTurn.judge_outputs` under a `v2_trace` key (consistent with CLAUDE.md "new judge fields go to `judge_outputs` only"). The rollup is the denormalized view; spans are the source of truth.

**Phase 3 deliverable:**
- **Aggregate dashboard signals** (handled in `apps/dashboard` or a small Django admin view, reading `TurnSpan` + the `judge_outputs.v2_trace` rollups):
  - Safe-template trigger rate over rolling N sessions.
  - Verdict distribution (`correct` / `partial` / `wrong` / `unverified` ratios).
  - Move-selection distribution.
  - P1 indicator counters: number of `correct→wrong` or `wrong→correct` candidate rejections caught by conformance, number of pre-pose refusals.
- **Alert thresholds**: safe-template rate above baseline + 2σ; `unverified` rate above N%; pre-pose refusal rate above N%. Mechanism TBD (whatever the existing ops setup uses — likely log-level INFO + a manual review cadence for the pilot).

### 3.4 — Default flip

- `NEW_TUTOR=on` becomes the default. New sessions route to the new engine.
- In-flight legacy sessions continue on the legacy path because of the sticky-per-session flag from Phase 1 (`TutorSession.engine_version`).
- A canary period (TBD — proposed: 48 hours on a non-prod environment with synthetic traffic, then 48 hours on a small staging cohort) before the prod flip.
- The kill switch (`NEW_TUTOR=off`) routes new sessions back to legacy for ops emergencies. **Critical**: kill switch is for student-facing safety incidents, not for "the benchmark dipped". Documented as such.

### 3.5 — Legacy deprecation (read-only, then delete)

- The following modules are marked `DEPRECATED` in their module docstrings and excluded from new-session code paths:
  - `apps/tutoring/conversational_tutor.py` — entire `ConversationalTutor` class.
  - `apps/tutoring/combined_judge.py`, `apps/tutoring/judges/unified.py`, `apps/tutoring/judges/{factual,arithmetic,coherence,figure_vision,history,step_eval}.py`.
  - `apps/tutoring/regen/` — entire package.
  - `apps/tutoring/judges/handoff.py` — its role moves to the `hands_floor_back_or_transitions` classifier label (§3 deletion table).
  - 460-line legacy system prompt files under `apps/tutoring/prompts/`.
- **Kept verbatim** (lifted forward in Phase 1, kept as utility modules):
  - `apps/tutoring/judges/safety.py` — conformance safety pre-screen.
  - `apps/tutoring/answer_leak.py` — scoped conformance post-check.
  - `apps/tutoring/judges/figure_ref.py` — extended in Phase 2 for figure-facts quantitative claim check.
  - `apps/tutoring/judges/rule.py` + `apps/tutoring/rule_compliance.py` — thin numeric-mutation + authored-example deterministic check.
  - `apps/tutoring/bank_grader.py`, `apps/tutoring/question.py`, `apps/tutoring/student_working_analyzer.py`, `apps/tutoring/praise_filter.py`, `apps/tutoring/repeated_question.py`.
- **Deletion gate (concrete criteria — all four must hold)**:
  1. New engine has served production traffic for **≥ 4 weeks** post-cutover.
  2. **Zero** kill-switch flips during that window.
  3. **Three consecutive weekly benchmark runs** stay within ±2 percentage points of the Phase 3 cutover numbers on each of the three P1 categories.
  4. No open P1 incidents tied to the new engine.
  When all four hold, delete the deprecated modules in a single commit (one logical change per migration file convention applies).
- The 40-key `engine_state` JSON column on `TutorSession` is **retained read-only** for historical session resume and dashboard back-views. Not deleted in this refactor.

### 3.6 — CLAUDE.md update

Two specific rules are rewritten (analysis §1 + §6):
- The "Unified judge is the default (2026-05-18)" block is replaced with a description of the grader-driven correctness architecture and the conformance layer.
- The architectural conservative-bias rule "Don't introduce multi-agent decomposition without measured bottleneck on the benchmark" is updated: it remains in spirit (no gratuitous decomposition), but the explicit reference to the cancelled pilot is the bottleneck-measurement the rule called for. The new wording invites grader / tutor / profiler decomposition as the validated pattern and rules out further decomposition (e.g., a separate "explainer agent" or "hint agent") unless the same kind of evidence justifies it.

### 3.6.1 — DEPLOY.md rewrite

`design/prompts/DEPLOY.md` is the ops runbook for two env-var knobs on the GitHub Actions deploy workflow. Both change in this refactor:

- **`TUTOR_PROMPT_VARIANT` is removed.** Its targets (`baseline` / `v6` / `v7`) are the 460-line legacy prompt variants, which are deprecated in §3.5. The new engine has no equivalent variant knob — the move table replaces it.
- **`TUTOR_MODEL_OVERRIDE` is replaced by per-purpose overrides.** The legacy variable scoped to `purpose=tutoring`; the new engine has **six** purposes (Phase 1 §7). Introduce six parallel env vars following the same `provider/model_name` format — uniform surface, every purpose dispatchable from the deploy workflow:
  - `TUTOR_MOVE_MODEL_OVERRIDE`
  - `GRADER_MATH_MODEL_OVERRIDE`
  - `GRADER_GROUNDED_MODEL_OVERRIDE` *(constrained — only Gemini providers accepted; non-Gemini values rejected with a deploy warning, since Google-grounding is provider-required)*
  - `CONFORMANCE_CLASSIFIER_MODEL_OVERRIDE`
  - `TUTOR_CLAIM_ADJUDICATOR_MODEL_OVERRIDE` *(same Gemini constraint as `GRADER_GROUNDED`)*
  - `PROFILER_SUMMARY_MODEL_OVERRIDE` *(no provider constraint; profiler runs async at session end and isn't student-facing, but the override exists for uniformity and for cost-tuning the summarization model independently)*
- **Workflow_dispatch dropdowns and the repo-variable UI** mirror the legacy DEPLOY.md shape (Method 1 / Method 2 / verification / rollback) — same patterns, new variable names. The "How this works under the hood" section updates to point at `ModelConfig.get_for(purpose)` for each purpose.
- **Rollback path stays the same shape**: workflow_dispatch with all overrides empty falls back to DB-active configs for every purpose. The `NEW_TUTOR` kill switch (R10) is documented as the additional revert lever specific to this refactor.
- Drop the prompt-variant verification step entirely; replace with a step that logs the resolved model per purpose at app boot.

This is a docs change tied to Phase 3 cutover, not an architectural deliverable.

### 3.7 — Data and migration notes

- The new column `TutorSession.runtime_state` (Phase 1) is the source of truth for new sessions. No backfill from `engine_state`.
- `TutorSession.engine_version` (Phase 1) lets the legacy engine handle resumes of sessions that started before the flip.
- `StudentProfile.profile_summary` (Phase 1) starts empty for all students; `StudentProfiler` populates it on the first new-engine session.
- `StudentProfile.asked_questions` (Phase 1) starts as `{}` for all students; `StudentProfiler` writes it end-of-session from `SessionRuntimeState.posed_question_ledger`, keyed by `"{source}:{id}"` per §4.1. `cross_session_repeat_guard()` (§4.3) reads it at the tool boundary from session start in Phase 2 but tolerates `{}` for new or pre-cutover students — empty map means no cross-session repeats are known yet, so no question is refused on that basis.

## Tests

- **End-to-end production-shape benchmark**: full new engine against `memory/eval_benchmark_v2_simplified.md`, including exit-ticket flow and resume scenarios.
- **Kill-switch tests**: flipping `NEW_TUTOR=off` mid-pilot routes new sessions to legacy without breaking in-flight new-engine sessions.
- **Resume tests**: a session started on legacy resumes on legacy (sticky flag); a session started on new resumes on new.
- **Resume artifact preservation test**: a new-engine session poses bank question Q (which writes `open_question` with `visible_context_at_pose`), then the student disconnects without answering. On resume, the engine re-renders Q with **identical visible text, attached media IDs, and (for MCQ) the same option order** — no new pre-pose check, no new question selection, no canonical leak in the resume opener. This is the preserved-surface behavior the legacy engine implemented at the artifact-panel layer; the typed `open_question` field is what makes it deterministic in the new engine.
- **Profiler tests**: end-of-session writes to both `profile_summary` and `asked_questions`; the next session of the same student reads the persisted snapshot.
- **Cross-session repeat avoidance test (two-session)**: session 1 poses a question with `question_ref={source: "exit_ticket_question", id: N}` and completes; profiler writes `asked_questions["exit_ticket_question:N"]` with the session-end timestamp. Session 2 (same student, within the 14-day avoidance window) attempts to pose the same `question_ref` — `cross_session_repeat_guard()` (§4.3) refuses it at the tool boundary; `TutorEngine` selects a different bank question. After the avoidance window expires (test fixture moves time forward), session 3 can pose the same `question_ref` again.
- **Observability tests**: spans emitted per stage via `apps/tutoring/tracing.py` on every turn; `judge_outputs.v2_trace` rollup matches a small manually-counted fixture session.

## Exit criteria

**P1 numeric targets (the architectural argument's measurable stake).** Measured against `memory/eval_benchmark_v2_simplified.md` with the legacy engine's most-recent benchmark run as baseline. `unverified` outcomes do **not** count as Error-A or Error-B — they are legitimate (the architecture explicitly carves out "we don't know" as not a correctness flip).

| Error | Definition | Required at cutover |
|---|---|---|
| **A** (correct → wrong) | Tutor asserts the student's correct answer is wrong | **≥ 80% reduction** vs legacy baseline |
| **B** (wrong → correct) | Tutor asserts the student's wrong answer is correct | **≥ 80% reduction** vs legacy baseline |
| **C** (incomplete questions) | Question whose canonical can't be derived from what the student sees | **Zero observed on tool-posed questions** (architecture closes this surface); **≥ 90% reduction overall** (covers prose Socratic / reflective surface) |

**Regression floors** (guards against fixing P1 by breaking something else):
- **No other failure category regresses by more than +5 percentage points absolute** vs legacy on the eval benchmark (covers the remaining 16 of the benchmark's 19 categories).
- **Zero P0 safety violations** during the canary window (existing safety floor maintained — `apps/tutoring/judges/safety.py` lifted forward).

**Operational gates:**
- New engine serves the canary cohort for the agreed-upon window with no P1 incidents.
- Safe-template trigger rate below the documented Phase 2 baseline (high rate = move prompts or classifier mistuned).
- Pre-pose refusal rate documented per question source (so the authoring-time gate's eventual landing has a target to hit).
- CLAUDE.md updated, `design/prompts/DEPLOY.md` updated, deprecation comments in place, deletion gate scheduled but not yet executed.
- Kill switch (`NEW_TUTOR=off`) demonstrably works in staging.

**If any P1 target is missed, the cutover does not proceed.** The architectural argument of this refactor is that grader-driven correctness + structural conformance + tool-only posing collapse the P1 error rate. If they don't, the design didn't deliver — drop back to the legacy engine via the kill switch and treat as a Phase 2 reopen, not a Phase 3 retry.

## Phase 3 risks and mitigations

- **Default flip exposes residual bugs invisible in benchmarks.** Mitigation: canary cohort + kill switch + the safe-template fallback (which is structurally fail-safe under any conformance retry failure).
- **Profiler drift compounds across sessions.** Mitigation: end-of-session-only writes start simple; the data is reviewable via the dashboard before a student returns to a new session. If summary text drifts, regenerate from raw turns (kept in `SessionTurn`).
- **Engine_version stickiness creates two long-lived code paths.** Mitigation: time-bound the legacy engine by setting a hard "in-flight legacy sessions complete by date X" and forcing remaining sessions to end / restart. Then deprecated-modules deletion can proceed.

---

# Cross-phase ownership and sequencing

| Concern | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| New module layout | created | filled out | finalized |
| `SessionRuntimeState` schema | shipped | consumed | unchanged |
| `MathVerificationTool` | shipped | consumed | unchanged |
| `StudentGrader` | skeleton | full impl | unchanged |
| `StudentTutor` + move prompts | – | full impl | minor tuning |
| Conformance check | – | full impl | trace-logged |
| Safe terminal templates | – | shipped | logged |
| `StudentProfiler` | both columns shipped | – | full impl |
| `MediaService` | – | inlined | extracted |
| `ExitTicketService` | – | shipped | unchanged |
| Tool-schema provenance | shipped (new tools) | wired | legacy tools dead |
| `NEW_TUTOR` flag | added (default off) | exercised in dev | default flipped |
| `ModelConfig` purposes | enum + 6 default rows | 5 purposes consumed | `PROFILER_SUMMARY` consumed |
| `StudentSkillMastery` write hook (dashboard-only) | – | wired post-grader | unchanged |
| Cross-session repeat avoidance | `asked_questions` column shipped (composite key `"{source}:{id}"` per §4.1) | `cross_session_repeat_guard()` reads at tool boundary (§4.3) | profiler writes (§3.1) |
| **In-session repeat ledger (Jaccard)** | `posed_question_ledger` field in `SessionRuntimeState` | wired into tool-boundary `in_session_repeat_guard()` (§4.3 / §2.1.1) — independent of `pre_pose_check` | unchanged |
| **Bare-answer detection** | `bare_answer_counts_by_objective` field in `SessionRuntimeState` | grader sets `bare_answer` flag; flag biases the `scaffold_hint` / `confirm_and_advance` **move prompts**, not move selection (§2.1.1) | unchanged |
| **Bank-question pre-pose recheck** | flag + token cache shipped | tool boundary runs `pre_pose_check` under `BANK_PREPOSE_RECHECK=on` | flag flips off when authoring-time gate lands (post-MVP) |
| **Session read-window (last 10)** | – | – | implemented as `ORDER BY … LIMIT 10` at profiler read boundary |
| **Physical session archival** | – | – | post-MVP follow-up (not in this plan) |
| Tracing via `apps/tutoring/tracing.py` | – | per-stage spans + per-turn rollup emitted (needed for Phase 2 benchmark gate) | dashboards + alerts on top of existing spans |
| CLAUDE.md + DEPLOY.md updates | none | none | rewrites |
| Legacy deletion | none | none | scheduled |

## Things this plan deliberately does NOT do

- Does not split into more than three phases. Each phase is sized to be a meaningful unit of work with a clear gate; further splitting creates intermediate states that are not independently shippable.
- Does not propose new abstractions beyond what `refactor-analysis.md` §3 specifies.
- Does not migrate or backfill `engine_state`. New column, new sessions.
- Does not touch authoring-time pipelines (analysis §1 scope).
- Does not commit to specific provider/model choices for `GRADER_MATH`, `TUTOR_MOVE`, `CONFORMANCE_CLASSIFIER`, or `PROFILER_SUMMARY` — these are sub-decisions to be made during Phase 2 (or Phase 3 for `PROFILER_SUMMARY`) against the benchmark, per §7. The plan ensures the dispatch surfaces (purpose-based `ModelConfig`, the existing 3-tier judge fallback chain) are in place; what's selected for each call is tuned, not designed here. `GRADER_GROUNDED` and `TUTOR_CLAIM_ADJUDICATOR` are Gemini-pinned because Google-grounding is provider-required (Phase 1 §7); that is an architecture constraint, not a provider preference.
- Does not redesign the cross-session spacing/interleaving scheduler. Out of scope per §4 of the analysis; the data fields `StudentProfiler` writes leave room for a future scheduler to read.

## When to come back to this plan

- Before starting any phase: re-read the phase section + the parts of `refactor-analysis.md` it cites.
- When a phase exit-criterion can't be met: do not proceed to the next phase; surface the blocker and update this plan.
- When the kill switch is flipped in production: this plan does not cover incident response. The kill switch routes traffic back to legacy; the incident review is a separate exercise.
