# skills_snapshot → v2 engine — Plan (2026-05-29)

## Problem

`StudentProfile.skills_snapshot` is a live, populated, per-objective mastery
signal — exactly what the Mastery Learning (Ch.13) + Layering (Ch.16)
principles ask for — and **nothing in the v2 engine reads it**. The router
and move prompts have plumbing for `profile_summary`, which is dormant
(writer deleted 2026-05-27 in `d58cd69`). The actually-populated field is
sitting unused.

There's also a write-side hazard: today the snapshot refreshes for v2
sessions only because `chat_exit_ticket` (`views.py:1685`) detours through
legacy `ConversationalTutor.submit_exit_ticket` (`conversational_tutor.py
:11646-12056`), which calls `refresh_student_snapshot` at line 11989. The
legacy module is scheduled for deletion under the 4-week deprecation gate
(CLAUDE.md "Phase 3 §3.5"). When it goes, v2 sessions stop refreshing the
snapshot on every completed lesson — silently degrading the very signal we
just wired in.

This plan handles both sides in one coordinated change: a small read-side
wiring + a defense-in-depth write call now, then the v2-native submission
path before legacy deletion.

## Current state (from audit)

- `ContextManager.assemble_context()` builds `TutoringContext` at
  `context_manager.py:108-200`. ORM reads happen at lines 122-177; new
  fields are injected before the return at line 180.
- `TutoringContext` is a frozen Pydantic model at
  `contracts/tutoring.py:103-159`. 19 fields, mix of primitives + one
  typed nested model (`SessionRuntimeState`). Adding a field is a 1-line
  schema change.
- `RouterRequest` (`contracts/tutoring.py:215-297`) carries 45+ flat
  fields including `profile_summary`. No typed JSON convention here —
  all primitives + `list[dict]`.
- `StudentTutor._build_user_prompt()` at `student_tutor.py:487-538`
  assembles per-turn user prompt via `_render_*_block` helpers. Slot for
  a new block: between `lesson_content_block` (line 517) and
  `reason_block` (line 518).
- `render_router_user_prompt()` at `router_prompts.py:344-374` does the
  same for the router LLM. Slot: between `profile_block` (line 364) and
  the transcript header (line 365).
- Snapshot read helper: `competency_tracker.student_skills_snapshot(
  student, course)` at `competency_tracker.py:415-453`. Returns
  `{objective_tag: {pct, level, source, attempts}}` with levels
  `mastered` (≥70), `developing` (40-70), `weak` (<40). Tags use
  `lesson.objective` as the canonical key per `collect_objective_
  signals_for_course` line 151.
- Lesson objective fields: `lesson.objective` (single primary,
  `CharField(500)`) + `lesson.enabling_objectives` (list, JSONField) +
  `LessonStep.enabling_objective` (per-step). No existing helper filters
  the snapshot to a lesson's objectives.
- Writer call sites today: `views.py:2464` (`summative_submit`),
  `views.py:2668` (`lesson_pretest`), `conversational_tutor.py:11989`
  (legacy in-session exit ticket). Only the last is engine-coupled.
- v2's `ExitTicketService` (`exit_ticket.py`) handles selection +
  per-question grading; **no submit path exists**. The chat-exit-ticket
  POST flow goes through legacy regardless of session engine.

## Target design

Three-piece coordinated change, in one PR series:

1. **Read-side wiring (Phase 1).** Load `skills_snapshot[course_id]` in
   `ContextManager.assemble_context()`, filter to lesson-relevant
   objectives, thread through `TutoringContext` → `RouterRequest` +
   `StudentTutor._build_user_prompt`. Render as a compact block. Add one
   short paragraph of prompt guidance — only because the data is now
   actually present.

2. **Defense-in-depth writer (Phase 1).** Add an idempotent
   `refresh_student_snapshot` call in `chat_exit_ticket` view AFTER the
   legacy call completes successfully. Legacy still writes its copy;
   ours is a no-op then but becomes load-bearing the moment legacy is
   deleted. Cheap insurance: ~10 lines.

3. **v2-native submission path (Phase 2).** Extract the snapshot-relevant
   side effects from `_submit_exit_ticket_inner` into
   `ExitTicketService.submit(answers)`. Wire `chat_exit_ticket` to
   dispatch by `session.engine_version`. Remove the defense-in-depth
   call once v2 owns submission end-to-end.

## Data model changes

None. No new fields on `StudentProfile`, no migrations, no schema
churn. Two new fields on the in-memory Pydantic contracts:

