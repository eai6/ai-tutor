# Martin's session — fix plan

**Status:** approved 2026-05-06 — executing
**Source:** Martin's WhatsApp report 2026-05-05 23:35 → 2026-05-06 00:16 + Azure log analysis of sessions `156`, `157`, `158`
**Owner:** Edward
**Last updated:** 2026-05-06

> Edward's notes are kept inline below as `> [Edward]` blocks. Resolution decisions are captured in §0 below.

---

## 0. Decisions taken (2026-05-06)

- **Phase 6 (disk space) → moved to first.** Investigate quota propagation before any code changes.
- **Phase 1a → random-sample fallback, NOT skip.** When EO/concept_tag matching misses, randomly sample from the lesson's full bank. Bank questions are loosely related to the teaching objective; preserves grounding (can't drop grounding).
- **Phase 1b → accepted.** `bare_answer=False` when student input contains an arithmetic expression / `=` sign.
- **Phase 1c → accepted.** Replace verbatim phrase prescription with non-attractor instruction.
- **Phase 1d → conservative.** Keep imperatives ("let's check" / "show me" / "walk me through") in `_QUESTION_RE` BUT require an actual `?` on the same line. Repetition was the problem, not the phrasing — Socratic prompts are valid.
- **Phase 2 root cause confirmed:** `LessonStep.enabling_objective` is empty on most steps. Production dashboard shows 1 of 10 steps tagged for "Angles around a point". Content-generation pipeline drops EOs. Phase 2 expanded to: investigate the generation gap (2a), fix generation so every new step gets an EO (2b), backfill existing untagged lessons (2c), add empty-bank audit log (2d).
- **Phase 3 (Sonnet vs Opus) → defer until Phase 1+2 ship.** Suspect the empty-bank cascade was the cause of low tool compliance. Stay on Sonnet for now; switch to Opus only if measured compliance is still bad post-Phase 1+2.
- **Phase 5 → confirmed.** Add structured per-turn logging.
- **Out of scope items confirmed.** v2 plan, more universalization, bank content cleanup at scale — all deferred.

### Revised sequencing

```
Phase 6 (disk space)    ← FIRST
   ↓
Phase 1 (cascade fix — random-bank fallback + bare-answer + regen-rewrite + ? required)
   ↓
Phase 2 (fix content-gen to populate EOs + backfill existing lessons + bank audit log)
   ↓
Re-test with Martin (fresh session URL, same lesson)
   ↓
Phase 3 (model decision based on actual compliance numbers)
   ↓
Phase 4 + 5 in parallel
```

---

## 1. Context — what we accomplished in the last 3 weeks

What we shipped (good and bad consequences flagged):

- **Correctness layer** ✅ — combined judge with arithmetic + factual + step-eval merged into one call. Deterministic numeric eval. Two-phase exit-ticket grading. Martin's session had **zero arithmetic errors** — this is the load-bearing win and stuck.
- **Subject universalization** ⚠️ — vision, question-bank, pose_question tool, judge: all gates removed, all subjects. Expanded surface area significantly.
- **Tool refactor + Sonnet swap** ❌ — merged 5 LLM calls/turn → 2; switched active tutoring model from Opus 4.7 → gpt-4o → Sonnet 4. Prompts are still Opus-tuned. Tool compliance collapsed (see §3).
- **Engine plumbing** ✅ — messages array (not embedded text), per-step-type hard caps, deterministic-only fast-path, opener rotation, prerequisite recap restrictions, defensive `_strip_leaked_tool_call_syntax`.
- **UX/infra (last 48h)** ✅ — staff password reset, Azure quota bump (5→100GB; not fully verified, see §7), exit-modal/pretest unification, password-change middleware.

Net: correctness gains held; conversational texture and selection logic regressed.

---

## 2. What Martin reported (chronological)

From WhatsApp messages, mapped to session IDs in production logs:

| Bug | Where | Frequency |
|---|---|---|
| "Show me your working" reflex on simple ops (e.g. `360-290`) | Both lessons | High |
| "Let's check this one together" appearing as filler — even when answer was correct | Both lessons | **9× in single session 157** |
| `pose_question(slot=N)` / `<tool_use>` XML leaking as text | Lesson 1 | Multiple |
| Quiz showing the exact same question student just answered | Lesson 1 | Yes |
| "Nonsensical questions" pulled from the bank | Lesson 1 | Multiple |
| "Suggested answer doesn't make sense" | Lesson 1 | Once |
| Image not related to question posed | Lesson 1 | Once |
| Fill-in-blank ambiguous (unclear what right answer is) | Geography lesson | Once |
| "Almost unusable... bugs not present before. New things." | Overall | — |
| **Positive:** images good; geography lesson "much better"; no arithmetic errors | — | — |

Direct quote: *"My brief conclusion is that we do not need more testing by other people. We are still at a stage in which a simple session by ourselves creates lots of mistakes. More than before."*

> NOTE: Martin tested at least 3 sessions tonight: `156` (different lesson, similar bugs), `157` (the angles-on-a-straight-line transcript he sent), `158` (his second attempt).

---

## 3. What the production logs prove

Pulled from Azure Log Analytics workspace `aitutor-pixel-logs` for the window `2026-05-05T19:40Z → 2026-05-05T20:10Z`.

### 3.1 Tool compliance is 3%

30 LLM calls observed during session 157. Distribution:
- `tool_use_count=0` on **29 of 30** calls
- `tool_use_count=1` on **1** call (only the very last turn at 20:02:16)
- Tools ARE being passed to the API (`tools=1` in every call)
- Sonnet sees the tool definition and emits text instead of a `tool_use` block

This is catastrophic, not borderline. The XML leakage Martin saw is Sonnet typing the tool call as prose because that's the only way it's "calling" the tool.

### 3.2 The bank was empty for this lesson on every turn

Every `pick_candidates_for_step` call returned `empty pool`. The lesson tag is `straight_line_angles`, EOs are well-formed (e.g. *"Calculate a missing angle on a straight line given one or more other angles"*), but candidate pool is consistently zero.

System prompt still offered the pose_question tool with `slots=[0]` while the bank itself had nothing in it. Sonnet was told *"use the bank, don't author"* with NO bank to use. It authored. Validator fired. Cycle below ran.

### 3.3 The texture phrases are PRESCRIBED verbatim

`apps/tutoring/conversational_tutor.py:5349-5354` — regen constraint block, fires whenever RULE_1 is flagged:

```python
"If RULE_1 was flagged: remove ALL praise and replace"
" with a request for the student's working — \"Walk"
" me through your steps\" or \"Show me how you got"
" there\"."
```

Combined with `apps/tutoring/validator.py:81-87` — `_QUESTION_RE` accepts `"let's check"` / `"show me"` / `"walk me through"` as valid "ends with question" patterns, even without a `?`. So the LLM finds the cheapest phrase that satisfies both the regen prescription AND the question regex, and it's exactly the phrase Martin saw 9 times.

### 3.4 Validator regenerated on ~80% of turns

Pattern across session 157 (~14 minutes):
- `authoring_violation` on every turn (because bank empty → Sonnet authored)
- `rule1_violation` whenever Sonnet praised a bare numeric answer
- `unfounded_praise_stripped` (validator removing "Right!" / "Perfect!")
- `arithmetic_violation`, `info_dump_warning` mixed in

Regen happens once, often with same issues again, then we accept the second pass.

### 3.5 Other findings

- **BankGrade returns None for lesson_step refs:** `[BankGrade] session=156 ref=lesson_step:9771 is_correct=None expected=None student=None` — the bank-grade lookup silently returns nothing when the ref is `lesson_step:N` (vs `exit_ticket_question:N`). Likely wrong PK lookup path.
- **System prompt is 37–39 KB per call.** Sonnet is drowning in directives.
- **Conversation messages reach 45 by end of session.** Context fills fast.
- **LayerS classifier knew when working was shown** — `partial_correct` fired when Martin wrote `180-62-30=88`, but RULE_1 still tagged the turn because of how `bare_answer` is computed.
- **Disk space error STILL hit at 14:52 UTC** (after our 5→100 GB quota bump): `"No space left on device: '/app/media/media/global/generated_*'"`. Quota change may not have propagated, or the path is on a different mount.

---

## 4. The cascade, mapped

