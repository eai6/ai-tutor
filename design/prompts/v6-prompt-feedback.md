## Bottom line

The v6 prompt is directionally right, but it tries to solve too many runtime-control problems with prose. The failures in the Final Report are mostly **not because the prompt lacks the relevant instruction**. They happen because the prompt is long, has collisions between rules, asks the model to maintain hidden state across turns, and relies on the model to self-validate things that should be enforced by the engine. The report itself separates system-prompt edits from engine/flow changes, and the strongest engine recommendations are exactly the failures that are mechanically detectable: truncated turns, no-question turns, absent-media references, answer misclassification, repeated questions, and premature mastery. 

## 1. Likely root causes of failures despite the prompt

### 1. Rule collisions reduce obedience

The prompt says every turn must end with exactly one question via the question tool, with no exceptions. It also says media should be emitted as the **last line** when media is used. Those are incompatible if a tutor turn both shows media and asks a question. The model has no clean ordering rule for “media + question” turns. 

There is a similar collision in error handling. One section says that on first incorrect attempt the tutor should ask the student to redo only the failed step on the **same problem**. A later rule says when re-posing after a wrong answer, the tutor must either ask a simpler prerequisite question or a structurally different question and not paste the same MCQ. Both may be pedagogically defensible, but together they create branch ambiguity. 

The prompt also requires “Correct — because…” as the canonical first sentence for correct answers, while later asking the model to vary praise phrasing. The report flags the resulting boilerplate as a student-experience issue. 

### 2. The prompt overuses “hard” language without a priority system

Many sections use absolute language: “EVERY,” “MUST,” “Never,” “No exceptions,” “If you cannot do all three, you have nothing to send.” But they are not ranked against each other. When everything is highest priority, the model has to infer which invariant matters most.

A better structure would distinguish:

1. **Validity invariants**: must pass or the turn is invalid.
2. **Branch procedures**: what to do when the last answer is correct, wrong, hedged, or missing.
3. **Pedagogical preferences**: vary praise, connect to prior knowledge, reduce cognitive load.
4. **Style constraints**: word count, punctuation, no JSON, no self-talk.

Right now these are interleaved, so high-end models may optimize for the “teaching philosophy” rather than the rigid turn protocol.

### 3. Some failures require state the model may not reliably have

The model is asked to track the active problem, correct answer, attempt count, sub-skill, whether a worked example has already been shown, previous three questions, media availability, mastery level, and whether the student’s answer is pending. The report shows breakdowns exactly in those stateful areas: skipped acknowledgement, false-negative remediation, repeated questions, mutated problem state, and premature mastery. 

The prompt says to “find the correct option/value in the question bank context” before feedback, but the report still found a case where a correct answer of 170° was treated as suspect. That suggests the answer-verification step should not rely only on the generative model; it should be a deterministic pre-pass or at least a structured state field supplied to the model. 

### 4. The “question tool” contract is under-specified in the prompt text

The prompt says every question must be posed via a question tool, and inline questions should have A/B/C/D options. But the student-visible section bans tool names, JSON, developer fields, and duplicated text. That is reasonable, but the model still needs a precise output grammar.

For example, the prompt should not just say “use the question tool.” It should define one canonical output shape, or better, the engine should expose separate structured fields such as:

```text
student_text: ...
media_id: optional
question_slot: optional
inline_question: optional
```

Otherwise the model is juggling invisible tool behavior, text-output bans, and “last line” constraints at the same time.

### 5. The prompt asks the model to self-lint issues that validators should catch

The Final Report’s engine recommendations are telling: add validators for truncated turns, no-question turns, absent-media references, answer checking, problem-state consistency, coherence, exit-ticket gating, and problem-type interleaving. 

Those are not best solved by adding more words to the system prompt. They are deterministic or semi-deterministic checks. The report’s programmatic counts also show this clearly: Sonnet 4 had 11 repeated-question flags and 6 dirty regen shipments; Gemini 3 Flash had 5 no-question flags and 7 dirty regen shipments. 

### 6. Regeneration artifacts are likely pipeline-level, not tutor-persona-level

The prompt already bans duplicated paragraphs and repeated questions, but the report still found “regen_did_not_clean” and repeated-question artifacts. That implies regeneration may be appending, partially replacing, or leaking rejected drafts. A prompt can ask the model not to repeat, but it cannot guarantee that a bad regenerated candidate is not concatenated or shipped.  

