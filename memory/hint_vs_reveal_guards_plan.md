# Sonnet-Era Tutor Hardening — Plan (2026-05-17)

## Problem

Tutoring runs on Sonnet 4 today; offline mobile path targets Qwen.
Neither model follows nuanced rule prose reliably. Reliability has
to come from: (a) deterministic guards, (b) structured fields, (c)
post-hoc detect-and-regen — not from prompt-charm.

This session surfaced 21 specific learnings. ~half already have
landed fixes; the rest need work, and several apply to the whole
prompt surface (judges + graders + regen + content gen), not just
the tutor.

## All 21 learnings — status table

| # | Learning | Status | Where covered |
|---|---|---|---|
| L1 | Sonnet soft-reveals via paraphrase | SHIPPED (Bundle A) — W1 leak guard + W2 forbidden examples both live |
| L2 | "re-explain" / "walk through canonical" read as reveal | PARTIAL — W2 shipped (Bundle A); W7 regen prompt restructure remains (Bundle B) |
| L3 | Answer in context is a hazard, not just an aid | OPEN | W3 (leak-aware regen — strip canonical) |
| L4 | Tool availability doesn't suppress text-authoring | SHIPPED `5495f7c` | — |
| L5 | Bank verdict beats LLM verdict | SHIPPED `7b72765` + W4 (judge prompt belt) |
| L6 | Stale state references cascade mis-routing | SHIPPED `285c160` | — |
| L7 | Multiple questions on screen → mis-grade | SHIPPED `5495f7c` | — |
| L8 | Pydantic schema/prompt name-drift silently drops fields | SHIPPED `43ce5d2` + W8 (audit ALL schemas) |
| L9 | Hint-then-retry, reveal gated, per-question counter | SHIPPED prompt-side `54fe231 b7323de` + W1 (programmatic gate) |
| L10 | "I don't know" is a real student state | OPEN | W9 (confusion-signal exception) |
| L11 | Difficulty signal must steer selection | SHIPPED `54fe231` | — |
| L12 | Question-type ↔ chat-render fit varies | SHIPPED `5d26aba` | — |
| L13 | Resume needs narrative continuity, not just state | LIVE (works) | W10 (regression test only) |
| L14 | Concrete forbidden-phrase lists bind; abstract prohibitions don't | PARTIAL — tutor side shipped (W2); judge prompts (W5) + regen prompt (W7) remain (Bundle B) |
| L15 | Surface the *why* of a rule, not just the *what* | OPEN | W4 + W7 (rationale lines in prompts) |
| L16 | Structured fields direct behavior more than prose | OPEN | W11 (`[STUDENT_STATE]` + `wrong_attempts` exposed as fields) |
| L17 | Bank ground-truth in regen prevents invention | SHIPPED `6b38875` + W7 (verify still top-of-prompt) |
| L18 | Subtractive prompt engineering — what's omitted shapes behavior | SHIPPED `1a72945` (artifact removed) + W3 (strip canonical on leak regen) |
| L19 | Local e2e is the ground truth | PROCESS | W12 (CLAUDE.md reinforcement) |
| L20 | Reset state before symptom-patching | PROCESS | W12 (CLAUDE.md addition) |
| L21 | Commit ↔ memory bidirectional links | PROCESS | already established (CLAUDE.md exists) |
| L22 | Feature plans + execution artifacts live in repo `memory/` (git-tracked); auto-memory is for cross-cutting / general notes only | PROCESS | W12 (CLAUDE.md addition) |
| L23 | Tutor repeats its own questions across turns; bank repetition is guarded but authored is not | SHIPPED (Bundle A) — module `apps/tutoring/repeated_question.py` + wiring in `_respond_impl` + `recent_tutor_question_sigs` persisted on `engine_state` | W14 |
| L24 | Tutor must always end with a question OR clear next-step directive — "Now let me ask:" with no question + mid-sentence truncation are real failure modes | SHIPPED — handoff LLM judge (`apps/tutoring/judges/handoff.py`) runs concurrently in `run_all_judges`, validator appends `ISSUE_NO_QUESTION` when `handed_off=False`, regen scorer hard-penalises (`apps/tutoring/regen/score.py`), regen prompt surfaces specific reason, post-regen safe-fallback substitutes a stock CTA on the rare case all cycles dangle | — |

## MAJOR ISSUES — validator regen-triggers (current set)

These are the issues that PROMPT REGEN today (i.e. `_REGEN_ISSUES` in
`apps/tutoring/validator.py`). When any fires, regen runs ≤3 cycles
and ships the cleanest candidate (or stock fallback if all dirty).
Listed roughly in order of severity / pilot frequency.

