# GEO-S5 Tutor Evaluation — 2026-05-28 (run 10)

**Persona**: S5 (Cycle 5) **advanced** geography student, Seychelles. Per run-instructions: rich, mostly correct answers naming mechanism + cause. The struggling register from prior 2026-05-28 GEO runs is intentionally flipped here.
**Engine**: v2 (`refactor/conversational-tutor-redesign`, post-prune; HEAD `5d5b209`).
**Sessions covered**:
  - **Session 117** — lesson `1441` *Climate and Weathering Influence* (Belonie Geography S3, Weathering & Mass Movement) — 3 student turns including a `too_easy` UI signal injection mid-session.
  - **Session 118** — lesson `1442` *Time Factor in Weathering* — 5 turns (4 student turns including `too_easy` injection), supposed-exit-ticket promise not delivered.
  - **Session 116** — lesson `1453` *Condensation and Precipitation* — 3 turns, `too_easy` injection produced trivial T/F.
**Student account**: `student1` / Anse Boileau / grade S3 (only S3 geography is seeded; played at S5 register — domain vocabulary, mechanism + cause, named processes).
**Evaluator**: Roy Manzi (claude-opus-4-7).

**Note on session length**: the workflow requires *> 5 turns*. The v2 engine on these 5-step geography lessons consistently closes the topic after **1–2 correct answers** (`recent_verdicts: ['correct']` or `['correct', 'correct']`). To accumulate a multi-turn body of evidence, three lessons were used. This itself is a finding — see §4 *Premature close*.

---

## 1. P1 unacceptable errors

