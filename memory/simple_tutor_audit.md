# simple_tutor — audit (2026-08-05)

Written for review, and to make the package testable component-by-component.
Companion notebook: `offline_eval/simple_tutor_components.ipynb`.

## 1. Shape of the package

10,353 lines across 14 modules.

| module | lines | role |
|---|---|---|
| `engine.py` | 3,358 | orchestrator: one turn end-to-end |
| `grader.py` | 1,450 | verdicts. 4 deterministic tiers + 2 LLM tiers |
| `tools.py` | 1,407 | the 5 tool handlers + question pool + advance |
| `prompts.py` | 1,333 | system-prompt assembly + `TOOL_SCHEMAS` |
| `family_prompts.py` | 1,127 | per-family Block-0 variants |
| `exit_ticket.py` | 343 | exit-ticket submission + scoring |
| `intent.py` | 277 | classify the student message |
| `state.py` | 253 | recent-turn window, step summaries |
| `stream_filter.py` | 250 | what is safe to show mid-stream |
| `family_tools.py` | 168 | **unwired** — see §6 |
| `step_question.py` | 150 | step-authored question adapter |
| `model_choice.py` | 145 | student's model pick → `ModelConfig` |
| `locale_profiles.py` | 49 | locale rules |
| `__init__.py` | 43 | `is_enabled()` |

`engine.py` is 5.6× its own stated target (docstring line 31: *"Target: ≤ 600
lines"*). That target is not achievable now and should either be removed or
restated; leaving it invites a refactor nobody has scoped.

## 2. What each component needs to run

Classified by static analysis of every function body (169 functions).

| class | count | lines | needs |
|---|---|---|---|
| **PURE** | 109 | 2,552 | nothing — import and call |
| **DB** | 50 | 2,821 | a Django session/lesson row |
| **LLM** | 10 | 1,316 | Ollama or a cloud key |

The useful consequence: **the whole deterministic grading path is pure.**
`_grade_mcq`, `_grade_math`, `_grade_fill_in_blank`, `_grade_matching` and all
their extractors run with no database and no network. That is where
component-level testing is cheapest and highest-value, and it is what the
notebook opens with.

Only 10 functions touch an LLM, and 4 of them are retry/prompt plumbing rather
than inference.

## 3. The grader's contract — why it is easy to test

`grade_answer(question=..., student_answer=...)` is **duck-typed**. Its
docstring says *"an `ExitTicketQuestion` instance (or any object with
`question_type` and the relevant per-type fields)"*, and that is true in
practice. The full set of attributes any tier touches:

```
question_type            'mcq' | 'short_numeric' | 'math' | 'numeric'
                         | 'short_answer' | 'data_interpretation'
                         | 'fill_in_blank' | 'matching'
correct_answer           MCQ letter, or the canonical phrasing
answer_data              dict: {computed, model_answer, unit, parameters,
                         blanks, pairs, ...} — per type
option_a .. option_d     MCQ options (read via getattr f-string)
question_text            used by the verifier-LLM tier only
pk                       logging only
```

So a `SimpleNamespace` with 4-6 fields exercises any deterministic tier. No
fixtures, no migrations.

The two open-response tiers are the exception: `_grade_embedding_gate` needs
`embed()` (ONNX MiniLM, local, no network) and `_grade_verifier_llm` needs a
judge provider chain.

## 4. Turn flow — as documented vs as it runs

`engine.py`'s module docstring describes an 8-step flow. **Step 1 is wrong.**

> *"1. Server picks the current question via `pick_current_question`. Sets
> `session.current_question_id` so the LLM sees one focused question."*

`pick_current_question` exists (`tools.py:371`) and `TutorSession.current_question_id`
exists, but grep finds the function referenced **only in docstrings — never
called**. It was deliberately removed in commit `2afc4e5` (M11.2+M11.3,
2026-05-26) because a server-picked anchor collided with LLM-authored questions
on lesson 1425 and the tutor lectured a student about a question it had not
asked. The reversal was correct at the time; the docstring was never updated.

The flow as it actually runs:

```
respond_for_view
  └── _persist_student_turn
  └── intent.classify_student_message          (LLM)
  └── model_choice.resolve_for_session         → family
  └── gather: step, KB chunks, figure catalog, recent window, summaries
  └── prompts.build_system_prompt              → blocks + 5 tools
  └── _plan_call1 (+ adaptive forcing)         → tool_choice
  └── _call_llm                                (LLM, Call 1)
  └── _dispatch_tools                          → handle_* per tool
  └── _missing_forced_tool → _run_second_call  (LLM, Call 2)
  └── autograde_bare_answer_if_clear           (salvage)
  └── _ensure_posed_question_in_text / _dedupe_reply / _align_reply_polarity
  └── _persist_tutor_turn
  └── maybe_advance_step
```

