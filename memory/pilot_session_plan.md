# Pilot session — fix plan

**Status:** revised 2026-05-07 — Edward's full responses applied, math-eval transcripts analysed
**Source:** `memory/pl.md` + Edward's edits + 5 student chat transcripts
**Owner:** Edward

> Edward's notes preserved. Decisions in §0. Math-eval analysis in §1.5.

---

## 0. Decisions taken

### Big architectural shift: drop the EO system

The lesson's `teaching_objective` becomes the single anchor. Steps, questions, exit tickets, and remediation all hang off the teaching objective directly — no per-step / per-question EOs, no canonical EO list per lesson. Most pilot bugs trace back to EO inconsistency; removing the abstraction removes the patches.

**Path:** UI-removal + runtime-ignore now. **No schema migration in this PR** — Edward: *"I am worried about doing too much and breaking things."* Columns stay, code stops reading them. Data drop is post-pilot.

### Other locked decisions (from Edward's §6 answers + body edits)

| Topic | Decision |
|---|---|
| **Existing lessons** | **Regenerate all** with the simplified generator. **Preserve `StudentCompetencyRecord` rows** — students must not lose progress. |
| **Review trigger** | **Auto-trigger** on exit-ticket fail. Explicit button on pass-but-clicked-Review. |
| **Retry cap per wrong question** | **10 retries**. Tutor MUST NOT give the answer directly — hints and reteaching only, until the student gets it on their own. After 10, give more hints and help asnwer the question with student
| **Walkthrough order** | Answer order (the order the student saw them). |
| **Figure prioritization** | **Always pick figure-bearing when available** (strict, not soft). |
| **Encouragement** | **Prompt-only, no injection.** Disabled praise filter stays disabled. |
| **Whiteboard scope** | BOTH paths: (a) photo upload via LLM vision, AND (b) in-platform draw/write canvas. Both are pilot scope. |
| **Item 11 cut-off** | Ignore. |
| **Item 2 word-problems mis-tag** | Defer — content task. |
| **Server not available** | Investigate root cause first (autoscaling vs gunicorn timeout) before fixing. |

---

## 1. Items from pl.md (with Edward's notes)

| # | Tester complaint | Edward's note | Resolution |
|---|---|---|---|
| 1 | Feedback button covers Enter on phone | "put it in the navbar" | → §2 B.1 |
| 2 | Word problems mis-tagged | "ignore for now" | **deferred** |
| 3 | "Server not available" → history lost | "What caused it? Dynamic scaling?" | → §2 A.3 |
| 4 | Diagrams are needed | "use figures for explanation AND practice. Prioritize practice from steps with figures." | → §2 A.5 |
| 5 | More encouragement | "make tutor more encouraging" | → §2 B.2 (prompt nudge) |
| 6 | Whiteboard mode | "use LLM vision" + "student should also draw on the platform" | → §2 B.3 (both paths) |
| 7 | Review button → home | "link to remediation" | → §2 A.4 |
| 8 | Review = replay, not re-quiz | Tutor announces correct/wrong, walks through wrong questions one after another | → §2 A.4 |
| 9 | Wrong on exit → root + retry | "remediation focuses on wrong QUESTIONS" | → §2 A.4 |
| 10 | Question variety — **positive** | (no edit) | Don't break it. |
| 11 | "Questions cover other …" cut off | "ignore" | dropped |

---

## 1.5 Math evaluation reliability — NEW critical issue

Edward provided 5 chat transcripts as examples for "mistakes made with evaluation (math)". Across all five, the same broken pattern surfaces — `is_correct` verdicts are unreliable in both directions:

**Correct answers marked incorrect:**
- "180°" → "Two angles on a straight line are 60° and 120°. What is their sum?" → marked ✗ (Chat 1)
- "90°" → "Two straight lines intersect. One angle is 90°. Vertically opposite?" → marked ✗ (Chat 1)
- "the other angle will 180 - 110 = 70 degrees" → marked ✗ (Chat 3)
- "180-50 = 130" → marked ✗ (Chat 3)
- "180-42=138" → marked ✗ (Chat 3)
- "subtract 65 from 180" (correct reasoning) → marked ✗ (Chat 5)
- "yes" → "Can you verify 120° + 60° = 180°?" → marked ✗ (Chat 1) — yes IS the answer