### 7. “Higher-end model worse” can happen when the prompt is too principle-heavy

A stronger model may be more likely to “helpfully” infer pedagogy: ask for working, transition to wrap-up, add narrative, declare mastery, or smooth over a missing diagram. Those behaviors can look intelligent but violate a strict tutoring protocol. When the desired behavior is more like a state machine than open-ended teaching, capable models need **less philosophical freedom and more branch-level contracts**.

The current prompt gives both: a detailed state-machine-like protocol and broad teaching principles. That can produce drift.

## 2. How I would rewrite and restructure the prompt

### A. Put a short “valid turn contract” first

This should be the first operational block after identity. It should be shorter and more mechanical than the current version.

```text
<valid_turn_contract>
A tutor turn is INVALID unless all checks pass:

1. If last_student_answer_pending=true, the first sentence evaluates it:
   Correct / Almost / Right idea, but...
2. The turn contains exactly one student action.
3. The student action is self-contained: all numbers, units, labels, and answer choices are present.
4. No visual reference appears unless media_attached_this_turn=true.
5. Only use quantities from active_problem or selected_question. Never invent or change numbers.
6. The final visible sentence is complete and ends with terminal punctuation.
7. The student action has not been asked in the previous 3 tutor turns.

If any check fails, discard the draft and rewrite before sending.
</valid_turn_contract>
```

This is better than scattering the same rule across `<every_turn>`, `<must_end_with_question>`, `<tools>`, `<figure_rules>`, and `<student_visible_output>`.

### B. Resolve the media/question ordering conflict

Do not make media the “last line.” Make the question the last student-facing action, and treat media as a separate attachment field.

```text
<media_contract>
If a visual is needed:
- Attach media using media_id before posing the student action.
- You may refer to the visual only after attaching it.
- If no media_id is attached, describe the setup in words.
- Never use: diagram, figure, image, picture, chart, map, shown, above, below, look at, see, unless media_id is attached in this same turn.
</media_contract>
```

If the runtime requires a text marker such as `|||MEDIA:N|||`, put it before the question tool call, not after. The current “media last line” instruction competes with “end with one question.” 

### C. Replace broad principles with a small branch table

The current 13 principles are pedagogically sound, but they are too many to use as runtime control. Compress them into branch logic.

```text
<turn_branching>
First choose exactly one branch:

A. FEEDBACK branch:
   Use when last_student_answer_pending=true.

B. WORKED_EXAMPLE branch:
   Use when new_procedure=true and worked_example_shown=false.

C. PRACTICE branch:
   Use when the student is ready for the current skill.

D. REMEDIATION branch:
   Use when same_subskill_errors>=2 or same_subskill_hedged_correct>=2.

E. EXIT_TICKET branch:
   Use only when exit_ticket_unlocked=true.
</turn_branching>
```

Then give each branch a compact template. Models follow templates better than abstract “be active, scaffold, fade, interleave” language.

### D. Make answer verification a structured precondition

Instead of asking the model to “find” and “match” the answer, pass the needed fields into the prompt.

```text
<answer_state>
active_problem_id: ...
active_problem_text: ...
correct_answer: ...
accepted_equivalents: ...
student_answer: ...
attempt_number: ...
subskill: ...
</answer_state>
```

Then the prompt can say:

```text
If student_answer matches correct_answer or accepted_equivalents, you MUST use the correct-answer branch.
Do not ask for working before confirming a correct final answer.
```

This directly targets the false-negative remediation case identified in the report. 

### E. Split “wrong answer” handling into same-problem vs new-problem cases

Current wording mixes both. I would make the progression explicit:

```text
<wrong_answer_policy>
Attempt 1:
- Keep the same problem.
- Name the failed step.
- Give one hint or corrected micro-step.
- Ask the student to redo only that step.

Attempt 2:
- Keep the same concept, but ask a simpler prerequisite micro-question.
- Do not reveal the final answer.

Attempt 3:
- Show the full solution.
- Ask the student to explain one step back.
- Then give one structurally similar confirmation item.
</wrong_answer_policy>
```

Remove the later “must either simpler prerequisite or structurally different” sentence, or scope it only to attempts 2+. That eliminates the contradiction.

### F. Make “one student action” the invariant, not necessarily “one question”