Two other docstring drifts: it says *"4 tool schemas"* (there are 5) and names
Claude Opus 4.7 as the model, which is right for cloud but not for the desktop
build.

## 5. The structural finding

**Every flow guard sits downstream of a tool call.** The repetition detector,
`answered_correct`, competence advance and grading are all keyed off
`record_answer` / `pose_question` firing. One missed call disables all four
simultaneously — which is what the stuck device session was.

Measured this session (3 replicates, `capable` persona, lesson 1427, qwen3-4b):

```
Call-1 compliance   13/15 = 87%
advance_step calls  0 / 15 turns
forced_advances     0    (every run reached step 5 of 5)
```

Two things follow:

- **`advance_step` is vestigial.** Zero calls across every run, yet every run
  completed all five steps. Advancement happens via the verdict-based path in
  `maybe_advance_step`. The real chain is
  `record_answer → verdict → auto-advance`. `advance_step` can probably be
  deleted; it is currently 5 tool-schema entries' worth of prompt for nothing.
- **Salvage masks compliance.** In the worst run measured, `record_answer`
  fired on 1 of 5 turns and the lesson *still* completed, carried by
  `autograde_bare_answer_if_clear`. Any compliance number read without the
  salvage log is measuring the harness, not the model.

## 5b. The tutor is MCQ-only by default

`_allowed_tutoring_types()` (`tools.py:226`) reads `TUTORING_QUESTION_TYPES` and
**defaults to `('mcq',)`**. The var is unset locally, and in production it is
only injected when the Pulumi key `tutoring-question-types` is configured
(`infra/__main__.py:687`) — conditionally, so unset means MCQ-only there too.

`prompts.py:825` then narrows `pose_question.question_type` to that allowlist
before the schema reaches the model. By default the tutor therefore **cannot
pose a `short_answer` or `short_numeric` question at all** — the enum offers
only `mcq`.

This bears directly on the reported bug *"it could grade MCQ letters but not my
written response."* Under the default config a written question is not something
the tutor can register; if one reached the student, it was narrated as prose
outside `pose_question`, so no gradable slot existed. The grader was never the
problem.

It also means the `short_answer` widening of `_AUTOGRADE_QTYPES` made earlier
this session is **inert in the default configuration** — worth knowing before
attributing any behaviour change to it.

`memory/math_mcq_fabrication_diagnosis.md` reaches the same conclusion from a
different direction and is the companion read.

**Check first, in any grading investigation:** `_allowed_tutoring_types()` —
§5b of the notebook prints it — tells you what the tutor can actually ask on the
config in front of you.

## 5c. `answered_correct` stores whole question stems

The repetition guard keys on normalised full question text plus an
`optset:a|b|c|d` blob, held in `session.engine_state['answered_correct']`.
Measured across the 18 most recent sessions that have it: **median 474 bytes,
max 1,533**.

Not yet a size problem, but two properties matter: it grows with every graded
question and is never trimmed, and matching is exact-on-normalised-text — so a
re-worded repeat does *not* match while a trivially reformatted one does. That
is the mechanism behind both "it repeated a question I already answered" and the
false-positive repeat detections seen this session.

## 6. Loose ends worth a decision

| item | state |
|---|---|
| `family_tools.py` | present but **unwired**. The compaction experiment it was built for regressed compliance 87% → 62% and was reverted; the module and 9 passing schema tests remain. Delete, or keep as scaffolding for a different variant. |
| `pick_current_question` | dead code + a stale docstring claiming it runs. |
| `advance_step` | never called by the model; advancement does not depend on it. |
| `test_rule_compliance.py` | **broken at HEAD** — imports `ISSUE_RULE1_VIOLATION`, absent from `validator.py`. Blocks collection of `apps/tutoring/tests/`. |
| `pytest` / `pytest-django` | not in `requirements.txt`; installed into `venv/` this session so the documented `pytest` command works. |
| harness default persona | `error_prone` answers wrongly, so it cannot exercise a correct-answering student — the actual bug class. `capable` should be the default for compliance work. |
| 34 suite failures | pre-existing, all in deprecated specialist judges + a legacy `conversational_tutor.py` assertion. |

## 7. Where to look first

If the goal is making tool calling reliable, the leverage is in this order:

1. `tools.py::autograde_bare_answer_if_clear` — already rescues missed calls;
   its coverage determines how much compliance actually matters.
2. `engine.py::_missing_forced_tool` + `_run_second_call` — the repair loop.
3. `engine.py::_adaptive_force_now` — when forcing engages.
4. `grader.py` tiers — deterministic, pure, and the place a wrong verdict
   silently corrupts everything downstream.

Prompt wording is the weakest lever and the easiest to fool yourself on: a
single-run improvement here is indistinguishable from noise (measured swing on
identical prompts was 1/5 to 6/6). Require ≥3 replicates per arm.