**Wrong / nonsensical answers marked correct:**
- "help" → marked ✓ (Chat 5)
- Student's wrong arithmetic ("42 + 132 is equal to 180" — actually 174) → marked ✓ (Chat 4)

**Self-contradicting within the same response:**
- Tutor says "✓ correct" then immediately "Not quite!" in the next sentence (Chat 1, multiple)

**What's actually causing it:**
- The combined_judge `answer_correct` field is set by Sonnet at CHECK 4. The prompt says null when the answer doesn't map to a clear verdict — but Sonnet returns true/false on conceptual answers ("yes", "help") where it should return null.
- Engage / teach steps don't have `expected_answer` populated. The judge has nothing to compare against and falls back to vibes.
- Authoring violations cascade: tutor authors a follow-up question, validator regenerates, student answers the OLD question, judge sees mismatched context and judges wrong.

**This needs a dedicated Tier A item** — see §2 A.0 below.

---

## 2. Plan

Three tiers. Tier A is the simplification + critical fixes. Tier B holds smaller polish. Tier C is post-pilot.

### Tier A — Critical (~3 days)

#### A.0 Fix evaluation reliability (NEW — from §1.5)

The verdict layer is the foundation. If `is_correct` is unreliable, every downstream behaviour (advancement, remediation, scoring) is also unreliable.

- **No verdict on steps without `expected_answer`.** Engage/teach/summary steps that have no `expected_answer` skip the answer_correct check entirely — return `null`, don't ask the judge.
- **Deterministic-first on practice/quiz.** When `expected_answer` exists: run `_deterministic_math_check` first. If it returns a verdict, use it and SKIP the LLM judge for `answer_correct`. Only fall to the LLM when the deterministic check returns None (e.g., short_answer with no numeric expected).
- **Override null on conceptual non-answers.** When the student's last input is in {"yes", "no", "ok", "help", "i don't know"}, force `answer_correct=null` regardless of what the judge says. These aren't math attempts.
- **Don't grade against a stale step.** When the validator regenerates, the response was for the previous turn. Tag the eval as "regenerated" and don't use the verdict to advance state on that turn.
- **Add `expected_answer_present` and `verdict_source`** to `[TurnSummary]` JSON so we can confirm post-deploy that the engine isn't grading turns it shouldn't.

#### A.1 Stop reading EOs at runtime

- Bank picker (`apps/tutoring/question_bank.py`): drop EO/tag match ladder. Keep `shown_question_ids` dedup. Picker becomes "N unshown questions, figure-bearing first (see A.5)".
- Drop `pick_published_for_concept_tag`.
- Combined judge: drop EO from `step_context`.
- Competency tracker: drop per-EO tracking. Mastery becomes binary at exit-ticket level. **Preserve `StudentCompetencyRecord` rows** — Edward: students must not lose progress.
- Dashboard: hide EO badges on step cards + bank questions. Hide the per-lesson EO list. Keep teaching objective.

#### A.2 Stop populating EOs in content generation

- Drop `_expand_to_subskills` (the "split teaching objective into 8 EOs" call).
- Drop `_normalize_enabling_objective` + `_llm_snap_eo` calls in step + question persistence.
- Stop writing `LessonStep.enabling_objective` / `ExitTicketQuestion.enabling_objective`.
- Drop the per-EO coverage warning in `_validate_against_profile`.
- Drop `figure_required_step_types` per-EO logic — figures required by lesson concept, not by EO list.
- New gen prompt: *"Generate N steps and M bank questions, all targeting the lesson's `teaching_objective`: '{teaching_objective}'."* — single anchor.
- **Regenerate all existing lessons** using the new simplified generator (content-team operation). `StudentCompetencyRecord` rows must survive — they're keyed by lesson, not by step/question, so regeneration shouldn't affect them, but verify.
- Drop `backfill_step_eos` management command (legacy lessons keep their EO data as ignored noise).

#### A.3 "Server not available" — investigate first (item 3)

