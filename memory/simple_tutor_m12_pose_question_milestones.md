# Simple-Tutor M12 — `pose_question` Architecture: Milestone Breakdown

Status: PLANNED · drafted 2026-05-26 evening · supersedes the
"reference_answer-on-record_answer" design from M11.3.

Read `memory/simple_tutor_pose_grade_architecture_research.md` first —
this milestone implements the architecture proposed there.

---

## TL;DR — what we're building

Reintroduce a server-persisted "in-flight question" slot, but with the
LLM (not the catalog picker) writing it. When the tutor wants to ask a
question that will be graded, it MUST call `pose_question(...)`. The
engine persists the question + reference. On the next student turn,
`record_answer(extracted_answer)` grades against the persisted slot —
the LLM no longer supplies the reference. This eliminates the
mis-anchoring class of bugs that prompt iteration couldn't reach.

---

## Why now

M11.3 shipped the "LLM provides reference per grade call" design and it
held up for clean turns. But when one tutor reply mixes praise of a
prior answer + a new question, the LLM's attention drifts and the
`reference_answer` arg lands on the wrong question. Every production
tutor surveyed (MWPTutor, Khanmigo, Duolingo Max) structurally
separates posing from grading. The prompt-level fixes we tried in
M11.1/.2/.3 closed many gaps but can't close this one — the LLM's
tool args and its text generation share a single attention pass.

---

## Milestones

### M12.0 — Branch + scaffolding (≤30 min)

- Branch from current `dev` HEAD (post-M11.3).
- Add the `InFlightQuestion` model skeleton (no migration yet).
- Write this milestone doc.

**Done when**: branch exists, model class compiles.

---

### M12.1 — Schema (≤45 min)

New model:

```python
class InFlightQuestion(models.Model):
    session = OneToOneField(TutorSession, related_name='in_flight_question')
    question_text = TextField()
    question_type = CharField(max_length=20, choices=[
        ('mcq', 'mcq'),
        ('short_numeric', 'short_numeric'),
        ('short_answer', 'short_answer'),
    ])
    options = JSONField(default=list)   # MCQ options A-D as list of strings; empty for non-MCQ
    reference_answer = TextField()
    source = CharField(max_length=20, choices=[
        ('catalog', 'catalog'),
        ('inline_authored', 'inline_authored'),
    ])
    catalog_question_id = IntegerField(null=True, blank=True)
    posed_at_turn = ForeignKey(SessionTurn, null=True, on_delete=SET_NULL)
    posed_at = DateTimeField(auto_now_add=True)
```

- Migration 0035 (additive, no backfill needed — empty table is fine).
- Keep the M11.3 `current_question_id` / `current_question_source`
  columns on TutorSession unused but in place (no removal yet — they
  carry no data we want to read but keeping them avoids an
  irreversible migration during a rollout).

**Done when**: migration applied locally + on staging Postgres.

---

### M12.2 — Tool schemas (≤30 min)

Update `apps/tutoring/simple_tutor/prompts.py::TOOL_SCHEMAS`:

- **`pose_question`** (NEW):
  - `question_text` (required, str)
  - `question_type` (required, enum of mcq / short_numeric / short_answer)
  - `options` (optional list of strings; only for MCQ)
  - `reference_answer` (required, str) — for MCQ: the correct letter
  - `source` (required, enum: catalog | inline_authored)
  - `catalog_question_id` (optional int; required when source=catalog)
  - Description emphasises: "Do NOT also include the question in your
    text reply — the engine renders it from this tool call."

- **`record_answer`** (SIMPLIFIED):
  - `extracted_answer` (required, str) — the ONLY field.
  - No more `reference_answer`, `question_type`, `question_text` —
    server reads from the persisted in-flight question.
  - Description: "Call when the student has answered the in-flight
    question. The engine grades against the persisted slot."

- Other tools (`request_figure`, `redirect_off_topic`, `advance_step`)
  unchanged.

**Done when**: `python manage.py test apps.tutoring.simple_tutor.tests.test_prompts.ToolSchemasTest` passes against the new shape (tests will need to be updated to match).

---

### M12.3 — `pose_question` handler (≤45 min)

In `apps/tutoring/simple_tutor/tools.py`:

```python
def handle_pose_question(
    session,
    *,
    question_text: str,
    question_type: str,
    reference_answer: str,
    source: str,
    options: list[str] | None = None,
    catalog_question_id: int | None = None,
) -> dict:
    """Persist the LLM's question as the session's in-flight question.

    Overwrites any prior in-flight question (logged for analytics).
    """
    from apps.tutoring.models import InFlightQuestion

    prior = InFlightQuestion.objects.filter(session=session).first()
    if prior is not None:
        # Analytics: LLM posed without grading the prior question
        _log_orphan_question(session, prior)
        prior.delete()

    new = InFlightQuestion.objects.create(
        session=session,
        question_text=question_text.strip(),
        question_type=question_type,
        options=options or [],
        reference_answer=reference_answer.strip(),
        source=source,
        catalog_question_id=catalog_question_id,
    )
    return {
        'posed': True,
        'in_flight_id': new.pk,
        'question_type': question_type,
    }
```

