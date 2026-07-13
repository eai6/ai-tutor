# Lesson content defects found while grounding the 400-scenario eval dataset

**Found**: 2026-07-13, as a side-effect of the eval dataset expansion
(`memory/eval_dataset_400_plan.md`). Fifteen authoring agents each read one
lesson's steps and exit-ticket questions closely enough to build correct
reference answers from them. That is the first time this content has been read
adversarially, and it surfaced defects that no test covers.

**Why this matters beyond the eval.** These are not eval-harness bugs. This is the
frozen extract of **production curriculum content** — the same steps and exit
tickets the tutor serves to students in the Seychelles pilot. A wrong
`expected_answer` means the tutor marks a correct student wrong. Contradictory
teach text means the tutor teaches a falsehood, confidently.

The eval dataset routes around all of these — no scenario's reference answer
depends on a defective item — so the dataset is sound. The *curriculum* is not.

## Defects

### Lesson 1139 — Angles formed within parallel lines

The lesson's own angle numbering contradicts itself. Steps 8 and 9 call angles
4 & 8 "co-interior" and angles 2 & 8 "alternate exterior". Under the numbering
the *same lesson* uses elsewhere (1↔5 corresponding, 3&6 alternate interior,
3&5 co-interior, 1&8 alternate exterior), both pairs are mislabelled.

A student following steps 1–7 and then hitting step 8 is taught two incompatible
schemes. The tutor will be misled at runtime too.

### Lesson 1144 — Calculate expected outcome

**Step 7's MCQ has no correct option.** Route A = 600 × 0.02 = 12; Route B =
400 × 0.05 = 20; so B expects **8 more** than A. None of the four options say
that, and the stored `expected_answer` is `'C'` ("Route A expects 14 more"),
which is wrong on both the direction and the magnitude.

Two exit-ticket rows are also internally inconsistent: one states "probability is
0.6" while its rationale computes 0.166667; another states "probability of success
is 5 with 90 trials" and silently reinterprets the 5 as 5/9.

### Lesson 1143 — Calculate the probability of single events

**Step 9's quiz MCQ has three correct options.** The options are
`A) 1/3, B) 4/12, C) 1/4, D) 2/6` with `expected_answer: B` — but 1/3, 4/12 and
2/6 are the same number. A, B and D are all correct, and a student picking A is
marked wrong for giving the right answer.

### Lesson 1465 — Coordinate systems / grid references

**The eastings/northings definition is backwards.** An exit-ticket fill-in-blank
asserts "eastings are horizontal grid lines, northings are vertical". Standard
cartography is the reverse: eastings are the *vertical* lines, numbered along the
horizontal axis.

Separately, the six-figure worked example explains `456734` as "7/10 across, 4/10
up" — the digits give 6/10 across, and the stated rationale is garbled.

This is the worst of the set: it teaches the core definition of the lesson
incorrectly, and grid references are exactly the topic where a reversed
convention produces confidently wrong answers forever.

### Lesson 1467 — Describing landscape

An exit-ticket MCQ key asserts that **steep terrain is "ideal for urban
settlement"**. That is the opposite of the geographical reasoning the lesson
teaches, and the item is keyed to it.

### Lesson 1468 — Shape and form of river channel

The lesson contradicts itself on channel shape. Step 1 teaches **box-shaped** for
the lower course, but an exit-ticket fill-in-blank asserts the lower course is
**U-shaped** — and step 2's worked example calls an *upper*-course channel
"U-shaped", which conflicts with step 1's own V-shaped upper course.

Channel shape by course is the entire point of the lesson, so all three
statements cannot stand.

## Recommended action

1. **Fix 1465 first** — a reversed core definition is the highest-harm defect
   here, and it is being taught in production now.
2. Fix 1144's step-7 MCQ (no correct option exists — the item cannot be answered).
3. Reconcile 1139's angle numbering across steps 1–9.
4. Fix 1143's step-9 MCQ (three of the four options are the same number).
5. Re-key 1467's land-use MCQ; reconcile 1468's channel shapes.
6. **Then ask the broader question**: these six defects came out of the *first
   sixteen lessons* anyone looked at closely. The dump holds 354. Nothing in the
   pipeline validates that a generated `expected_answer` is actually correct, or
   that teach text is self-consistent. A content-validation pass over the
   curriculum — even a cheap LLM-judge sweep asking "is this item's stored answer
   actually right?" — would likely find many more. The judge infrastructure to do
   it already exists.

The defect rate is the number worth acting on, not any individual item: **6 clear
defects across the 16 lessons examined**. Three of them (1144 step 7, 1143 step 9,
1465's eastings/northings) are items where a *correct* student is marked wrong or
taught a falsehood outright.

Refs: memory/eval_dataset_400_plan.md