The report recommends “exactly one question or explicit action prompt,” while the current prompt says exactly one question via the question tool. Pick one.

For tutoring, I would define the invariant as:

```text
Every turn ends with exactly one student action.
Allowed student actions:
- answer a selected question
- redo one named step
- explain one step in their own words
- choose A/B/C/D
```

Then require the engine to represent all of these as a single `student_action` object. This avoids awkward situations where “redo the conversion step” is pedagogically right but technically not phrased as a question.

### G. Move mechanically checkable constraints out of the prompt and into validators

Keep these in the prompt as short reminders, but enforce them outside the model:

| Failure                     | Prompt reminder                     | Engine validator                                    |
| --------------------------- | ----------------------------------- | --------------------------------------------------- |
| No question/action          | “Exactly one student action”        | Reject if no action object                          |
| Phantom visual              | “No visual reference without media” | Regex/LLM check for visual terms without media      |
| Repeated question           | “Do not repeat previous 3”          | Compare against recent question IDs/text embeddings |
| Dirty regeneration          | “Discard failed draft”              | Ensure candidate replaces, never appends            |
| Truncation                  | “Complete final sentence”           | Reject if incomplete sentence / dangling phrase     |
| Wrong answer classification | “Use answer_state”                  | Deterministic answer checker                        |
| Mutated problem numbers     | “Use active_problem only”           | Compare quantities in output to active problem      |
| Premature mastery           | “Exit ticket required”              | State gate before mastery/wrap-up                   |

This aligns with the Final Report’s engine/flow recommendations. 

### H. Reduce negative bans and repeated wording

The current prompt repeats several ideas across blocks: end with a question, no phantom figures, no duplicated questions, self-contained problem statements, complete sentence. Repetition can help, but too much repetition creates competing phrasings.

I would keep one canonical version of each rule in the “valid turn contract,” then remove duplicates from later sections. The prompt should feel like a checklist, not a constitution.

### I. Add a compact final self-check

```text
<final_check>
Before sending, answer silently:
1. Did I evaluate the last answer first, if needed?
2. Did I include exactly one student action?
3. Is the action self-contained?
4. Did I avoid absent-media references?
5. Did I keep the same problem state unless the branch allows changing it?
6. Is the turn complete, non-duplicated, and under the word limit?
If no to any item, rewrite.
</final_check>
```

This is more useful than “If you cannot do all three, you have nothing to send,” because it gives the model a concrete repair path.

## Recommended prompt structure

I would restructure v7 as:

```text
<identity>
Short tutor identity only.
</identity>

<valid_turn_contract>
7 mechanical validity rules.
</valid_turn_contract>

<provided_state>
Defines the fields the engine will provide:
last_student_answer_pending, active_problem, correct_answer,
attempt_count, subskill, previous_questions, media_catalog,
worked_example_shown, mastery_state, allowed_question_slots.
</provided_state>

<turn_algorithm>
Choose exactly one branch:
FEEDBACK, WORKED_EXAMPLE, PRACTICE, REMEDIATION, EXIT_TICKET.
</turn_algorithm>

<branch_templates>
Tiny templates for each branch.
</branch_templates>

<media_contract>
No media references unless media is attached.
</media_contract>

<student_action_contract>
Exactly one student action, self-contained, no repeated question.
</student_action_contract>

<style>
Short, warm, no JSON/tool names/self-talk, no filler, complete sentence.
</style>

<safety>
Age-appropriate and distress handling.
</safety>
```

## Highest-priority rewrite changes

1. **Resolve ordering conflicts**: media cannot be “last line” if the turn must end with a question.
2. **Replace principle lists with branch templates**: make the model choose one branch per turn.
3. **Supply structured answer state**: do not rely on the model to rediscover the correct answer from context.
4. **Use “one student action” as the invariant**: broader and more pedagogically natural than “one question.”
5. **Make every problem self-contained**: all quantities in the same turn, enforced by validator.
6. **Move repeated-question, no-question, phantom-figure, truncation, and dirty-regen checks into engine gates**.
7. **Shorten the prompt**: keep only one canonical statement of each hard rule.
8. **Separate hard validity from pedagogy**: models need to know which rules make a turn invalid versus merely less ideal.

The core redesign is: **turn the tutor from a free-form teacher with many principles into a constrained state-machine speaker with one action per turn.** That should improve instruction following across both mid-tier and higher-end models.
