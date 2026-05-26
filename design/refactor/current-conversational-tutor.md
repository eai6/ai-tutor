# The Current Conversational Tutor — How It Works Today

This document describes the conversational tutoring engine as it is implemented today in `apps/tutoring/conversational_tutor.py` and its surrounding modules. It is a snapshot for the upcoming refactor: a faithful, structural account of what actually runs in production, not what we wish we had.

The engine is a single ~12,600-line Django class, `ConversationalTutor`, supported by ~40 sibling modules under `apps/tutoring/` (judges, graders, validators, regen ensemble, question abstractions, working analyzers, image services). Persistence flows through three Django models: `TutorSession`, `SessionTurn`, and a JSON `engine_state` blob that holds the engine's running counters and flags.

The five sections below mirror the questions in the task brief.

---

## 1. Step-by-step response generation in different scenarios

The public surface of the engine is small. Three entry points on `ConversationalTutor` are wired through `apps/tutoring/views.py`:

- `start()` — first turn of a brand-new session.
- `respond()` — every subsequent student message.
- `start_review()` — a session being resumed for remediation/review after a failed exit ticket.

`respond()` is a thin wrapper around `_respond_impl()`, which contains the canonical eight-phase pipeline described below. `start()` shares much of the same infrastructure but has its own opening generator (`_generate_opening()`) that produces a greeting plus a grounding question.

### 1.1 The canonical eight-phase pipeline (every turn)

**Phase 1 — Session state checks and history load.** The engine reads `SessionState` (TUTORING, EXIT_TICKET, COMPLETED) and reroutes EXIT_TICKET turns to `_handle_exit_ticket()`. Otherwise it loads the in-memory conversation list (built once at session init from `SessionTurn` rows) and increments two counters: a session-wide `exchange_count` and a per-step `step_exchange_count` that resets when the engine advances to a new lesson step.

**Phase 2 — Deterministic signal extraction.** Before any LLM call, the engine harvests verifiable signals from the student's input:

- If a question from the question bank was awaiting an answer, it is graded deterministically against the canonical answer (`_grade_against_last_bank_question()`). The verdict is cached as `_pending_bank_grade`.
- If the current step has a numeric `expected_answer`, the student's input is parsed as a number and compared with ±0.01 tolerance (`_pending_math_check`).
- Bare-answer detection flags math turns where the student submitted only a number with no working (`bare_answer_counts_by_step`).
- A "student working analyzer" performs step-by-step arithmetic on equations the student typed, producing one of five terminal states (NO_WORKING, PARTIAL_CORRECT, PARTIAL_WRONG, COMPLETE_CORRECT, COMPLETE_WRONG).
- A visual-request detector scans for phrases like "show me a diagram".

These results are not used to grade the student directly — they are injected as evaluation signals into the system prompt so the LLM cannot hallucinate praise for a wrong answer or vice-versa.

**Phase 3 — LLM response generation.** A fresh system prompt is built each turn (see Section 2.3) and passed alongside the full conversation transcript to the tutoring LLM via `_generate_contextual_response()`. The tutoring model is dispatched via `ModelConfig` purpose `TUTOR`, with temperature clamped to [0.1, 0.3]. When the provider supports tool use, two tools are bound to the call — `pose_question` and `pose_inline_question` — used by the tutor to formally pose bank questions or short inline MCQs with explicit answer keys.

**Phase 4 — Post-response validation (judges).** The draft response is evaluated by `run_combined_judge`, which dispatches to the unified judge (`apps/tutoring/judges/unified.py`) — a single multi-axis LLM call evaluating ten dimensions: factual claims, NO_AUTHORING rule compliance, coherence, figure references, safety, step completion, handoff, answer correctness, arithmetic verification, and answer leakage. Specialist judges (`factual`, `rule`, `coherence`, `handoff`, `safety`, `step_eval`, `figure_ref`, `figure_vision`) remain in the codebase as a fallback but are deprecated as of the unified-judge rollout. After the judge run, a "Socratic validator" applies structural checks for MCQ-block detection, truncation, numeric mutation, and repeated questions. A praise filter strips affirmative openers when the deterministic check said "wrong".

