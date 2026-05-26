# GEO-S5 Tutor Evaluation — 2026-05-26

**Persona**: S5 (Cycle 5) advanced student in Seychelles, studying *Map Scale and Map
Types*. Provides mostly correct answers.
**Lesson evaluated**: L1425 — *Map Scale and Map Types* (S3 Geography; the requested
lesson is present in this dataset).
**Engine**: v2 (`engine_version='v2'`, `NEW_TUTOR=on` default).
**Session ID**: 76.

> **Note**: see the MATHS-S1 report (`MATHS-S1-evaluation-2026-05-26.md`) for the
> upstream architectural fix to `pose_question` tool wiring that landed during the
> session pair. Both reports share the same engine build.

---

## 1. Transcript

> Meta block on each tutor turn: `[move=… verdict=… fallback=…]`. `fallback=True`
> = verdict-keyed safe template fired after two conformance rejections.

| Turn | Role | Content | Meta |
|------|------|---------|------|
| 0 | tutor | Welcome! Let's get started. 😊 **In S3 maths**, we cover a wide range of topics — algebra, geometry, trigonometry, statistics, and more. What topic would you like to work on today? | `move=explain  verdict=None  fallback=False` ⚠ Subject hallucinated (this is a Geography lesson) |
| 1 | student | I am studying Map Scale and Map Types in geography | — |
| 1 | tutor | *(safe template)* Let's pick this back up together. Let's try a question on this together. | `move=pose_question  verdict=None  fallback=True` |
| 2 | student | I am ready for a question about map scales | — |
| 2 | tutor | Read the statement carefully and decide if it's true or false. **A large-scale map (such as 1:25,000) shows a smaller geographic area in greater detail than a small-scale map (such as 1:5,000,000).** | `move=pose_question  verdict=None  fallback=False` ✅ tool-posed bank Q |
| 3 | student | True - large-scale maps show smaller areas in more detail | — |
| 3 | tutor | Good effort putting your reasoning in writing — that's a great habit! The grader couldn't verify your answer from the transcript alone, so let's make sure we're on solid ground. Have a go at this official question to confirm your understanding. **You are a government planner in the Seychelles Ministry of Environment tasked with identifying all coral reef protection zones around the entire Seychelles archipelago. Which map would be most appropriate for this task?** | `move=scaffold_hint  verdict=unverified  fallback=False` ✅ **best turn of either session** — surfaces uncertainty, credits effort (growth mindset), poses a follow-up tool-Q |
| 4 | student | I would pick option B because small-scale maps cover larger areas | — |
| 4 | tutor | *(safe template)* I want to check that with you before I'm sure either way. Let's try a question on this together. | `move=scaffold_hint  verdict=unverified  fallback=True` |
| 5 | student | Yes - the scale ratio of 1:25000 means each unit on the map equals 25000 of the same units on the ground | — |
| 5 | tutor | *(safe template)* | `move=scaffold_hint  verdict=unverified  fallback=True` |
| 6 | student | Topographic maps show physical features like elevation, while political maps show borders and administrative divisions | — |
| 6 | tutor | *(safe template)* | `move=explain  verdict=unverified  fallback=True` |
| 7 | student | A small-scale map would be better for navigation across countries; a large-scale map for finding a specific street | — |
| 7 | tutor | *(safe template)* | `move=explain  verdict=unverified  fallback=True` |

**Runtime state at end**: `posed_question_ledger` contains both bank questions
that were tool-posed (the True/False scale-detail question + the Seychelles
coral-reef MCQ); `attempts_on_open_question` did not increment because the grader
never returned anything except `unverified`. The student gave essentially
correct geography content on every turn but received no positive confirmation.

---

## 2. Science-of-learning evaluation