| Code | Detector | Trigger |
|---|---|---|
| `tutor_unsafe` | safety LLM judge | harmful or inappropriate tutor content |
| `answer_leak` | det + LLM + arbiter (`answer_leak.py`) | tutor stated the canonical answer or paraphrased it |
| `tutor_incoherent` | coherence LLM judge | self-contradiction, parallel questions, scaffold-vs-posed mismatch |
| `verdict_mismatch` | deterministic | tutor text contradicts the deterministic bank/math verdict |
| `figure_ref_without_signal` | regex | "looking at the diagram…" with no `|||MEDIA:N|||` attached |
| `figure_mismatch` | vision LLM judge | attached figure doesn't match the question |
| `numeric_claim_contradicted` | factual judge | numeric fact contradicted by grounded sources |
| `arithmetic_violation` | arithmetic judge | wrong arithmetic in the tutor's prose |
| `authoring_violation` | rule judge | tutor authored an MCQ inline instead of using `pose_question` |
| `rule1_violation` | rule judge | praised a bare answer without showing working |
| `repeated_question` | det + LLM + arbiter (`repeated_question.py`) | tutor re-asked an already-seen question (cross-turn / bank-repeat / active-paraphrase) |
| **`no_question`** | **handoff LLM judge** | **response doesn't hand the floor back (dangling promise, mid-sentence truncation, pure ack)** |

## Open work items (W1–W12)

Each item: scope · file:line · concrete deliverable · test.

---

### W1 — Parallel answer-leak detector (deterministic + LLM judge + arbiter)

**Scope:** L1, L3, L9, L14. The load-bearing fix.

**Design (per pilot directive 2026-05-17):** run BOTH detectors in
parallel for maximum safety, then resolve disagreements via a third
arbiter call:

```
                ┌─ deterministic ─→ leak?  YES/NO ─┐
input turn ─────┤                                  ├─ both agree → use verdict
                └─ LLM judge ──────→ leak?  YES/NO ─┘  disagree → arbiter call
                                                          ↓
                                              picks final verdict + reason
```

**Files:**
- `apps/tutoring/validator.py` — add `ISSUE_ANSWER_LEAK` constant +
  add to `_REGEN_ISSUES`.
- `apps/tutoring/answer_leak.py` (new module) — `detect_answer_leak()`
  orchestrator + `_deterministic_leak_check()` + `_llm_leak_check()`
  + `_arbiter_call()`.
- `apps/tutoring/exit_ticket_grader.py` — generalize so the LLM judge
  reuses the same client, retry, schema infrastructure as exit-ticket
  grading (see W6 — they're now joined).
- `apps/tutoring/conversational_tutor.py::_respond_impl` — call
  detector after `validate_tutor_response()` returns; pass
  `bank_question`, `chat_authored_question_text` (when applicable),
  `wrong_attempts`.

**Orchestrator signature:**
```python
def detect_answer_leak(
    response: str,
    bank_question,                       # canonical bank entry, or None for chat-authored
    chat_authored_q: Optional[str],      # last tutor question text when chat-authored
    wrong_attempts: int,
    llm_client,                          # required — LLM judge always runs
) -> Optional[LeakVerdict]:              # None when no leak, LeakVerdict when leak
```

**`LeakVerdict`:**
```python
@dataclass
class LeakVerdict:
    leaked: bool
    reason: str
    sources: List[str]           # ['deterministic', 'llm', 'arbiter']
    deterministic_said: Optional[bool]
    llm_said: Optional[bool]
    arbiter_said: Optional[bool] # only set when det != llm
    elapsed_ms: int
```

**Skip cases:** `wrong_attempts >= 3` (reveal allowed), empty
response.

**Deterministic check (MCQ + short_answer + numeric + FIB):**
- MCQ: against `correct_letter`'s option text. Normalize (lowercase,
  strip punctuation, drop stopwords). Subtract n-grams that also
  appear in the question stem OR in 2+ options (generic/topical).
  Leak if any 4-word remaining n-gram in response, OR Jaccard ≥ 0.6
  on significant words, OR regex `\bthe\s+(answer|correct\s+option)\s+is\s+[A-D]\b`
  matches `correct_letter`.
- short_answer / numeric / FIB: same n-gram + Jaccard against
  `expected_answer` + `explanation`.
- Chat-authored (no canonical): skip — LLM judge handles solo.

**LLM judge:** ALWAYS runs (regardless of deterministic). Calls
`grade_written_responses_batch`-style infrastructure (per W6) with
a leak-judgment prompt:

```
Did the tutor response REVEAL the correct answer to the student?

QUESTION:        {stem}
CORRECT ANSWER:  {canonical}
TUTOR RESPONSE:  {response}

REVEAL means the tutor stated the correct answer, named the
correct option letter, or paraphrased the canonical answer so the
student can copy it. Concept-level hints that don't restate the
answer are NOT reveals.

Output: leaked (bool) + reason (str ≤200 chars).
```

For chat-authored questions: pass `chat_authored_q` as both
question stem AND canonical (since the LLM-grader's prior
reasoning is the only "truth"). The judge's job becomes "did the
response give away the expected answer to its own question?"

**Arbiter (only on disagreement):**
```
Two leak detectors disagreed on this tutor response.

QUESTION:        {stem}
CORRECT ANSWER:  {canonical}
TUTOR RESPONSE:  {response}

DETECTOR A (n-gram):  said {det_verdict} because {det_reason}
DETECTOR B (LLM):     said {llm_verdict} because {llm_reason}

Resolve: did the tutor reveal the answer?
Output: leaked (bool) + reason (str ≤300 chars).
```