Tests:
- Posing overwrites prior + logs orphan analytics.
- MCQ with options is persisted with the options list.
- Posing during a graded turn works (just clears the cleared slot).

---

### M12.4 — `record_answer` rewrite (≤45 min)

```python
def handle_record_answer(session, *, extracted_answer: str) -> dict:
    """Grade extracted_answer against the persisted in-flight question.

    Returns an error dict (NOT a verdict) if no question is in flight —
    the LLM should respond conversationally instead.
    """
    from apps.tutoring.models import InFlightQuestion
    from apps.tutoring.simple_tutor.grader import grade_answer

    in_flight = InFlightQuestion.objects.filter(session=session).first()
    if in_flight is None:
        return {'recorded': False, 'error': 'no in-flight question'}

    # Build a transient question shape the grader expects (already a pattern from M11.3).
    transient = _build_transient_question_from_in_flight(in_flight)
    try:
        result = grade_answer(question=transient, student_answer=extracted_answer)
    except Exception as e:
        return {'recorded': False, 'error': f'grader exception: {e}'}

    # Persist verdict on the current tutor turn (engine handles); clear in_flight.
    in_flight.delete()

    return {
        'recorded': True,
        'verdict': result.verdict.value,
        'tier': result.tier,
        'confidence': result.confidence,
        'justification': result.justification,
        'question_text': in_flight.question_text,  # echo for audit
        'reference_answer': in_flight.reference_answer,
    }
```

Tests:
- Correct answer on persisted question → verdict=correct, slot cleared.
- Wrong answer → verdict=incorrect, slot cleared, but verdict persisted.
- record_answer with empty in_flight → error dict; LLM doesn't crash.

---

### M12.5 — Engine refactor (≤90 min)

`apps/tutoring/simple_tutor/engine.py::respond()`:

- Two-call loop stays (verdict still feeds Call 2 for the reply).
- Before Call 1: load `in_flight_question` if present.
- System prompt branches on `in_flight_question`:
  - **GRADE MODE** (`in_flight` present): prompt shows the persisted
    question + reference verbatim, framed as "THIS is the question the
    student is replying to. If their message is an answer, call
    `record_answer`. If it's a clarification, respond conversationally
    (do not call record_answer)."
  - **TEACH/POSE MODE** (no `in_flight`): prompt instructs "no question
    is in flight. Teach, scaffold, or pose a new question via
    `pose_question`. The text reply should NOT contain the question
    body itself when you pose — the engine renders it from the tool
    call."

- Dispatch order: `pose_question` first (sets slot); then
  `record_answer` (reads slot); then the others. If both fire in one
  turn (rare), pose wins.

- After dispatch, if `record_answer` recorded a verdict AND the LLM
  ALSO called `pose_question`, that's a confirm-and-pose turn — clean
  pattern, no ambiguity because the slot is freshly written.

---

### M12.6 — Prompt rewrites (≤60 min)

`apps/tutoring/simple_tutor/prompts.py::_BLOCK_1_TEMPLATE`:

- Drop the "Identify the in-flight question correctly" rule (server
  handles it now).
- Add **GRADE MODE block**: rendered when `in_flight` is present,
  shows the question + reference in a `<in_flight_question>` block.
- Add **TEACH/POSE MODE block**: rendered when no `in_flight`, omits
  the in-flight block, includes a `<question_pool>` for context.
- Hint ladder rule unchanged — still about the GRADE mode reply text.
- Tutor-driven rule unchanged.
- Reveal rules unchanged.

Concrete prompt sketch:

```
<grade_mode>
The student is replying to this question:

<in_flight_question type="{type}">
  <stem>{text}</stem>
  {options as <option key="A">...}
  <reference_answer>{reference}</reference_answer>
</in_flight_question>

If the student's message is an answer attempt, call record_answer
with extracted_answer = their literal text. The engine grades against
the reference above. If they're asking a clarification or off-topic,
respond conversationally without calling record_answer.

After grading: brief acknowledgement of their attempt, then either
explain (on incorrect — hint ladder) or pose the next question via
pose_question.
</grade_mode>
```

---

### M12.7 — Tests (≤60 min)

- Unit tests for `handle_pose_question` + `handle_record_answer`.
- Integration test: full respond() cycle with mocked LLM.
  - Tutor poses → student answers → graded correct → slot cleared.
  - Tutor poses → student clarifies → no record_answer call → slot
    intact.
  - Tutor poses → tutor poses again before student answers → orphan
    logged, new slot persisted.
  - record_answer with no in-flight → error dict, no crash.
- Regression: the M11.3 tests that asserted `reference_answer` on
  `record_answer` tool calls need to be updated to the new shape.

Target: ~360 tests still passing (down a handful, up a handful — net
neutral).

---

