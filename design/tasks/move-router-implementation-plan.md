# LLM Move Router — Implementation Plan

**Status**: Approved direction; spec ready to implement.
**Owner**: Roy Manzi (review) / Claude (implementation).
**Created**: 2026-05-27.
**Supersedes**:
- `apps/tutoring/v2/services/move_selection.py` (deterministic 14-gate `select_move`) — **deleted**.
- `apps/tutoring/v2/services/intent_classifier.py` (Haiku help-request detector) — **deleted**.
**Cutover**: immediate. No flag-gated migration. The new path becomes the production routing layer in a single PR.
**Related**:
- `design/science-principles.md` — the 13 principles the router weighs.
- `design/tasks/two-llm-grader-implementation-plan.md` — the structural pattern this plan mirrors (two LLMs doing different jobs on different inputs, with a deterministic veto layer).
- `test-reports/MATHS-S1-evaluation-2026-05-27-run7.md`, `test-reports/GEO-S5-evaluation-2026-05-27-run7.md` — root-cause evidence for the redesign.

---

## 1. Why this plan exists

The v2 routing layer has three structural problems that produced the run-7 P1 errors:

1. **`select_move` is counter-driven, not transcript-aware.** It only sees `verdict` + counters (`attempts`, `objective_progress`, `move_history`, `doing_rate_window`). It does not read what the student actually said. Consequence: `verdict=WRONG, attempts >= 3 → name_misconception` fires whether the three wrong answers reveal *the same* misconception (where the move is correct) or *three different ones* (where naming a single "slip" is pedagogically wrong). The deterministic ladder cannot tell those apart.

2. **The 14-gate decision table is flow control disguised as safety floors.** Only 4 of the 14 gates are genuine safety floors (turn caps, repeat detection, objective-evidence force-close, stickiness). The other 10 are pedagogical decisions made by a state machine with no semantic context. This is incompatible with **Goal 2: natural, unscripted, adaptive sessions**.

3. **`classify_student_intent` is a single point of failure.** A standalone Haiku call whose fail-soft default (`"attempting"`) silently disables the help-request branch on any LLM hiccup (the GEO-S5 P1-3 root cause). One LLM, one job, no backstop.

The Two-LLM Grader redesign (`design/tasks/two-llm-grader-implementation-plan.md`) is the structural template: two LLMs doing **different jobs on different inputs**, mediated by a deterministic comparator. Failure modes do not correlate because the LLMs are not making the same decision.

This plan applies the same shape to routing:

