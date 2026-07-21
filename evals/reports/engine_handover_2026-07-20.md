# simple_tutor engine — handover report (2026-07-20)

Written for the incoming engine owner. Covers: how the engine works today, the
guard/repair stack and why each piece exists, the evaluation infrastructure, the
full fix-cycle history with results, and what's still open. Companion documents:
`evals/reports/multi_turn_bottlenecks_2026-07-18.md` (the analysis that drove the
latest fixes) and `offline_eval/multi_turn_results/fixcheck/_cycle_log.md` (per-cycle
change log).

---

## 1. What the engine is

`apps/tutoring/simple_tutor/` is the production tutoring engine (default since
commit `da8b57f`; kill-switch `SIMPLE_TUTOR_ENGINE=off` falls back to the legacy
`conversational_tutor`). It is a **tool-calling loop around a server-owned
question slot**:

- **The platform owns question state.** When the tutor LLM wants to ask a
  question it calls `pose_question(question_text, question_type, options,
  reference_answer)`; the engine persists it as the session's single
  `InFlightQuestion` row (OneToOne with `TutorSession`). When the student
  answers, the LLM calls `record_answer(extracted_answer)` and the server-side
  grader (`grader.py`) grades against the slot's reference — the LLM never
  re-identifies "which question is live" (that was the M11.3 failure mode).
- **Two LLM calls per turn** (`engine.py::respond`). Call 1: the model decides
  which tools to fire (grade / pose / figure / redirect). Tools are dispatched
  server-side (pose before record, so a same-turn record sees the fresh slot).
  Call 2: tool results are fed back and the model composes the student-facing
  reply with the verdict in hand. Call 2 also carries the *repair* mechanism —
  if Call 1 skipped an expected tool, Call 2 is forced to make it.
- **Turn modes** are derived, not stored: slot present + answer-intent =
  GRADE; no slot = POSE/TEACH; conversational intents (clarification /
  pushback / off_topic / non_engagement, classified deterministically in
  `intent.py`) suppress grading and leave the slot live.
- **Question pool**: `tools.py::build_question_pool` feeds the prompt catalog
  questions for the current step (filtered to drop already-graded stems);
  `question_type` allowlist via `TUTORING_QUESTION_TYPES` (default `mcq`;
  math lessons need `short_numeric` enabled — see the MCQ-fabrication memo).
- **Prompts**: `prompts.py` (XML base) + `family_prompts.py` (per-family
  variants: Qwen gets Markdown Block 0; Gemini and Kimi get targeted rule
  add-ons). Family resolution is **eval-only** — production runs with
  `family=None` and the unmodified base prompt.

## 2. The guard / repair stack (and the incident behind each layer)

These accumulated over the July fix cycles. Each is toggleable and
production-safe (most are gated to non-Anthropic families, i.e. eval-only).