| # | Principle | Score | Evidence |
|---|-----------|-------|----------|
| 1 | **Active Learning** (Ch.10) | ⚠ partial | The student *was* doing on turns 2 (TF) and 3 (MCQ + reasoning) — two real attempts. After turn 4 the conformance loop kicked in and the student got safe-template re-prompts. Effective doing-ratio ≈ 2/7 ≈ 28% (target ≥60%). |
| 2 | **Direct Instruction** (Ch.11) | ❌ fail | The opening turn never introduces "scale", "large-scale vs small-scale", or "map types". It jumps straight to "what topic do you want?" then drops into a True/False discriminator question. The student would have to *already know* the scale convention to answer turn 2 correctly. |
| 3 | **Deliberate Practice** (Ch.12) | ⚠ partial | The lesson's first two questions hit a genuine S5-level discrimination (large vs small scale; choosing map type for a real planning task). That's edge-of-ability for S3 / S5 students. ✅ on item selection; ❌ on calibration (no diagnostic before posing). |
| 4 | **Mastery Learning** (Ch.13) | ⚠ partial | The bar is held — but with the grader returning `unverified` on every correct answer, the student would never advance through the lesson regardless of competency. Pedagogically correct in principle; broken in practice. |
| 5 | **Minimising Cognitive Load** (Ch.14) | ❌ fail | No worked example. No subgoal labels. No dual coding (the lesson has *no* media in the catalog — the catalog block is empty). A "Map Scale" lesson without a single rendered map figure is a major dual-coding gap (Ch.14 says "verbal + visual throughout"). |
| 6 | **Automaticity** (Ch.15) | n/a | Not measurable here. |
| 7 | **Layering** (Ch.16) | ⚠ partial | The Seychelles coral-reef question (turn 3) authentically composes "scale" + "domain context" — that's good layering. But the prerequisite of "what does 1:25,000 mean" is never gated. |
| 8 | **Non-Interference** (Ch.17) | n/a | Single lesson. |
| 9 | **Spaced Repetition** (Ch.18) | n/a | Single session. |
| 10 | **Interleaving** (Ch.19) | n/a | Single lesson. |
| 11 | **Testing Effect / Retrieval** (Ch.20) | ⚠ partial | Retrieval-first is the right shape on turns 2–3. But again, retrieval without working feedback is just guessing. |
| 12 | **Targeted Remediation** (Ch.21) | ❌ fail | The student gave a *correct* explanation on turns 3, 4, 5, 6, 7 — every one was marked `unverified` and triggered the safe-template loop. No diagnosis is ever delivered, even when there's nothing to diagnose. The bar is held, but no scaffolding is delivered. |
| 13 | **Gamification** (Ch.22) | n/a | Out of scope. |

**Net**: like the math session, the v2 engine's pedagogical *intent* (move table,
verdict-aware moves, retrieval-first) is correct, but the runtime fails at the
grader → conformance handshake. The advanced student's reward for correct
reasoning is the same generic safe template the struggling student saw. This
violates the most basic feedback signal Ch.20 (Testing Effect) depends on.

---

## 3. What's working

- **Turn 3 is a great turn.** `scaffold_hint` on an `unverified` verdict produced:
  *"Good effort putting your reasoning in writing — that's a great habit! The
  grader couldn't verify your answer from the transcript alone, so let's make
  sure we're on solid ground. Have a go at this official question to confirm
  your understanding."* — followed by a *new* bank question via the
  `pose_question` tool. This is the v2 engine at its best:
  - growth-mindset framing (effort praise)
  - surfaces the grader's uncertainty
  - hands the floor back with a concrete task (not a generic re-prompt)
  - uses the tool channel to post the next Q
  This proves the architecture works when the conformance gates don't fight the move.

- **Tool-posed questions render exactly the bank stem** — no LLM rewording, no
  added prose, no answer-key leak. Two bank questions ended up in
  `posed_question_ledger` cleanly.

- **Move selection adapts to verdict** — `scaffold_hint × 3` after the first
  unverified, then `explain × 2` after `unverified_run_length ≥ 3`. Right shape.

## 4. What's not working

### 4.1 Grader returns `unverified` on every student answer