- **Router LLM** decides *what to do* (move selection + principle emphasis + focus note), transcript-aware.
- **Move LLM** (the existing `StudentTutor`) decides *how to say it* (renders the chosen move's prompt with principles baked in).
- **Deterministic safety floors** (5 of them) sit *after* the router and can override.

---

## 2. Target architecture

```
                  ┌──────────────────────────────────────────────────┐
                  │                  TutorEngine.pick_move            │
                  │                                                   │
  GradingResult ──┤                                                   │
  transcript      ├─► MoveRouter.route                                │
  profile_summary │   ├─ LLM call (Sonnet 4.6, cached preamble)       │
  objective       │   └─► RouterDecision {                            │
  step            │        chosen_move,                               │
  move_history    │        principle_emphasis: list[str],             │
  safety_state    │        focus_note: str,                           │
                  │        rationale: str,                            │
                  │      }                                            │
                  │                          │                        │
                  │                          ▼                        │
                  │   apply_safety_floors(decision, runtime_state)    │
                  │   ├─ turn caps                                    │
                  │   ├─ objective evidence saturation                │
                  │   ├─ name_misconception repeat block              │
                  │   ├─ posing block (ledger saturated)              │
                  │   └─ help-request regex backstop                  │
                  │                          │                        │
                  │                          ▼                        │
                  │   final_move + focus_note + principle_emphasis    │
                  └──────────────────────────────────────────────────┘
                                             │
                                             ▼
                  StudentTutor.respond(move=final_move,
                                       focus_note=..., principles=[...])
```

**Two LLM calls per turn that involves routing** (grader + router), plus the existing StudentTutor call. Latency / cost are not constraints per the 2026-05-27 directive ("quality and robustness over latency").

### 2.1 MoveRouter — new service

A new service mirroring `StudentGrader`'s shape: stateless, constructor-injectable LLM client factory, single public method, Pydantic-typed output, span instrumentation.

**Inputs** (passed as a Pydantic `RouterRequest`):

| Input | Source | Why the router needs it |
|---|---|---|
| `last_n_turns` (10) | `TutoringContext.full_transcript` | Transcript awareness — the core thing `select_move` is missing today |
| `grader_verdict` | `GradingResult` | What the grader concluded (CORRECT / WRONG / PARTIAL / UNVERIFIED) |
| `grader_reason_code` | `GradingResult.reason_code` | `meta_input`, `self_reported_guess`, `arithmetic_failed`, etc. — drives nuanced routing |
| `student_safe_feedback` | `GradingResult.student_safe_feedback` | The `what_right` / `what_missing` / `first_misconception_redacted` fields the move prompt will use |
| `profile_summary` | `StudentProfile.profile_summary` | Struggling / advanced / fresh context |
| `objective` | `TutoringContext.current_objective` | What's being taught |
| `lesson_step` | `TutoringContext.lesson_step` | Step content + media catalog summary |
| `move_history` | `runtime_state.move_history[-5:]` | What was tried recently |
| `objective_progress` | `runtime_state.objective_progress[objective]` | Counts: correct, wrong, partial, attempts |
| `safety_state` | `runtime_state.safety_valve_counters` + `unverified_run_length` | So the router can pre-emptively pick `close_topic`/`explain` before floors override |
| `open_question` | `runtime_state.open_question` (rendered_stem + has_pending) | Whether a question is in flight |
| `pose_tool_available` | `TutorEngine._pose_tool_available()` | Whether the router can pick a pose-capable move |

**Output** (Pydantic `RouterDecision`):

```python
class RouterDecision(BaseModel):
    chosen_move: Literal[
        "confirm_and_advance", "confirm_and_extend",
        "scaffold_hint", "name_misconception", "worked_example",
        "explain", "pivot", "close_topic",
    ]
    principle_emphasis: list[str]  # 1-3 principle names from the 13
    focus_note: str                # 1-2 sentences, ≤ 50 tokens
    rationale: str                 # 1 sentence for audit trail
```

**Note on the move set.** `pose_question` is **deleted** from the move table. In the prior architecture it played two roles: (a) "neutral retrieval — just ask, no preamble", and (b) the engine's way to force a tool call via `tool_choice={"type":"any"}`. Both roles disappear under the new design:

- (a) is covered by the other 8 moves all ending in a tool-posed question. Lesson opening = `explain` or `worked_example` + tool pose. Mid-flow continuation = `confirm_and_advance` + tool pose. There is no remaining case where "ask with literally no framing" is the right pedagogical move.
- (b) is already enforced by the conformance gate `all__no_assessment_in_prose` (`verdict_matrix.py:158-166`). If a pose-capable move emits an assessment question in prose, conformance rejects it. The force-mode is no longer needed.

`principle_emphasis` is constrained to names from `design/science-principles.md` (Active Learning, Direct Instruction, Deliberate Practice, Mastery Learning, Cognitive Load, Automaticity, Layering, Non-Interference, Spaced Repetition, Interleaving, Testing Effect, Targeted Remediation, Gamification — 13 total). The router system prompt enumerates these; Pydantic validates membership.

`focus_note` is **what to address this turn specifically**, not how to say it. Example: *"Student confused condensation with precipitation. Name that slip; do not reveal the canonical."*

`rationale` is for the v2 observability trace — one sentence the auditor can grep on.

### 2.2 SHARED_ROUTER_SYSTEM — the router's system prompt

Lives in `apps/tutoring/v2/services/router_prompts.py`. Three sections:

1. **Role + output schema** — strict JSON, Pydantic-validated, no prose.
2. **The 8 moves** — one-line description + when-to-pick guidance, mirroring `move_selection.py:174-341`'s decision logic but written as guidance, not as gates. `pose_question` is **not** in this list — see §2.1.
3. **The 13 principles** — one-line behavioral imperatives from `design/science-principles.md` Table 1.

System prompt is ~2.5K tokens, stable across all turns → prompt caching with 1-hour TTL (same shape as `MATH_DSL_SYSTEM`).

### 2.3 Safety floors (deterministic, post-router)

Five floors in `apps/tutoring/v2/services/safety_floors.py`, applied in order. Each can override the router's `chosen_move`. Pure functions, no LLM.

| # | Floor | Trigger | Override to | Why |
|---|---|---|---|---|
| 1 | Turn caps | `turns_in_session ≥ 40` OR `turns_on_current_objective ≥ 12` OR `verdictless_turns ≥ 6` | `close_topic` | Hard cost / loop bounds — same as today's `_safety_valve_override` |
| 2 | Objective evidence saturation | `obj_progress.correct ≥ 2 AND attempts ≥ 3` | `close_topic` **with atomic `current_step_index` advance** | Mastery floor (Principle #4). Fixes MATHS P1-4 — close and advance must be atomic |
| 3 | `name_misconception` repeat | Router picked `name_misconception` AND move_history shows it fired in the last 2 turns without a verdict-change | `pivot` | The misconception isn't resolving; different angle |
| 4 | Pose ledger saturation | Router picked any non-terminal move AND no un-posed slots remain for this objective | `close_topic` if `obj_progress.correct ≥ 1` else `pivot` | Cannot pose; every non-terminal move is now pose-capable, so saturation forces a terminal transition |
| 5 | Help-request regex backstop | Latest student turn matches `\b(i don'?t (understand|get|know how)|what (is\|does)|explain|show me|teach me|i'?m (lost|stuck))\b` AND router did NOT pick `explain`/`worked_example` | `explain` (or `worked_example` if profile_summary contains "struggl") | Belt to the router's braces — defends against LLM judgment failure on a high-cost branch (GEO P1-3 shape) |

Floors are applied **sequentially**; later floors observe earlier overrides. Each floor that fires emits a `router.floor_override` span with the from-move, to-move, and floor name for audit.

### 2.4 Integration into TutorEngine

`TutorEngine.pick_move` is rewritten:

```python
def pick_move(
    self, *,
    context: TutoringContext,
    verdict: Optional[GradingResult],
    student_input: str,
) -> tuple[str, str, list[str]]:
    """Returns (final_move, focus_note, principle_emphasis)."""
    request = RouterRequest.from_context(
        context=context, verdict=verdict, student_input=student_input,
    )
    decision = self.move_router.route(request)
    final_move, override_floor = apply_safety_floors(
        decision=decision, runtime_state=context.runtime_state,
        student_input=student_input,
    )
    return final_move, decision.focus_note, decision.principle_emphasis
```

The `pick_move` return type widens from `str` to `(str, str, list[str])`. Call sites in `respond` thread `focus_note` and `principle_emphasis` into `StudentTutor.respond`.

### 2.5 StudentTutor changes

`StudentTutor.respond` signature gains two kwargs:

```python
def respond(
    self, context, verdict, move, *,
    focus_note: str = "",
    principle_emphasis: Optional[list[str]] = None,
    media_catalog=None, student_input="",
) -> TutorResponse:
```

The user prompt builder appends a **"This turn specifically:"** block when `focus_note` is non-empty:

```
This turn specifically:
- Focus: {focus_note}
- Principles to emphasize: {", ".join(principle_emphasis)}
```

Placed AFTER the per-move prompt body, BEFORE the transcript — so the move's general guidance is contextualized by this turn's specific direction.

`POSE_CAPABLE_MOVES` is reframed as "every move except `close_topic`" — and since `pose_question` is deleted, the set becomes the full 7 non-terminal moves:

```python
# Before: 6 moves (including pose_question)
# After:  7 moves (every move except close_topic; pose_question deleted)
POSE_CAPABLE_MOVES = frozenset({
    "confirm_and_advance", "confirm_and_extend",
    "scaffold_hint", "name_misconception", "worked_example",
    "explain", "pivot",
})
```

The `tool_choice={"type":"any"}` force-mode in `student_tutor.py:297-299` is **deleted**. All pose-capable moves use `tool_choice="auto"`; the conformance gate `all__no_assessment_in_prose` enforces that any visible assessment question must have come through the tool.

The Active Learning floor (every turn ends on a student action) is enforced by the per-move prompt body — every non-terminal move's prompt instructs the LLM to end with a tool-posed question — plus the conformance gate, plus the question-extractor's `move_body_present` checks.

### 2.6 What does NOT change

- `StudentGrader` — runs first every turn, unchanged.
- `StudentTutor`'s per-move prompt bodies (`MOVE_PROMPTS` in `move_prompts.py`) — unchanged; the router feeds them new context but does not rewrite them.
- The conformance pipeline (`apps/tutoring/v2/services/conformance/`) — unchanged.
- Phase A `validate_pose` and Phase B `commit_pending_pose` — unchanged.
- The `pose_question` tool schema — unchanged.
- The v2 observability dashboard (`/dashboard/v2-observability/`) — gains new spans (`router.decision`, `router.floor_override`); no breaking changes.
- The kill switch `NEW_TUTOR=off` — unchanged; still routes to legacy `ConversationalTutor`.

---

## 3. Files touched

| File | Change | Lines |
|---|---|---|
| `apps/tutoring/v2/services/move_router.py` | **New.** `MoveRouter` service: `route(request: RouterRequest) -> RouterDecision`. Mirrors `StudentGrader` shape — constructor injection, span emission, Pydantic parsing, fail-soft contract. | ~300 new |
| `apps/tutoring/v2/services/router_prompts.py` | **New.** `SHARED_ROUTER_SYSTEM` constant + `render_router_user_prompt(request)`. ~2.5K tokens stable, ~600 tokens dynamic per turn. | ~250 new |
| `apps/tutoring/v2/services/safety_floors.py` | **New.** `apply_safety_floors(decision, runtime_state, student_input) -> (move, floor_name)`. Pure functions, one per floor. | ~180 new |
| `apps/tutoring/v2/contracts/tutoring.py` | **Extend.** Add `RouterRequest` and `RouterDecision` Pydantic models. | ~80 new |
| `apps/tutoring/v2/services/tutor_engine.py` | **Rewrite** `pick_move`. **Delete** `_safety_valve_override` (folded into `apply_safety_floors`). **Delete** the call to `classify_student_intent` and `intent_to_move`. Thread `focus_note` + `principle_emphasis` through to `StudentTutor.respond`. | ~120 net (mostly delete) |
| `apps/tutoring/v2/services/student_tutor.py` | **Extend** `respond` signature with `focus_note` + `principle_emphasis`. **Extend** `_build_user_prompt` to inject the "This turn specifically:" block. **Shrink** `POSE_CAPABLE_MOVES` to the 7 non-terminal moves (every move except `close_topic`); **delete** the `tool_choice={"type":"any"}` force-mode branch at `:297-299`. | ~60 net |
| `apps/tutoring/v2/services/move_prompts.py` | **Delete** the `POSE_QUESTION` move prompt body. **Audit** the remaining 8 prompt bodies — each must instruct the LLM to end on a tool-posed question (where pose-capable) so the Active Learning floor is enforced at the prompt level. | ~80 net (delete + audit-driven edits) |
| `apps/tutoring/v2/services/move_selection.py` | **Delete.** | -350 |
| `apps/tutoring/v2/services/intent_classifier.py` | **Delete.** | -250 |
| `apps/llm/models.py` | **Add** `Purpose.MOVE_ROUTER = 'move_router'`. Add to the temperature-0 purposes list (`:314-325`). Add env-override `MOVE_ROUTER_MODEL_OVERRIDE`. **Deprecate** `Purpose.CONFORMANCE_CLASSIFIER`'s intent-classifier usage — keep the purpose; the classifier itself goes away. | ~15 net |
| `apps/llm/migrations/0038_add_move_router_purpose.py` | **New migration.** Adds the `MOVE_ROUTER` ModelConfig row pinned to `claude-sonnet-4-6`. Removes the legacy `INTENT_CLASSIFIER` row if it exists. | ~50 new |
| `apps/tutoring/v2/tests/test_move_router.py` | **New.** Comprehensive router behavior tests (see §5). | ~600 new |
| `apps/tutoring/v2/tests/test_safety_floors.py` | **New.** Per-floor unit tests + ordering tests. | ~250 new |
| `apps/tutoring/v2/tests/test_move_selection.py` | **Delete.** Tests pinned to the deterministic ladder no longer apply. | -400 |
| `apps/tutoring/v2/tests/test_routing_dispatch.py` | **Rewrite** to test the new `pick_move` integration end-to-end with a `FakeRouter`. | ~200 net |
| `apps/tutoring/v2/tests/test_session_writes_runtime_state.py` | **Update** to use the new `pick_move` return shape. | ~30 |
| `apps/tutoring/v2/tests/test_doing_rate_bias.py` | **Delete.** Doing-rate bias was a `select_move` mechanic; the router observes the transcript directly and doesn't need a precomputed bias. | -180 |
| `apps/tutoring/v2/tests/test_run5_fixes.py` | **Audit + update** — some assertions pin to deterministic move outcomes; rephrase as router behavior with a FakeRouter. | ~40 |
| `CLAUDE.md` | **Update** the "Grader-driven correctness" paragraph to describe the new router + safety floors. | ~30 |

Net: ~2400 lines added (mostly router code + tests), ~1180 lines deleted (deterministic ladder + intent classifier). Net +1200 lines; the codebase shrinks in surface area (one fewer service) while gaining router code.

---

## 4. Implementation order

Each step is a checkpoint where tests stay green. Steps 1-4 are non-invasive (new code, not yet wired); step 5 is the cutover.

1. **Add `Purpose.MOVE_ROUTER` + migration `0038`** (1 file + 1 migration). Tests still green. No code path uses the new purpose yet.

2. **Add `SHARED_ROUTER_SYSTEM` prompt + `render_router_user_prompt`** in `router_prompts.py`. Add unit tests for prompt rendering (deterministic — does the prompt contain the student input, transcript, verdict, move table, principle table?). No integration yet.

3. **Add `RouterRequest` + `RouterDecision` contracts** in `contracts/tutoring.py`. Add Pydantic round-trip tests.

4. **Implement `MoveRouter.route` + `apply_safety_floors`** in their new modules. Unit tests against stubbed Sonnet client. The router is callable from tests but not yet wired into `TutorEngine`.

5. **Cutover PR — single commit:**
   - Rewrite `TutorEngine.pick_move`.
   - Update `StudentTutor.respond` signature + user prompt builder.
   - Shrink `POSE_CAPABLE_MOVES`.
   - **Delete** `move_selection.py` + `intent_classifier.py`.
   - **Delete** `test_move_selection.py` + `test_doing_rate_bias.py`.
   - Rewrite `test_routing_dispatch.py` against `FakeRouter`.
   - Update `test_run5_fixes.py` + `test_session_writes_runtime_state.py`.
   - Update `CLAUDE.md`.

6. **Run MATHS-S1 + GEO-S5 evaluation scenarios** end-to-end against the cutover engine. Target: 0 P1 errors on both scenarios.

7. **Run the full benchmark** (`memory/eval_benchmark_v2_simplified.md`) against the cutover engine. Target: P1-class category counts ≤ current production baseline; total category coverage unchanged or better.

---

## 5. Test plan

### 5.1 Router tests (required to pass before "done")

Located in `apps/tutoring/v2/tests/test_move_router.py`. Each test uses a real (deterministic) prompt-rendering pass + a stubbed Sonnet client returning crafted `RouterDecision` JSON. The goal is to verify the **prompt → response → parse → output** pipeline, not Sonnet's pedagogical judgment.

```python
# ─── Core routing behavior ─────────────────────────────────────

def test_router_picks_explain_on_help_request_in_transcript():
    """Last student turn: 'i dont understand. what is condensation'.
    No prior open question. Stubbed Sonnet returns chosen_move=explain.
    Router output passes through floors unchanged (floor #5 would
    have overridden anyway — verified separately)."""

def test_router_does_not_emit_pose_question_as_a_move():
    """Stubbed Sonnet returns chosen_move='pose_question'.
    Pydantic ValidationError — pose_question is no longer in the
    Literal. Fail-soft fallback fires."""

def test_router_picks_confirm_and_extend_on_first_correct_streak():
    """Student gave a fully-shown correct Pythagoras solution. Grader
    verdict=CORRECT. Stubbed Sonnet returns confirm_and_extend with
    principle_emphasis=[Deliberate Practice, Active Learning]."""

def test_router_picks_name_misconception_when_grader_reason_known():
    """Grader returned WRONG with reason_code=known_misconception.
    Router output includes name_misconception + focus_note naming
    the specific misconception from student_safe_feedback."""

def test_router_picks_close_topic_on_objective_evidence():
    """Objective_progress: correct=2, attempts=2, ratio=1.0.
    Router picks close_topic. (Floor #2 would force this anyway —
    test verifies the router agrees rather than fighting the floor.)"""

# ─── Pydantic validation ──────────────────────────────────────

def test_router_rejects_invalid_chosen_move():
    """Stubbed Sonnet returns chosen_move='guess'. Router raises
    ValidationError; caller treats as failure and falls back to
    a conservative default (covered in fail-soft test below)."""

def test_router_rejects_unknown_principle_in_emphasis():
    """principle_emphasis=['Active Learning', 'Telepathy']. Router
    raises ValidationError because Telepathy is not in the closed
    set of 13."""

def test_router_rejects_overlong_focus_note():
    """focus_note > 250 chars. Pydantic constraint trips, ValidationError."""

# ─── Fail-soft contract ────────────────────────────────────────

def test_router_fail_soft_on_llm_outage():
    """Sonnet client raises. Router returns a conservative
    RouterDecision: chosen_move based on verdict (correct→
    confirm_and_advance, wrong→scaffold_hint, partial→scaffold_hint,
    unverified/None→pose_question or explain on objective_just_opened).
    principle_emphasis defaults to ['Active Learning']. focus_note
    is empty. rationale='router_unavailable_fallback'.
    Span emitted with fail_soft=true."""

def test_router_fail_soft_on_unparseable_json():
    """Sonnet returns prose instead of JSON. Same fallback shape."""

def test_router_fail_soft_does_not_break_turn():
    """Full TutorEngine.respond runs to completion on router outage —
    no exception bubbles past pick_move."""

# ─── Span instrumentation ─────────────────────────────────────

def test_router_emits_router_decision_span():
    """Successful router call emits a router.decision span with
    chosen_move, principle_emphasis, focus_note (truncated to 80 chars),
    rationale, tokens_in, tokens_out."""

def test_router_emits_floor_override_span_when_floor_fires():
    """Router picks pose_question; turn_caps floor overrides to
    close_topic. Span emitted with from_move, to_move, floor_name."""

# ─── Caching ──────────────────────────────────────────────────

def test_router_prompt_cache_keys_stable_across_turns():
    """The cacheable portion of the system prompt is byte-identical
    across two consecutive calls with different student inputs.
    (Verifies the static block doesn't accidentally include per-turn
    content.)"""
```

### 5.2 Safety floor tests

Located in `apps/tutoring/v2/tests/test_safety_floors.py`. Per-floor unit tests + ordering.

```python
# ─── Floor #1: turn caps ──────────────────────────────────────

def test_floor_turn_caps_session_overrides_to_close():
    """turns_in_session=40, router picked pose_question.
    apply_safety_floors returns (close_topic, 'turn_caps_session')."""

def test_floor_turn_caps_objective_overrides_to_close():
    """turns_on_current_objective=12, router picked scaffold_hint.
    Returns (close_topic, 'turn_caps_objective')."""

def test_floor_turn_caps_verdictless_overrides_to_close():
    """verdictless_turns=6, router picked pose_question.
    Returns (close_topic, 'turn_caps_verdictless')."""

# ─── Floor #2: objective evidence + atomic step advance ──────

def test_floor_objective_evidence_overrides_and_advances_step():
    """obj_progress(correct=2, attempts=3). Router picked confirm_and_extend.
    Returns (close_topic, 'objective_evidence_saturated'). Caller
    receives a side-channel signal to advance current_step_index — this
    is the MATHS P1-4 fix; verbal close + step advance are atomic."""

# ─── Floor #3: name_misconception repeat block ───────────────

def test_floor_misconception_repeat_overrides_to_pivot():
    """Router picked name_misconception. move_history[-3:] ==
    ['name_misconception', 'scaffold_hint', 'name_misconception'].
    Verdict still WRONG. Returns (pivot, 'misconception_not_resolving')."""

def test_floor_misconception_repeat_does_not_fire_on_verdict_change():
    """name_misconception fired 2 turns ago; verdict went CORRECT then WRONG.
    Floor does NOT override — the verdict change broke the pattern."""

# ─── Floor #4: pose ledger saturated ─────────────────────────

def test_floor_pose_saturated_overrides_to_close_when_some_correct():
    """All bank slots posed. obj_progress.correct >= 1. Router picked
    pose_question. Returns (close_topic, 'pose_ledger_saturated')."""

def test_floor_pose_saturated_overrides_to_pivot_when_zero_correct():
    """All bank slots posed. obj_progress.correct == 0. Router picked
    pose_question. Returns (pivot, 'pose_ledger_saturated')."""

# ─── Floor #5: help-request regex backstop ───────────────────

def test_floor_help_regex_overrides_to_explain():
    """Student input 'what is condensation'. Router picked
    pose_question. Returns (explain, 'help_request_regex_backstop')."""

def test_floor_help_regex_picks_worked_example_for_struggling_profile():
    """Same as above but profile_summary contains 'struggling'.
    Returns (worked_example, 'help_request_regex_backstop')."""

def test_floor_help_regex_does_not_fire_when_router_already_chose_explain():
    """Student input 'i dont understand'. Router picked explain.
    Floor sees the router already agrees — no override."""

def test_floor_help_regex_does_not_fire_on_attempting_input():
    """Student input '12'. Router picked confirm_and_advance.
    No help-request match. No override."""

# ─── Ordering ────────────────────────────────────────────────

def test_floor_ordering_turn_caps_wins_over_help_regex():
    """turns_on_objective=12 AND student input matches help regex.
    Turn caps fires first → close_topic. Help regex floor sees
    close_topic and does not override (close_topic is terminal)."""
```

### 5.3 Edge cases (must pass)

```python
# ─── Empty / degenerate inputs ───────────────────────────────

def test_router_handles_empty_transcript():
    """Lesson opening turn — transcript is []. Router prompt still
    renders. Sonnet stub returns explain or worked_example.
    No KeyError, no IndexError."""

def test_router_handles_no_verdict():
    """Student input is meta ('hi', 'ok'). Grader returns no verdict.
    RouterRequest.grader_verdict = None. Router output is sensible
    (explain / pose_question / pivot depending on stage)."""

def test_router_handles_resume_turn_with_no_open_question():
    """Session resumed; open_question is None despite prior poses
    in the ledger. Router has the ledger via move_history but no
    live open_question. Picks confirm_and_advance or explain
    based on the latest student turn — does NOT hit the verdictless
    cascade (the GEO P1-2 / MATHS P1-1 shape)."""

def test_router_handles_student_self_reported_guess():
    """Grader verdict=PARTIAL, reason_code=self_reported_guess.
    Router picks scaffold_hint OR confirm_and_advance-with-recheck
    (must not be confirm_and_extend — that would be the
    advance-on-admitted-guess P1)."""

# ─── Multilingual / informal student inputs ──────────────────

def test_router_handles_misspelled_help_request():
    """Student input 'i dunno wat condenstion is'. Router prompt
    includes the raw text; floor #5 regex also matches "i dunno".
    Either layer catches it."""

def test_router_handles_code_switched_input():
    """Mixed English/French/Kreol student input. Sonnet stub
    returns a valid RouterDecision based on intent. No
    encoding errors in prompt rendering."""

# ─── Adversarial students ────────────────────────────────────

def test_router_handles_off_topic_student_input():
    """Student input 'what's your favourite colour'. Grader returns
    UNVERIFIED reason_code=meta_input. Router picks explain or pivot
    to bring student back to the lesson. focus_note mentions the
    redirect."""

def test_router_does_not_advance_on_guessed_correct():
    """Grader: CORRECT but reason_code=self_reported_guess.
    Router MUST NOT pick confirm_and_extend (which advances).
    Acceptable picks: confirm_and_advance with same-difficulty
    retrieval, or scaffold_hint to verify understanding."""

# ─── Adversarial routing ─────────────────────────────────────

def test_router_does_not_pick_close_topic_without_evidence():
    """obj_progress.correct=0, attempts=4. Stubbed Sonnet returns
    close_topic. Pydantic doesn't catch this (close_topic is a
    valid move). Verify this is caught by... actually it isn't —
    floor #2 fires the opposite direction (force close on saturation).
    Document this as a tolerable behavior: if the LLM router decides
    to close, the deterministic floors don't second-guess that
    direction. The conformance gate downstream may reject if the
    close_topic prose claims mastery without evidence."""

def test_router_picks_a_pose_capable_move_when_no_open_question_and_attempting():
    """Student is attempting to answer but there's no open_question
    (resume frame). Router picks worked_example or explain —
    something that creates a new active turn ending in a tool-posed
    question. Does NOT pick confirm_and_advance (nothing to confirm)."""
```

### 5.4 Integration tests (TutorEngine end-to-end)

Located in `apps/tutoring/v2/tests/test_routing_dispatch.py` (rewritten). These use a `FakeRouter` (deterministic, configured per-test) + a `FakeStudentTutor` + a `FakeGrader` to exercise the `pick_move → safety_floors → StudentTutor.respond` glue.

```python
def test_tutor_engine_threads_focus_note_to_student_tutor():
    """FakeRouter returns RouterDecision with focus_note='X'.
    FakeStudentTutor.respond captures kwargs. Assert focus_note='X'
    in captured kwargs."""

def test_tutor_engine_threads_principle_emphasis_to_student_tutor():
    """Same as above for principle_emphasis."""

def test_tutor_engine_applies_floors_after_router():
    """FakeRouter returns chosen_move=confirm_and_advance.
    runtime_state has turns_on_objective=12.
    FakeStudentTutor.respond captures move='close_topic'."""

def test_tutor_engine_atomic_step_advance_on_floor_2():
    """Floor #2 fires. current_step_index advances atomically with
    the close_topic move emission. Verify runtime_state.current_step_index
    after the turn = previous + 1."""

def test_tutor_engine_no_more_intent_classifier_call():
    """Patch classify_student_intent to raise. Run a turn.
    No exception — the function is no longer called."""

def test_tutor_engine_router_outage_does_not_break_turn():
    """FakeRouter.route raises RuntimeError. Engine still produces
    a tutor response (via fail-soft router default). No 500."""
```

### 5.5 Behavior tests against real Sonnet (manual, gated)

Marked `@pytest.mark.live_llm` — skipped in CI. Run pre-cutover and against the run-7 scenarios:

```python
@pytest.mark.live_llm
def test_router_live_geo_p1_3_scenario():
    """Real Sonnet call against the GEO-S5 P1-3 transcript:
    student says 'i dont understand. what is condensation'.
    Assert router.chosen_move in {'explain', 'worked_example'}."""

@pytest.mark.live_llm
def test_router_live_maths_p1_1_scenario():
    """Real Sonnet call against the MATHS-S1 P1-1 resume frame:
    student delivered 3 fully-shown correct Pythagoras solutions
    on resume. Assert router.chosen_move in
    {'confirm_and_extend', 'confirm_and_advance', 'close_topic'},
    NOT explain (which would re-emit the engage paragraph)."""

@pytest.mark.live_llm
def test_router_live_maths_p1_4_scenario():
    """obj_progress.correct=2 after rich correct streak.
    Router picks close_topic OR confirm_and_extend. Floor #2 forces
    close_topic. Verify current_step_index advances."""

@pytest.mark.live_llm
def test_router_live_geo_p1_4_self_reported_guess():
    """Student: 'guess B'. Grader: CORRECT, reason_code=self_reported_guess.
    Router does NOT pick confirm_and_extend."""
```

---

## 6. Cutover

**Per the 2026-05-27 directive: no flag-gated migration.** Single PR cuts the old routing layer out and the new one in. Steps 1-4 of the implementation order land first (non-invasive); step 5 is the cutover PR; step 6 (eval scenarios) is the validation; step 7 (full benchmark) is the post-cutover guardrail.

**Pre-cutover checklist:**

- [ ] All §5.1-5.4 tests green.
- [ ] §5.5 live tests pass against the four run-7 P1 scenarios.
- [ ] Migration `0038` applied locally and review reads clean.
- [ ] `CLAUDE.md` update drafted and reviewed.
- [ ] One MATHS-S1 + one GEO-S5 dry run against the cutover engine in dev (`runserver`) produces sensible move traces.

**Post-cutover validation (within 24h):**

- [ ] Production v2 observability dashboard shows `router.decision` spans on every turn.
- [ ] `router.floor_override` rate < 20% (if higher, the router and floors are fighting — investigate which floor and why).
- [ ] No `intent_classifier` spans (the service is deleted).
- [ ] No `select_move` references in code (`grep -r "select_move\|classify_student_intent\|intent_to_move" apps/` returns empty).

**Rollback path:** the `NEW_TUTOR=off` kill switch already routes to legacy `ConversationalTutor`. If the router causes a student-facing P1 within 24h of cutover, flip the switch — this routes new sessions back to legacy and gives time to revert. Not a flag toggle within v2; the entire engine reverts to v1.

---

## 7. Risks

1. **Router pedagogical judgment is wrong on edge cases not covered by §5.5.** Mitigated by the 5 safety floors (which catch the highest-cost shapes) and the conformance layer (which catches incoherent responses regardless of router choice). The router can produce a sub-optimal move; the system cannot produce a P1 from that alone.

2. **Router latency spikes on Sonnet outages.** Sonnet 4.6 is fast (~1-2s p95) but slower than Haiku. The fail-soft default returns a conservative move based on verdict and emits a span — the turn proceeds. No retry on outage (would compound latency).

3. **Prompt cache miss rate higher than expected.** The cacheable preamble is ~2.5K tokens; the dynamic per-turn block is ~600 tokens. If the dynamic block ends up larger than expected (long transcripts, large objective text), the 5-min TTL may not amortize. Monitor `router.decision` span `cache_hit` field post-cutover; adjust the transcript window from 10 → 6 turns if cache miss rate > 30%.

4. **The `focus_note` field becomes a ghost-writer.** If the router's focus notes are 1-2 sentences of detailed prose, the Move LLM may over-rely on them and lose the per-move prompt's pedagogical shape. Mitigated by the 50-token cap (enforced by Pydantic max_length) and the prompt instruction "focus_note steers, it does not script."

5. **Routing decision is harder to test than counter-based dispatch.** The new test shape is "given this transcript + verdict, the router's chosen_move falls within this acceptable set" — softer than "given these counters, this exact move must fire." §5 mitigates by combining structured-output validation (Pydantic) + behavioral assertions on the chosen_move enum.

6. **Deletion of `move_selection.py` is destructive and load-bearing.** Mitigated by the cutover PR being a single atomic commit; if the commit fails review, nothing has changed. If the post-cutover validation fails, the kill switch flips. The deletion is recoverable via `git revert` in the worst case.

---

## 8. Out of scope

- Cross-session spacing / interleaving — the router has the data it needs (`profile_summary`, `move_history`) but does not yet implement the spacing algorithm. Follow-up plan.
- Multi-objective routing within a single turn — the router picks one move for one objective. Multi-objective transitions still happen at the engine level via `close_topic` → next-step bootstrap.
- Replacing the conformance layer or any part of `StudentGrader` — both stay as-is.
- Replacing the `pose_question` two-phase commit — separate plan covers the MCQ-options renderer fix, the Phase-A `tool_result` feedback, and the sticky PendingPose retry. Independent of this plan but lands in the same release window.
- Frontend changes — none. The wire shape between backend and frontend is unchanged; the router is fully server-side.

---

## 9. Definition of done

- [ ] §3 file changes landed in a single cutover PR.
- [ ] All §5.1-5.4 tests green in CI.
- [ ] §5.5 live tests pass on real Sonnet calls for the four run-7 P1 scenarios.
- [ ] `move_selection.py`, `intent_classifier.py`, `test_move_selection.py`, `test_doing_rate_bias.py` deleted from the codebase.
- [ ] `CLAUDE.md` updated to describe the router + safety-floors architecture.
- [ ] One MATHS-S1 and one GEO-S5 evaluation scenario run end-to-end against the cutover engine with 0 P1 errors.
- [ ] v2 observability dashboard shows `router.decision` and `router.floor_override` spans on representative production sessions.
- [ ] Post-cutover monitoring stable for 7 days: no P1-class incident reports tied to routing decisions.