Diagnose before fixing:

1. **Azure Container Apps autoscaling** — `min=1 / max=1` per CLAUDE.md. Run:
   ```
   az containerapp show --name aitutor-pixel-app --resource-group aitutor-pixel-rg \
     --query "properties.template.scale"
   ```
2. **Gunicorn 120s worker timeout** on slow LLM turns. Check Log Analytics for `WORKER TIMEOUT` and the slowest 10 turn latencies.

Cheap insurance regardless of cause: when the chat URL loads with an existing session_id, reload `SessionTurn` rows from DB so refresh recovers the conversation. Verify `chat_tutor_interface` and `renderHistory(history)` flow.

#### A.4 Review = Remediation = walk through wrong questions (items 7 / 8 / 9)

When student finishes the exit ticket:

- **Pass:** completion modal unchanged.
- **Fail (auto-trigger) or "Review" clicked (explicit):**
  - Stay on the **same chat session**.
  - Engine flips into review state.
  - Tutor's first message: *"You got 7/10. Let's go over the 3 you missed."*
  - Walk through each wrong question, **one at a time**, in answer order. For each:
    1. Re-pose the **exact** question the student got wrong (must match the actual wrong question, not a similar one) via `pose_question` tool.
    2. Wait for answer.
    3. Correct: brief affirmation → next wrong question.
    4. Wrong again: 1–2 sentence explanation, one targeted hint, ask retry. **Cap at 6 retries per question.**
    5. Tutor MUST NOT give the answer directly. Hints and reteaching only, until the student gets it on their own.
    6. After 6, move on without revealing the answer (the question is logged as "needs review later" but doesn't block progression).
  - After all wrong questions revisited, re-offer the exit ticket with a fresh question sample.
  - "Restart Lesson" stays as a separate path on completion modal.

**Key data:** persist `failed_question_ids` on `engine_state` after grading. Walkthrough pops them in order.

#### A.5 Figure prioritization in bank picker (item 4) — STRICT

Edward: always pick a figure-bearing question when one is available.

- After `shown_question_ids` filtering, partition the candidate pool: figure-bearing vs not.
- If figure-bearing pool is non-empty → pick from it. Period.
- Only fall to non-figure questions when no figure-bearing question remains.
- Expand the in-chat tutoring pool to include practice/quiz questions from **other steps** of the same lesson that have figures attached — not just the exit-ticket bank slice.
- `figure-bearing` = `answer_data.figure_url` set OR parent step has `step.media['images']`.

---

### Tier B — Smaller fixes (ship alongside Tier A)

#### B.1 Move feedback button to navbar (item 1)
- Today: floating widget at bottom-right, overlaps Enter on phones.
- New: button in the chat header. Tap → opens the same modal.

#### B.2 Tutor encouragement nudge (item 5) — PROMPT ONLY
- Edward: prompt > injection. Injection causes inconsistent / conflicting tutor messages.
- Add to socratic_rules: *"Be warm and affirming on correct answers — short and specific. 'Right!', 'Nice work', 'Exactly' — before transitioning. Don't be flat."*
- Avoid the previously-banned stock-phrase patterns.
- Don't reintroduce the post-process praise filter.

#### B.3 Whiteboard — both paths (item 6)

**Path 1 — photo upload (LLM vision):**
- Reuse the existing image-upload UI in the chat input.
- When student uploads, send the image as a vision content block alongside their text.
- System prompt addendum: *"If the student uploads a photo of working, read each step from the image. Confirm what's right, flag the first incorrect step specifically, advance."*

**Path 2 — in-platform canvas (NEW):**
- Add a draw/write button next to the chat input.
- Touch canvas (HTML5 `<canvas>`, simple pen tool, eraser, clear).
- On submit, the canvas exports as PNG and goes through the same vision path as the photo upload.
- For pilot: no handwriting OCR layer — let the LLM read the strokes directly. (OCR + structured math input is v2.)

---

### Tier C — Post-pilot

- **Hard EO migration.** Drop `LessonStep.enabling_objective`, `ExitTicketQuestion.enabling_objective`, `Lesson.enabling_objectives` columns. Delete `_normalize_enabling_objective`, `_llm_snap_eo`, `backfill_step_eos`, EO-coverage validators. Drop `StudentCompetencyRecord.per_eo_status` if it exists.
- Touch-canvas with proper handwriting OCR (vs. raw vision read).
- Bank content audit + regen for off-topic lessons (item 2).
- v2 mastery model if lesson-level pass/fail proves too coarse.

---

## 3. Sequencing

```
A.3 first — diagnose "server not available" (cheap, may need no code change)
   ↓
A.0 — fix evaluation reliability (foundation for everything else)
   ↓
A.1 + A.2 — drop EOs at runtime + content gen + UI hiding (single PR)
   ↓
A.4 — Review = Remediation walkthrough (same PR or fast follow)
   ↓
A.5 — figure prioritization (small, same PR)
   ↓
Live test: complete a lesson, fail exit ticket, verify Review walks
            through wrong questions; verify is_correct verdicts match
            the actual answers in [TurnSummary] log.
   ↓
   Stop condition: §0 behaviour matches → ship.
   ↓
Tier B in same PR or fast follow.
Regenerate all lessons (content-team operation, parallel).
Tier C post-pilot.
```

---

## 4. Explicitly NOT in this plan

- **Item 2 — word-problems mis-tag.** Deferred per Edward.
- **Hard EO column drop.** Post-pilot. Soft-deprecate now (UI removed, runtime ignores, columns kept).
- **Item 11 cut-off.** Ignored per Edward.

---

## 5. Verification

After Tier A ships:

- [ ] **Eval reliability (A.0):**
  - [ ] Engage/teach steps no longer mark "yes"/"help" as right/wrong — verdicts come back null.
  - [ ] Practice steps with `expected_answer` use deterministic check first; LLM judge only fires when deterministic is inconclusive.
  - [ ] `[TurnSummary]` shows `verdict_source` = `deterministic` for math practice; `null` for engage; `llm_judge` only when both conditions don't apply.
- [ ] **EO removal (A.1 + A.2):**
  - [ ] Generate a new lesson → no EOs populated; tutoring still works.
  - [ ] Open an existing lesson → runtime ignores EOs; bank picks still surface questions.
  - [ ] Dashboard hides EO badges; teaching objective still visible.
  - [ ] `StudentCompetencyRecord` rows from before this deploy are unchanged.
- [ ] **Review = remediation (A.4):**
  - [ ] Complete a lesson, fail the exit ticket → engine **auto-routes** to review/remediation in the same chat (no redirect).
  - [ ] Tutor's opening review message: "You got X right, Y wrong — let's go over the ones you missed."
  - [ ] Each wrong question is re-posed exactly. Cap of 6 retries; tutor never gives the direct answer.
  - [ ] After all wrong questions, re-offers exit ticket with fresh sample.
- [ ] **Figure prioritization (A.5):**
  - [ ] Bank picker prefers figure-bearing questions when present (strict).
- [ ] **Resilience (A.3):**
  - [ ] Refresh mid-lesson on a network blip → prior turns visible.
- [ ] **Regression (item 10):**
  - [ ] `shown_question_ids` dedup still works across attempts.

---

## 6. Open questions remaining

> All §6 questions from the prior version are now answered (in §0). Listed here for cross-reference; nothing blocking.

1. ~~Item 1 math evaluation transcript~~ — provided. See §1.5.
2. ~~Item 11 cut-off~~ — ignored.
3. ~~Existing lessons keep or regenerate~~ — regenerate, preserve competencies.
4. ~~Review trigger auto vs button~~ — auto on fail, explicit on pass-Review.
5. ~~Retry cap~~ — 6, hints only.
6. ~~Walkthrough order~~ — answer order.
7. ~~Figure prioritization~~ — strict.
8. ~~Encouragement floor~~ — prompt only.
9. ~~Migration policy~~ — soft-deprecate (UI removal, no schema change).

**One contradiction to flag for Edward:** in the body of A.4 (line 121) the retry cap was "3"; in §6 Q5 you said "6". I've gone with **6** (the §6 answer is later/more specific). Confirm if that's right.
