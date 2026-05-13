# LLM-as-Student Simulator — Plan (2026-05-13)

## Problem

Tutor quality signal currently depends on pilot students generating real traffic. Two students annotated; ~3 BenchmarkItems labelled. Iterating prompt and judge changes is slow because every round needs new sessions from real users — and pilot bandwidth is the binding constraint.

We want a **synthetic student client** that drives `respond()` end-to-end with an LLM playing the student persona. Same code path real students hit → same judges fire → same `SessionTurn.judge_outputs` populated → resulting turns flow into the existing benchmark sampler. Personas (struggler, average, capable, probe-resistant, non-responder) stress different parts of the engine. Cost-bounded so it can run nightly.

This is **not** an evaluator. It generates traffic. Evaluation still happens through the existing judge stack and the labelled benchmark — see `memory/eval_benchmark_v2_simplified.md`.

## Current state (from audit)

Citations are file:line throughout.

### Tutor entrypoint is already drivable

- `ConversationalTutor.respond(student_input: str) -> TutorMessage` at `apps/tutoring/conversational_tutor.py:1971`. String in, dataclass out. No request object required.
- Production call: `apps/tutoring/views.py:1089-1093` — `tutor = ConversationalTutor(session); result = tutor.respond(message)`. We can call this directly from a management command.
- `TutorMessage` (`conversational_tutor.py:587-616`) carries `content`, `is_complete`, `expects_response`, `is_correct`, `show_exit_ticket`, etc. — enough state to drive a finite-state synthetic-student loop.

### Session bootstrapping is shallow

- `TutorSession.objects.create(student=user, lesson=lesson, institution=inst, status=ACTIVE)` (`apps/tutoring/views.py:749-754`) — no factory helper exists. Tests construct directly (`apps/tutoring/tests/test_math_eval_integration.py:139-145`).
- Required: `institution`, `student` (User), `lesson`. Optional: `prompt_pack`, `model_config`, `engine_state`. The student-User can be a dedicated `simulator-bot` account.

### LLM factory already supports a new purpose cleanly

- `BaseLLMClient.generate(messages, system_prompt, max_tokens=None, *, temperature=None) -> LLMResponse` at `apps/llm/client.py:56-94`. `LLMResponse` carries `tokens_in`, `tokens_out`, `model` (`apps/llm/client.py:28-35`).
- `get_llm_client(config)` factory (`apps/llm/client.py:779-802`) dispatches by `ModelConfig.provider`. GeminiClient at `apps/llm/client.py:530`; OpenAIClient at `:417`.
- Adding `ModelConfig.Purpose.STUDENT_SIM` (`apps/llm/models.py:120-138`) lets the simulator pick its provider through the same config-driven path the tutor uses. Temperature clamps in `effective_temperature` (`apps/llm/models.py:205+`) — STUDENT_SIM should default to ~0.7 (more variance than tutoring) and not be clamped.

### Sampler accepts synthetic turns out of the box — with a caveat

- `candidate_tutor_turns(*, require_full_tracking=True, ...)` at `apps/benchmark/sampling.py:209-268` filters on non-empty `judge_outputs` and `metadata['judge_history_turns']` key. Synthetic sessions running through `respond()` populate both automatically (judges fire at `conversational_tutor.py:2374+`; persistence at `:9319`).
- **Caveat**: synthetic turns will silently pollute Edward's manual annotation queue if we don't tag them. The sampler currently has no notion of synthetic vs real.

### No simulator exists yet

- `git grep` for "simulator", "synthetic_student", "scripted_session" returns 0 hits.
- `verify_math_regression` (`apps/tutoring/management/commands/verify_math_regression.py`) replays the deterministic numeric grader only — does NOT call `respond()`.
- Replay scripts under `scripts/` (`replay_failed_judge_transcripts.py`, etc.) are offline analysis, not session drivers.

### Cost tracking is partial

