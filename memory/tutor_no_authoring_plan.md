# Tutor No-Authoring Plan (2026-05-01)

## Problem

The runtime tutor LLM has been authoring its own arithmetic questions and computing its own answers. Across recent transcripts:

- `65° → 125°` confirmed as the adjacent angle on a straight line (correct: 115°). The bad math then poisoned later turns: the tutor cited its own wrong precedent.
- Misread `106` as `105`, mid-sentence self-correction.
- Pivoted to a brand new concept after a wrong answer instead of pinning the student to the failed step.
- Reused the same `65° → adjacent` trap question across sessions.

Each failure was the tutor exercising authoring freedom it shouldn't have.

## Locked decisions

1. The tutor never authors questions or numerical examples.
2. The tutor pulls only from already-verified content. "Verified" = `is_published=True` on the parent model — that's the existing teacher-approval gate.
3. Practice steps and exit ticket share one bank. No separate practice bank.
4. Worked-example numbers in `LessonStep.teacher_script` are kept (already Layer-1 verified). The tutor recites; never invents new numbers in prose.
5. Math-only via `Course.is_math` — non-math tutoring untouched in v1.

## Sources of truth (the bank)

Two pre-validated stores already in the schema:

- `LessonStep.teacher_script` + `LessonStep.expected_answer` — the practice question for that step. Already Layer-4-templated and Layer-1-verified.
- `ExitTicketQuestion` rows where parent `ExitTicket.is_published=True` — the 35-question bank, every row Layer-4-validated.

Bank coverage is enforced at **content-generation time**, not at publish time. Content-gen already runs Layer 1 + Layer 4 validation; coverage just becomes another check in the same pass. If a practice step's `concept_tag` has no matching bank question after generation completes, content-gen warns and (optionally) generates one more question for that tag — never blocks the teacher from publishing.

At runtime, if the exact `concept_tag` has no match, the tutor falls back to any published bank question for the same lesson. The bank is small enough (~35 questions per lesson) and topically homogeneous, so a same-lesson question is always a reasonable substitute.

## What the tutor IS allowed to do

- Pose a verified question (server renders `LessonStep.teacher_script` verbatim, or a bank `ExitTicketQuestion` verbatim).
- Explain the rule the question tests.
- Walk the student through working.
- Diagnose mistakes using the **known** correct answer (`expected_answer` or `correct_answer`).
- Pull the next question from the bank when the current one is resolved.
- Conceptual scaffolding without numbers ("which rule applies here?").

## What the tutor is NOT allowed to do

- Compose a new arithmetic question.
- Invent numerical examples ("let's say it were 30°…").
- Riff on a wrong answer with a tweaked variant.
- Paraphrase the question stem (server renders it verbatim).

## Code changes

| Layer | Change | File |
|---|---|---|
| Tutor prompt | Hard rule: only `<question_bank>` items may be posed; no authoring | `apps/tutoring/conversational_tutor.py` (`_build_system_prompt`) |
| Bank context | At session start, sample a per-session pool from the published bank (same path as exit-ticket randomisation). On every turn, inject `<question_bank>` block listing the current step's question + this session's sampled `ExitTicketQuestion` rows for the same `concept_tag` | same file |
| Tail-line signal | `|||QUESTION:N|||` (mirrors `|||MEDIA:N|||`). Server intercepts, renders bank entry verbatim, replaces any LLM-authored stem | same file + `_parse_*_signal` family |
| Server guard | After generation: detect question patterns (`?`, "find", "calculate", "what is") containing numbers NOT in the active bank entry → drop those sentences and log violation | same file |
| Remediation re-ask | Pull a sibling published bank question with same `concept_tag`; never LLM-authored | `_start_remediation` |
| Content-gen coverage check | After lesson generation completes, log/warn if any practice step's `concept_tag` is uncovered. No publish-time block — teacher stays unblocked. Runtime fallback handles uncovered tags by pulling any same-lesson published question. | `apps/curriculum/content_generator.py` |

No new model fields, no migration, no new UI.

## Resolved (locked)

1. **Conceptual scaffolding asides** ("on a straight line, angles always sum to 180°") — allowed. They cite a rule, not a calculation. The server guard fires only on sentences ending with `?` or starting with imperatives (find / calculate / what is).

2. **Per-session randomisation** — at session start, sample a subset of the published bank into a per-session pool, mirroring how exit tickets already randomise their 10-of-35 selection. The tutor's `<question_bank>` context shows only this session's pool, so different sessions for different students draw different items. Re-uses the same sampling code path the exit ticket uses.

3. **Tutor never invents** — non-negotiable, enforced by the prompt rule, the `\|\|\|QUESTION:N\|\|\|` signal-only path for posing questions, and the server guard that drops invented-question sentences post-generation.

## Out of scope (v2+)

- Per-student question rotation / randomisation
- Difficulty laddering across the bank
- Auto-generated practice variants (Layer 4 templates already enable this — defer until pilot data shows we need it)
- Non-math subjects — geography/humanities tutor flow untouched

## Phased delivery

| Phase | Work | Estimate |
|---|---|---|
| P1 | Bank context injection + `|||QUESTION:N|||` signal + verbatim render | 3 hrs |
| P2 | Server guard for invented-question detection | 2 hrs |
| P3 | Remediation rework to pull from bank | 2 hrs |
| P4 | Content-gen coverage warning + tests against failing transcripts | 3 hrs |

Total: ~1 day of focused work.

## Test strategy

Replay the three failing transcripts as fixtures:
- `65° → 125°` confirmation hallucination
- `106` misread as `105`
- bare-answer `n=135` advancement

Each test asserts: (a) any question posed is byte-equal to a published bank entry, (b) the grader compared student input against `expected_answer` (not LLM output), (c) on wrong answer, no new-concept introduction sentence appears in the response.

## Next step

Write the bank-context injection in `apps/tutoring/conversational_tutor.py::_build_system_prompt` + the `|||QUESTION:N|||` parser in the signal family. P1 ships first; P2-P4 follow same day.