Same root cause as the math session (see `MATHS-S1-evaluation-2026-05-26.md` §4.1):
the grounded grader path for non-math (geography) is not finding answer-key
matches for free-text student replies. The student's "True - large-scale maps
show smaller areas in more detail" is a correct restatement of the canonical
"True", but the grader doesn't accept it. Likewise the MCQ answer "I would pick
option B" — the canonical is "B" — should be a hard match, but came back
unverified.

Hypothesis: the v2 grader's non-math path (`_grade_non_math`) is doing an LLM
grounded-grade rather than a string match on MCQ canonicals. Without good KB
chunks the grounded LLM defaults to "unverified".

### 4.2 Same conformance × unverified fallback loop

Identical failure mode to the math session (see MATHS-S1 §4.2). Every
`unverified` verdict + non-pose move (scaffold_hint, explain) cannot satisfy
the unverified ruleset's requirements (must surface uncertainty AND not
refute AND hand the floor back). The LLM produces something pedagogically
correct, conformance rejects, safe template fires.

### 4.3 No media in the catalog for a "Map Scale" lesson

For a lesson called "Map Scale and Map Types", the media catalog passed to
`StudentTutor._render_media_catalog_block` was empty — `(none available)`. No
figures, no example maps, no scale diagrams. This is a curriculum-content gap
but it directly defeats Ch.14's "dual coding (verbal + visual) throughout"
imperative for this specific topic. A map-scale lesson without a single map
shown to the student is the highest-leverage content gap I can identify.

### 4.4 Subject hallucination at session start

The opening message claimed the lesson is "S3 maths" when it's S3 Geography.
Same root cause as the math session (§4.3): `TutoringContext.current_objective`
is hard-coded empty in `ContextManager.assemble_context()`. The LLM has no
lesson signal and falls back to the dominant prior in its training data.

### 4.5 First-attempt rejection on a perfectly fine "I'm ready for a question"

Turn 1 ("I am studying Map Scale and Map Types in geography") triggered a
conformance rejection on first attempt — the LLM either acknowledged the topic
in prose (which got flagged) or made a factual claim about maps that the
adjudicator couldn't ground. Even an idle "ready signal" pays the 2× retry tax.

---

## 5. Recommended changes to `apps/tutoring/v2/services/move_prompts.py`

These build on the recommendations in the MATHS-S1 report (§6) — the prompts are
shared. The geography session reinforces and adds:

1. **`SCAFFOLD_HINT`** — turn 3 ("the grader couldn't verify your answer…") is
   the *desired* shape. Lift that pattern into the prompt explicitly:

   ```
   - If the grader's verdict is ``unverified`` (no graded answer key match),
     open with effort praise (e.g. "Good effort putting your reasoning in
     writing — that's a great habit"), then surface the grader's uncertainty
     plainly ("The grader couldn't verify that from the transcript alone"),
     then hand the floor back with ONE follow-up question via the
     ``pose_question`` tool. Do NOT type the question stem in prose.
   ```

   This was apparently in the model's head on turn 3 but not in the prompt
   body — codify it so it fires reliably.

2. **`EXPLAIN`** — on `unverified` verdict the move prompt currently lets the
   LLM author concept restatement, which conformance rejects when adjudication
   is sparse. Constrain it:

   ```
   - If the grader's verdict is ``unverified`` AND the lesson is non-math:
     DO NOT restate the concept. Instead, restate the QUESTION in simpler
     words ("Let me make sure we're answering the same thing — the question
     is asking…") and pose a 1-step diagnostic question via the tool.
   ```

3. **Add a `MEDIA_REFERENCE` directive to every pose-capable move** (or to the
   shared preamble) for lessons where the media catalog is empty:

   ```
   - If the media catalog is empty for a topic where visuals would help
     (geography maps, geometry diagrams, biology cells, etc.): mention the
     absence to the student so they can ask their teacher ("Your teacher has
     a paper map for this — I don't have a digital figure yet"). Do not
     improvise a description of a figure that doesn't exist; that's a
     hallucinated visual.
   ```

   This is a soft mitigation for the §4.3 content gap; the real fix is
   curriculum content authoring.