**Conflict-resolution rule (per pilot directive):**
- BOTH say leak → confirmed leak (high confidence)
- BOTH say no-leak → confirmed clean (high confidence)
- DISAGREE → arbiter call resolves; its verdict is used; log the
  disagreement with both reasons for tuning

**Test:**
- Unit: 12-15 crafted (response, question, wrong_attempts) tuples
  covering letter-statement, paraphrase, concept-word overlap
  false-positives, reveal-allowed cases, chat-authored cases,
  agreement cases, disagreement cases.
- E2E: lesson 540 wrong-then-wrong → first response detected →
  regen → clean hint → on third attempt the reveal_allowed path
  passes through.
- Log every disagreement to a file for post-hoc tuning of the
  deterministic thresholds.

**Latency budget:**
- Agreement case (~80% of wrong turns): det (~0ms) + LLM (~1.5s
  parallel via async) = ~1.5s
- Disagreement case (~20%): + arbiter (~1.5s) = ~3s
- Run det + LLM concurrently via `asyncio.gather` to mask the
  LLM cost when no disagreement.

**Estimate:** 4 hr (was 2 — added LLM judge + arbiter + chat-authored
coverage).

---

### W2 — Concrete forbidden-phrase examples + difficulty-tiered hint obviousness — **SHIPPED (Bundle A, 2026-05-17)**

**Status:** Helper `_build_hint_calibration_block(correct_option_letter,
correct_option_text, reveal_allowed)` added next to
`_build_active_bank_question_block`. Called from both the active-question
block (status `awaiting_answer` or `answered_wrong`, reveal not yet
allowed) and the bank-grade-signal block (verdict INCORRECT, reveal
not yet allowed). Returns empty when `reveal_allowed=True` so the
tutor isn't constrained on the 3rd-wrong walkthrough.

Renders:
- A FORBIDDEN list that quotes the actual canonical option text when
  MCQ ("Restating ... 'A pictorial graph of...' counts as REVEAL")
  + generic fallback for non-MCQ / chat-authored.
- A 5-tier OBVIOUSNESS directive keyed off `difficulty_level`
  (-2 VERY OBVIOUS ↔ +2 MINIMAL). Reveal threshold stays uniform at
  `wrong_attempts >= 3`.

**Scope:** L1, L2, L11, L14.

**Decision (pilot directive 2026-05-17):** reveal threshold stays
**uniform at `wrong_attempts >= 3` for ALL difficulty levels**.
Difficulty steers the OBVIOUSNESS of each hint, NOT when reveal
fires.

**Files:**
- `apps/tutoring/conversational_tutor.py::_build_active_bank_question_block`
- `apps/tutoring/conversational_tutor.py::_build_bank_grade_signal_block`

**Change 1 — concrete forbidden examples:** append to the wrong-status
rule a FORBIDDEN / ACCEPTABLE list rendered with the SPECIFIC option
text of the current question. Two paraphrase examples derived
programmatically from `correct_option`:

```
FORBIDDEN PHRASES (count as REVEAL):
  - "The answer is B." / "The correct option is B."
  - Restating option B in different words. E.g. if option B says
    "{correct_option_text}", these all count as reveal:
      - "{soft_paraphrase_1}"  ← built by light NLP swap
      - "{soft_paraphrase_2}"
ACCEPTABLE HINTS (concept-level):
  - "Think about what {topic_noun} is for."
  - "One of the other options is about a totally different feature."
```

If runtime example-generation is too fragile, fall back to one
static list calibrated to a math + a geography example.

**Change 2 — difficulty-tiered hint obviousness:** append an
[OBVIOUSNESS LEVEL] directive based on `difficulty_level`:

| difficulty_level | hint character | example for "compass rose / direction" |
|---|---|---|
| -2 (very easy) | very obvious, near-Socratic, eliminates wrong options | "A compass rose has arrows pointing N/S/E/W. Two of the options are about something other than direction — can you spot them?" |
| -1 (easy) | obvious, names the concept directly | "Think about what a compass rose actually shows on a map — it's about which way is which." |
| 0 (default) | concept-level hint | "Think about what a compass rose actually shows on a map." |
| +1 (hard) | subtle, requires inference | "What information does a compass rose contain that's different from a scale?" |
| +2 (very hard) | minimal — just signal which concept to revisit | "Reconsider the function of a compass rose." |

Render the appropriate one based on `self.difficulty_level`. Same
across both blocks.

**Test:** snapshot all three blocks (W2 forbidden list, W2 obviousness
tier, combined) for 3 sample questions × 5 difficulty levels =
15 snapshots; manual review.

**Estimate:** 1 hr (was 30 min — added the tiered obviousness).

---

### W3 — Leak-aware regen (suppress canonical)

**Scope:** L3, L18.

**Files:**
- `apps/tutoring/conversational_tutor.py::_build_regen_bank_context`
  — add `suppress_canonical=False` param.