| # | Session.Turn | Category | Evidence |
|---|--------------|----------|----------|
| **P1-1** | 117 / turn 1837 | **`close_topic` body ends with a fresh probing question — student's answer is then abandoned.** | The close_topic move emitted: *"You matched the dual-driver reasoning — high temperature *and* high precipitation working together, not just one factor in isolation. **Now flip it: which of those four regions would show the *most* dominant physical weathering, and what's the key climate variable driving it?**"* — i.e. the close response **poses a brand-new bank-style question** ("which of those four regions … and what's the key climate variable driving it?"). The student then responded ("A — the high altitude mountain at -5°C…"), and the API returned `"This lesson is already complete! Great work!"` — the student's substantive answer was thrown away. This is the "incomplete question" P1 in the worst direction: a question that *appears* answerable, gets an answer, then the system silently ignores it. The `close_topic` move's "What NOT to do" at `move_prompts.py:918-921` explicitly bans this: *"Add another assessment question on this objective. Close means close."* |
| **P1-2** | 118 / turn 1840 | **Posed question contains a duplicated stem fragment — options A/B/C/D printed twice.** | The MCQ on Boulder X/Y/Z weathering depth rendered with the full options block printed **twice in the same message**: stem → A/B/C/D → blank line → A/B/C/D again. Not a "missing crucial info" P1, but it's an authoring defect that surfaces to the student. Most likely a tool-call + prose-restatement collision (the pose tool emitted the options and the prose body restated them). Borderline P1 — the question is *answerable*, just visually broken. |
| **P1-3** | 118 / turn 1846 → response post-T1847 | **Exit ticket promised but never delivered.** | `close_topic` emitted *"You're ready for the exit ticket — I'll set it up."* with `phase: completed`, `selected_move: close_topic`. The next student turn ("Ready.") returned: `{"phase": "completed", "show_exit_ticket": null, "message": "This lesson is already complete! Great work!"}`. The API's `show_exit_ticket` was **`null`, not `true`** — the close_topic body promised an exit-ticket modal that the engine had no way to dispatch. The `close_topic` move prompt at `move_prompts.py:922-925` warns against exactly this: *"Promise the exit ticket modal when you can't see whether one exists. … use a softer transition ('we'll wrap here for now') rather than repeating the promise."* The rule is authored; the engine ignored it. |

**Bottom line**: **1 hard P1 hit (T1837, close_topic abandons a student answer)** + 1 visual / structural defect (T1840 duplicated options) + 1 broken promise (T1846 exit-ticket no-show). The grader was correct on every verdict that landed — there are **no wrong-as-correct or correct-as-wrong P1s** in this run. The failure cluster is on the **close path**: when the engine decides to close, the LLM-authored body either (a) keeps posing questions, (b) duplicates content, or (c) promises a modal the runtime cannot deliver.

P1 categories from the skill rubric:
- "Tutor says student's correct answer is wrong" — **0 hits**.
- "Tutor says student's wrong answer is correct" — **0 hits**.
- "Posing incomplete questions (missing crucial info needed to answer)" — **1 hit (P1-1, close_topic question that is abandoned by the engine state machine)**. The student CANNOT actually answer it because the next turn doesn't route — the question has no completion path, which is functionally the same as missing crucial info to answer.

---

## 2. Full transcripts

### Session 117 — lesson 1441 (Climate and Weathering Influence)

| Turn id | Role | Content (truncated) | Move | Verdict |
|---------|------|---------------------|------|---------|
| 1831 | tutor | "Welcome to today's lesson on **Climate and Weathering Influence**! … which type of weathering do you think would be most active there, and why?" | `explain` | n/a |
| 1832 | student | "Chemical weathering dominates — high temperatures accelerate Arrhenius-rate hydrolysis and oxidation, monsoon rainfall supplies water and dissolved CO2 for carbonation, and seasonal wetting/drying cycles keep reactive surfaces exposed. Physical weathering is present (thermal expansion contributes) but chemical reactions remove far more mass." | — | — |
| 1833 | tutor | "You nailed the carbonation + hydrolysis chain — and the point about wetting/drying cycles keeping reactive surfaces exposed is exactly the kind of nuance that separates a good answer from a great one. … T/F: Granite in Mauritius would experience dominant chemical weathering, just like in Seychelles." | `worked_example` | n/a | — | Confirm-extend voice; quotes back substantive terms ("carbonation + hydrolysis chain") matching `move_prompts.py:194-201`. **But** poses a T/F in prose (not via tool) — defensible since the previous turn was rich-correct without a verdict, but borderline. |
| 1834 | student | (injected) "This is too easy — could you make it more challenging?" → `difficulty_level: 1`, `last_system_event: difficulty_change:too_easy` |
| 1835 | tutor | "Fair enough — you've already shown you can handle the mechanism, so let's stress-test it. A student examines rock samples from four different regions. Which region's climate would produce the most dominant chemical weathering? A) High altitude mountain… / B) Tropical rainforest… / C) Desert plateau… / D) Polar region…" | `worked_example` | n/a | Good 4-region discrimination MCQ — actually does raise the rigor (Deliberate Practice Ch.12). |
| 1836 | student | "B — tropical rainforest. Highest temperature accelerates reaction rates and abundant precipitation supplies water for hydrolysis, dissolution and carbonation." | — | — | Correct, rich answer. |
| 1837 | tutor | "You matched the dual-driver reasoning — high temperature *and* high precipitation working together, not just one factor in isolation. **Now flip it: which of those four regions would show the *most* dominant physical weathering, and what's the key climate variable driving it?**" | `close_topic` | correct | **P1-1** — close_topic body asks a fresh probing question; the engine has marked the lesson complete; the student's next answer is abandoned. |

Final state: `move_history: ['explain', 'worked_example', 'worked_example', 'close_topic']`, `recent_verdicts: ['correct']` (only 1 verdict registered across the whole session — the close fired after one unscaffolded correct).

### Session 118 — lesson 1442 (Time Factor in Weathering)

| Turn id | Role | Content (truncated) | Move | Verdict |
|---------|------|---------------------|------|---------|
| 1838 | tutor | "Today we're looking at the **Time Factor in Weathering** … Which do you think would show more weathering damage — a rock face that was only recently uncovered by erosion, or one that has been exposed on the coast for hundreds of years, and why?" | `explain` | n/a |
| 1839 | student | "The one exposed for hundreds of years — longer exposure means more cumulative wetting-drying, freeze-thaw, and chemical reaction time." | — | — |
| 1840 | tutor | "A geologist studying rocks in Seychelles measures the weathering depth on three granite boulders: Boulder X (exposed 20 years) has a 1 cm weathered layer, Boulder Y (60 years) has a 3 cm, Boulder Z (100 years) has a 5 cm. … A) Weathering depth does not depend on exposure time / B) Weathering depth increases proportionally with exposure time / C) … / D) … **\\n\\nA) … B) … C) … D) …**" | `confirm_and_advance` | n/a | **P1-2** — options block printed twice. |
| 1841 | student | "B — proportional to exposure time (20→1, 60→3, 100→5 follows roughly a 1cm per 20 years pattern)." | — | — | Correct + named the pattern. |
| 1842 | tutor | "The Seychelles Government Building … 1950 (75 years ago). A neighbouring fort built in 1800s (200 years ago) … A) different rock type / B) more rain / C) **fort's granite exposed for 125 more years** / D) older = weaker. **A) … B) … C) … D) …**" | `confirm_and_advance` | correct | Same duplicated-options defect. |
| 1843 | student | (injected) "This is too easy — could you make it more challenging?" → `difficulty_level: 1` |
| 1844 | tutor | "A granite quarry face … After 10 years, 15%; after 50 years, 60%; after 100 years, 95%. True or False: Weathering accelerates over time, meaning the rate of surface area damage increases as more time passes." | `confirm_and_advance` | n/a | Good — the post-`too_easy` item DOES raise rigor: invites the student to spot the non-linear / saturation shape from the data. |
| 1845 | student | "True. The data shows non-linear acceleration: 10→50 years adds 45% damage in 40 years, while 50→100 adds 35% in 50 years — though the front end accelerates fast then approaches saturation as the easily-weathered surface gets depleted." | — | — | Correct + nuanced. |
| 1846 | tutor | "You nailed the saturation effect — spotting that the rate slows as easily-weathered surface gets depleted is exactly the kind of nuance that separates a good answer from a great one. You're ready for the exit ticket — I'll set it up." | `close_topic` | correct | Voice good — affirmation cites a SPECIFIC substantive term ("saturation effect", "easily-weathered surface gets depleted") matching `move_prompts.py:876-899` `what_right`-scoped rule. **But the exit ticket promise is empty (P1-3).** |
| 1847 | student | "Ready." | — | — | |
| post-1847 | API | `{"phase": "completed", "show_exit_ticket": null, "message": "This lesson is already complete! Great work!"}` | — | — | **P1-3** — exit ticket promised, never delivered. |

### Session 116 — lesson 1453 (Condensation and Precipitation)

Notable evidence of "`too_easy` raised level but the next question is easier than the prior one":

- T1825 (advanced student): "As air cools below its dew point, water vapour condenses on condensation nuclei to form micro-droplets. Latent heat is released during the phase change…" — strong, rich answer.
- T1826: confirm-extend voice quoting back ("dew point trigger, condensation nuclei, and the latent heat feedback") → poses an MCQ where distractor D is *"The glass is sweating because it is alive"* — comically below register.
- (injected too_easy) → T1828: `worked_example` "Fair point — you've already shown you know the mechanism cold. Let's go somewhere harder. **Condensation is the process where water vapor cools and changes into liquid water. (True or False?)**" — the post-too_easy item is a **trivial restatement of the definition the student gave in advanced form 2 turns earlier**. The voice claims "let's go somewhere harder" while the question is the basic-recall version of the open-ended prompt the student already answered.

This is a **direct contradiction** between voice and action — and the student-visible voice mis-represents what the engine actually does next. Cognitive Load Ch.14 violation (expertise-reversal: re-running a definition the student already showed they own).

---

## 3. Science-of-learning assessment

Mapped to `design/science-principles.md`. Mix of strong execution (the affirmation/extend pattern is excellent) and the close-path defects in §1.

### Working well

| Principle | Evidence |
|---|---|
| **#3 Deliberate Practice (Ch.12) — calibrate to the edge.** | T1835 (post-too_easy, session 117) raised rigor from a T/F to a 4-region discrimination MCQ comparing climates. T1844 (post-too_easy, session 118) raised rigor by inviting the student to spot non-linear / saturation shape from data. **The `too_easy` UI signal actually changed the next question's rigor in 2/3 sessions.** This is a meaningful upgrade from session 116 where post-too_easy went *easier* (P1-adjacent). |
| **#11 Testing Effect (Ch.20) — affirmation that quotes back the substantive term.** | T1833 ("carbonation + hydrolysis chain"), T1836 ("dual-driver reasoning"), T1846 ("saturation effect — spotting that the rate slows as easily-weathered surface gets depleted") all carry the SPECIFIC substantive term the student used. This satisfies the `confirm_and_extend` directive at `move_prompts.py:194-201` exactly. The shared preamble's "make the very first words content-bearing" (no stand-alone praise opener) is well-applied — no "Great!", "Perfect!", "Yes!" filler. |
| **Voice — subject anchoring + locale.** | Mauritius vs Seychelles climate comparison (T1833), Port Victoria Government Building 1950 vs Anse Royale fort 1800s (T1842), Anse Intendance / Beau Vallon (session 116 prior turn). Authentic local anchoring throughout, no system vocabulary leakage. |
| **#2 Direct Instruction (Ch.11) — opening turn frames the concept before retrieval.** | T1831 (climate-and-weathering opener), T1838 (time-factor opener) both lead with the core mechanism + cause (climate controls weathering type AND rate; exposure time accumulates damage). Then close with an open-ended reflective prompt — clean Rule 5 (Direct Instruction opening) shape. |

### Not working

| Principle | Failure |
|---|---|
| **#4 Mastery Learning (Ch.13) — premature close.** | Sessions 117, 118, 116 all closed the topic after **1 or 2 unscaffolded correct answers** on a 5-step lesson. `recent_verdicts: ['correct']` in 117 and 116; `['correct', 'correct']` in 118. The router's I-2 invariant requires `unscaffolded_correct_on_objective >= 1` (i.e. THIS would be the 2nd) — but the threshold is too lax for an advanced student: one rich answer + one MCQ correct on a different sub-objective triggers close. Ch.13 calls for retrieval-with-feedback across the **knowledge frontier** of the lesson; closing on 1-2 items leaves 3+ assessable slots unexercised. Premature close also defeats Layering (Ch.16) — the lesson's later slots compose the early mechanism with new material; closing before they fire means the composition signal is never collected. |
| **#5 Minimise Cognitive Load (Ch.14) — `close_topic` body should NOT extend the question.** | T1837 closed with "Now flip it: which of those four regions would show the *most* dominant physical weathering?" — a fresh probing question inside a close. The student's answer was then abandoned. The move prompt forbids this; the engine ignored it. |
| **`close_topic` — exit-ticket promise without delivery.** | T1846's "I'll set it up" promise produced an API response with `show_exit_ticket: null`. The move's `What NOT to do` at `move_prompts.py:922-925` warns against this exact failure mode. |
| **#3 Deliberate Practice (Ch.12) — session 116 too_easy went *easier*.** | T1828 (post-too_easy) posed a T/F restatement of the definition the student had just given in advanced form. Voice claimed "let's go somewhere harder"; action did the opposite. This is the expertise-reversal effect: re-instruction on material the student owns is pure load, not progress. |
| **Authoring defect — duplicated MCQ options.** | T1840, T1842 (session 118) both rendered options A/B/C/D twice in the same message. Likely a `pose_question` tool emission + LLM prose-restatement collision; the conformance gate that would have caught duplicate-option blocks was deleted in the post-prune. Visually it's a "broken question". |
| **Voice — distractor quality in non-targeted MCQs.** | Session 116 T1826 included distractor D *"The glass is sweating because it is alive"* — comedy distractor, well below S5 register. The bank includes nonsense distractors that pass through unfiltered for a strong student. Discrimination Ch.19 calls for **plausible** distractors; comedy ones don't exercise discrimination. |

---

## 4. Move-prompt review — `apps/tutoring/v2/services/move_prompts.py`

The prompts are well-authored and principle-cited. The failures observed are concentrated on the **close path** and the **post-difficulty-signal calibration**. Recommendations focus there.

### M1 — `CLOSE_TOPIC`: hard-guard against trailing questions

T1837 closed by posing a fresh probing question. The move's "What NOT to do" at `move_prompts.py:918-921` says *"Add another assessment question on this objective. Close means close."* — but as a NOT-do bullet at the bottom of the move body, it under-weights. Hoist the rule as a structural directive at the top of the move body, written positively:

```
STRUCTURAL — every close_topic turn ends with a transition sentence,
never a question. The LAST sentence is one of:
  - "Let's move on to <next objective>." (when more objectives remain)
  - "You're ready for the exit ticket — I'll set it up." (when
    assessable_slots_remaining == 0)
  - "We'll wrap here for now and pick this up next time." (forced
    close, no mastery evidence)
A close that ends with '?' confuses the student — the move announces
an end, then asks for input on what just ended. The student's next
response cannot be routed because the engine has already marked the
lesson complete; their answer is silently lost.
(Science of learning principle: Mastery Learning Ch.13 — the close
signal is the transition out of this objective; mixing it with a
fresh retrieval probe breaks the gating semantics.)
```

This is positive (the LAST sentence is one of …) rather than negative (don't end with a question), which the gemini-prompting-expert directive prefers.

### M2 — `CLOSE_TOPIC`: exit-ticket promise gated on runtime evidence

T1846 promised the exit ticket; the API returned `show_exit_ticket: null`. The move prompt at `move_prompts.py:922-925` warns about this in the "What NOT to do" list ("Promise the exit ticket modal when you can't see whether one exists"). Make it a structural pre-condition at the head of the "How (earned close)" section:

```
EXIT-TICKET PROMISE — only when the runtime signal is present:
- Say "You're ready for the exit ticket — I'll set it up." ONLY when
  the context block from the engine includes
  `assessable_slots_remaining == 0` AND
  `lesson_complete_signal == true`. The frontend listens for these
  exact cues; if you say "I'll set it up" without the signal, the
  modal does not dispatch and the student waits for nothing.
- When the close is router-driven but the lesson isn't done (more
  objectives remain), use "Let's move on to <next objective>."
  instead. When the close is forced (safety valve) and there's no
  mastery, use "We'll wrap here for now and pick this up next
  time." — never overclaim an exit ticket you cannot see.
```

This makes the runtime signal an explicit input to the move's wording.

### M3 — `CONFIRM_AND_ADVANCE` + pose-tool path: dedupe MCQ options before emission

T1840 and T1842 rendered options twice. Most likely cause: the `pose_question` tool call emits the option block AND the LLM prose body also writes the options out. A targeted directive in the `confirm_and_advance` body (and `worked_example` if it ever poses an MCQ):

```
STRUCTURAL — when posing an MCQ via pose_question, do NOT also write
the options into the prose body. The tool call emits the stem and
options; the prose body provides ONLY the transition sentence
before the tool call ("Try this one:", "Here's a contrast item:"),
not a restatement of the options. Writing the options twice (once
in prose, once via tool) is the duplicated-options bug — the
student sees A/B/C/D listed twice and the question looks broken.
```

This is a model-side guard; the structural conformance gate that would catch a duplicate-options block at the body level is the runtime fix, but the prompt-side directive shrinks the surface.

### M4 — `CONFIRM_AND_EXTEND` (currently used as `worked_example` after a rich-correct in 117, T1833): tighten the post-rich-correct extension rule

T1828 (session 116) said "let's go somewhere harder" and posed a basic T/F restatement. The `confirm_and_extend` body at `move_prompts.py:346-371` calls for raised rigor; the actual behavior on the `too_easy` signal in session 116 produced lower rigor. Reinforce in the `worked_example` body (which is the move that actually fires post-rich-correct here per the router):

```
DEFENSIVE — when the prior turn was a rich-correct answer (the
student named the mechanism, formula, or chain of reasoning), the
worked_example body must NOT re-derive the mechanism the student
just named. Either:
  (a) demonstrate the mechanism on a NEW surface (different
      domain, different parameters, transfer item) — this matches
      Layering Ch.16's "exercise prerequisite knowledge by
      composing", OR
  (b) skip to the practice prompt only — the worked example is
      degenerate when the student already showed they own the
      method.
Never restate the named mechanism. Never pose a T/F that asks the
student to confirm a definition they have already produced in
richer form. (Science of learning principle: Cognitive Load Ch.14
expertise-reversal — re-instruction on owned material is pure
load.)
```

### M5 — `EXPLAIN` opening: address the "advanced student in opening turn" branch

Sessions 117, 118 opened with a 4-paragraph explain (T1831, T1838) — completely justified at Rule 5, **but** the student is at S5 register and the open-ended reflective prompt at the end is calibrated to a novice ("what do you reckon might cause this?"). For an advanced student, the opening explain is overhead. Add a defensive at the bottom of `EXPLAIN`:

```
DEFENSIVE — when grade_level (from the preamble context) is S5 or
higher AND the lesson has a `direct_instruction_anchor` short
enough to fit in 1-2 sentences, keep the opener tight: ONE sentence
framing the concept, then the open-ended reflective prompt. Do NOT
unfurl the full 4-paragraph engagement framing on an S5+ student.
The 4-paragraph opener is calibrated for novices; on a strong
student it reads as the engine over-explaining the trivial.
(Science of learning principle: Direct Instruction Ch.11 + Cognitive
Load Ch.14 expertise-reversal — the opener should be as compact as
the student's prior-knowledge supports.)
```

This is a calibration directive, not a structural rule — useful for the S5 advanced register persona but also for review-mode resumed sessions.

### M6 — `WORKED_EXAMPLE`: forbid trivial-distractor restatement on advanced students

Session 116 T1828's T/F ("Condensation is the process where water vapor cools and changes into liquid water") is a *definition-restatement* the student had already answered in advanced form. Add to the `worked_example` body (which fires here per the router on the post-too_easy turn in session 116):

```
DEFENSIVE — the worked_example never poses a definition-restatement
of material the student demonstrated in the prior turn. If the
student named the mechanism (e.g. "condensation is when air cools
below dew point and water vapour condenses on nuclei"), do NOT
follow with "Condensation is when water vapour changes to liquid
(T/F?)" — that is a recall item on prior-turn content, not the
edge of ability. Pose instead a transfer / discrimination / inverse
item ("If air warms instead of cooling, what happens to droplets
already formed?", "How would dew-point change at higher
altitude?"). (Science of learning principle: Deliberate Practice
Ch.12 — practice at the *edge*, not the middle.)
```

---

## 5. Summary

| Dimension | Verdict |
|---|---|
| P1 errors | **1 hard P1 (T1837, close_topic abandons a posed question)** + 1 visual defect (T1840 duplicated options) + 1 broken promise (T1846 exit ticket no-show). 0 grader-correctness P1s — the grader was right on every verdict that landed. The failure cluster is on the **close path** and the **`too_easy` calibration on novice-register lessons**. |
| Science-of-learning adherence | Strong: Direct Instruction Ch.11 openings, Testing Effect Ch.20 substantive-term affirmations, locale-anchored subject voice. Weak: Mastery Learning Ch.13 close fires after 1-2 corrects (premature), Cognitive Load Ch.14 expertise-reversal on session 116 post-too_easy T/F, exit-ticket promise without runtime evidence. |
| Move prompts (`move_prompts.py`) | Sound principle citations. M1 (close_topic no-trailing-question — hoist to structural top of move body), M2 (close_topic exit-ticket promise gated on runtime signal), M3 (confirm_and_advance MCQ-pose-tool dedup), M4 (worked_example post-rich-correct no re-derivation), M5 (explain compact opening for S5+ students), M6 (worked_example no definition-restatement on advanced students) are the targeted fixes for the run-10 evidence. |
| Difficulty signal (`too_easy`) | Worked correctly in 2/3 sessions — T1835 (4-region discrimination) and T1844 (saturation-curve data) actually raised rigor. Failed in session 116 (T1828 trivial T/F restatement). The `runtime_state.difficulty_level: 1` persisted in all 3 sessions; the next-question selection used it correctly twice. |
| Exit ticket reached | **No**, in all three sessions. Sessions 117 and 116 closed without exit-ticket dispatch. Session 118 promised the exit ticket but did not deliver. Across the three sessions, **2 of 3 exit-ticket transitions failed**. |

Refs: `test-reports/GEO-S5-evaluation-2026-05-28.md` (struggling register, 1 empty-content P1), `test-reports/GEO-S5-evaluation-2026-05-27-run9.md` (Mudflows lesson), `test-reports/MATHS-S1-evaluation-2026-05-28-run10.md` (paired run).