4. **`render_shared_preamble`** — same as the math report (§6.4): wire
   `current_objective` + `subject` + open-question stem into the preamble.

5. **Tighten the slot-tool description for the new builder** in
   `apps/tutoring/v2/tools/pose_question.py::build_anthropic_pose_question_tool` —
   when the menu contains MCQ items (turn 2 of this session: B), include the
   option letters in the menu so the LLM can preview the choices and pick a
   pedagogically-fitting slot. Right now it only sees the stem prefix.

---

## 6. Per-principle prompt-level patches (concrete)

| Principle | Current prompt gap | Concrete patch |
|-----------|---------------------|----------------|
| Active Learning (Ch.10) | Move table targets ≥60% "doing" but the conformance fallback loop short-circuits this. | Reduce fallback rate by adding the `unverified`-aware directives to `SCAFFOLD_HINT` + `EXPLAIN` above. |
| Direct Instruction (Ch.11) | No move prompt explicitly *teaches* before asking. `EXPLAIN` is selected only on `objective_just_opened=True` OR `unverified_run_length≥3`. | Have `select_move` prefer `EXPLAIN` on turn 1 when the lesson type is "discriminator" / "MCQ-heavy" — the student needs the rule before they discriminate. |
| Cognitive Load (Ch.14) | `WORKED_EXAMPLE` exists but never fired in either session. Dual-coding directive in the media catalog block is the only visual hook. | (a) Run `WORKED_EXAMPLE` on `objective_just_opened` for any lesson with a populated `worked_example` step in the bank. (b) Add a "list the media that should exist for this topic" expectation in the preamble so empty catalogs are flagged. |
| Testing Effect (Ch.20) | Retrieval-first is shipped; feedback loop broken by grader. | Prompt-side: nothing more to do. The fix is in `student_grader.py` (out of scope for `move_prompts.py`). |
| Targeted Remediation (Ch.21) | `NAME_MISCONCEPTION` fires on `attempts ≥ 3` — but `attempts_on_open_question` never increments here because the grader returns `unverified` not `wrong`. | Make `NAME_MISCONCEPTION` also fire on `unverified_run_length ≥ 3 AND open_question is set` — three "I can't tell" turns is itself a misconception signal. |

---

## 7. Open issues (shared with MATHS-S1 report)

- **Grader UNVERIFIED on correct restatement** of "True", "B", and prose
  reasoning — needs investigation of `_grade_non_math`'s grounded-grader path.
- **`current_objective` never wired** — same fix proposed in MATHS-S1 §7.
- **Empty media catalog for a Geography Map lesson** — curriculum content gap.
- **Tutor-claim adjudicator over-conservative on sparse KB** — same as math.
- **First-attempt conformance rejection rate is ~100%** — every turn pays the 2×
  retry tax. Worth a separate audit ticket.

---

## 8. Summary

The v2 engine's **architecture is sound**: the move table picks pedagogically
right moves; the per-move prompts cite the right learning-science principles;
the tool-call path (after the fix landed in this session) correctly poses bank
questions and commits Phase B state. Turn 3 of the geography session is a
genuinely good tutor turn by science-of-learning standards.

The engine's **runtime is broken at the grader → conformance handshake**. The
grader returns `unverified` on essentially every student answer (correct,
wrong, or ambiguous), which puts every subsequent turn into a verdict-keyed
ruleset that no scaffold-style response can satisfy, which triggers the safe
template, which makes the session feel like a broken bot. From the student's
perspective:

> "I gave a correct answer, then a more detailed correct answer, then a third
> way of saying the same correct thing — and the tutor said the same generic
> 'let's check that together' message five times in a row."

That experience violates Ch.10 (Active Learning), Ch.20 (Testing Effect), and
Ch.21 (Targeted Remediation) simultaneously. The fix path is upstream of
`move_prompts.py` — `student_grader.py` needs the non-math grader to give
accurate `correct` / `wrong` verdicts on simple MCQ + short-answer responses,
and the conformance unverified ruleset needs an exemption (or a different rule
shape) for non-pose moves.