- `apps/tutoring/conversational_tutor.py` — at regen call site, pass
  `True` when `ISSUE_ANSWER_LEAK` in issues.
- `apps/tutoring/regen/prompt.py::build_regen_prompt` — when
  `bank_context.suppress_reason == 'answer_leak_regen'`, prepend
  strict directive.

**Strict directive text:**
```
The previous response LEAKED the canonical answer. The student
answered wrong and is still allowed to retry. The canonical answer
has been INTENTIONALLY REMOVED from your context so you cannot
leak it.

Output: ONE concept-level hint that names what the question is
testing. Do NOT describe what any option says. Let the student
attempt again.
```

**Test:** trigger via lesson 540 wrong-answer; verify regen ran;
verify regen output doesn't contain canonical option text.

**Estimate:** 1 hr.

---

### W4 — Step evaluator: bank-verdict deference

**Scope:** L5, L15.

**Files:**
- `apps/tutoring/conversational_tutor.py::_evaluate_step` — prepend
  `[BANK VERDICT]` block when `_pending_bank_grade` exists.

**Block:**
```
[BANK VERDICT — TRUST THIS]
The deterministic bank grader has already verdicted this turn.
  is_correct: {bool}
  expected:   {bank_expected}
  student:    {bank_student_parsed}

Your `answer_correct` field MUST equal the bank verdict above.
Do NOT re-derive. Use `step_complete` for the orthogonal question:
has the WHOLE step's objective been demonstrated?
```

**Redundancy note:** `BANK_OVERRIDE` (commit `7b72765`) already
programmatically overrides at the engine layer. Prompt change is
belt to that brace — costs nothing, helps when porting to Qwen
where the override might be in a different code path.

**Estimate:** 30 min.

---

### W5 — Combined-judge prompt audit

**Scope:** L7, L14, L16.

**Files:** `apps/tutoring/judges/combined.py` + sibling judge
prompts under `apps/tutoring/judges/`.

**Audit task — read each judge's prompt + schema and check:**
1. Returns structured fields, not free-form narrative?
2. Each field has a `Literal[...]` or typed Pydantic type?
3. Concrete forbidden examples in the prompt (L14)?
4. Rationale (why this rule exists) included (L15)?

**Output:** Per-judge mini-report; ship fixes for any judge that
fails 2+ of the four.

**Estimate:** 30 min audit, 0–2 hr fix depending on findings.

---

### W6 — Unify exit-ticket grader + mid-lesson grader + leak judge

**Scope:** L14. Per pilot directive 2026-05-17 — the SAME grading
implementation must serve exit ticket, mid-lesson, AND the W1 LLM
leak judge. No parallel grading codepaths.

**Files:** `apps/tutoring/exit_ticket_grader.py`.

**Current state:** `grade_written_responses_batch` already handles
both exit-ticket batch grading AND mid-lesson grading
(`bank_grader.py::_grade_with_llm_batch` calls it for short_answer /
FIB / matching). W1's LLM judge needs to plug into the same
infrastructure.

**Change 1 — generalize the grader to multiple judgment types:**
```python
class JudgmentType(str, Enum):
    GRADE_CORRECTNESS = "grade_correctness"  # current — is student answer right?
    JUDGE_LEAK       = "judge_leak"          # NEW — did tutor reveal the answer?
```

Refactor `grade_written_responses_batch` → renamed
`run_grading_batch(items, *, judgment_type, llm_client)`:
- Same client + retry + structured-output infrastructure.
- Two prompt-builder branches by `judgment_type`.
- Two output schemas:
  - `BatchGradeResult` (existing — `correct: bool, reasoning: str, parts: [...]`)
  - `BatchLeakResult` (new — `leaked: bool, reason: str`)
- Backward-compat shim: keep `grade_written_responses_batch` as a
  one-line wrapper that calls `run_grading_batch(judgment_type=GRADE_CORRECTNESS)`
  so existing callers don't break.

**Change 2 — paraphrase acceptance examples (existing W6 scope):**
append acceptance examples to the GRADE_CORRECTNESS prompt:
```
ACCEPTABLE PARAPHRASES (mark correct):
  Expected: "Geography studies Earth and its inhabitants."
  Student:  "Geography is about Earth and the things on it." ✓
  Student:  "The study of our planet and its people." ✓

UNACCEPTABLE (mark wrong even if related):
  Student:  "Geography is a school subject." ← too narrow
  Student:  "It studies maps." ← partial / missing inhabitants
```

This addresses the lesson 538 "Earth and its inhabitants" friction
where the grader was too strict.

**Change 3 — JUDGE_LEAK prompt + schema:** see W1 for the prompt
text and conflict-resolution logic. The arbiter call also uses the
same `run_grading_batch` infrastructure with `judgment_type=JUDGE_LEAK`
and a third `arbiter=True` flag toggling the arbiter prompt variant.

**Test:**
- Verify all existing callers of `grade_written_responses_batch`
  still work (exit ticket grading + mid-lesson bank_grader.py).