- Tokens persisted on `SessionTurn.tokens_in/.tokens_out` (`apps/tutoring/models.py:224-225`) and `TurnSpan.tokens_in/.tokens_out` (Phase 1 of `agentic_platform_architecture_plan.md`).
- **No `cost_usd` field anywhere.** No (provider, model) → $/1K token table. Budget enforcement does not exist.

## Target design

A **persona-driven session driver** packaged as a Django management command. Same shape as the existing `judges/` decomposition: one persona = one focused module with a system prompt + a thin response-shaping function.

### Components

```
apps/tutoring/student_sim/
├── __init__.py            # public API: simulate_session(lesson_id, persona, ...)
├── client.py              # StudentClient — wraps BaseLLMClient, adds persona system prompt
├── personas.py            # PERSONAS dict: name → SystemPrompt + temperature + few_shot
├── driver.py              # SessionDriver — creates TutorSession, runs the loop, enforces budget
└── transcript.py          # write/read transcript JSON for offline inspection
```

Plus:

```
apps/llm/cost_estimator.py # (provider, model) → cost-per-1K. Used by sim AND future trace work.
apps/tutoring/management/commands/simulate_session.py
```

### Data model changes — minimal

One additive migration on `TutorSession`:

```python
# apps/tutoring/models.py
is_synthetic = models.BooleanField(default=False, db_index=True)
sim_persona = models.CharField(max_length=40, blank=True)
```

Why a column and not a JSON key in `engine_state`: the sampler needs to filter on this, and we don't want every sampler query to JSON-unwrap. Indexed boolean is cheap.

No new tables. The transcript is just a sequence of existing `SessionTurn` rows; we export to JSON on demand for inspection but don't persist a separate copy.

### Personas (initial v1 set)

Each persona is a ~150-200 word system prompt + a decision rule about answer correctness rate. Recommend Claude drafts, Edward reviews.

| Persona | Behavior | Stresses | Approx correct rate |
|---|---|---|---|
| `STRUGGLER` | Misreads, arithmetic errors, partial work, asks for help | remediation flow, working-request handling | ~30% |
| `AVERAGE` | Gets most answers right, mixed working presentation | the steady-state path | ~65% |
| `CAPABLE` | Right answers, pushes back on tutor errors, asks clarifications | tutor's response to challenges, false-accept behavior | ~85% |
| `PROBE_RESISTANT` | Bare answers, refuses to show working, "I just know" | working-request loop, repeats detection, regen | ~60% |
| `NON_RESPONDER` | Monosyllabic — "ok", "yes", "idk" | non-answer skip path, exit-ticket gating | n/a |

Each persona's system prompt does NOT reveal the answer key — the persona is told the lesson topic but reacts to questions in real time. This avoids the simulator becoming an oracle.

### Loop shape (driver.py)

```python
def simulate_session(*, lesson_id, persona, max_turns=40, max_cost_usd=0.50,
                     student_user=None, institution=None) -> SimResult:
    session = TutorSession.objects.create(
        student=student_user or _default_sim_user(),
        lesson_id=lesson_id, institution=institution,
        is_synthetic=True, sim_persona=persona,
    )
    tutor = ConversationalTutor(session)
    student = StudentClient(persona)
    cost = 0.0

    # Tutor opens; persona reacts.
    msg = tutor.respond("")  # opening turn
    for turn in range(max_turns):
        if msg.is_complete or msg.show_exit_ticket and turn > 1:
            break
        if cost >= max_cost_usd:
            return SimResult(reason='budget_exhausted', cost=cost, ...)
        if _detect_deadlock(session):
            return SimResult(reason='deadlock', cost=cost, ...)

        student_reply = student.next_reply(tutor_msg=msg.content,
                                           history=_history(session))
        cost += _estimate_cost(student.last_response)

        msg = tutor.respond(student_reply)
        cost += _estimate_cost_from_session_turn(session)  # tutor-side
    return SimResult(reason='completed' if msg.is_complete else 'max_turns', ...)
```