- `TutoringContext.skills_snapshot: dict[str, dict] = Field(default_
  factory=dict)` — shape `{objective_tag: {pct, level, source,
  attempts}}`, already-filtered to lesson-relevant tags. Empty dict
  when the student has no signal yet.
- `RouterRequest.skills_snapshot: dict[str, dict] = Field(default_
  factory=dict)` — same shape, threaded through `MoveRouter.build_
  router_request` from the context field.

## Backend changes

### Phase 1 — read side

**`apps/tutoring/v2/services/context_manager.py`** — `assemble_context`
(lines 108-200):
- After line 175, add `skills_snapshot = self._load_filtered_skills_
  snapshot(student=student, lesson=lesson)`.
- New helper `_load_filtered_skills_snapshot(*, student, lesson) -> dict`:
  - Read `StudentProfile.skills_snapshot.get(str(course.id), {})`.
  - Build the relevant-tag set: `{lesson.objective}.union(
    set(lesson.enabling_objectives or []))` + each `LessonStep.
    enabling_objective` from `lesson.steps.all()`.
  - Normalize both sides via `competency_tracker._normalize_tag`.
  - Return `{tag: data for tag, data in slice.items() if normalized(
    tag) in relevant_set}`.
  - Fail-soft: any exception logs warning + returns `{}`.
- Thread into `TutoringContext(...)` keyword args at the return.

**`apps/tutoring/v2/contracts/tutoring.py`** — add the field to both
`TutoringContext` (after line 158) and `RouterRequest` (after line
296).

**`apps/tutoring/v2/services/move_router.py`** — `build_router_request`
(line 642 already pulls `profile_summary`): add `skills_snapshot=
context.skills_snapshot or {}` adjacent to it.