- New: W1's leak detector calls `run_grading_batch(judgment_type=JUDGE_LEAK)`
  and gets back `BatchLeakResult`.
- Cross-cutting: same client, same retry policy, same timeout for
  all three judgment types.

**Estimate:** 2 hr (was 30 min — refactor + new judgment type +
schema + back-compat shim).

---

### W7 — Regen prompt restructure

**Scope:** L2, L14, L15, L17.

**Files:** `apps/tutoring/regen/prompt.py`.

**Changes:**
1. Verify bank ground-truth block is FIRST in user prompt (already
   shipped `6b38875` — confirm).
2. Replace "rewrite to fix violations" with per-violation fire
   conditions — model knows exactly what regenerates and why.
3. Add the strict directive from W3 when ANSWER_LEAK is among
   violations.
4. Add a "common rewrite anti-patterns" section listing what NOT to
   do (paraphrasing canonical, adding new questions, dropping the
   media signal).

**Estimate:** 1 hr.

---

### W8 — Pydantic schema strictness audit (system-wide)

**Scope:** L8.

**Audit scope:**
```bash
grep -rln "response_model=" apps/ | xargs grep -l "BaseModel"
```
~10-15 schemas across `apps/tutoring/judges/`, `apps/curriculum/
content_judges/`, `apps/curriculum/content_gen_schemas.py`,
`apps/curriculum/content_generator.py`.

**Per schema, verify:**
1. `model_config = ConfigDict(populate_by_name=True, extra='forbid')`
2. Every field the prompt mentions IS in the schema (no name drift)
3. Aliases set when prompt uses an alternate name
4. `@model_validator(mode='after')` enforces cross-field invariants

**Output:** Audit table per file. Fix any drifter (likely 1-3
schemas).

**Estimate:** 2 hr (audit + fixes).

---

### W9 — Confusion-signal exception in awaiting_answer rule

**Scope:** L10.

**Decision (pilot directive 2026-05-17):** use an LLM intent
classifier, not a regex. Misses too many phrasings ("i'm not sure",
"can you help with this one", "what's this about", "no clue") and
false-positives on natural prose. The LLM call is cheap (single
short call, structured output).

**Files:**
- `apps/tutoring/conversational_tutor.py::_build_active_bank_question_block`
- `apps/tutoring/conversational_tutor.py::_respond_impl` — call
  classifier on `student_input` BEFORE response gen, surface as
  `confusion_signaled: True` in the active_bank_question record.
- `apps/tutoring/exit_ticket_grader.py` — add a new judgment type
  `CLASSIFY_INTENT` to the unified grader (per W6) so this also
  flows through the shared infrastructure.

**Classifier prompt:**
```
The student is in a tutoring session and was just asked a question.
Classify their reply:
  - attempt: they tried to answer (right or wrong)
  - confusion: they signalled they don't know / are stuck /
    asking for help WITHOUT attempting
  - off_topic: unrelated to the question

STUDENT REPLY: {student_input}

Output: intent (attempt | confusion | off_topic) + reason (≤80 chars).
```

**Rule addition (to awaiting_answer scaffolding):**
```
If confusion_signaled is True, treat this as attempt-equivalent —
give the hint immediately (don't wait for a literal first attempt).
Still HINT, not REVEAL. Apply the difficulty-tiered obviousness
from W2.
```

**Test:** type variants ("i dont know", "no clue", "what's this
about?", "help me", "i'm not sure but maybe X") → first three should
flag `confusion`, last two should flag `attempt`.

**Estimate:** 1.5 hr (was 1 — added LLM call + new judgment type
plumbing).

---

### W10 — Resume regression test

**Scope:** L13.

**Status:** Resume currently works (session 44 e2e verified).

**Task:** Add the bail-and-resume scenario to whatever benchmark or
e2e harness exists (or add a Django shell-driven script under
`scripts/` if none does yet). Stub for now — pin the behavior so
it doesn't silently regress.

**Estimate:** 30 min.

---

### W11 — `[STUDENT_STATE]` structured block

**Scope:** L16.

**Files:** `apps/tutoring/conversational_tutor.py` — add a small
block to the tutor system prompt exposing live state as fields:
```
[STUDENT_STATE]
  consecutive_wrong: {n}
  cognitive_load: {0.0-1.0}
  difficulty_level: {-2..+2}
  confusion_signaled: {bool}
  wrong_attempts_on_active_q: {n}
[/STUDENT_STATE]
```

Sonnet handles current prose okay; Qwen will probably need these as
structured fields. Cheap to add now.

**See W13 for the Qwen prompt-engineering research that informs how
this block (and the rest of the prompt surface) should be structured
to be portable.**

**Estimate:** 30 min.

---

### W12 — CLAUDE.md process additions

**Scope:** L19, L20, L21, L22.

**Decision (pilot directive 2026-05-17):** **fold into existing
sections**; don't add a new "Lessons from 2026-05-17" section.
Keep the doc lean. Each rule is one sentence, reference memory
files for detail.

**Files:** `CLAUDE.md`.

**Targeted edits:**

1. **L19 → fold into "Bug-fix workflow — test locally before
   deploy" section.** Add ONE line: "Reading code is not a substitute
   for driving the UI — subtle LLM behavior surfaces only in the
   actual chat flow."

2. **L20 → fold into "Bug-fix workflow" section.** Add ONE line:
   "When a bug looks weird, RESET the relevant state (session, DB
   row, cache) before proposing a patch — see
   `memory/hint_vs_reveal_guards_plan.md` for the prototype case."