### M12.8 — Engine flag rollout (≤30 min)

- Add per-session flag `engine_state.use_pose_question = bool`.
- Default OFF for existing sessions; opt-in via env var
  `SIMPLE_TUTOR_POSE_QUESTION=on` for new sessions.
- Engine inspects flag at session creation, locks at creation time.
- This way both architectures coexist while we shadow-test.

---

### M12.9 — Shadow grade against eval benchmark (≤2h)

`memory/eval_benchmark_v2_simplified.md` already has a 30-label
dataset. Add a harness:

- Replay the labeled traces through the new engine.
- Compare verdicts against the labels.
- Compare hint quality (text + scaffolding) heuristically.
- Track: false-correct rate, false-incorrect rate, hint topic-drift
  (manual audit on a sample).

Acceptance: new engine matches or beats M11.3 on every benchmark
dimension before we flip the default.

---

### M12.10 — Flip default + monitor (≤30 min)

- Set `SIMPLE_TUTOR_POSE_QUESTION=on` as the default in
  `Pulumi.staging.yaml`.
- Push to staging. Drive 3-5 lessons end-to-end.
- Watch logs for `posed_without_grading` orphan events (should be rare).
- Cut over prod once staging is clean for 24h.

---

## Critical guardrails (from the research)

1. **Don't over-FSM.** One in-flight slot + two new tools (pose +
   record) is the right amount of structure. Don't build a state
   machine with explicit ASK/TEACH/GRADE states — that's MWPTutor
   territory and overkill for our pilot.

2. **Keep `tool_choice='auto'`.** Forcing the LLM to call
   `pose_question` on every turn would break pure-teaching turns.
   Trust the LLM to decide; the orphan-question detector catches the
   times it forgets.

3. **Orphan detection.** If a turn ends with a question-shape in the
   text but no `pose_question` tool call, log it for analytics. We can
   tune the prompt later if this fires too often.

4. **Don't remove the auto-fallback grading immediately.** Keep it as
   a safety net for the first week — if the LLM forgets to call
   `record_answer` but the student clearly answered, server-grade
   against the persisted slot. Remove once shadow data shows it's
   firing < 1% of the time.

5. **Cache invalidation.** `in_flight_question` is persisted per
   session, so prompt caching breaks at the GRADE/TEACH-mode
   boundary. That's fine — the cache savings are at the per-turn
   level, and the boundary changes only when posing or grading fires.

---

## Open questions

- **Catalog binding**: when `source=catalog` and `catalog_question_id`
  is provided, should the engine cross-check the LLM's
  `reference_answer` against the catalog's `correct_answer` field?
  Pro: defends against the LLM picking the wrong option from a catalog
  question. Con: assumes the catalog is authoritative, which has been
  wrong in pilot data (some authored questions have errors).
  **Recommendation**: cross-check + log mismatches, but trust the
  LLM's reference. Use mismatches to flag catalog content for review.

- **Hint ladder counter**: with one in-flight slot, where does the
  "attempts so far" counter live? Two options:
    - Field on `InFlightQuestion`: `attempt_count`, incremented on
      each incorrect verdict. Cleared on slot replacement.
    - Counted from `SessionTurn.judge_outputs` per slot.
  **Recommendation**: field on `InFlightQuestion` — cheaper, no
  recomputation.

- **Migration path for in-flight sessions**: if we flip the default
  while sessions are mid-conversation, what happens? Engine flag is
  locked at session creation, so existing sessions stay on M11.3.
  No mid-conversation surprises.

---

## Estimated total

~9 hours of focused engineering, spread across 2-3 days. Could compress
to 1.5 days if we skip the eval benchmark gate, but that gate is the
whole point of having `memory/eval_benchmark_v2_simplified.md`.

---

## Files touched (preview)

- NEW: `apps/tutoring/migrations/0035_in_flight_question.py`
- NEW: `apps/tutoring/models.py` (InFlightQuestion class)
- `apps/tutoring/simple_tutor/prompts.py` (tool schemas + system prompt)
- `apps/tutoring/simple_tutor/tools.py` (handlers + handle_pose_question)
- `apps/tutoring/simple_tutor/engine.py` (mode branch + dispatch order)
- `apps/tutoring/simple_tutor/tests/test_prompts.py`
- `apps/tutoring/simple_tutor/tests/test_tools.py`
- `apps/tutoring/simple_tutor/tests/test_engine.py`
- NEW: `apps/tutoring/simple_tutor/tests/test_pose_question.py`
- NEW: `scripts/shadow_grade_pose_question.py` (benchmark replay harness)

---

## Refs

- `memory/simple_tutor_pose_grade_architecture_research.md` — the survey
  this milestone is grounded in.
- `memory/simple_tutor_engine_milestones.md` — M0–M11 baseline.
- `memory/eval_benchmark_v2_simplified.md` — gate before flipping the default.
- Commits: `cf6218b` (M11.1) and `2afc4e5` (M11.2/.3) — the floor this builds on.