**`apps/tutoring/v2/services/router_prompts.py`**:
- New `_render_skills_snapshot_block(request: RouterRequest) -> str`.
  Returns empty string when `request.skills_snapshot` is empty (skip
  rendering — per the user's no-noise directive). Otherwise:
  ```
  === Your skill levels on this lesson's objectives ===
  - <tag>: <level> (<attempts> attempts)
  - ...
  ```
  Sort tags alphabetically. Cap to 8 entries; collapse the rest into
  "(+ N more)". Show level, not pct.
- Insert block call in `render_router_user_prompt` between line 364
  (profile block) and line 365 (transcript header). Skip its section
  header when the block returns empty.
- Add ~3 lines to `SHARED_ROUTER_SYSTEM` ONLY when the block is
  non-empty: "When the `Your skill levels` section is present, treat
  `weak` prerequisites as a Mastery Learning Ch.13 signal to favour
  `worked_example` on wrong-with-no-method; treat `mastered` as a
  Layering Ch.16 signal that the student can handle composed
  follow-ups (bias `confirm_and_extend` over `confirm_and_advance` on
  rich correct)."

**`apps/tutoring/v2/services/student_tutor.py`**:
- New `_render_skills_snapshot_block(context: TutoringContext) -> str`
  on the class, same shape as the router's renderer.
- Insert in `_build_user_prompt` between `lesson_content_block`
  (line 517) and `reason_block` (line 518).
- Add ~2 lines to the bodies of `CONFIRM_AND_EXTEND`,
  `WORKED_EXAMPLE`, and `EXPLAIN` in `move_prompts.py` ONLY referring
  to the block when present (Mastery + Layering language matching the
  router system prompt addition).

**Empty-data handling.** If `_load_filtered_skills_snapshot` returns
`{}`, the field stays empty, the block renderers return "", the user
prompts skip the section header entirely. **Zero noise when there's
no signal.** The prompt instructions can still mention the block
because they're scoped "when the section is present" — the LLM
doesn't see a contradiction.

### Phase 1 — write side (defense in depth)

**`apps/tutoring/views.py`** — `chat_exit_ticket` (lines 1685-1736):
- After line 1717 (the existing `competency = attempt_response_block
  (...)` call), add a guarded call:
  ```python
  try:
      from apps.tutoring.competency_tracker import refresh_student_snapshot
      if session.lesson.unit and session.lesson.unit.course:
          refresh_student_snapshot(request.user, session.lesson.unit.course)
  except Exception as exc:
      logger.warning("snapshot refresh in chat_exit_ticket failed: %s", exc)
  ```
- Idempotent: legacy already called it inside `submit_exit_ticket`;
  this is a no-op now but becomes the only writer once legacy is
  deleted.

### Phase 2 — v2-native submission

**`apps/tutoring/v2/services/exit_ticket.py`** — add
`ExitTicketService.submit(*, session, answers) -> ExitTicketSubmission`:
- Per-question grading via existing `grade_response`.
- Aggregate via existing `aggregate`.
- Side effects in this order (parity with
  `conversational_tutor.py:11952-11996`):
  1. Create `ExitTicketAttempt` rows for the active participants
     (single-user sessions are the common case; group-session parity
     is out-of-scope unless the runtime has multiple participants).
  2. Update `StudentLessonProgress` via the existing
     `_update_competency` helper (lift from legacy as a module-level
     function in `competency_tracker.py` if it isn't already there).
  3. Call `refresh_student_snapshot(participant, course)`.
  4. Update `TutorSession.mastery_achieved` + `engine_state` exit-
     ticket keys.
- Return a `ExitTicketSubmission` dataclass: `{message: str, results:
  list, score: int, passed: bool, phase: str = "completed"}`. The
  legacy `TutorMessage.exit_ticket_data` shape is preserved.

**Gamification side effects** (legacy lines 11998-12056 — XP, streak,
achievements) are **out of scope** for Phase 2. They live in the
gamification subsystem and don't depend on the v2 engine — best done
as a follow-up that lifts the gamification helper into a callable
that runs from `chat_exit_ticket` regardless of engine.

**`apps/tutoring/views.py`** — `chat_exit_ticket`:
- Dispatch by `session.engine_version`:
  - `'v2'` → call `ExitTicketService(session).submit(answers)`.
  - else → existing legacy path.
- The defense-in-depth `refresh_student_snapshot` call from Phase 1
  becomes redundant on the v2 branch (the service handles it) but
  stays harmless. Remove in a cleanup commit after Phase 2 ships
  green.

## Tests

**Phase 1 reads — new file `apps/tutoring/v2/tests/test_skills_
snapshot_wiring.py`:**
- `_load_filtered_skills_snapshot` returns the lesson-relevant
  intersection.
- Returns `{}` when `StudentProfile` is missing, when the course slice
  is empty, when no overlap exists with lesson objectives, and when
  the underlying read raises.
- Tag normalization works across whitespace + case differences.
- `_render_skills_snapshot_block` returns `""` on empty input; renders
  sorted tag list on populated; caps at 8 with "+ N more".
- Router system prompt addition is present in `SHARED_ROUTER_SYSTEM`
  template but only fires when the section is non-empty (content
  regression test).

**Phase 1 writes — new tests in `test_skills_snapshot_wiring.py`:**
- `chat_exit_ticket` view test: when legacy `submit_exit_ticket`
  succeeds, `refresh_student_snapshot` ends up called (at least
  once — legacy AND defense-in-depth both fire). Mock
  `refresh_student_snapshot` and assert `call_count >= 1`.
- When `chat_exit_ticket` raises from the legacy call, the defense-in-
  depth call is skipped (it's after the try block's success path).

**Phase 2 writes — new file `apps/tutoring/v2/tests/test_exit_ticket_
submission.py`:**
- `ExitTicketService.submit` creates an `ExitTicketAttempt` with the
  expected `answers` JSON shape.
- `refresh_student_snapshot` called exactly once per participant.
- `StudentLessonProgress` updated.
- Return shape parity with legacy `TutorMessage.exit_ticket_data`.

**Regression:** the full v2 test suite (`pytest apps/tutoring/v2/
tests/`) must stay green at every phase.

## Out of scope

- New writers for `profile_summary` (the dormant field). If we want
  qualitative cross-session memory later, that's its own design pass.
- New fields on `StudentProfile`. The existing `skills_snapshot` is
  enough.
- Wiring `asked_questions` for cross-session repeat avoidance (the
  other dormant field). Separate plan.
- Gamification side effects in Phase 2 — XP / streak / achievements
  belong to a different subsystem and don't gate the snapshot
  refresh.
- Group-session exit-ticket parity in v2. The current v2 code path is
  single-user; group sessions go through legacy until v2 grows
  participant-aware exit-ticket handling.
- Touching `profile_summary` rendering. Leave the existing
  `=== Student profile summary ===` section in `router_prompts.py` as
  it is — it'll render `(no profile summary yet)` until someone
  builds a writer. No regression there.
- Changing the math/non-math grader. The grader sees the canonical;
  `skills_snapshot` lives upstream of grading and doesn't affect
  per-turn verdicts.

## Phased delivery

| Phase | Work item | Est. (solo days) |
|---|---|---|
| **1** | Contract fields + `_load_filtered_skills_snapshot` + block renderers + prompt prose + defense-in-depth writer call | 1.0 |
| **1 tests** | Layer 1 + Layer 2 + regression sweep | 0.5 |
| **2** | `ExitTicketService.submit` + view dispatch + tests + remove defense-in-depth on v2 branch | 1.5 |
| **2 tests** | Integration tests on the v2 native path | 0.5 |
| **3** | Cleanup commit when legacy is actually deleted (4-week gate per CLAUDE.md) | 0.25 |

Total: ~3.75 days of focused work, plus the 4-week wait for the
deprecation gate before Phase 3 cleanup.

Phase 1 is the immediately valuable piece. It can ship independently
of Phase 2 because the legacy detour still runs and the defense-in-
depth call is idempotent. Phase 2 is what lets legacy actually be
deleted without losing the snapshot writer.

## Open questions

**Q1. Which lesson-to-objective mapping?** Recommend: union of
`lesson.objective` + `lesson.enabling_objectives` + each
`LessonStep.enabling_objective`, normalized via `competency_tracker.
_normalize_tag`. Reason: matches how `collect_objective_signals_for_
course` populates the snapshot (it uses `lesson.objective` for per-
lesson exit tickets, line 151); enabling_objectives covers the
edge case where the snapshot was populated from a summative test
that drilled per-step tags.

**Q2. Show unassessed objectives?** Recommend: no. Render only tags
where the student has real data (`mastered`/`developing`/`weak`).
Per the user's directive — only show data we actually have. If a
lesson has 5 objectives and the student has signal on 2, show the
2.

**Q3. Show pct numbers or just the level label?** Recommend: level
only ("Read scale ratios: mastered (3 attempts)"). The pct numbers
are precise but precision is wasted on the LLM; the level
classification is the actionable signal. Skip pct in the rendered
block.

**Q4. Defense-in-depth write call now or wait for Phase 2?**
Recommend: ship in Phase 1. `refresh_student_snapshot` is
idempotent; the cost of a duplicate write is one extra ORM update
per exit-ticket submission. The cost of forgetting and shipping
Phase 2 simultaneously with legacy deletion is a silent regression.
Defensive write is cheap insurance.

**Q5. Add prompt guidance to the router system prompt?** Recommend:
yes, but minimally — one paragraph citing Mastery Ch.13 + Layering
Ch.16. Keep it scoped to "when the `Your skill levels` section is
present" so the LLM doesn't try to reason from missing data. The
move-prompt additions (in `CONFIRM_AND_EXTEND`, `WORKED_EXAMPLE`,
`EXPLAIN`) are likewise gated on section presence.

**Q6. Should `_load_filtered_skills_snapshot` be called eagerly on
every turn?** Recommend: yes. The snapshot lives on `StudentProfile`,
a single ORM read with `select_related('student_profile')`. Cheaper
than the LLM call it informs. Don't cache across turns — exit ticket
submissions mid-session shouldn't happen, but a Phase 2 defense-in-
depth would refresh mid-session via the view, so always-fresh reads
keep the contract simple.

## Risks

- **R1**. Lesson objective taxonomy drift. The snapshot tags use
  `lesson.objective` as authored, normalized via
  `_normalize_tag`. If lessons are re-authored with different
  objective strings, the snapshot's old keys won't match the new
  lesson's relevant set. Mitigation: documented in
  `competency_tracker._normalize_tag`; the filter falls through
  silently (no objectives match → empty block → no prompt section).
  No code change required for now; flag in the eval reports if it
  surfaces.

- **R2**. Phase 1's defense-in-depth call adds one ORM write per
  exit-ticket submission even when legacy already did it.
  Idempotent and cheap (~few ms). Not a blocker.

- **R3**. Phase 2's `ExitTicketService.submit` must preserve the
  exact `exit_ticket_data` JSON shape consumed by the chat client.
  Schema drift could break the frontend modal. Mitigation: snapshot
  the legacy response shape in a fixture test, assert v2 produces
  byte-equivalent JSON.

- **R4**. Gamification XP/streak side effects live in legacy's
  `_complete_session_with_results` lines 11998-12056. They were
  explicitly scoped out, but if the chat client's gamification
  toast depends on a side effect that fires only on the legacy
  path, v2 sessions lose the toast. Mitigation: verify in Phase 4
  live verification; if breaking, lift gamification into a
  view-level call that runs regardless of engine.

## Next step

Phase 1 read-side wiring: contract field + `_load_filtered_skills_
snapshot` helper + block renderers + the Q1-Q3-Q5 prompt prose, all
in a single commit. The defense-in-depth writer call goes in the
same commit since both pieces are no-ops on absent data.