3. **L21 — already in CLAUDE.md.** No-op.

4. **L22 → fold into the "Project-local planning" section
   (already exists).** Reinforce: "Feature plans + execution
   artifacts live HERE (`memory/`, git-tracked). Auto-memory
   (`~/.claude/projects/.../memory/`) is for cross-cutting / general
   notes ONLY (deployment history, resolved incidents,
   cross-session learnings). Feature work in git for ease of
   review + history."

**Doc cleanup pass while we're in there:** identify any sections
that have grown bloated and replace inline detail with memory-file
references. Aim to keep CLAUDE.md under its current length.

**Estimate:** 1 hr (was 30 min — added cleanup pass).

---

### W13 — Qwen prompt-engineering research

**Scope:** L16, L22, plus L1/L2/L14 (concrete-rule binding) applied
to a smaller model. Pilot directive 2026-05-17: build the current
prompt surface to work for Sonnet **today** AND Qwen **in the future**
(offline mobile path). Need expertise before W11 / Session C ships.

**Files:** new skill at `.claude/skills/qwen-prompting-expert/SKILL.md`,
modeled on the existing `claude-prompting-expert`,
`gemini-prompting-expert`, `openai-prompting-expert` skills.

**Research scope:**
1. Which Qwen sizes are realistic for offline mobile? Reference
   `auto-memory/feedback_on_device_llm_findings.md` (Qwen 2.5 0.5B = 9
   tok/s, 0.8B marginally better, 3B too slow). Likely 0.5–3B band.
2. Qwen-specific prompt patterns from the model card + Hugging Face
   docs + qwen-team writeups:
   - Chat template format (Qwen2/2.5/3 vary)
   - Reasoning vs non-reasoning modes (Qwen 3 has both)
   - Tool-calling reliability vs Sonnet
   - Structured output (JSON mode? `instructor` compatibility?)
   - Rule-following discipline (anecdotally weaker than Sonnet → need
     even MORE structured-field-driven, even LESS prose-rule reliance)
3. What prompt patterns degrade hardest going from Sonnet → Qwen?
4. Concrete portability checklist: every prompt in this codebase
   gets a "Qwen-friendly?" pass.

**Deliverable:** SKILL.md + 1-page audit applying the new skill to
the prompts touched by W1–W12. Identify portability gaps; create
follow-up work items if any are critical.

**Estimate:** 4 hr research + 1 hr audit = **5 hr.**

**Sequencing:** ship W13 BEFORE Session C, so W11 (structured-field
block) is informed by it. Could also run alongside Session A as
background research.

---

### W14 — Repeated-question structural guard — **SHIPPED (Bundle A, 2026-05-17)**

**Status:** Module `apps/tutoring/repeated_question.py` complete;
wired into `_respond_impl` after the W1 leak check; persists
`recent_tutor_question_sigs` (cap 10) on `engine_state` for cross-turn
+ resume coverage; routes borderline Jaccard cases through
`run_grading_batch(JUDGE_REPEAT)`; appends `ISSUE_REPEATED_QUESTION`
to validation.issues so the regen path picks it up via the
violation-handler block in `regen/prompt.py`.

**Final tuned thresholds (lowered after smoke tests):**
- `JACCARD_EXACT_REPEAT = 0.75`
- `JACCARD_STRONG_REPEAT = 0.55` (was 0.7)
- `JACCARD_BORDERLINE_LOW = 0.20` (was 0.45 — catches verb-swap paraphrases)
- `ACTIVE_PARAPHRASE_THRESH = 0.45`

**Original design (kept below for reference):**

**Scope:** L23. Parallel to W1 — same flag-and-regen structural
pattern. Per pilot directive 2026-05-17: enforce no-repeated-questions
via deterministic structure, not just prompt rules. The prompt rule
exists ("DO NOT re-author the question stem", line ~4120) — Sonnet
sometimes ignores it. The guard catches what the prompt misses.

**Three repetition modes to catch:**
1. **Cross-turn authored repetition.** Tutor authors "What's a
   compass rose used for?" on turn 5, then again on turn 9 after
   topic drift.
2. **Authored ≈ already-shown bank.** Tutor authors a question whose
   substance overlaps with a bank Q the student already answered.
3. **Paraphrased re-ask of CURRENT bank Q.** Tutor paraphrases the
   bank question the student is actively trying to answer instead
   of giving a hint.

**Files:**
- `apps/tutoring/validator.py` — add `ISSUE_REPEATED_QUESTION`
  constant + add to `_REGEN_ISSUES`.