Termination conditions (any one ends the session):
1. `msg.is_complete == True`
2. `msg.show_exit_ticket == True` after at least one exchange (avoids opener-only sessions)
3. Turn count ≥ `max_turns` (default 40)
4. Cost ≥ `max_cost_usd` (default $0.50/session)
5. Deadlock: same tutor response (normalized) twice in last 3 turns

### Cost estimator (apps/llm/cost_estimator.py)

```python
COSTS_PER_1K = {  # USD as of 2026-05
    ('anthropic', 'claude-opus-4-7'):       (15.00, 75.00),  # in, out
    ('anthropic', 'claude-sonnet-4-6'):     ( 3.00, 15.00),
    ('anthropic', 'claude-haiku-4-5'):      ( 0.80,  4.00),
    ('openai',    'gpt-4o'):                ( 2.50, 10.00),
    ('openai',    'gpt-4o-mini'):           ( 0.15,  0.60),
    ('google',    'gemini-2.5-pro'):        ( 1.25,  5.00),
    ('google',    'gemini-2.5-flash'):      ( 0.075, 0.30),
}

def estimate(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    rates = COSTS_PER_1K.get((provider, _normalize(model)))
    if rates is None:
        return 0.0  # log warning, don't block
    cin, cout = rates
    return (tokens_in * cin + tokens_out * cout) / 1000
```

Numbers are seed values; centralize so they're updated in one place when prices move.

### Composition with judges and benchmark

Synthetic sessions hit the **same** `respond()` → same judge stack fires → same `judge_outputs` populated → eligible for `candidate_tutor_turns(require_full_tracking=True)` automatically. No special judge handling needed.

Sampler gains an `include_synthetic` parameter (default `False`):

```python
# apps/benchmark/sampling.py
def candidate_tutor_turns(..., include_synthetic: bool = False):
    qs = SessionTurn.objects.filter(role='tutor', ...)
    if not include_synthetic:
        qs = qs.exclude(session__is_synthetic=True)
    return qs
```

`create_benchmark_items()` exposes the same flag and stratifies synthetic turns into a new `synthetic_<persona>` stratum so they don't dilute the wrong_answer / random buckets.

## Out of scope (explicitly deferred)

These are NOT v1. Calling them out so they don't sneak in:

1. **Vision-aware personas.** Synthetic students react to tutor text only; attached media (figures) are ignored. v2 could add image-capable Gemini for the CAPABLE persona.
2. **Adaptive personas.** Each persona is stateless across turns beyond conversation history. No "this persona learns from earlier corrections."
3. **Production deployment.** Local dev management command only. No web UI, no scheduled cron, no Azure execution. Phase 5 is conditional and explicitly gated.
4. **Auto-grading synthetic transcripts.** Existing judges score them; benchmark UI displays results. No new auto-grader.
5. **Multi-tenant simulator users.** One `simulator-bot` user per institution. No persona-per-school customization.
6. **Cost ceiling at the LLM-call site.** Budget is checked between turns in the driver, not enforced inside `BaseLLMClient.generate()`. A single turn that overshoots is allowed; the next turn aborts.
7. **Persona for the exit-ticket phase as a separate mode.** Same persona drives both tutoring and exit-ticket; the persona prompt mentions both contexts.
8. **Replaying real transcripts as synthetic input.** Different problem (evaluation). Use existing replay scripts under `scripts/` if needed.

## Phased delivery

Each phase ships value standalone. Stop after any phase if the signal isn't worth the next.

