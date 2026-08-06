# Catalog-only questions — the tutor selects, never invents (2026-08-06)

## Problem

The tutor authors its own questions and their reference answers. On a 4B model
that corrupts content, and the corruption is invisible to every guard we have.

Measured on the eval dataset (5 multi-turn scenarios, `qwen3-4b-jetson`,
sessions 983-987):

```
posed:    "a scale of 1:200,000, a measured distance of 3 cm..."   ref='6'
re-posed: "a scale of -200,000,  a measured distance of 3 cm..."   ref='6'   x4

posed:    "the probability of rolling a 6 is 1/6"                  ref='4'
re-posed: "the probability of rolling a 6 is -1/6"                 ref='4'   x2

authored: "The probability of success is 5 and there are 90 trials"  ref='50'
```

Numeric notation collapses on re-authoring — `1:200,000` -> `-200,000`,
`1/6` -> `-1/6`, `0.5` -> `5` — while **the reference answer keeps the original
correct value**. The student sees a corrupted question and is graded against a
reference for a different one, so a correct answer is marked wrong. That is the
reported bug *"my first answer was correct but it did not pick it up"*: not a
grading fault, but the grader faithfully enforcing a reference the visible
question no longer matches.

Independent confirmation from the rubric judges, who saw only the transcript:
*"tells the student 600,000 / 1,000 = 600 is wrong multiple times when it is
correct"*; *"introduced questions with impossible probability values
(probability=5, probability=-1/6)"*.

Question provenance in those sessions:

| source | count | |
|---|---|---|
| `auto_pose_fallback` (server, catalog) | 20 | 51% |
| `pose_question` inline_authored | 12 | 31% |
| `pose_question` catalog | 4 | 10% |
| `pose_question` (source unset) | 3 | 8% |

Every corrupted question came from the inline-authored set. None came from the
server's catalog path.

## Target design

**The model selects a question by index. The server renders it from the bank.**

`pose_question` collapses from six parameters to one:

```
pose_question(question_index: int)     # 1..N, indexing <question_pool>
```

The server looks up `pool[index-1]` and writes the `InFlightQuestion` from the
catalog row — stem, options, correct answer, type. **The model never transmits
question text, so it cannot corrupt it.** This is a structural fix, not a
validation one: there is no path by which a mangled stem can reach a student.

It also removes the second failure mode. Today the model supplies
`reference_answer` for questions it invents; a 4B that miscomputes the reference
produces a question that is *unanswerable correctly*. Catalog rows carry
human-authored references.

Feasibility (measured over 600 steps): **median 5 catalog questions per step
objective, mean 5.0, and only 2% of steps have zero.** The pool is already
rendered with `index="1..N"` (`prompts.py::_render_question_pool`), so the
selection key exists and the model is already shown it.

Precedent: `engine._auto_pose_fallback` already poses from the catalog
server-side and produced 51% of questions in the eval — with none of the
corruption. This generalises what already works.

## Changes

### 1. `prompts.py::TOOL_SCHEMAS` — pose_question becomes one integer

Drop `question_text`, `question_type`, `options`, `reference_answer`, `source`,
`catalog_question_id`. Keep `question_index`. The 792-char `reference_answer`
guidance and the MCQ-letter-rotation block go with it — both become moot, since
letters and references now come from the bank.

Schema drops from 2,795 bytes to roughly 200.

### 2. `tools.py::handle_pose_question` — server renders from the bank

New signature `handle_pose_question(session, *, question_index)`. Resolve
against the same `build_question_pool(session)` the prompt was built from, and
write `InFlightQuestion` with `source='catalog'` always. Reject an out-of-range
index with `posed=False` (the existing rejection path already triggers
`_auto_pose_fallback`).

Keep the old keyword-argument form callable internally — `_auto_pose_fallback`
and the tests use it — but it is no longer reachable from a tool call.

### 3. `engine.py::_ensure_posed_question_in_text` — unconditional

Today it returns early when `pose_question` did not fire. Make the slot the
single source of truth for the visible question on every turn where a slot
exists. Combined with (1) this closes the loop: the question the student reads
and the question being graded are the same object by construction.

### 4. Empty pool (2% of steps)

Nothing to ask. Log and let `maybe_advance_step` move on rather than leaving the
turn dangling. Never fall back to inline authoring — that is the behaviour being
removed.

### 5. Prompt updates

`family_prompts.MARKDOWN_BLOCK_0_COMPACT` and the base template tell the model
to write stems and options into `pose_question`. Rewrite to: pick the index of
the pool question you want; the platform shows it to the student. The hint
ladder's "pose an easier item" still works — the model picks a different index.

## Scope

**Global. No flag.** The system never invents questions — that is the invariant,
not a mode. A flag would mean the corruption class still exists somewhere in the
matrix, and "which arm was that session on?" becomes a question every future
debug has to answer.

This is settled by the platform's **offline-first, then cloud** priority
(`auto-memory/project_offline_first_priority.md`): the local 4B is the primary
target and the cloud tutor adapts to what it requires. Roughly half of today's
production questions are `inline_authored` and Opus does not exhibit the stem
corruption that motivates this — but a frontier model is strictly more capable
than a 4B, so a constraint that keeps the local model correct is one the cloud
model can satisfy without difficulty. Selecting from a verified bank is not a
downgrade for Opus; it is the same job with the authoring removed.

The real dependency is **bank coverage**, not model capability: 2% of steps have
no catalog questions and will pose nothing. That is a content gap to close, and
it is the thing to watch in the pilot — not a reason to keep an authoring path
alive.

## Out of scope

- Growing the question bank. 2% of steps have no questions; that is a content
  task, not an engine one.
- The MCQ B-bias in the authored bank (60.6% of 7,073 MCQs) — separate, and
  arguably more urgent once the tutor can only draw from that bank.
- `TUTORING_QUESTION_TYPES` mcq-only default (`simple_tutor_audit.md` §5b).

## Verification

1. `apps/tutoring/simple_tutor/tests/` — pre-existing failure count is 9; must
   stay 9. New tests: index selection, out-of-range rejection, empty pool,
   and that a corrupted stem in the model's prose cannot reach the student.
2. Re-run the same 5 eval scenarios, `--seed 0`, on `qwen3-4b-jetson`. Baseline
   is **3/5**, with the two failures scoring 0.40 and 0.50 against a 0.60
   threshold. Success is the corrupted-question class disappearing from the
   transcripts — check for `-200,000` / `-1/6` / `probability of success is 5`.
3. Desktop app, lesson 1427: confirm questions still appear and grade.

Do not claim a pass-rate improvement from 5 scenarios — the harness's own
`--sample` help states 15 scenarios cannot resolve a difference below ~36pp.
The defensible claim is the disappearance of a specific, identifiable defect.