- `apps/tutoring/repeated_question.py` (new module) —
  `detect_repeated_question()` orchestrator with deterministic
  signature-based check + optional LLM judge for semantic
  paraphrases.
- `apps/tutoring/conversational_tutor.py` — track
  `recent_tutor_questions` list (last N=10 normalised signatures)
  on `engine_state`; call detector after `validate_tutor_response`.
- `apps/tutoring/regen/prompt.py` — add directive when
  `ISSUE_REPEATED_QUESTION` fires: "you repeated a question already
  asked — pick a DIFFERENT angle or advance to the next concept".

**Orchestrator signature:**
```python
def detect_repeated_question(
    response: str,
    recent_questions: List[str],         # normalised signatures, last 10
    shown_bank_stems: List[str],         # stems of bank Qs already shown
    active_bank_stem: Optional[str],     # current pending bank Q (if any)
    llm_client,                          # for the optional LLM judge
) -> Optional[RepeatVerdict]:
```

**Question extraction:**
- Find all `?`-terminated sentences in the response.
- For each, normalise: lowercase, strip punctuation, drop stopwords,
  sort tokens.
- The signature is the resulting bag-of-words string.

**Deterministic checks (cheap, run first):**
1. **Exact signature match** against `recent_questions` → REPEAT.
2. **Jaccard ≥ 0.7** against any recent question signature → REPEAT.
3. **Jaccard ≥ 0.7** against any `shown_bank_stems` → BANK_REPEAT.
4. **Jaccard ≥ 0.6** against `active_bank_stem` (current pending Q) →
   ACTIVE_PARAPHRASE (the worst case — paraphrasing the question
   the student is being asked).

**LLM judge (only on borderline 0.45–0.7 Jaccard cases):**
Routes through `run_grading_batch(judgment_type=JUDGE_REPEAT)` —
piggy-backs on the W6 infrastructure. Prompt:
```
Did the tutor REPEAT a question they (or the bank) already asked?

PREVIOUS QUESTIONS:
  - {q1}
  - {q2}
NEW QUESTION FROM TUTOR: {extracted_question}

REPEAT means the new question is substantively the same as a
previous one — not just same topic, same actual question.
Asking about a different angle of the same concept is NOT a
repeat. Asking literally the same thing in different words IS.

Output: repeated (bool) + reason (str ≤200 chars).
```

**Disagreement (deterministic says repeat, LLM says not) →** trust
the LLM (false-positives hurt UX more than missing one repetition).
Symmetric trust pattern with W1's arbiter — log the disagreement.

**State tracking:**
- `recent_tutor_questions: List[str]` — append signature on every
  tutor turn that ends with `?`. Cap to last 10. Persist on
  `engine_state`.
- `shown_bank_stems: List[str]` — already tracked via
  `shown_question_ids` + lookup; just need to surface stems by
  joining at detection time.

**Regen directive (when ISSUE_REPEATED_QUESTION):**
```
You REPEATED a question you (or the bank) already asked. The
student already saw this question. Do NOT re-ask it. Choose ONE:
  - Advance to the next concept / step
  - Ask about a DIFFERENT angle of the same concept (not the
    same question rephrased)
  - Give a hint about the still-pending question if there is one
```

**Test:**
- Unit: 8-10 crafted (response, recent, bank, active) cases —
  exact match, paraphrase, different angle (should NOT flag),
  active-paraphrase, borderline Jaccard → LLM call.
- E2E on lesson 540: deliberately drift topic then re-author
  earlier question; verify regen.

**Estimate:** 3 hr (deterministic detector + LLM judge integration +
state tracking + regen directive + tests).

---

## Phased delivery

**Decision (pilot directive 2026-05-17):** **bundle all 13 work items
into one delivery**, framed as "learnings from e2e tutoring sessions
2026-05-17". One large logical change, ship as a series of related
commits with clear messages so the diff is reviewable.

### Pre-work
- **W13** (Qwen prompt-engineering research + skill) — runs in
  background; outputs inform W11 + ALL prompt-touching work items

### Bundle A — Load-bearing structural guards (grader + leak + repeat)
Pilot directive 2026-05-17: build the structural enforcement layer
first. W6 is the foundation both W1 and W14 depend on; W3 closes
the loop with leak-aware + repeat-aware regen; W2 adds prompt
belt to the structural braces.
- **W6** (refactor `exit_ticket_grader` → `run_grading_batch` with
  GRADE_CORRECTNESS + JUDGE_LEAK + JUDGE_REPEAT + CLASSIFY_INTENT
  judgment types; back-compat shim)
- **W1** (answer-leak guard: deterministic + LLM judge + arbiter)
- **W14** (repeated-question guard: deterministic signature +
  optional LLM judge on borderline)
- **W3** (regen-aware: strip canonical on leak; rephrase directive
  on repeat)
- **W2** (forbidden examples + difficulty-tiered hint obviousness)
- E2E smoke test on lesson 540: trigger leak → regen → clean hint;
  trigger repeat → regen → different angle.