```
Lesson has no bank questions matching tag/EO          (§3.2)
   ↓
System prompt: "use pose_question tool" (with empty bank)
   ↓
Sonnet authors a question in prose                    (§3.1 — won't use tool anyway)
   ↓
Validator → AUTHORING_VIOLATION
   ↓
Sonnet praises student's bare answer ("Right!")
   ↓
Validator → RULE_1_VIOLATION + unfounded_praise_stripped
   ↓
Regen constraint: "remove ALL praise; replace with 'Walk me through your steps'"   (§3.3)
   ↓
Sonnet emits exactly that phrase
   ↓
Validator's question regex accepts it as a valid question                          (§3.3)
   ↓
Student sees "Let's check this one together — can you walk me through your steps?"
```

**Implication:** the texture problems aren't a rotation/opener bug. They're the deterministic output of the validator-regen loop running on a lesson with an empty bank. Fix the cascade at its sources, the symptoms collapse.

---

## 5. Plan

### Phase 1 — Stop the cascade at its root

> **Goal:** kill the loop that produces "Let's check this one together" / "Walk me through your steps" verbatim every turn.
> **Effort:** ~1 day
> **Files touched:** `apps/tutoring/conversational_tutor.py`, `apps/tutoring/validator.py`, `apps/tutoring/question_bank.py` or wherever bank slot construction lives.

**1a. Random-bank fallback when EO/tag matching fails.** ✅ Already in code (no change needed).

`apps/tutoring/question_bank.py::pick_candidates_for_step` (lines 240-246) already random-falls-back to `pool[:max_candidates]` when both EO and concept_tag matching return zero. So when a step has no EO (or its EO doesn't match anything in the bank), the engine still grounds questions on the lesson's bank-sampled pool. No engine change needed for the EO-mismatch case.

The "empty pool" log Martin's session showed (`[QuestionTool] pick_candidates_for_step: empty pool`) is a different and upstream issue: the lesson genuinely has no published `ExitTicketQuestion` rows. That's not supposed to happen — every lesson should have a populated bank. **Phase 2 addresses that root cause.**

> Edward's original note (kept for context):
> "Why would the questions bank be zero. This is a bug. There must always be a question bank for a lesson. It seems this lesson 'Angles around a point' had lesson steps that did not have the EO attached, thus this might have made it impossible to match with EO in the questions bank. Thus what we should do in the future if this happens is to just sample randomly from the questions bank. Every question in the bank is loosely related to the teaching objective thus this is a better solution than no real verified question."


**1b. Soften RULE_1 sensitivity.**
> yes this is a good solution. bare_answer is only true if student just submit one number or the final answer like 88. 

`bare_answer` should be False when the student's input contains an arithmetic expression (regex: `\d.*[\-+×÷*/].*\d` or `=`). When student wrote `180-62-30=88`, that's working — no praise stripping warranted, no "show me your working" follow-up.

**1c. Remove prescribed phrases from the regen constraint.**

> I am not sure here. But i like the proporsal. 

`apps/tutoring/conversational_tutor.py:5349-5354` — instead of
```
"replace with a request for the student's working — 'Walk me through your steps' or 'Show me how you got there'"
```
say:
```
"Drop the praise; ask one focused question OR transition forward without restating prior content."
```
Let the LLM phrase it; we just remove the bad-pattern attractor.

**1d. Tighten `_QUESTION_RE`.**
> Good

`apps/tutoring/validator.py:81-87` — remove `let's check`, `show me`, `walk me through`, `let's see` from the imperative-accepted list. Require an actual `?` or one of the strict interrogative patterns + `?`. This stops the validator from rubber-stamping bland transitions.

**Verification (replay session 157 against the fixes):**
- ✅ Zero `authoring_violation` on empty-bank turns >>> there should never be empty bank. Check notes above
- ✅ Prescribed phrases ("Walk me through your steps") absent from regen constraint output
- ✅ Validator no longer accepts "let's check this one together" as a valid question
- ✅ Re-running Martin's exact transcript turn shapes shouldn't produce the 9× repetition

> QUESTION FOR EDWARD: are you OK with 1d removing these phrases entirely? They WERE accepted as valid Socratic prompts on purpose. The risk is rejecting genuinely good imperatives like "Walk me through how you got there" when a real teacher would phrase it that way. Alternative: keep them but require a `?` on the same line.

> I think the issue was that they were being repeated multiple times. If we only had it for a few turns it would not be an issue. Socratic approach is valid and we should have it. Student should be asked to show working if just give bare answer without showing work in previous turn or exchange.

---

### Phase 2 — Bank audit + EO population fix

> **Goal:** stop the upstream cause of empty/unmatched banks: lesson steps that don't have EOs attached.
> **Effort:** ~1 day
> **Owner:** Edward / engineering
> **Confirmed root cause (2026-05-06):** "Angles around a point" lesson dashboard shows 1 of 10 steps with EO tag. Content-gen pipeline drops EOs on most steps.

**2a. Investigate why lesson steps lack EO tags.**
- Trace the LessonStep write path in the content-generation pipeline (likely `apps/curriculum/content_generator.py` or the parser that produces steps from teaching objectives).
- Identify whether the LLM-generated payload is omitting EOs, the parser is dropping them, or the model field isn't being populated on save.
- Document the gap.

**2b. Fix generation so every new step gets an EO.**
- Each LessonStep belongs to a TeachingObjective which decomposes into EOs; every step should be tagged with one of those EOs (or, fallback, the broader concept_tag).
- Add validation at save-time: refuse to persist a step without EO when the parent lesson has EOs defined. Surface the failure in the teacher dashboard (so generation re-runs are explicit, not silent).

**2c. Backfill EOs on existing untagged steps.**
- Management command: for each lesson, walk steps with empty EO, infer the most likely EO from the step's content (LLM call against the lesson's EO list), persist.
- Dry-run mode first; review a sample before committing.