| Phase | Goal | Days | Files | Success metric | Risk |
|---|---|---:|---|---|---|
| **1. Persona library + StudentClient** | One persona (`STRUGGLER`) drives Gemini 2.5 Flash; produces plausible turns in isolation (no tutor yet). | 2 | `apps/tutoring/student_sim/{__init__,client,personas}.py`, `apps/llm/models.py` (add `Purpose.STUDENT_SIM`) | Edward reads 5 sample replies and judges them "looks like a real struggling student" | Persona prompt steers poorly. **Mitigate:** few-shot 3 example turns from real pilot transcripts inside the persona system prompt. |
| **2. SessionDriver + management command** | One end-to-end synthetic session completes against one lesson. Transcript persists as SessionTurns + JSON export. | 3 | `apps/tutoring/student_sim/driver.py`, `apps/tutoring/student_sim/transcript.py`, `apps/tutoring/management/commands/simulate_session.py`, migration `00XX_add_synthetic_to_session.py` | `python manage.py simulate_session --lesson 42 --persona STRUGGLER` finishes in <2 min, <$0.50, transcript readable | Infinite loop on deadlock. **Mitigate:** deadlock detector + max_turns hard cap. |
| **3. Cost estimator + budget enforcement** | Per-session cost tracked and enforced. Per-run cost cap configurable. | 2 | `apps/llm/cost_estimator.py`, driver integration, command flag `--max-cost-usd` and `--max-run-cost-usd` | Driver aborts at budget; reports `cost_breakdown_by_provider` in SimResult | Wrong cost numbers (rates shift). **Mitigate:** centralized table; comment with retrieval date. |
| **4. Sampler integration + tagging** | Synthetic sessions excluded from default sampler. Opt-in flag pulls them in as a separate stratum. | 2 | `apps/benchmark/sampling.py`, `apps/benchmark/management/commands/sample_benchmark.py`, dashboard sample form gains "Include synthetic" toggle | One synthetic-only sample creates BenchmarkItems with `stratum='synthetic_struggler'`; default sampling does NOT include them | Sampler accidentally pulls synthetic. **Mitigate:** test asserts default `include_synthetic=False` excludes; integration test with mixed real+synthetic session set. |
| **5. Multi-persona run + nightly cron** *(conditional)* | Run all 5 personas × N lessons nightly. Dashboard widget tracks pass rate per persona over time. | 4 | `apps/tutoring/management/commands/simulate_run.py` (multi-session driver), `apps/dashboard/views.py` (widget) | Nightly run produces ≥40 new BenchmarkItems flagged synthetic; pass-rate trend visible in dashboard | Cost explosion. **Mitigate:** per-run cap default $5; opt-in scheduling not enabled until manual approval. |

**Total Phases 1–4: ~9 focused days. Phase 5 is conditional on Phases 1–4 producing signal Edward judges valuable.**

## Testing

| Phase | New tests |
|---|---|
| 1 | `test_personas.py` — each persona system prompt loads; mock LLM returns canned text → `StudentClient.next_reply()` formats history correctly. |
| 2 | `test_driver_completes.py` — drive a 3-step lesson with `MockLLMClient` for student AND tutor (canned responses); assert session reaches `is_complete` or hits max_turns deterministically. `test_driver_deadlock.py` — feed the loop the same tutor response twice; assert `reason='deadlock'`. |
| 3 | `test_cost_estimator.py` — known token counts → known cost. `test_driver_budget.py` — set max_cost_usd=$0.001, assert driver aborts after one turn. |
| 4 | `test_sampler_excludes_synthetic.py` — create 5 real + 5 synthetic SessionTurns; default `candidate_tutor_turns()` returns only the 5 real; `include_synthetic=True` returns all 10. |

Per `memory/feedback_concurrency_testing_patterns.md`: mocked LLM clients should inject a 50ms sleep when used in driver tests so any future parallel-driver code surfaces races. Single-session driver is sequential, so this is a v2 concern.

## Open questions

Resolve before Phase 1 starts:

1. **Student LLM choice.** Gemini 2.5 Flash, Gemini 2.5 Pro, or GPT-4o-mini? **Recommend: Gemini 2.5 Flash.** Reason: persona steering is well within Flash's range; cost is the binding constraint at scale (~$0.075/1K in, $0.30/1K out). A 30-turn session with avg 100 tokens out per student turn = ~$0.001 student-side. Tutor (Opus 4.7) dominates total cost.

2. **Tag mechanism.** New BooleanField + CharField on `TutorSession`, or `engine_state['sim_meta']` JSON? **Recommend: column.** Reason: sampler needs efficient filter; admin needs queryability; migration is one additive line.

3. **Default exclude synthetic from sampler.** **Recommend: yes, exclude by default.** Synthetic distribution may not match real student distribution. Treat as a separate cohort. Edward opts in explicitly when he wants to annotate them.

4. **Persona prompt authorship.** Claude drafts, Edward reviews — or Edward writes from scratch? **Recommend: Claude drafts using 3-5 anchor turns from real pilot transcripts.** Reason: faster; Edward edits in chat. Anchor turns ground the prompt in real student voice.

5. **Lesson context for the persona.** Should the persona know the lesson topic + objective, or react blind? **Recommend: knows topic + objective only.** No prior knowledge of answer key. The persona prompt says "you're a Form 1 student studying angles on a straight line; you sometimes confuse 180 with 360." This avoids the persona becoming an oracle.

6. **Run location.** Local dev only, or also CI? **Recommend: local dev only for v1.** Reason: Azure costs are unbudgeted; CI costs are unbudgeted. Phase 5 cron is the explicit cost-bearing path.

7. **Persona stress test before scaling.** How do we know `STRUGGLER` actually behaves like a struggling student before generating 100s of items? **Recommend: Phase 1 success metric is Edward reading 5 sample replies.** No quantitative check possible without real student baseline.

## Risks

1. **Persona drift to "ideal student".** LLMs default to helpful + confident. Persona prompts must explicitly model error patterns (off-by-one, formula confusion, bare answers). **Mitigate:** anchor with real-pilot examples; Phase 1 review gate.

2. **Cost overrun.** A pathological loop could run 40 turns × $0.05/turn = $2/session unflagged. **Mitigate:** budget check between turns; per-run cap.

3. **Polluting human annotation queue.** If sampler accidentally includes synthetic turns, Edward annotates synthetic data thinking it's real. **Mitigate:** Phase 4 sampler test; default exclude; explicit `--include-synthetic` flag.

4. **Synthetic distribution ≠ real distribution.** Pass rate on synthetic might not predict pass rate on real students. **Mitigate:** treat synthetic as a separate stratum; report metrics side-by-side, not pooled. The benchmark already handles slicing.

5. **Tutor engine assumes a real `User` for `student`.** Foreign key cannot be null. **Mitigate:** create one `simulator-bot` user per institution at first run; idempotent fixture.

6. **Locale / language drift.** Personas need to match Seychellois English register (some Creole influence). **Mitigate:** anchor turns from real pilot transcripts; Phase 1 review.

## Composition with related plans

- **`memory/eval_benchmark_v2_simplified.md`** — synthetic SessionTurns flow into the same item schema. No changes to the benchmark itself.
- **`memory/agentic_platform_architecture_plan.md`** — Phase 1 trace logging will give synthetic runs the same per-LLM-call observability as real runs. No coupling required: the simulator works without traces; traces work without the simulator.
- **`auto-memory/feedback_chrome_devtools_default_verification.md`** — UI-affecting changes in this plan (Phase 4 dashboard sample form toggle) need browser verification before commit.
- **`tutoring-engine-expert` skill** — consult during Phase 2 driver implementation; it knows the SessionState transitions and the deadlock conditions worth detecting beyond same-response repetition.

## Next step

Build Phase 1 in isolation: write `STRUGGLER` persona prompt + `StudentClient.next_reply()`. No tutor yet. Hand 5 sample replies to Edward; if they don't read like a real struggling student, iterate the prompt before any other work.