### Bundle B — Judge + grader sweep
- **W4** (step evaluator bank-verdict block)
- **W5** (combined judge audit + fixes)
- **W7** (regen prompt restructure)
- Run lesson 537 + 538 + 540 e2e

### Bundle C — Edges + structure
- **W9** (confusion-signal LLM classifier; uses W6 infra)
- **W11** (`[STUDENT_STATE]` structured block; informed by W13)
- **W10** (resume regression test)

### Bundle D — Schema audit + docs
- **W8** (Pydantic schema audit + fixes — system-wide)
- **W12** (CLAUDE.md fold-in + cleanup pass)

### Pre-work (background)
- **W13** (Qwen prompt-engineering research + skill) — runs in
  background during Bundle A/B; output gates Bundle C's W11

**Total: ~20 hr solo** (was ~17 — added W14 ~3 hr).

**Ship cadence:** complete a bundle, smoke-test, commit, move to
next bundle in the same session OR next. All five bundles target
one logical PR / one push to main when the full bundle works
end-to-end.

## Out of scope

Won't be built in this iteration:

- **Multi-question guard** (>1 `?` in response). Observe production
  frequency first.
- **LLM-based answer-leak judge** (parallel to W1's regex). Skip
  unless W1 false-pos rate is >15% in prod.
- **Difficulty-tag-aware partial-credit thresholds in
  exit-ticket grader.** Hold for pilot ask.
- **Auto-derived per-student difficulty tier.** Future competency
  work.

## Risks

- **W1 false-positive rate too high.** Mitigations layered:
  subtract stem n-grams, subtract n-grams in 2+ options, escalate
  Jaccard 0.6 → 0.75. Worst case: turn off and ship as warning-only
  flag while we tune.
- **W2 dynamic paraphrase generation flaky.** Fall back to static
  list (one math + one geography example).
- **W8 reveals 5+ schema drifters.** Add 2-3 hr to Session C.
- **Sonnet still soft-reveals despite W1 + W2 + W3.** Then W3 regen
  re-fires and the leak-aware path produces a clean hint. Guard is
  the load-bearing piece; prompts are insurance.
- **CLAUDE.md bloat (W12).** Cap each rule to one sentence; link to
  memory files for detail. << yeah lets keep this document simple. I think we can do some clean up here. and reference the memory files more for the details.>>

## Resolved decisions (from pilot directive 2026-05-17)

| Question | Decision |
|---|---|
| Reveal threshold | Uniform `wrong_attempts >= 3` for ALL levels. Difficulty steers hint OBVIOUSNESS instead (W2). |
| Confusion detection (W9) | LLM intent classifier, not regex. |
| Schema audit scope (W8) | System-wide — schemas must be consistent and structured everywhere, runtime + scaffolding. |
| Session split | Bundle all as one logical delivery "learnings from e2e tutoring sessions 2026-05-17"; ship in 5 reviewable bundles. |
| CLAUDE.md placement (W12) | Fold into existing "Bug-fix workflow" + "Project-local planning" sections. No new section. Cleanup pass while we're in there. |
| Exit-ticket grader reuse (W1 LLM judge) | YES — refactor `exit_ticket_grader` so the SAME implementation serves exit ticket, mid-lesson, and the leak judge (W6). |
| LLM judge placement in leak detector (W1) | Parallel deterministic + LLM. Agree → use verdict. Disagree → arbiter call resolves. Maximum safety. |
| Qwen portability | New W13 — research + skill + audit pass BEFORE W11 structured-state block ships. |
| File placement convention | Plans + execution artifacts → repo `memory/` (git-tracked). Auto-memory for cross-cutting / general only. Codified in W12. |

## Remaining open questions

1. **Chat-authored leak coverage** — confirmed via comment "use LLM
   judge for sure here". So W1 covers chat-authored via the LLM
   judge solo (no deterministic since no canonical). Include in
   Bundle 2 (not deferred). ✅ resolved.
2. **W13 sequencing** — run Qwen research as background BEFORE
   Bundle 4 (which contains W11), or block Bundle 4 on W13
   completion? Recommend background-then-block — start W13 first,
   bundle the rest, gate W11 specifically on W13 output.

## Next step

Confirm Bundle order + W13 sequencing → I start with W13 (4 hr
Qwen research) running in background while Bundle 1 (W6 grader
refactor) ships first → then Bundle 2 (W1 + W3 + W2) end-to-end
test → Bundles 3-5 follow.

## References

- 21 learnings (L1–L22): in-chat 2026-05-17.
- Shipped this session: 5495f7c, 7b72765, 285c160, 9aeec08,
  5d26aba, 6b38875, 1a72945, 43ce5d2, 54fe231, b7323de.
- Difficulty + judge audit: Explore agent report 2026-05-17.
- Tutor model decision: auto-memory
  `project_tutor_model_choice.md` (recommends Opus 4.7); current
  ModelConfig reverted to Sonnet 4 per pilot directive
  (model-agnostic for Qwen mobile path).
- Qwen on-device findings: auto-memory
  `feedback_on_device_llm_findings.md` (informs W13 scope).