**Phase 5 — Student response analysis and adaptation.** `_analyze_student_response()` updates the engine's view of the student: confusion signals add to `student_struggles`, correct answers update practice scores and consecutive-correct streaks, wrong answers bump cognitive load and decrement consecutive-correct counters. Auto-difficulty fires here — four consecutive wrong drops `difficulty_level` to −1 (easy), two first-try-correct in a row bumps it to +1 (hard). Skill-graph practice attempts are logged via the skill-assessment service.

**Phase 6 — Regen ensemble (conditional).** When any judge or validator raises issues, `run_regen_ensemble` is invoked. It builds a focused rewrite prompt (the original draft, the violated issues, the bank question stems, and a media-catalog excerpt — roughly 1–2 KB, not the full 30 KB tutoring framing), fans out to N concurrent regen clients, judges every candidate concurrently, and selects the highest-scoring clean candidate. Up to two cycles run (down from four after prod data showed cycles three and four converged identically with cycle two), with temperature annealing from 0.20 to 0.15. Early-exit on the first judge-clean candidate. If no clean candidate emerges, the "least bad" one wins.

**Phase 7 — Step advancement and exit-ticket gating.** `_should_advance_step()` consults a safety-valve hierarchy: hard cap at 10 exchanges per step, minimum floor of 1–2 exchanges depending on step type, deterministic-correct fast-path (advance immediately on a numeric or bank verdict), then the LLM step-eval verdict from the combined judge. When all curriculum steps are complete, an exit-ticket hold gate decides whether to transition to EXIT_TICKET state immediately or hold for one more turn so the student can acknowledge a still-pending answer. A force-clear after five hold cycles prevents infinite stalls.

**Phase 8 — Persistence.** The response is saved as a new `SessionTurn` row with full judge output captured in `judge_outputs` (per-judge JSON) and rolled-up metadata in `metadata`. The in-memory conversation list is appended. The engine then serializes its full running state (~40 keys) to `TutorSession.engine_state` via `_save_state()`.

### 1.2 First-time student (session opening)

The first turn takes the `start()` path rather than `respond()`. The engine has no prior conversation to load, so it skips the deterministic signal phase and goes directly to `_generate_opening()`. Three things differ from a regular turn:

- A personalization block is assembled from `StudentSkillMastery` and the denormalized `StudentProfile.skills_snapshot`. If this is a brand-new student with no mastery data, the block contains a placeholder and the engine sets `difficulty_level = 0`. If prior mastery exists, low mastery (< 40%) seeds `−1`, high mastery (> 70%) seeds `+1`.
- The opening prompt instructs the tutor to greet warmly, state the lesson objective, recall one or two prior concepts (or connect to everyday life if this is the student's first lesson on the course), and pose a grounding warmup question — never a blank greeting.
- The same regen and Socratic validation guards run on the opening as on a normal turn.

### 1.3 Struggling student

"Struggling" is not a single signal — the engine tracks several:

- `consecutive_wrong` reaching four triggers auto-downshift to `difficulty_level = −1`. Hints surfaced via the `[HINT CALIBRATION]` block become more obvious (multiple-choice prompts, fill-in-blank scaffolding, explicit concept naming).
- Three wrong attempts on a single bank question (`_awaiting_answer.wrong_attempts >= 3`) trigger a no-reveal pivot: the engine moves the student to a different same-concept question rather than revealing the answer. This is a pilot policy directive — never leak the canonical even after repeated wrong attempts.
- Confusion phrases ("I don't get it", "help", "?") add the current topic to `student_struggles`, increment `cognitive_load`, and instruct the next turn to scaffold more.
- After a failed exit ticket (score < 80%), the session enters remediation mode (`is_remediation = True`). The engine stays on remediation steps, walks the student through targeted reteach questions on the specific concepts they missed, and only advances the walkthrough when the student answers correctly.

When `difficulty_level = −1`, the `[HINT CALIBRATION]` block embedded in the system prompt enumerates ACCEPTABLE scaffolds (concept-level hints, option elimination, prerequisite probes) and FORBIDDEN ones (direct answer, paraphrase). The unified judge's answer-leak dimension enforces this at validation time.

### 1.4 Advanced student

The engine has parallel signals for mastery:

- `consecutive_first_try_correct` reaching two bumps `difficulty_level` to +1.
- On session opening, a per-objective competency snapshot is consulted. Objectives where the student is ≥ 70% mastered cause the prompt to direct the tutor to "keep it tight: short check-for-understanding, one applied problem, then move on. Do not over-teach."
- Worked examples already shown are tracked in `shown_worked_example_indices`. The engine will not re-show a worked example the student has already seen.
- Deterministic-correct verdicts (numeric match or bank verdict) trigger the step-advancement fast path, so a student who knows the material does not wait for the LLM judge to confirm — they progress immediately after the minimum exchange floor.

At `difficulty_level = +1`, the hint calibration block instructs the tutor to give subtle, inferential hints with no explicit concept naming and to favor open-text questions over multiple-choice.

### 1.5 Returning student (resume mid-session)

`_load_state()` rehydrates every counter, difficulty level, covered-concept set, awaiting-answer dict, and cognitive-load value from `engine_state`. The conversation history is reloaded from `SessionTurn` rows. If an unanswered bank question was in flight when the student left, the artifact panel re-renders that exact question on resume — no new question is posed. The resume opening reminds the student where they were without revealing the pending answer.

---

## 2. How the tutor manages and updates its context

The engine maintains three layers of context: a JSON state blob, a conversation transcript, and a dynamically rebuilt system prompt. There is no shared memory store and no agent-level scratchpad — every piece of context is either in the database or rebuilt from it each turn.

### 2.1 The `engine_state` JSON

`TutorSession.engine_state` is the single source of truth for the engine's running state. It holds roughly forty keys, read on session start via `_load_state()` and rewritten on every turn via `_save_state()`. The keys group into:

- **Session-level counters**: `session_state` (enum), `display_phase`, `exchange_count`, `current_topic_index`, `step_exchange_count`.
- **Learning progress**: `concepts_covered`, `student_struggles`, `student_strengths`, `practice_correct`, `practice_total`, `covered_concept_ids`, `covered_objectives`, `covered_enabling_objectives`.
- **Remediation state**: `is_remediation`, `remediation_attempt`, `failed_exit_questions`, `failed_eos`.
- **Personalization signals**: `difficulty_level`, `pretest_diagnostic`, `correct_streak`, `cognitive_load`, `consecutive_wrong`, `consecutive_correct_streak`, `consecutive_first_try_correct`.
- **Question and media persistence (the "artifact panel")**: `shown_question_ids`, `turn_questions`, `awaiting_answer`, `turn_media`, `recent_tutor_question_sigs`, `shown_media_urls`, `shown_worked_example_indices`.
- **Exit-ticket gating**: `exit_ticket_hold_until_exchange`, `exit_ticket_hold_count`, `bare_answer_counts_by_step`.

This blob is the entire "memory" the engine carries between turns. It is untyped — there is no Pydantic schema — which is a known inconsistency flagged in the codebase's own architecture notes.

### 2.2 The conversation transcript

A Python list of `{role, content}` dicts is loaded once at init from `SessionTurn` rows and held in memory as `self.conversation`. Every student message and tutor response is appended to it. Media signals (`|||MEDIA:N|||`) and other internal markers are stripped before the content is appended, so the transcript stays clean.

The full transcript is sent to the tutoring LLM each turn — there is no windowing or summarization at generation time. The judge layer, however, operates on a bounded window (controlled by `JUDGE_HISTORY_TURNS`) to keep cross-turn coherence checks fast and focused.

### 2.3 The system prompt assembled each turn

`_build_system_prompt()` rebuilds a fresh prompt every turn in two layers separated by a cache-break marker:

**Stable prefix (cacheable, ~10 KB).** Provider-specific base prompt loaded from `apps/tutoring/prompts/` (variants v3 through v7 exist as separate files; one is selected per deploy via env var). Wrapped with the institution name, grade level, language, tutor name, and locale. Includes universal socratic rules, subject-specific rule blocks (math gets a dedicated `math_teaching` block enforcing "BANK IS SOURCE OF TRUTH" and arithmetic rules), an optional group-session block, regional context, the full media catalog (Section 5), the question bank serialized to prose, and the mobile response format if the client is mobile.

**Dynamic suffix (per-turn).** Appended after the cache-break marker so the stable prefix can be cached by the LLM provider while the suffix changes. Contains:

- The current step's directive (step type, objective, constraints).
- Retrieved knowledge-base chunks matching the student's input.
- A `[STUDENT PROFILE]` block with mastery signals, XP/streak data, prerequisite gaps, and pace recommendation.
- A `[RETRIEVAL]` block with spaced-repetition warmup questions (if personalization populated them).
- The deterministic evaluation signals from Phase 2: `<bank_evaluation_signal>`, `<math_evaluation_signal>`, bare-answer flags, and the `<student_working_analysis>` breakdown.
- A `[HINT CALIBRATION]` block tied to `difficulty_level`.
- A `<figure_facts>` block when the current step's media has structured metadata.

The dynamic suffix is the engine's primary lever for shaping behavior turn-to-turn. It does not modify the prompt — it injects facts the prompt instructs the LLM to honor.

### 2.4 Student profile and cross-session memory

Three persistent records carry knowledge of the student across sessions:

- `StudentProfile` — grade level, school, optional tutor personality modifier, and a denormalized `skills_snapshot` JSON keyed by course → objective → mastery percentage.
- `StudentSkillMastery` — per-skill mastery levels, last-practiced timestamps, retention estimates, and due-for-review flags. Updated incrementally on every graded question.
- `StudentLessonProgress` — per-lesson mastery level (NOT_STARTED, IN_PROGRESS, MASTERED), best score, attempts count. Updated at session end.

When a session ends, an aggregation routine reads all exit-ticket attempts, updates `StudentLessonProgress` and refreshes `StudentProfile.skills_snapshot` so the next session sees current mastery. There is no chat-level memory carried across sessions — only structured competency data.

---

## 3. How the tutor addresses the principles of the science of learning

The engine encodes pedagogy in three layers: the system prompt (declarative rules the LLM is told to honor), the judges (post-hoc enforcement that triggers regeneration on violation), and the engine code itself (deterministic counters and gates). Coverage of the standard principles is uneven — some are structurally enforced, others are aspirational prompt language only.

**Active processing / generation effect — implemented.** The system prompt requires the student to be doing something on at least 60% of turns and caps tutor responses around 60 words. The unified judge's handoff dimension checks every response for a real question or directive; failure triggers regen. This is among the strongest structural enforcements in the system.

**Immediate, specific feedback — implemented.** A detailed feedback protocol lives in the system prompt (confirm correct + brief why; nudge on first wrong; structured hint on second; name the misconception on third). Correctness is determined by deterministic check first, LLM judge second, and the praise filter strips affirmative openers when the deterministic verdict is "wrong". Exit-ticket grading provides batched per-question feedback.

**Worked examples and faded scaffolding — partially implemented.** The curriculum supports a `worked_example` step type and the system prompt instructs the tutor to fade from full worked examples to guided practice to independent practice. Fading depends on lesson authors sequencing steps correctly — the engine does not auto-detect when to skip a worked example beyond tracking `shown_worked_example_indices` to avoid re-showing the same one.

**Cognitive load management — implemented at the prompt level.** "One idea at a time", "two sentences per idea", and explicit instructions to use concrete numbers before abstract notation are in the prompt. The 60-word target is enforced by the truncation/coherence judges and by the regen loop. Dual-coding is encouraged via the media catalog (Section 5).

**Dual coding — partially implemented.** The system prompt instructs the tutor to pair verbal explanations with diagrams when relevant, and the media catalog gives it the affordance. Whether dual coding actually happens depends on whether teachers authored figures for the step and whether the LLM chooses to attach one. The figure-reference judge catches deictic language with no attached figure, but does not require figures.

**Prerequisite mastery and prior-knowledge activation — partially implemented.** The curriculum supports per-lesson prerequisite gating (opt-in per course) and the skill model carries prerequisite relationships. The system prompt instructs the tutor to address weak prerequisites first if the student is failing because of them. Detection is reactive — the engine notices struggle and the prompt routes the tutor to a prerequisite, but there is no proactive pre-assessment of prerequisite mastery before a lesson begins (beyond the manual gate).

**Retrieval practice — partially implemented.** The opening prompt and a `[RETRIEVAL]` block direct the tutor to pose one or two retrieval questions if a personalization service has populated them. There is no scheduler — the retrieval questions only fire when something upstream provides them.

**Spaced practice — not implemented in the engine.** The prompt contains aspirational language about celebrating remembered material and previewing future revisits, but there is no scheduling code, no "last reviewed" timestamps used for spacing decisions, and no algorithm enforcing intervals. `StudentSkillMastery` carries the data that could drive this (last-practiced, retention estimate, due-for-review) but no service currently consumes it for spacing.

**Interleaving — not implemented in the engine.** Like spacing, the prompt mentions weaving in interleaved review when a context block provides it, but no code generates interleaved questions or enforces topic switching. Lessons execute their steps in authored order.

**Desirable difficulty — partially implemented.** Auto-difficulty (the consecutive-wrong / consecutive-first-try-correct triggers) is a coarse implementation. There is no quantified difficulty model — difficulty levels span only −2 to +2 and shift hint obviousness rather than question selection.

**Metacognition / self-explanation — deliberately not implemented.** Pilot feedback in mid-2026 reported that constant probing felt like interrogation. The current rule on correct answers is the opposite of metacognitive: confirm with a brief "because…" and move on, no probing. On wrong answers, the tutor asks for working diagnostically (to locate the error), not reflectively (to deepen reasoning). This is an explicit trade against the principle, made for engagement.

**Growth mindset / effort praise — aspirational.** The prompt prescribes effort-specific praise ("you did that without any hints", "three in a row — you're getting fluent") and forbids ability praise ("you're so smart"). No judge or filter validates that praise is effort-coded — it is LLM compliance only.

**Formative assessment and mastery gating — implemented.** Exit tickets gate lesson completion. Questions are tagged by teaching objective. The competency tracker aggregates per-objective signals across baseline, latest, final, and practice attempts. Failing the exit ticket routes the student to remediation walkthroughs. The summative baseline gate (opt-in) blocks lesson entry until a course baseline has been taken.

**5E phase structure — implemented at the curriculum layer.** Each `LessonStep` carries a `phase` field (Engage, Explore, Explain, Elaborate, Evaluate). The engine does not control flow by phase — it advances steps in authored order, and the phase tag is purely display-level metadata. The removal of the old `ConversationPhase` enum was deliberate: flow control is by state and step, not by 5E phase.

In summary: active processing, immediate feedback, formative assessment, cognitive load, and worked examples are the principles with real structural support. Spacing, interleaving, metacognition, and most-of dual coding live in the prompt but lack engine-level enforcement.

---

## 4. How the tutor validates answers and knows what the right answer is

Answer validation is layered. The tutor does not, in general, decide correctness on its own — it consults a hierarchy of sources, falling back to the LLM only when no deterministic source is available.

### 4.1 The four sources of a question

Every question the tutor poses comes from one of four origins, and each origin carries its own answer key:

1. **Curriculum-authored questions** on `LessonStep`. The step model carries `question`, `answer_type` (mcq, short_numeric, free_text, true_false), `expected_answer` (the canonical correct answer), `choices` (for MCQ), `rubric` (pedagogical guidance for the LLM grader), and per-attempt hints. These are the highest-trust source because a teacher wrote both the question and the answer.

2. **Exit-ticket bank questions** stored as `ExitTicketQuestion` rows, each with `question_text`, MCQ options A–D, a `correct_answer` letter, and an `answer_data` JSON for structured types (model answers for short-answer, blanks for fill-in-blank, pairs for matching, keyword lists for keyword-count rubrics).

3. **Inline-authored questions** posed by the tutor via the `pose_inline_question` tool. The tool is restricted to MCQ with explicit options and a correct-answer letter, so a deterministic answer key always exists. The metadata is stashed in `engine_state` as `inline_authored_question`.

4. **Chat-authored questions** — questions the tutor improvises in prose without calling a tool. These have no built-in answer key.

A unified `Question` abstraction wraps all four sources, with adapters (`from_lesson_step`, `from_exit_ticket`, `from_inline_authored`, `from_chat_authored`). Downstream code calls a single `grade()` method without caring which source produced the question — the dispatch is based on whether a canonical answer exists (`has_canonical`).

### 4.2 Deterministic grading paths

When a canonical answer exists, grading is deterministic and runs without an LLM:

- **MCQ**: letter match after normalization, or option-text match against the choices.
- **Short numeric**: arithmetic parse plus comparison with ±0.01 tolerance. The student-working analyzer splits chained equations on `=`, evaluates each side via Python's `ast` module (no `eval`), and produces a five-state verdict (NO_WORKING, PARTIAL_CORRECT, PARTIAL_WRONG, COMPLETE_CORRECT, COMPLETE_WRONG).
- **Fill-in-blank**: per-blank normalized exact match, with optional keyword alternatives.
- **Matching**: per-pair equivalence check.
- **Short-answer**: keyword-count rubric with math-symbol normalization (degree, square, square root symbols normalized to ASCII), fallback to LLM batch grading when exact-match fails.

Summative exit-ticket grading uses only this deterministic path — an entire 30-question exam grades in under 100 ms with zero LLM calls.

### 4.3 LLM grading for improvised questions

When no canonical answer exists (the chat-authored case), the chat-authored grader runs an LLM judge that consults, in priority order:

1. The lesson's knowledge-base chunks (most authoritative).
2. Google Search grounding (when the provider is Gemini) — used for live verification of named entities, dates, places, and statistics. Particularly valuable for local Seychelles facts.
3. The LLM's parametric knowledge (only when grounding is unavailable).

The grounded path runs in two calls: first a grounded judgment, then a structured extraction into a verdict object carrying `is_correct`, `reasoning`, and a `derived_answer` that downstream leak detection can use.

### 4.4 The unified judge as second-pass verification

After the tutor generates a response, the unified judge evaluates ten dimensions in a single LLM call:

1. **Factual** — checkable claims are categorized as supported, contradicted, or unverified; only contradicted claims trigger violations.
2. **Rule (NO_AUTHORING)** — the tutor must not invent concrete numerical problems; it may only reuse bank stems or pose conceptual questions.
3. **Coherence** — self-contradictions, cross-turn flips, and structural incoherence.
4. **Figure reference** — deictic phrases ("look at the diagram") with no attached figure are flagged.
5. **Safety** — harmful or inappropriate content.
6. **Step complete** — tri-state verdict on advancement.
7. **Handoff** — does the response hand the floor back with a real question or directive.
8. **Answer correct** — tri-state verdict on the student's input, anchored to the exact question posed. Defers to the deterministic verdict when one is available; overrides only with clear reason (equivalent form, typo).
9. **Arithmetic** — implicit and explicit arithmetic claims in the tutor's prose ("they sum to 360°") are checked. Pre-gated on the presence of any digit in the response.
10. **Answer leak** — gated on student-was-wrong AND a live open question. Detects letter reveals, canonical-text reveals, paraphrased reveals, and teach-back reveals.

The judge's `answer_correct` axis is itself a second-pass check on the tutor's own assessment: if the tutor said "correct" but the judge says "wrong", that registers as a coherence violation and triggers regen.

### 4.5 Structural guards

Three additional guards run alongside the judge:

- **Repeated-question detection** — a deterministic Jaccard signature match catches near-identical re-asks; borderline cases (Jaccard 0.20–0.55) fall through to an LLM judge that detects semantic repeats and active paraphrases. A template-repeat detector catches the case where the tutor re-poses the same procedure with different surface numbers.
- **Rule compliance** — an LLM judge enforces NO_AUTHORING and arithmetic rules on math turns. Pre-gated on the presence of any digit, question mark, or praise vocabulary so conversational turns skip the call.
- **Praise filter** — strips affirmative openers from the first sentence when the deterministic verdict said wrong, rotating the replacement opener to avoid a stuck-record effect.

### 4.6 The regen loop closes the validation

When any judge or guard raises issues, the regen ensemble (Section 1.1, Phase 6) rewrites the response with the violations surfaced in a focused rewrite prompt. The canonical answer is suppressed from the regen context when an answer-leak violation fired, so the regenerated text has no access to the answer it just leaked.

**Net effect**: the tutor never calculates correct answers from scratch. Either a teacher authored the answer, or an LLM judge grounded its judgment in curriculum content. And every tutor response is independently validated before it reaches the student.

---

## 5. How the tutor decides when and how to include figures and media

The tutor is a curator, not a generator. All media is pre-existing in a catalog at session start; the tutor's job is to recognize when a catalog item fits the current explanation. Dynamic image generation during tutoring does not exist — it runs only at lesson-authoring time.

### 5.1 The media catalog is assembled per turn

`_build_media_catalog()` runs each turn and merges three sources, deduplicated by URL:

1. **Step-scoped media** — each `LessonStep` carries a `media` JSON field with images, videos, and audio entries. Each entry has a URL, alt text, caption, type ('diagram', 'photo', 'illustration', 'chart', etc.), and a source tag ('generated', 'library', 'curriculum').

2. **Lesson-level media** — items attached at the lesson level (not a specific step) via `get_lesson_media`.

3. **Knowledge-base figures** — when the lesson is sparse, `CurriculumKnowledgeBase.query_for_figure_descriptions()` queries ChromaDB for `chunk_type='figure_description'` chunks extracted from textbook PDFs, ranked by topic relevance.

The catalog is built into a 1-indexed map (`_media_id_map`) so the LLM can refer to items by number. A separate `_step_media_ids` index tracks which catalog items are available for the current step, used for observability and to populate the per-step reminder.

A per-course gate (`Course.tutoring_images_enabled`) can suppress the entire catalog. When suppressed, the LLM sees no media affordance at all.

### 5.2 The catalog is injected into the system prompt

The catalog appears as an XML block listing each item by index and label: `[1] <label>`, `[2] <label>`, and so on. Generated images that lack a pre-populated alt or caption fall back to using the generation prompt as their label. The injection includes:

- A numbered list of available items.
- An instruction that the signal `|||MEDIA:N|||` must be emitted as the **very last line** of the response.
- A rule that no media reference appears in prose — only as the trailing signal.
- A one-per-response constraint.
- A fallback instruction: if no item fits, omit the signal entirely and teach with text.

A per-step reminder is also injected: "MEDIA AVAILABLE for this step" when items exist, or "Only show images from the media catalog using `|||MEDIA:N|||`. Do NOT reference figures or diagrams that are not in the catalog" when none do.

For steps whose figure carries structured metadata, a separate `<figure_facts>` block lists labelled features (e.g., "angle 1 at the upper-left intersection"), verified relationships ("angles 1 and 5 are corresponding"), and anchor prompts. Rules tied to this block forbid imagined spatial descriptions and require the tutor to anchor scaffolding to actual labelled features.

### 5.3 The decision to attach media is LLM-driven

There is no deterministic rule like "always show media at step intro" or "never show during a hint". The decision is fully delegated to the tutoring LLM, constrained by the prompt above. The model reads the student's input, the current step directive, the catalog, and decides whether a figure would aid the explanation it is about to give.

The one explicit reactive rule: if the student asks for a visual ("show me a diagram", "can I see it"), the prompt instructs the tutor to emit `|||MEDIA:N|||` if a catalog item exists, or describe in text if not — never to fabricate a reference.

### 5.4 The signal is parsed and stripped before persistence

The `|||MEDIA:N|||` signal is parsed by a regex that extracts the index, resolves it via `_media_id_map`, and returns the cleaned text plus a media dict. The signal is always stripped from the saved content — defense-in-depth sanitization in `_create_message` ensures even legacy signal formats never reach the database. The frontend separately sanitizes anything that slipped past.

### 5.5 Figure correctness is validated post-hoc

Two judges check that media usage is correct:

- **Deterministic figure-reference judge** scans the cleaned text for 72 known deictic phrases. If a phrase like "looking at the diagram" appears and no media was attached, that is a violation. If a figure was attached, the judge skips with `skip_reason='figure_attached'`.
- **Vision-based figure-vision judge** runs only when a figure is attached, the response poses a figure-dependent question, and a vision-capable LLM is available. It sends the figure to the model as a base64 image alongside the response text and verifies the figure actually matches the question (e.g., "question describes three angles on a straight line; figure shows two").

The unified judge subsumes the deterministic figure-reference check but does not yet implement the vision check — figure-vision is marked skipped in unified results, with the specialist judge remaining available as a fallback.

### 5.6 Offline image generation is separate

Image generation lives in `ImageGenerationService` and runs only at lesson-authoring time, never during tutoring. It generates via OpenAI's image API or Gemini with provider fallback, runs a pre-generation prompt judge to avoid wasted spend, a post-generation alignment judge to verify the image matches the brief, and an auto-regen ensemble when the post-gen judge rejects. The final image is saved to a `MediaAsset` row and a separate `figure_facts_extractor` populates the labelled-features metadata used by the runtime `<figure_facts>` block.

### 5.7 End-to-end flow summary

A media item makes it from authoring to a student's screen via this path:

1. Teacher uploads or generates a figure for a lesson step; metadata extracted into `MediaAsset.figure_facts`.
2. Lesson is published.
3. Session starts; `_build_media_catalog()` assembles the three-source catalog and builds the 1-indexed map.
4. System prompt injects the catalog block, the per-step reminder, and (when available) the `<figure_facts>` block.
5. Tutor LLM generates a response, decides a figure aids the explanation, and emits `|||MEDIA:N|||` as the last line.
6. The signal is parsed, stripped, and resolved to a media dict.
7. The figure-reference and figure-vision judges validate the choice.
8. The cleaned response and the media reference are saved to the `SessionTurn` row.
9. The frontend reads the media reference and renders the image below the tutor's text.

The design enforces a clean separation: figures are curated offline, validated at generation, and the tutor decides only **when**, not **what**.

---

## Closing note

The current engine is a single large class with significant defense-in-depth: deterministic gates override LLM judgments wherever possible, the regen loop catches what judges flag, and the system prompt carries pedagogy that the judges then enforce. Its weak points are the ones called out above — untyped state, prompt-only pedagogy in several principles (spacing, interleaving, metacognition), keyword-based concept coverage on the per-turn path, and a 460-line system prompt with internal contradictions. The refactor target is to keep the strengths (deterministic-first grading, judge-enforced active processing, layered media validation) while resolving the structural issues that have accumulated.