| Layer | Where | Why it exists |
|---|---|---|
| Forced pose/grade (`_should_force_pose/_grade` + B2 adaptive gate) | `engine.py` | Non-Anthropic models narrate questions as prose without calling `pose_question` (qwen: 3% compliance unforced) → no gradable slot → stall. Gate stays open until a model misses once, then pre-forces. |
| Call-2 repair (`_run_second_call`) | `engine.py` | Ollama/others don't honour `tool_choice`; the repair rides on Call 2 so a repaired turn costs no extra call. |
| Anti-desync guard (intent-gated) | `tools.py::handle_pose_question` | Gemini posed over unanswered questions 161×/cycle (55% of poses), swapping the question under the student. Blocks a new pose only when the student *attempted* an answer; allows pivot on "idk" (the ungated version caused re-pose deadlocks — cycle 3→4). |
| Anti-repetition, stage 1: reject | `tools.py` (cycle 7) | First pose of an already-correct stem is rejected with corrective feedback (`repeat_rejected_stems`). |
| Anti-repetition, stage 2: force-advance | `tools.py::_note_pose_repetition` | If the model re-poses the same already-correct stem anyway, accept it (never leave a turn slotless) and advance the step underneath after a streak. |
| Slot-rendering + divergence strip | `engine.py::_ensure_posed_question_in_text` (cycle 7) | The dominant cycle-6 failure: visible prose question ≠ registered slot question → students graded against a question they never saw. Now the slot is rendered server-side and a divergent trailing prose question is stripped. |
| Same-turn pose after correct verdict | `engine.py::_should_pose_next_after_correct` (cycle 7) | On correct-verdict turns models wrote the next question in prose only; the next answer met an empty slot and the model fabricated a re-grade. Call 2 is now forced to register the next question (non-Anthropic, not on the last step). |
| Vocabulary scrub | `engine.py::_scrub_engine_vocab` (cycle 7) | Models echoed platform vocabulary to students ("we're in POSE/TEACH mode", "isn't currently in flight"). Deterministic sentence-level strip before persistence + `_PRIVATE_NOTE` markers on all tool results. Punctuation-only residue scrubs to empty (kimi's narrated tool-JSON left an orphan `[` bubble → deadlock; fixed same day). |
| Slot-aware placeholder | `engine.py::_empty_reply_placeholder` (cycle 7) | The old "Here's the next one:" with no question attached cost 2 turns each occurrence. Now renders the in-flight question. |
| Transient-error retry | `engine.py::_invoke_with_transient_retry` (cycle 1) | Vertex 429/503 made `_call_llm` return None → `_FALLBACK_REPLY` loops → deadlocks (kimi: 12/20 in one cycle). Backoff [2, 5, 12]s; kimi rode through 177 rate-limit hits in cycle 7 with zero deadlocks. |

Env toggles: `SIMPLE_TUTOR_ANTIDESYNC`, `SIMPLE_TUTOR_ANTIREPEAT`,
`SIMPLE_TUTOR_TRANSIENT_RETRY` (all default on), `TUTORING_QUESTION_TYPES`,
`TUTOR_MODEL_OVERRIDE` (eval sweeps only).

## 3. Evaluation infrastructure

- **Dataset**: `evals/dataset/` — 400 scenarios (single + multi-turn), balanced
  by persona × lesson × shape (`evals/matrix.py`); `evals/lint_dataset.py`
  asserts structure, balance, *and now fixture content quality* (float-noise
  stems; statement-MCQs with lost option texts). Runs under
  `pytest evals/test_dataset_balance.py`.
- **Multi-turn runner**: `evals/runner.py` drives real engine sessions against
  a student simulator (`apps/tutoring/student_sim/`, Anthropic Haiku persona
  player) and scores with deterministic assertions (turn budgets — persona-aware
  since cycle 2, repetition, tool-syntax leaks) + a Sonnet 4.6 session-level
  rubric (BEA-aligned standard items + per-scenario items). Run headers record
  `git_sha`, `engine`, and (since cycle 7) `tutor_model`.
- **Sweeps**: `offline_eval/run_cloud.sh` — one run per model row
  (`CLOUD_MODELS_FILE`), swapping only `TUTOR_MODEL_OVERRIDE`. The fixcheck
  configuration: `MODE="--multi-turn --sample 20 --seed 5"` against
  `cloud_models_3.txt` (gemini-2.5-flash, kimi-k2-thinking,
  qwen3-next-80b-instruct). Fixtures must be loaded first:
  `manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json`.
  Vertex auth is the gitignored isolated gcloud config (`.env` carries
  `CLOUDSDK_CONFIG`; see auto-memory `vertex-model-garden-eval-setup`).
- **Cost/latency**: gemini ≈ 40-50 min, qwen ≈ 40-50 min, kimi ≈ 2.5-4 h per
  20-scenario leg (thinking model + rate-limit backoff).
- **Data-integrity rule**: before trusting a sweep, check for judge/sim
  infrastructure errors (`errored` count in the run JSON; Anthropic
  `overloaded_error` and `APIConnectionError` are infra, not model failures) and
  re-run those scenarios.

## 4. Fix-cycle history — one table

20 multi-turn scenarios, `--sample 20 --seed 5`, pass counts out of 20.
glm-4.7 (18) and deepseek-v3.1 (15) were dropped after cycle 2 as at-ceiling;
the 3-model total is the tracked number.

| Cycle | Date | Change shipped | gemini-2.5-flash | kimi-k2-thinking | qwen3-next-80b | 3-model total |
|---|---|---|---|---|---|---|
| 0 | 07-16 | Baseline | 7 | 3 | 7 | 17/60 (28%) |
| 1 | 07-17 | Turn budgets step+8; transient-error retry | 5 | 7 | 8 | 20/60 (33%) |
| 2 | 07-17 | Persona-aware turn budgets; sim/judge retry | 4 | 10 | 11 | 25/60 (42%) |
| 3 | 07-17 | Anti-desync guard (block pose over unanswered) | 8 | 12 | 9 | 29/60 (48%) |
| 4 | 07-18 | Intent-gated anti-desync (allow pivot on "idk") | 5 | 9 | 8 | 22/60 (37%) |
| 5 | 07-18 | Per-family prompt variants (kimi/gemini/qwen) | 9 | 11 | 8 | 28/60 (47%) |
| 6 | 07-18 | Anti-repetition guard; MCQ-fabrication fix | 10 | 9 | 7 | 26/60 (43%) |
| 7 | 07-20 | Bottleneck fixes (slot rendering, pose-after-correct, vocab scrub, repeat-reject, placeholder, fixture repairs) | 17 | 13 | 11 | 41/60 (68%) |
| 8 | 07-21 | Semantic dispatch order, MCQ value→letter grading, pose salvage, pivot guidance, tool-JSON scrub | 18 | 14 | 15 | 47/60 (78%) |
| **9** | **07-21** | **Catalog letter coherence (correct TEXT is the authority), rotation rule scoped to authored questions, auto-pose fallback** | **18*** | **18** | **14** | **50/60 (83%)** |

| **10** | **07-21** | **Qwen prompt-variant iteration (spent micro-steps, precision acceptance, answered-question-finished, hint-without-reveal example, number sanity, opener variety) — qwen leg only** | 18* | 18* | **16** | **52/60 (87%)** |

Cycle-10 note: qwen 14→16 (all-time best; avg session 13.4→10.5 turns;
"logically consistent" low-scores 3→1). Its one residual deadlock traces to a
POOL question with a broken template ("probability of success is 5" with an
inconsistent reference) — a fixture-content bug in the float-noise family; next
content-lint target: flag probability parameters that render > 1. The other
three failures are 2–4-turn budget overruns on otherwise-clean sessions.

\* Gemini was not re-run in cycles 9–10, and kimi not in cycle 10 (their bests
carry over — the changed code paths were qwen-prompt-only in cycle 10). Cycle-9 headline: kimi 14→18 with ZERO "logically
consistent" low-scores — the letter/option mismatch (Beau Vallon class) is
gone; 121 catalog option+letter adoptions and 29 auto-pose fallbacks fired
in-sweep. qwen 15→14 is within the ±2 noise band; its six residual failures
are all turn-budget overruns with no deadlocks. Remaining frontier: turn
budgets on long non-responder/struggler lessons (worth re-examining now that
sessions are pedagogically clean) and qwen self-consistency.

Cycle-8 note: the first kimi/qwen legs collapsed (5 and 4 of 20) because the new
pose validations *rejected* their malformed poses (~90 rejections each — mostly
catalog MCQs posed without options) and neither model recovers from corrective
errors; every rejection became a slotless placeholder deadlock. The salvage
re-legs (same day) convert malformed poses into the nearest valid slot instead —
catalog-option adoption by stem match, or mcq↔short_numeric conversion. Gemini's
18 is from the first leg (only 3 rejections; salvage impact negligible). Zero
deadlocks and zero infra errors across the final cycle-8 board — a first.

Cycle-7 notes: kimi's 20 includes 3 scenarios re-run after Anthropic-side
`overloaded_error` (2 passed, 1 failed genuinely). Cycle-to-cycle noise is real
(cycle 4 dipped on gemini flakiness) — judge temperature is 0 but tutor sampling
is not, so treat ±2/model as noise; cycle 7's +15 is far outside that band.

**Where cycle 7's gain came from** (matches the bottleneck analysis's
prediction): "stayed logically consistent" rubric low-scores 26 → 9 across the
three models (gemini 0); `max_turn_count` assertion failures 15 → 10; average
session length gemini 13.7 → 9.3 turns, kimi 15.0 → 11.8.

