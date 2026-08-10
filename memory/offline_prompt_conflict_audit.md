# Offline tutor prompt — conflict audit (2026-08-06)

## Why

The offline hint keeps grounding itself in the wrong question. Device session,
Practice 4/5: the live question was *"A student plots four points on a map: A at
2540, B at 2640, C at 2740, D at 2840. Which points lie on the same horizontal
line?"*, the student picked B, and the tutor replied:

> Not quite — in a four-figure grid reference, the easting comes first, then the
> northing. So **4535** means easting 45, northing 35. Now try this: which two
> digits in a grid reference represent the northing value?

`4535` appears nowhere in the live question. Neither does anything about digit
order. The hint belongs to a different item, and it ends with a question the
student cannot answer — the buttons on screen are still the four-points options.

Three prompt edits (`d6194b7`, `cc5be39`) improved the symptoms without moving
the reveal rate at all. That is the signal that the problem is not a missing
instruction. So: read the assembled prompt end to end as the model receives it
and find the contradictions.

Audited artefact: the real Block 0/1/2 assembly for a qwen (offline) session
with a live MCQ — **28,612 chars**, of which Block 0 is ~20,800. Dumped via
`build_system_prompt(..., family='qwen', answer_mode=ANSWER_MODE_PICKER)`.

## The conflicts, ranked by how directly they cause the observed failure

### C1 — "always pose next" vs "on incorrect, pose nothing" (CRITICAL)

Two unconditioned rules, ~10 lines apart, that cannot both be followed.

*How to write each reply*, first bullet:

> **Close one question and open the next in the same reply.** When the student
> has answered, a complete turn does three things at once: call `record_answer`
> with their literal answer, say one teaching sentence about it, and call
> `pose_question` for the next question you ask. **A reply that grades without
> posing leaves the student with nothing to answer, and the lesson stops
> moving.**

*GRADE mode*, a few lines above:

> **Incorrect** → give one hint per the ladder below, and keep the same question
> live (**pose no new question this turn** — the in-flight question stays until
> graded correct or pivoted).

The first is written for the correct-answer case but says only "when the student
has answered". It then supplies a *reason* — grading without posing strands the
student — that argues against the second rule on exactly the turn the second
rule governs. A model resolving the clash by the stated rationale will pose.

This is the best explanation for the invented follow-ups we keep seeing
("Now try this: …", "Here's the next one: …") — better than the hint-ladder's
sub-question line, because it names the tool.

### C2 — the worked example demonstrates what the hint-vs-reveal section forbids (CRITICAL)

*Worked examples*, wrong-answer case:

> Not quite — 120 isn't right here. Three equal angles around a point share
> 360°, so you divide 360 by 3. What's 360 ÷ 3?

*Hint vs reveal*, 50 lines later, same topic:

> **Reveal (don't):** "Sum the three known angles and subtract from 360."
> **Hint (do):** "What do angles around a single point always add up to?"

The worked example states the rule AND the operation — strictly more than the
line labelled a reveal. One section shows it as the model answer; the other
labels it as the failure. Examples beat prose (prompting-fundamentals: "examples
that subtly conflict with prose instructions → model follows examples"), and the
example is the thing shaped like the output.

The same section's *"Name the specific error"* bullet has it too: the sample
correction is *"you used 270 instead of 360"*, which hands over the value.

This is the most likely reason three prompt edits have not moved the reveal rate.

### C3 — instructions for a capability the tool no longer has (HIGH)

`pose_question` takes exactly one parameter — `question_index`. Verified against
`TOOL_SCHEMAS`. The prompt still carries ~500 words of question-*authoring*
guidance:

| Prompt text | Reality |
|---|---|
| "`question_type="mcq"` with four options" | not a parameter |
| "`short_numeric` entries let the student type the value" | not a parameter |
| "**Balance the MCQ correct letter across A/B/C/D** … decide the correct TEXT first, then roll a fair 1-in-4 pick for which LETTER holds it" | the tutor never picks letters |
| "**Check your numbers before posing an authored question**" | there are no authored questions |
| "**Keep MCQ distractors plausible.** Every wrong option must be…" | the tutor never writes options |

Each one tells the model that writing its own question is a thing it does. That
is the exact behaviour catalog-only (`f59bdb7`) was built to remove, and the
prompt still teaches it at length. The letter-balancing bullet does end with
"applies ONLY to questions you author yourself" — a caveat that presumes the
capability rather than denying it.

### C4 — "don't write the question" vs "write the question" (HIGH)

*POSE / TEACH mode*:

> The platform writes that exact question to the slot and shows the student its
> stem and options — **do not write the question, its options, or its answer
> yourself**; your reply just introduces it ("Here's the next one:").

*How to write each reply*:

> **One clearly-marked question per turn.** … the question matching
> `pose_question`'s text is the only question mark in your reply **before the
> A/B/C/D list**.

The second presumes the reply contains the stem and an A/B/C/D list; the first
forbids exactly that. Explains the offline turn that reprinted the full question
with all four options underneath a reply, directly above the buttons already
rendering them.

### C5 — "end every turn with a question they can type" vs the letter picker (HIGH)

> **End every turn with one concrete action the student can take now** — an
> imperative or **a direct question they can type an answer to**. … After an
> explanation, check understanding with a question.

Offline there is no text box. This bullet is in Block 0; `<answer_surface>` (the
per-turn override that says ask nothing) is ~7,000 tokens later, which is the
right position — but the Block 0 rule is stated absolutely and phrased around
typing, which is impossible on the device.

### C6 — the hint ladder is written for a text box (MEDIUM — known, partly addressed)

"ask a clarifying sub-question" (attempt 0), "A hint carries at most ONE
micro-step … when the student answers your micro-step", and both hint-vs-reveal
exemplars are themselves questions. `<answer_surface>` now overrides this
offline; the underlying text still assumes typing.

### C7 — six other questions, with their answers, sitting in context (MEDIUM — structural, not a wording conflict)

`<question_pool>` renders 6 full questions in Block 1: every stem, all four
options each, and `<correct_option>` for each. ~2,500 chars of numerals that are
not the live question — 5234, 3452, 4729, 3641, 3645, 7258, and so on.

Nothing instructs that a hint's numbers must come from the in-flight stem.
*"Ground every reply in the student's actual turn"* is one bullet among ~40 and
is about verdicts, not about arithmetic in hints.

On a GRADE-incorrect turn the pool is **unusable by definition** — C1's other
half says pose nothing — so it contributes only distractor numerals and six
correct answers to leak. The two-call loop knows the verdict at Call 2, which is
where the visible reply is composed. Suppressing the pool there is a structural
lever, not a wording one.

## What this adds up to

Every fix so far has been an *addition* to a 28.6k-char prompt in which the
governing rules already contradict each other. A 4B resolving C1 and C2 by
proximity or by stated rationale produces precisely the transcript above: pose a
new question after a wrong answer (C1), reveal the rule while doing it (C2),
write the stem out in prose (C4), and source the numbers from whatever question
is nearest in context (C7).

## Fix order

1. **C3 + C4 — delete, don't rewrite.** Remove the authoring guidance and the
   A/B/C/D-list instruction. Purely mechanical: the tool cannot do what they
   describe. Biggest reduction in bytes and in wrong affordances.
2. **C1 — condition the pacing rule on the verdict.** "Close one and open the
   next" applies to a CORRECT verdict; on INCORRECT the same question stays. The
   rationale clause ("nothing to answer") must be scoped too, or it keeps
   arguing the other way.
3. **C2 — replace the worked example.** It must show the hint *without* the
   rule, matching the hint-vs-reveal section it currently contradicts.
4. **C5** — scope the closing-action rule to what the student can actually send.
5. **C7 — suppress `<question_pool>` on a GRADE turn whose verdict is
   incorrect.** Structural, and the only item here that also reduces the reveal
   surface rather than just the instruction count.

C1–C5 are edits to `MARKDOWN_BLOCK_0_TEMPLATE` (`family_prompts.py`) and its XML
sibling `_BLOCK_0_TEMPLATE` (`prompts.py`) — the same conflicts exist in both,
since the Qwen variant is a Markdown restatement of the same content. **Anything
touching the base XML template changes the hosted Anthropic path**, so measure
before and after on the benchmark rather than shipping on inspection.

## Measurement

Prompt work is not verifiable by reading. Baseline before touching anything, on
the same three-trial harness used for `cc5be39` (offline, deliberately wrong
letter): **3/3 reveal the correct option, 1/3 write a question in prose without
posing it, 3/3 name the student's own option**. Re-measure after each numbered
fix, one at a time — the eval cannot resolve small moves at n=5 (four runs on
monotonically improving code went 3/5, 3/5, 5/5, 3/5), so a per-fix defect count
on a fixed harness is the readable signal, not pass rate.