**2d. Add `[BankAudit]` startup log:** if `pick_candidates_for_step` returns empty across all steps in a lesson, emit a warning at session start so we catch this systemically rather than discover it from chat transcripts.

> NOTE: Phase 1a (random-bank fallback) makes empty-EO sessions GRACEFUL — student still gets bank-grounded questions even when step EOs are missing. Phase 2 fixes the upstream gap so steps are properly tagged going forward. The two together: belt + suspenders.

---

### Phase 3 — Tool compliance decision (Sonnet vs. Opus)

> **Goal:** stop the 3% tool-use rate.
> **Effort:** ~1 day, dependent on Phase 1 outcome
> **Files touched:** `apps/llm/migrations/00XX_*.py` (model config) OR `apps/tutoring/question_bank.py` (tool description rewrite)

>> i suspect this was due to the empty bank, but we will see after the test.

With Phase 1 done (no false-positive authoring violations from empty banks), re-measure Sonnet's tool compliance on a session where the bank actually has slots.

**Decision tree:**
- If compliance is still <50% → revert active tutoring config to **Opus 4.7**. Pay the latency cost. Pilot needs a working tool more than it needs speed.
- If compliance is 50–85% → keep Sonnet but rewrite the tool description shorter / more imperative; remove the warning text (warnings don't help; structure does).
- If compliance is ≥85% → leave model alone. Keep `_strip_leaked_tool_call_syntax` as a defensive backstop and expand its regex to catch the multi-line `<tool_use>...<parameters>...</parameters></tool_use>` shape we observed.

> QUESTION FOR EDWARD: pilot launch deadline — does Opus latency (≈8–12s/turn) put student attention at risk? If yes, we may need to live with worse tool compliance and accept manual question authoring as the path.

if we need to we will switch to opus. But before we do that lets do the test after we fix the empty bank and tool issues for sonnet

---

### Phase 4 — Bank grade lookup bug

> **Goal:** fix `[BankGrade] is_correct=None expected=None student=None` for `lesson_step:N` refs.
> **Effort:** ~half day

Find the bank-grading path that handles `ref=lesson_step:N` vs `ref=exit_ticket_question:N`. Likely a missing branch or wrong PK lookup. Symptom: when student answered an MCQ with a letter, the grader returned None instead of correct/incorrect.

---

### Phase 5 — Cosmetic + observability

> **Goal:** polish the things Martin noticed and add the per-turn logging we needed for THIS analysis.
> **Effort:** ~half day

- **Phase classifier badge leak** — Martin saw `tutor off_topic` as a badge label in the chat UI. Trace where the phase classifier output reaches the frontend label and gate it.
- **Summary template typo** — *"You've ready for today's exit ticket to show what you've learned., Martin!"* — extra comma + wrong contraction. Find the template string in `_get_step_phase_instructions` or the close-out path.
- **Structured per-turn log line** — emit one JSON line per turn with: `eval_layer`, `is_correct`, `tool_use_count`, `validator_issues`, `regen_count`, `bank_empty`, `bare_answer`, `student_input_excerpt`. This lets us replay any future session against the same metrics without scraping multiple log lines.

>> Yes add detailed per-turn logging 

---

### Phase 6 — Disk space root cause (separate but ongoing)

> **Goal:** the 5→100 GB quota bump didn't fully propagate.
> **Effort:** ~half day

- `az storage share show` to compare `quota` vs. `actualQuota`
- Restart the container app revision to remount with new quota
- Verify the path that's hitting Errno 28 is on the bumped share, not a different mount

>> lets investigate this

---

## 6. Sequencing + stop conditions

```
Phase 1 (single PR, 4 small changes — validator + regen)
  ↓
Replay session 157 turn shapes locally — verify no "Let's check this one together"
  ↓
Phase 2 in parallel (content/DB query)
  ↓
Send Martin a fresh session URL on the same lesson
  ↓
  Stop condition: if Martin still reports "almost unusable", abort and re-evaluate
  before doing Phases 3–6
  ↓
Phase 3 decided after re-measuring tool compliance
  ↓
Phases 4, 5, 6 in parallel
```

Total: ~3–4 days of focused work, gated by Martin re-test after Phase 1+2.

---

## 7. What's explicitly NOT in this plan

- **v2 redesign** (`memory/curriculum_tutor_v2_plan.md`) — haven't read it; if any of Phases 1–6 conflict with v2 direction we should align before Phase 1 starts.
- **More universalization** — frozen until this is back to a clean baseline.
- **New features.**
- **Any change to the correctness layer** — combined judge + deterministic eval are working; do not touch.
- **Bank content quality cleanup at scale** — Phase 2 is just for `straight_line_angles`; broader content cleanup is a parallel content-team task.

>> focus on the issue we have here.

---

## 8. Open questions — resolved 2026-05-06

| # | Question | Resolution |
|---|---|---|
| 1 | Phase 1d aggressiveness | **Conservative.** Keep imperatives; require `?` on same line. |
| 2 | Phase 3 model decision | **Defer.** Stay on Sonnet; revisit only if compliance still bad after Phase 1+2. |
| 3 | Phase 2 ownership | **Edward / engineering.** Content-gen pipeline gap (LessonStep.enabling_objective empty). Confirmed: question (1) "Does the lesson have EOs populated on steps?" → False. |
| 4 | v2 plan reconciliation | **Skip.** Focus here. |
| 5 | Phase 6 priority | **Promoted to first.** Disk-space first, then Phase 1. |

---

## 9. Appendix — log query snippets

For reproducibility / future investigations:

```bash
# Latest log time
az monitor log-analytics query \
  --workspace 9555deab-8f9a-4403-98ee-957284f03cc5 \
  --analytics-query "ContainerAppConsoleLogs_CL | summarize Latest=max(TimeGenerated)" \
  -o json

# All Validator + LayerS lines for a session window
az monitor log-analytics query \
  --workspace 9555deab-8f9a-4403-98ee-957284f03cc5 \
  --analytics-query "ContainerAppConsoleLogs_CL | where TimeGenerated between (datetime('2026-05-05T19:40:00Z') .. datetime('2026-05-05T20:10:00Z')) | where Log_s contains '[Validator]' or Log_s contains '[LayerS]' | project TimeGenerated, Log_s | order by TimeGenerated asc" \
  -o tsv

# Tool-use rate per session
az monitor log-analytics query \
  --workspace 9555deab-8f9a-4403-98ee-957284f03cc5 \
  --analytics-query "ContainerAppConsoleLogs_CL | where TimeGenerated > ago(24h) | where Log_s contains '[QuestionTool] final' | project TimeGenerated, Log_s" \
  -o tsv
```

Subscription must be `Pixel Design Labs LLC` (`az account set --subscription "Pixel Design Labs LLC"`).