## 5. Open items for the incoming owner

1. **Cycle-7 regressions**: kimi's two scrub-residue deadlocks are fixed and
   re-verified (`long_session_capable_001` re-passes; second re-check was in
   flight at handover — see `cycle7_bottleneck_fixes/kimi_recheck/`). qwen's two
   are a distinct class: *grading self-contradiction* (insists a correct MCQ
   answer is wrong, then re-teaches the correct fact). Candidate next target.
2. **The flat rubric ceiling**: error-localization (~0.49) and
   mistake-recognition (~0.51) haven't moved through prompt tuning (cycle 5) or
   the desync fixes. Likely needs a structural aid (e.g. grader justification
   fed into the hint, which cycle 7's `record_answer` result already surfaces).
3. **qwen empty-Call-2 turns**: the slot-aware placeholder now papers over them
   (sessions complete) but the judge dings the templated repetition. Worth a
   look at why qwen's Call 2 returns no text on some grade turns.
4. **Specialist judges** are deprecated pending unified-judge parity data
   (see CLAUDE.md); don't build on `apps/tutoring/judges/*` specialists.
5. **Working tree is uncommitted** at handover: engine fixes + fixture repairs +
   eval infra changes. The 12 re-authored fixture MCQs
   (`evals/fixtures/lessons.json`) are content changes — review before commit.
   The float-noise root cause also exists in any *production* DB content
   generated before the `parametric_renderer.py` rounding fix; fixtures are
   patched, prod content may need the same backfill.
6. **Last-step gap in pose-after-correct**: the forced same-turn pose is
   skipped on the lesson's final step (to avoid colliding with the exit-ticket
   handoff), so prose/slot desync remains possible there. Bounded, but real.

## 6. How to reproduce everything

```bash
# unit tests
./venv/bin/python manage.py test apps.tutoring.simple_tutor      # engine (473 tests)
./venv/bin/pytest evals/test_dataset_balance.py                  # dataset + fixture lint

# one scenario against one model
GOOGLE_CLOUD_LOCATION=global \
TUTOR_MODEL_OVERRIDE="vertex_model_garden/qwen/qwen3-next-80b-a3b-instruct-maas" \
./venv/bin/python manage.py run_eval --scenario speedrun_capable_1139_14

# the 3-model fixcheck sweep (≈4-5 h, ~$5-8)
RESULTS_DIR="$PWD/offline_eval/multi_turn_results/fixcheck/cycle8_<name>" \
CLOUD_MODELS_FILE="$PWD/offline_eval/cloud_models_3.txt" \
MODE="--multi-turn --sample 20 --seed 5" \
bash offline_eval/run_cloud.sh
```
