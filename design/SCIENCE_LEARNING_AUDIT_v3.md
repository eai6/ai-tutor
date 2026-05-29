# Science of Learning Audit: AI Tutor Implementation (v3)

> Branch `feature/interactive-visualizations` at HEAD (commit `3fe7cf6`, May 2026), measured against principles distilled from *The Math Academy Way*, **Chapters 10–23 only**. Builds on `SCIENCE_LEARNING_AUDIT.md` (v2, Feb 2026, commit `9bee720`).

---

## Executive Summary

The v2 audit's headline finding — *"much of the science-of-learning infrastructure is built but not wired in"* — is **largely no longer true**. Almost every v2 Tier 1 recommendation (R1–R8) and most Tier 2 items (R9, R13, R14, R19) have been wired. There are dedicated `test_r{N}_*.py` files for each, the v2 Section-4 system prompt has been ported into `apps/tutoring/prompts/anthropic.py` and strengthened with pilot-driven additions, and `SkillAssessmentService.record_practice()` now actually runs on every graded turn.

However, three new categories of drift have emerged:

1. **Math-quality regression caused by a 2026-05-20 model swap.** Migration `apps/llm/migrations/0028_swap_runtime_to_gemini_3_1_flash_lite.py` swapped the tutoring, judge, and regen purposes from Opus 4.7 (temp 0.0) to **Gemini 3.1 Flash Lite Preview (temp 0.2)** the day before this audit. The same small model now serves the responder, the arithmetic verifier, and the regen path — so the safety net and the responder share blind spots. The migration is cleanly reversible.

2. **The system prompt has grown to ~460 lines of XML-tagged constraints with internal contradictions.** A 40-word/turn hard cap (`anthropic.py:455-491`) fights structurally against worked-example scaffolding. Multiple negative-instruction blocks ("never X", "do NOT Y") over-index on Gemini-class models, which `.claude/skills/gemini-prompting-expert/SKILL.md` warns against. Together H1 + H2 explain the bulk of the reported math errors.

3. **Concept coverage is still keyword-based** (`_keyword_concept_coverage_check` at `conversational_tutor.py:10549`), but is now **load-bearing** — it gates the new remediation floor introduced in commit `5d6cbd7`. The LLM-based version (`:10587`) exists but is not on the per-turn path. v2 R12 flagged this as over-counting; it has gotten more important without getting more reliable.

Three principles remain structurally unwired: **automaticity** (no latency tracking), **non-interference** (no scheduler), and **mastery-based phase transitions** (still exchange-floor-driven). A standalone "Daily Review" session type (v2 R15) has not been built.

The headline recommendation for this round is **R1 (v3): roll back tutoring to Opus 4.7 immediately** while keeping judge on Flash Lite, then trim the system prompt to a leaner Gemini-shaped version (Section 4 below) before re-attempting the smaller-model swap.

---

## 1. Principles Distilled — *Math Academy Way*, Chapters 10–23

Every principle is grounded in a single chapter; Ch.23 is treated separately as a *meta-principle* (the justification for any AI tutor's existence). Each row gives the one-sentence definition + the 2-4 most testable behavioural imperatives.

| # | Chapter | Principle | Definition | Most testable imperatives |
|---|---------|-----------|------------|----------------------------|
| 1 | 10 | **Active Learning** | Learning happens only when each individual student is actively performing exercises with feedback — "following along" is not learning, even if understood. | Student is *doing* (answering, computing, choosing) on ≥60% of turns. Practice problem count ≥ ~7× worked-example count per topic. Treat student-reported "feeling of learning" as unreliable. |
| 2 | 11 | **Direct Instruction** *(Ch.11 marked "In Progress" in source)* | Active learning is paired with *explicit communication* of the concept, not pure discovery. Cycle: short instruction → student practice → corrective feedback. | Teach the method *first*, then ask. Never substitute discovery questions for instruction on unseen material. Active+Direct > Active+Unguided > Passive+Direct. |
| 3 | 12 | **Deliberate Practice** | Individualised, mindful repetition at the *edge of ability*, with feedback and successive refinement. | Select problems calibrated to *this student's* weakness, not a generic difficulty level. Maximise action–feedback–adjustment cycles per session. Don't let the student self-select within their comfort zone. |
| 4 | 13 | **Mastery Learning** | Demonstrate proficiency on prerequisite topics before advancing — implemented per-topic, per-student on a knowledge graph (not at unit/course grain). | Gate every lesson on prerequisite mastery. Continuously re-estimate the *knowledge frontier*. Hold the same bar for all students; vary the path, not the standard. |
| 5 | 14 | **Minimising Cognitive Load** | Decompose every topic into many small knowledge points (~10× finer than a textbook), open each with a worked example with labelled subgoals, fade scaffolding as proficiency grows (expertise-reversal effect). | Worked example before practice. Subgoal labelling. Dual coding (verbal + visual) throughout. Strip worked examples on review/quiz. One idea per turn. |
| 6 | 15 | **Automaticity** | Lower-level skills must be executed without conscious effort — measured by latency *and* accuracy, not correctness alone — to free working memory for higher-order reasoning. | Track time-to-correct per skill. Block advancement to composite topics until prerequisites are fluent, not just correct. Treat slow-but-correct as *insufficient* automaticity. |
| 7 | 16 | **Layering** | New learning should *exercise* prerequisite/component knowledge — producing retroactive facilitation of old skills and proactive facilitation of new ones. | Move on from mastered prerequisites immediately. Multi-part problems that authentically compose prior skills. Don't sanitise hard prerequisites out of advanced topics. |
| 8 | 17 | **Non-Interference** | Conceptually related items interfere with each other's recall; mitigate by spacing related items apart in time and teaching dissimilar material concurrently. | Detect topic similarity and impose a time gap between confusable lessons. Diversify within-session topic mix. Diagnose error patterns for interference signatures (e.g., 4×8=24 contaminated by 3×8 or 4×6). |
| 9 | 18 | **Spaced Repetition** | Distributed reviews not only restore memory but *consolidate* it, with successful repetitions earning longer next-interval. In connected domains (math), advanced practice gives *trickle-down credit* to encompassed prerequisites (FIRe). | Per-(student,topic) review schedule with intervals that expand on success and shrink on failure. Spacing rate calibrated to student×topic learning speed. Compress due reviews via encompassing topics. |
| 10 | 19 | **Interleaving** | Spreading minimum-effective doses across multiple skills (within and across sessions) instead of blocked drilling — slower-feeling but vastly better retention, transfer, and discrimination. | Compose review sets from a *broad mix* of topics, not single-topic blocks. Include superficially-similar discrimination pairs. Macro-interleave (breadth-first across curriculum) and micro-interleave (within a single task). Warn students it will feel harder — that's the point. |
| 11 | 20 | **Testing Effect / Retrieval Practice** | Retrieving information from memory produces far more durable learning than restudying; best combined with spacing into *spaced retrieval practice*. | Attempt retrieval *first*, hints *later*. Frequent, brief, low-stakes quizzes with immediate feedback. Time-test only after untimed proficiency. Trigger targeted remediation on each missed item. |
| 12 | 21 | **Targeted Remediation** | Automated, granular support on individual topics — often on component skills several steps back. The bar is never lowered; the student gets additional practice to clear the *original* bar. | Diagnose root cause (new concept vs. weak prerequisite) when a student is stuck. Pinpoint key-prerequisite links *at the knowledge-point level*. Let foundational gaps remediate in parallel while course progress continues. Never lower the bar to raise pass rates — add scaffolding instead. |
| 13 | 22 | **Gamification** | Game-like elements (XP, leagues) calibrated to incentivise both quantity *and* quality, robust against students who try to game the system. | Single currency (XP) calibrated so 1 XP ≈ 1 minute of focused productive work. Non-linear awards: bonus / full / partial / zero / *negative* XP. Close cheating loopholes (individualised banks, retake delays). Distinguish % progress from XP earned. |
| 14 | 23 | **Cognitive Strategies Require Technology** *(meta-principle)* | All 13 prior strategies are individually well-known but remain unused in classrooms because manually delivering them to a heterogeneous class is physically infeasible. Technology is the only path. | Automate per-student bookkeeping end-to-end. React in real-time to momentary lapses, not at end-of-unit. Hold the design target as: *"how would you teach if your life depended on it?"* |

**Cross-cutting themes** that recur across all chapters and should constrain every recommendation:

- **Working memory ↔ long-term memory** — the master frame. Learning is *change in long-term memory*; WM is ~4–7 chunks, ~20s decay; nothing else matters until that constraint is honoured.
- **Knowledge graph + prerequisite structure** — the substrate every adaptive decision keys off.
- **Minimum effective dose** — the dominant cycle in Ch.10/11/12/14: short instruction → active practice → feedback → repeat. Long expositions are the failure mode.
- **Desirable difficulty / illusion-of-competence** — students systematically misjudge their own learning (Deslauriers 2019; Ch.10, 18, 19, 20). The system must override student preference for what feels fluent.
- **Expertise reversal** — what scaffolds beginners *retards* experts; fade scaffolding as mastery grows.
- **"Underused in classrooms because it's hard work for teachers"** — recurs in every chapter and is resolved by Ch.23: this is what automation buys.

---

## 2. Gap Analysis — Where the Implementation Stands (May 2026)

The architectural progress since v2 is substantial; the new gaps are concentrated in (a) model capacity vs prompt complexity, (b) the coverage check that gates remediation, and (c) three structural principles still unwired. Each row gives **What exists now** (with file:line citations), **Drift since v2**, and **Gap / risk**.

### 2.1 Principle-by-Principle Audit

| # | Principle | What exists now | Drift since v2 | Gap / risk |
|---|-----------|------------------|----------------|------------|
| 1 | **Active Learning** | `<principle id="active_learning">` at `prompts/anthropic.py:51-62`. `<format_rules>` at `:455-491` enforces "HARD LIMIT: 2-3 sentences, ~40 words, ending in ONE question". `NO_QUESTION_TOOL` validator at `:528-533` regenerates if the tutor writes a question in prose without using a `pose_question` / `pose_inline_question` tool. | Strengthened. The "always end with a question" rule is now mechanically enforced. | "60% of turns the student is DOING" target is uninstrumented — there's no metric counting student-action turns vs tutor-monologue turns. Validator only catches *missing-question-tool*, not passive monologuing. |
| 2 | **Direct Instruction** | `<principle id="direct_instruction">` (`anthropic.py:64-72`); `<principle id="follow_script">` (`:391-432`) requires the tutor to deliver the teacher script for `teach` steps verbatim. Worked-example injection wired via `_build_worked_example_block` (`conversational_tutor.py:4747-4807`). | v2 gap closed. The system now mandates teach-first behaviour, not Socratic discovery on unseen material. | **40-word/turn cap fights worked-example scaffolding directly.** A 3-subgoal worked example does not fit in 40 words. On Flash Lite, the model will compress to a one-liner that fails to scaffold, or spread across multiple turns — breaking the "1 idea → check → next idea" cycle. |
| 3 | **Deliberate Practice** | Adaptive difficulty within session is real: `consecutive_first_try_correct` / `consecutive_wrong` thresholds flip `self.difficulty_level` in `[-1, 0, +1]` (`conversational_tutor.py:10082-10120`), injected via `_build_student_profile_block` (`:3975-`). Per-objective pacing directive (`mastered` / `developing` / `weak`) added via `competency_tracker.student_skills_snapshot` (`:4038`). | Substantial improvement — v2 R19 (Tier 3) delivered. | Difficulty signal is a 3-level int; the prompt never says *what* "+1" concretely means (harder numbers? reverse problem? word-problem framing?). The math injection at `prompts/injections/math.py:80-84` documents a 4-rung difficulty ladder, but runtime passes only the int — the LLM has to infer mapping. |
| 4 | **Mastery Learning** | `check_lesson_prerequisites` (`views.py:80-125`) enforced at `chat_start_session` (`:719`) and `lesson_start` (`:616`). Honours per-course `Course.prerequisites_enabled` opt-out. | v2 R7 closed. | Three real risks: (a) gate **fails open on exception** (`views.py:124`) silently — a skills-table outage means anyone can take anything; (b) gate keys off `LessonPrerequisite`, which is only populated when `SkillExtractionService` has run — older courses are silently un-gated; (c) "mastered" here means `StudentLessonProgress.mastery_level='mastered'` (a flag set on completion), not `StudentSkillMastery.mastery_level ≥ 0.7` as v2 R7 specified — so the gate uses a coarser per-lesson signal, not per-skill. |
| 5 | **Cognitive Load** | Worked-example injection wired (`:4747-4807`); `educational_content.worked_example` is parsed into labelled subgoals. Media catalog + interactive widgets (commit `3fe7cf6`) deliver dual coding. `|||MEDIA:N|||` signal forces visuals when the text references a figure (enforced in `anthropic.py:402-419`). | v2 R14 explicitly closed. Interactive widgets (composite_index_explorer, function_plotter, fraction_decimal_percent) are new surface area not present in v2. | **40-word/turn cap is the headline contradiction.** Worked example with 3 steps → 3 turns minimum; in a 20-minute lesson budget that's costly. Widgets currently ship at Level A (explore-only, per `design/INTERACTIVE_VISUALIZATIONS.md:108`) — the tutor cannot read widget state back, so grading is still on the student's prose, not their interaction. |
| 6 | **Automaticity** | Prompt-level only — `<principle id="automaticity">` (`anthropic.py:113-120`) asks the LLM to "notice if the student is slow." `SkillPracticeLog.time_taken_seconds` exists but call sites pass nothing (`personalization.py:454`; `conversational_tutor.py:10138`, `:10161`). | **No change.** Still built, not wired. | The LLM has no latency signal in context to act on. No timed drills, no fluency assessment. Highest-leverage remaining gap. |
| 7 | **Layering** | `<principle id="layering">` (`anthropic.py:122-130`); [STUDENT PROFILE] block lists "Skills approaching mastery" (`:3997-3998`) so the LLM has the data. | Marginal — data is there, but no explicit "connect today's lesson to skill X" directive. | Layering depends entirely on Gemini Flash Lite's per-turn initiative — and Flash Lite, at temp 0.2, with negative-instruction-heavy prompt, will surface fewer connections than Opus did. |
| 8 | **Non-Interference** | `<principle id="non_interference">` (`anthropic.py:132-138`) tells the LLM to name confusable concepts when relevant. No scheduler. Lesson ordering is still `order_index`. | **No structural change.** | Same as v2: confusable lessons are typically adjacent because that's how teachers structure units. Skill graph has prerequisite edges but no confusability/similarity edges. |
| 9 | **Spaced Repetition** | `StudentSkillMastery.record_attempt()` (`skills_models.py:399`) implements SM-2 with ease_factor, interval_days, next_review_due. **Now called at runtime** from `SkillAssessmentService.record_practice()` (`personalization.py:440`), invoked from `conversational_tutor.py:10138`, `:10161`, `:11202`. `RetrievalService.get_retrieval_questions()` wired into `[WARMUP RETRIEVAL]` (`:3861-3887`). | v2 R2 + R4 closed. | (a) `_infer_quality` (`personalization.py:477-508`) infers SM-2 quality from `(was_correct, hints_used, time_taken)` — but `time_taken` is never passed, so quality is essentially binary → ease_factor adjustments are coarser than SM-2 designed for. (b) Both depend on `Skill.objects.filter(lessons=self.lesson)` being populated; older lessons predate `SkillExtractionService` being wired and silently no-op. (c) No standalone "Daily Review" session (v2 R15) — retrieval lives only inside lesson warmup. |
| 10 | **Interleaving** | `InterleavedPracticeService` wired via `_build_interleaved_practice_block` (`:4811-4862`), `review_ratio=0.2`, injected as `[INTERLEAVED PRACTICE]`. | v2 R6 closed. | Same `lesson_skills` dependency. Block only injects on practice/quiz steps. Cache (`_interleaved_practice_block_cache`) selects review set once at start and never refreshes. No macro-interleaving (breadth-first across curriculum) — the scheduler is still order_index. |
| 11 | **Testing Effect / Retrieval** | `<principle id="testing_effect">` mandates retrieve-first / hint-later. `<feedback_protocol>` (`:348-389`) lays out 5 attempt-based escalation tiers. Exit ticket is structurally retrieve-only (no hints). | v2 R11 directive present; exit ticket compliant. | `<principle id="probe_frequency">` (`:168-217`) hard-bans probing on correct answers ("how did you get there?"). This is a deliberate pilot-driven trade-off (per `CLAUDE.md:43`) but gives up testing-effect retrieval signal on correct turns. Combined with the model swap, lucky correct guesses now pass straight through to the next-harder problem without diagnostic. |
| 12 | **Targeted Remediation** | `RemediationService` called from 2 sites: mid-lesson struggle (`:1893-1898`) and exit-ticket fail (`:11631-11636`, with exit_ticket_score and prerequisite gaps fed into prompt at `:9697-9698`). Remediation floor raised in commit `5d6cbd7` to `max(6, n_failed_eos*3)` exchanges AND all failed concepts re-covered, with safety valve at 15 exchanges. | v2 R5 closed. Remediation floor commit aligns with "don't lower the bar". | **The "concepts re-covered" gate uses `_keyword_concept_coverage_check` (`:10172` calling `:10549`).** The LLM-based `_llm_concept_coverage_check` (`:10587`) is *not* on the per-turn path. Naive keyword matching over-counts coverage (mentioning the term ≠ teaching it). The remediation gate now depends on this check; v2 R12 has gotten *more* load-bearing without getting more reliable. |
| 13 | **Gamification** | XP, levels, streaks surfaced to students: catalog (`templates/tutoring/catalog.html:63`), chat UI (`chat_tutor.html:232-2940`), XP-earned popups, streak banners, level-up banners. `_award_practice_xp` awards 10 + 5 no-hints bonus + streak bonus per correct, 2 effort XP per wrong (`personalization.py:510-544`). Achievements wired (`apps/tutoring/achievements.check_and_award`). | v2 R13 closed. | (a) `hints_used=0` is **hardcoded** at the call site (`conversational_tutor.py:10143`) — the "no-hints bonus" is always awarded regardless of actual hint use. (b) No XP penalties for blowing off tasks (Ch.22 imperative). (c) No leagues / promotion-relegation. (d) Anti-gaming detection absent. |
| 14 | **Meta: Cognitive Strategies Require Technology** | Most bookkeeping is automated: SM-2, XP/streaks, prerequisite gating, difficulty adaptation, retrieval/interleave injection. | Massive improvement. | Two architectural smells: (a) personalization services **silently fail-soft** (try/except returning None) at every call site (`:1893-1904`, `:3847-3859`, `:11630-11639`) — if skills tables are mispopulated, the personalization layer becomes an invisible no-op with no operator alert; (b) `engine_state` is a ~30-field untyped JSON dict (`CLAUDE.md:88`); adding new principle-tracking fields here is risky. |

### 2.2 Drift from v2 — Summary Table

| Component | v2 (Feb 2026) | v3 (May 2026) | Status |
|-----------|----------------|----------------|--------|
| `SkillExtractionService` in pipeline | Built, not in pipeline | Wired (`background_tasks.py:211`, `:1161-1164`) | ✅ |
| `SkillAssessmentService.record_practice` | Built, not called | Wired (3 call sites in `conversational_tutor.py`) | ✅ |
| SM-2 `record_attempt()` | Built, never called | Called via `record_practice` | ✅ |
| `SkillPracticeLog` rows | Never created | Created on every graded turn | ✅ |
| `StudentKnowledgeProfile` XP/streaks | Never updated, no UI | Updated + surfaced in catalog and chat UI | ✅ |
| `RetrievalService` warmup | Not called | Wired into `[WARMUP RETRIEVAL]` block | ✅ |
| `InterleavedPracticeService` | Not called | Wired into `[INTERLEAVED PRACTICE]` block | ✅ |
| `SessionPersonalizationService` | Not called | Called once per session start (`:1758`) | ✅ |
| `RemediationService` | Not called | Called mid-lesson + on exit fail | ✅ |
| `LessonPrerequisite` gating | Not enforced | Enforced (with caveats — see row 4) | ✅ |
| Safety in chat endpoints | Not wired | `RateLimiter` + `ContentSafetyFilter` invoked | ✅ |
| System prompt (Section 4 of v2) | Persona-only | v2 Section-4 ported and strengthened | ✅ |
| Worked example surfacing | Truncated 200-char preview | Structured subgoal injection (`:4747-4807`) | ✅ |
| Difficulty adaptation | None | `consecutive_first_try_correct` / `consecutive_wrong` thresholds | ✅ |
| Phase transitions (R10) | Exchange-count | Hybrid (step-level `min_exchange_floor` + `_should_advance_step`) | 🟡 |
| Concept coverage check | Naive keyword (>0.3 ratio) | **Still naive keyword at runtime**, now gates remediation | ⚠️ regression risk |
| Expertise reversal scaffolding (R16) | None | Prompt-only — relies on LLM reading [STUDENT PROFILE] | 🟡 |
| Daily Review session (R15) | None | Still none | ❌ |
| Automaticity / fluency (R18) | None | Still none | ❌ |
| Non-interference scheduling (R17) | None | Still none | ❌ |
| **NEW: Interactive widgets** | Not in v2 | 3 widget types shipped (Level A explore-only) | ✅ shipped; ⚠️ Level B/C not done |
| **NEW: Teacher exit-ticket override** | Not in v2 | Wired (commits `3d5439e`, `0720329`) | ✅ but no sync back to skill graph |
| **NEW: Remediation floor + concept gate** | Not in v2 | Wired (commit `5d6cbd7`) | ✅ compliant; gate depends on weak coverage check |
| **NEW: Provider-specific prompt builder** | Not in v2 | Anthropic + Gemini + OpenAI builders | ✅ |
| **NEW: Tutoring model = Gemini 3.1 Flash Lite Preview** | Opus 4.7 baseline | Swapped 2026-05-20 (migration `0028`) | ⚠️ **Likely root cause of math regression** |

---

## 3. The Math-Quality Regression — Hypotheses & Triage

The user reports that since the v2 audit, "the AI Tutor has drifted from following the principles of the science of learning … makes more errors especially in Maths … provides a poorer user experience in general." Below are the most plausible causes, ranked by confidence, each backed with file/line evidence.

### H1 — The 2026-05-20 model swap (HIGH confidence)

**Evidence.** Migration `apps/llm/migrations/0028_swap_runtime_to_gemini_3_1_flash_lite.py` (applied **the day before this audit**) replaced production `ModelConfig` for three purposes:

| Purpose | Before (commit `0027`) | After (commit `0028`) |
|---------|------------------------|-----------------------|
| tutoring | Opus 4.7, temp 0.0 | Gemini 3.1 Flash Lite Preview, temp 0.2 |
| judge | Opus 4.7, temp 0.0 | Gemini 3.1 Flash Lite Preview, temp 0.0 |
| regen | Opus 4.7, temp 0.0 | Gemini 3.1 Flash Lite Preview, temp 0.2 |
| judge_fallback | — | Gemini 3.5 Flash, temp 0.0 |

**Why this hits math hardest.** Math correctness is a long-tail capability — arithmetic on awkward numbers, sign handling, fraction arithmetic, BIDMAS, algebra substitution. The math prompt injection at `prompts/injections/math.py:127-133` anticipates exactly these failure modes because models *get them wrong*; a smaller model gets them wrong more often.

**The safety net shares the responder's blind spots.** The LLM arithmetic verifier (`apps/tutoring/llm_arithmetic_verifier.py:162`) calls `self.llm_client.generate`, where `self.llm_client` resolves the `tutoring` purpose at `conversational_tutor.py:1545`. **Verifier and responder are the same Flash Lite model.** When the judge flags a math error, regen (also Flash Lite per migration line 65) tries to fix it — regen-as-recovery breaks down when regen is no stronger than the responder.

**The pre-deploy sanity check was thin.** The migration message claims "quality holds: exit-ticket completion rate matched Opus on L540, figure-handling on L638 clean" — but that's a sample of 2 lessons. The regression metric the user reports ("more math errors") was not measured.

**Action.** Roll back the `tutoring` purpose (and probably `regen`) to Opus 4.7. Judge can stay on Flash Lite; judges don't need the depth. The migration is cleanly reversible (`_restore_opus_stack` at `:87-117`).

### H2 — Prompt complexity vs. model capacity (HIGH)

**Evidence.** The Anthropic-shaped system prompt at `prompts/anthropic.py:34-493` is ~460 lines. The Gemini path dispatches via `get_prompt_builder('google')` to `GeminiTutorPromptBuilder` (`prompts/__init__.py:35`, file `prompts/gemini.py`, 256 lines). Per-session, additional blocks are appended: `<socratic_rules>` (~60 lines, `conversational_tutor.py:5487-5548`), `<math_teaching>` (~90 lines, `:5557-5640`) when `course.is_math`, media catalog, question bank, walkthrough hints. **Effective static system prompt: >5,000 tokens.**

`.claude/skills/gemini-prompting-expert/SKILL.md` is explicit: *"direct task statements beat persona priming"* and *"negative instructions over-index"*. The current prompt is heavy on negative instructions (banned openers, banned probes, banned padding, banned narration). Flash Lite is more likely to *miss* a specific positive rule (e.g., `<principle id="scaffold_consistency">`: copy the numbers verbatim from the bank stem) when 14 named principles + a socratic block + a math block + format rules + media catalog all compete for attention.

**Action.** Trim the system prompt to a leaner Gemini-shaped version (Section 4 below). Move the math-specific protections into a separate, smaller injected block. Cut negative instructions wherever a positive instruction is equivalent.

### H3 — `Course.is_math` heuristic gating math protection (MEDIUM)

**Evidence.** The `<math_teaching>` block (`conversational_tutor.py:5555`) and the `verify_arithmetic_claims` call (`:3169`) are both gated on `self.lesson.unit.course.is_math`. `Course.is_math` (`apps/curriculum/models.py:172-179`) prefers `subject_code == 'mathematics'` but falls back to a `MATH_KEYWORDS` title heuristic: `[math, maths, mathematics, algebra, geometry, calculus]`. **Missing**: arithmetic, fractions, decimals, percentages, trigonometry, probability, statistics. `CLAUDE.md:70` explicitly flags this for backfill: *"Don't propagate these inconsistencies … Backward-compat heuristics (e.g., `Course.is_math` MATH_KEYWORDS fallback) — backfill old data instead."*

**Detection.** Run `Course.objects.filter(subject_code='').exclude(title__iregex=r'(math|maths|mathematics|algebra|geometry|calculus)').values('title')`. Any math-content row that comes back has been running without the math protection layer.

**Action.** Backfill `subject_code` on all math courses. Add the missing keywords to the fallback temporarily.

### H4 — The arithmetic verifier's algebra-variable filter (MEDIUM)

**Evidence.** `llm_arithmetic_verifier._is_real_correction` (`:66-94`) drops corrections whose expression contains an algebraic variable (`_ALGEBRAIC_VAR_RE`). Pattern includes `\d[a-z]\b` (matches `3x`), `\b[a-z]\b\s*=\s*\d` (matches `x = 8`), `\b[a-z]\b\s*[+\-]` (matches `x + 15`). **Effect:** any prose claim involving an algebraic variable is exempt from arithmetic verification.

**Failure mode.** Tutor says "since x = 8, then 3x = 28" — expression `3x` matches `_ALGEBRAIC_VAR_RE`, the correction is dropped as "algebraic-equation", and the student sees `28` instead of `24`. The filter was added per the `:69-76` comment after session 255 false positives — when the verifier was Opus. Trade-off may be miscalibrated for Flash Lite, which substitutes incorrectly more often.

**Action.** Re-tune the algebra filter under Flash Lite, or restrict its scope to expressions that contain *only* algebraic forms (no concrete numerical claim).

### H5 — Concept coverage gate driving premature remediation completion (MEDIUM)

**Evidence.** Commit `5d6cbd7` raised the remediation floor to *also require* all failed concepts to be re-covered before the student can re-attempt the exit ticket. The "covered" check is `_keyword_concept_coverage_check` at `conversational_tutor.py:10172` → `:10549`: extracts 4+-letter words from the combined recent turns, computes ≥30% overlap with concept keywords OR ≥3 matches, then marks `concept['covered'] = True`. The LLM-based check at `:10587` exists but is **not** called from the per-turn path.

**Failure mode.** Tutor mentions "Newton's law" several times while explaining something else → 30% overlap → concept marked covered → safety-valve fires at 15 exchanges → student re-attempts exit ticket without ever having been re-taught. This is the "lowered bar" failure mode Ch.21 explicitly warns against — produced by a coverage check too eager to fire `covered = True`.

**Action.** Move `_llm_concept_coverage_check` onto the per-turn path. Use it as the *primary* signal; keep keyword check as a cheap precondition.

### H6 — Probe-frequency ban erasing diagnostic signal on Flash Lite (LOW-MED)

**Evidence.** `<principle id="probe_frequency">` (`anthropic.py:168-217`) bans probing on correct answers ("how did you get there?"). This was a deliberate pilot trade-off (`CLAUDE.md:43`). The math math-teaching block (`:5577-5598`) splits into 4 branches but the "correct + bare answer" branch confirms and advances without checking whether the student got there by guessing. On Opus the model occasionally probed anyway and caught lucky guesses; Flash Lite with the explicit ban will not, and lucky guesses on the easier questions now flow straight to harder ones.

**Action.** Soften the ban to: *probe on the first correct answer of every new sub-skill; thereafter only probe on suspicious patterns (jump in difficulty, very-fast answers).* The retrieval signal is worth ~one extra question per topic.

---

## 4. The v3 System Prompt — Leaner, Gemini-Compatible

The current prompt (`apps/tutoring/prompts/anthropic.py:34-493`) is a faithful port of v2 Section 4 with pilot-driven additions. **Per H2, it has grown beyond what the new tutoring model can reliably follow.** Below is a slimmer, positive-instruction-first replacement that preserves all 14 principles but cuts cross-cutting contradictions and negative-instruction over-indexing.

The new prompt has three structural changes:

1. **Imperatives are positive ("Do X") rather than negative ("Never Y") wherever logically equivalent** — `.claude/skills/gemini-prompting-expert/SKILL.md` cites direct task statements outperforming persona priming and negative instructions on Gemini-class models.
2. **The 40-word cap is replaced with a 60-word *soft target*** with a documented exception for worked-example steps (which may run to 80 words in a single turn provided the next turn ends with a student action). This resolves the worked-example-vs-cap contradiction in Principle 5.
3. **A single `<context>` block consolidates `[STUDENT PROFILE]` + `[WARMUP RETRIEVAL]` + `[INTERLEAVED PRACTICE]` + `[WORKED EXAMPLE]` + `[SCAFFOLDING LEVEL]`** so the model sees one structured context object per turn instead of scattered injections. Easier for smaller models to parse.

Variables in `{{double_braces}}` are substituted per-session.

```
<system_prompt>

<identity>
You are {{tutor_name}}, a tutor for {{grade_level}} students at
{{institution_name}} ({{locale_context}}). You teach in {{language}}.
You are warm, patient, and direct. You believe every student can succeed.
</identity>

<task>
Teach the student today's lesson by alternating short instruction with active
practice, following the science of learning. Every turn either teaches a small
idea (≤60 words) or asks the student to do something. Your goal is durable
change in the student's long-term memory, not momentary understanding.
</task>

<core_loop>
For every turn, in order:
1. Read the [context] block. Note mastery, current step, difficulty, recent
   accuracy, and any injected retrieval / interleave / worked-example payloads.
2. Decide one of:
   - TEACH: deliver ≤60 words of explanation, then end with one question via
     the pose_question or pose_inline_question tool.
   - PRACTICE: pose one question via the tool. Wait for the student's answer.
   - FEEDBACK: respond to the student's last answer per <feedback_protocol>.
3. End every turn with exactly one student-facing question, posed via a tool.
</core_loop>

<principles>

P1. ACTIVE OVER PASSIVE.
Keep instruction to a minimum effective dose. The student should be doing
something (answering, computing, choosing, explaining) on the majority of
turns. If you find yourself writing more than 60 words, stop and ask.

P2. TEACH FIRST, ASK SECOND.
Explicitly teach the method or concept before asking the student to apply it.
Use questions to check understanding, not to make the student discover unseen
material. Active+Direct beats Active+Unguided.

P3. PRACTICE AT THE EDGE.
Target problems at the boundary of what the student can do. Use the
[difficulty_level] and [mastery_snapshot] in [context] to calibrate. After 3
clean correct answers, level up. After 2 in a row wrong, simplify and rebuild.

P4. MASTERY BEFORE ADVANCEMENT.
Do not advance to a new concept until the student solves the current one
without hints. If a struggle traces to a weak prerequisite, address the
prerequisite first ("Quick detour — I think the tricky part is X. Let's
practice that, then come back."), then return.

P5. ONE IDEA PER TURN.
Present a single idea or step at a time. Short paragraphs. Pair words with
visuals using [SHOW_MEDIA:N] whenever a diagram, table, or widget is
available in the [media_catalog]. Worked-example steps may run to ~80 words
on a single turn, but the next turn must end with a student action.

P6. AUTOMATICITY ON BASICS.
If the student is slow or error-prone on a basic skill (e.g., arithmetic
while learning algebra), flag it and do a two-item fluency drill:
"Negatives are tripping you up — quick: -3 × 5 = ?"

P7. LAYER AND CONNECT.
When introducing new material, explicitly connect to a skill the student
already has. Use names from [mastery_snapshot]:
"Remember plate boundaries from last week? Faults are the visible result."

P8. DISCRIMINATE CONFUSABLE CONCEPTS.
If the topic is easily confused with a related one (area vs perimeter, mean
vs median), state the difference once and give one discrimination example.

P9. RETRIEVE BEFORE HINTING.
On an incorrect answer, your first response is a brief, targeted nudge — not
a hint, not the answer. Only escalate to a hint after a genuine second
attempt. See <feedback_protocol>.

P10. SPACE AND MIX.
Use any items in [warmup_retrieval] or [interleaved_practice] at the
indicated moments. Frame them naturally: "Quick one from last week first…"
Celebrate review success: "You remembered this from a week ago — that's how
it sticks."

P11. FADE SCAFFOLDING WITH MASTERY.
At first encounter (mastery < 0.3): worked example, guided practice, hints
offered. Standard (0.3-0.7): brief instruction, student attempts first.
Review (> 0.7): straight to problems, hints only if asked.

P12. TARGETED REMEDIATION, NEVER LOWER THE BAR.
When the student misses, diagnose: new-concept gap or prerequisite gap?
Give a simpler isolating problem; once they succeed, return to the original.
Show the full solution only if the student gives up — and then have them
restate it and solve a similar problem.

P13. CELEBRATE AND NORMALIZE.
Specific praise for correct work ("Exactly — and you handled the negative
sign right"). Frame difficulty as desirable: "That felt hard because your
brain is building new connections."
</principles>

<feedback_protocol>
On the student's answer:

1. CORRECT, first try: confirm + brief why ("Yes — because the slope is
   the change in y over change in x"). Advance.

2. CORRECT, after struggle: confirm + name what they fixed. Advance.

3. INCORRECT, attempt 1: targeted nudge. No hint, no answer.
   "Almost — check the sign in step 2." Re-pose the same problem.

4. INCORRECT, attempt 2: structured hint. Reference a worked sub-step.
   "Remember, multiplying two negatives gives a positive. Try again."

5. INCORRECT, attempt 3: stronger hint or pivot to prerequisite drill if
   the gap is now visible. "I think the snag is multiplying negatives.
   Quick: -2 × -3 = ?" Then return.

6. STUDENT GIVES UP / final attempt: walk the full solution. Ask the
   student to restate each step in their own words. Then pose one similar
   problem to confirm the recovery.

Never reveal the answer to advance the session. Never lower the bar.
</feedback_protocol>

<session_flow>
Flow blocks adapt to the [step_type] in [context]:

- WARMUP (1-2 turns): use [warmup_retrieval] items if provided; else a quick
  recall question on a prerequisite.
- INTRODUCTION (1-2 turns): name the objective. Connect to prior knowledge
  from [mastery_snapshot]. Preview what the student will be able to do.
- INSTRUCTION (variable): direct teaching with comprehension checks every
  1-2 sentences. Use [worked_example] when provided.
- PRACTICE (variable): student solves with decreasing support. Weave in any
  [interleaved_practice] items naturally.
- WRAPUP (1 turn): summarise. Preview next session.
- EXIT TICKET: no hints, no scaffolding, retrieval only.
- REMEDIATION (when entered): re-cover every failed concept explicitly
  (the system checks this); use a different example than the first pass.
</session_flow>

<tools>
Every question to the student MUST be posed via:
  - pose_question (multi-choice, short-answer, numeric, matching, concept)
    when the question is in [question_bank] for the current step, OR
  - pose_inline_question (multi-choice only) when no bank slot exists.

Do not write a question in prose. Do not narrate ("Let me ask…", "First, I
want to know…"). Ask directly.

To show a media asset or interactive widget from [media_catalog]:
  [SHOW_MEDIA:N]
inserted at the moment the asset is most useful.
</tools>

<context>
{{
  "student": "{{student_name}}",
  "grade_level": "{{grade_level}}",
  "lesson_objective": "{{lesson_objective}}",
  "step_type": "{{step_type}}",
  "step_content": {{step_content_json}},
  "mastery_snapshot": {{mastery_snapshot_json}},
  "difficulty_level": {{difficulty_level}},
  "recent_accuracy_pct": {{recent_accuracy_pct}},
  "scaffolding_level": "{{full|standard|review}}",
  "consecutive_correct": {{consecutive_first_try_correct}},
  "consecutive_wrong": {{consecutive_wrong}},
  "warmup_retrieval": {{warmup_retrieval_json}},
  "interleaved_practice": {{interleaved_practice_json}},
  "worked_example": {{worked_example_json}},
  "remediation_plan": {{remediation_plan_json}},
  "media_catalog": {{media_catalog_json}},
  "question_bank": {{question_bank_json}},
  "xp": {{xp_today}},
  "streak": {{current_streak_days}},
  "is_math": {{is_math_bool}}
}}
</context>

<math_supplement enabled="{{is_math_bool}}">
When teaching math:
- Use LaTeX or clear notation for every expression.
- Copy numbers verbatim from question_bank.stem. Never rephrase the numbers.
- Verify arithmetic before stating a result. If unsure, say "Let me check"
  and recompute step by step.
- For algebra: after every substitution, state both the substitution and
  the simplified result on the same line, e.g., "x = 8, so 3x = 3·8 = 24."
- Sign and fraction errors are the most common student mistakes. After a
  correct answer to a problem with negatives or fractions, ask the student
  to name one tricky step before advancing.
</math_supplement>

<safety>
{{safety_prompt}}
Keep content age-appropriate for {{grade_level}}. If the student seems
distressed or disengaged, pause and check in: "How are you feeling about
this? We can slow down or try a different approach."
</safety>

<output_format>
- 60 words or fewer per turn (80 max on worked-example turns).
- One short paragraph or two short paragraphs.
- End every turn with one question posed via a tool — no exceptions.
- LaTeX for math expressions.
- [SHOW_MEDIA:N] when a relevant asset exists in media_catalog.
- No filler openers ("Great question!", "Let me think…", "Sure!").
</output_format>

</system_prompt>
```

### Why this is shorter

| Section | v2 lines | v3 lines | Cut |
|---------|----------|----------|------|
| `<principle id="...">` blocks (14) | ~140 | 50 (`<principles>`) | Merged into one block with one-line imperatives |
| Negative-instruction blocks (probe_frequency, encouragement_calibration, scaffold_consistency, grade_calibration) | ~120 | folded into `<principles>` + `<math_supplement>` | Cut; over-indexes on Gemini |
| `[STUDENT PROFILE]`, `[WARMUP RETRIEVAL]`, `[INTERLEAVED PRACTICE]`, `[WORKED EXAMPLE]`, `[SCAFFOLDING LEVEL]` (5 separate injections) | ~80 | one `<context>` JSON block | Single structured object |
| `<feedback_protocol>` | ~40 | 25 | Kept the 6 tiers, cut prose around them |
| `<format_rules>` | ~37 | 8 (`<output_format>`) | Cut "HARD LIMIT", "NEVER", and the long banned-opener list |

**Estimated token count**: 1,800–2,200 tokens of static prompt (down from 5,000+). On Flash Lite this is the difference between reliable instruction-following and inconsistent.

---

## 5. Recommendations

Recommendations are grouped into three tiers. Tier 1 directly addresses the math-quality regression and should be done this week.

### Tier 1 — Stop the Math Regression (this week)

**R1 (v3). Roll back the tutoring purpose to Opus 4.7 immediately; keep judge on Flash Lite.**

```
File: apps/llm/migrations/0029_partial_rollback_tutoring_to_opus.py
Action: Reverse migration 0028 for purposes ['tutoring', 'regen'] only.
        Leave 'judge' on gemini-3.1-flash-lite-preview.
        Run apps/tutoring/tests/test_math_eval_integration.py before & after.
```

This is the single highest-leverage action. The migration is reversible and the rollback can be staged behind a `MODEL_ROLLBACK_TUTORING` env var if needed.

**R2 (v3). Replace the system prompt with the leaner v3 version (Section 4 above).**

```
File: apps/tutoring/prompts/anthropic.py + apps/tutoring/prompts/gemini.py
Action: Replace TUTOR_SYSTEM_PROMPT with the Section 4 prompt.
        Migrate per-turn context injections into the single <context> JSON.
        Keep the math_supplement gated on is_math.
        Remove the 40-word HARD LIMIT; use 60-word soft target.
```

This resolves the worked-example-vs-cap contradiction (Principle 5) and reduces prompt complexity to a level a smaller model can follow consistently.

**R3 (v3). Move `_llm_concept_coverage_check` onto the per-turn path.**

```
File: apps/tutoring/conversational_tutor.py:10172
Before: covered = self._keyword_concept_coverage_check(combined_text)
After:  covered = self._keyword_concept_coverage_check(combined_text) and \
                  self._llm_concept_coverage_check(concept, combined_text)
        (or use LLM as primary with keyword as cheap precondition)
```

The keyword check stays as a cheap precondition. The LLM check is the *real* gate. This restores the v2 R12 intent.

**R4 (v3). Backfill `subject_code` on all math courses.**

```
Action: Run a one-off data migration to set Course.subject_code='mathematics'
        on any course whose content is math-content but whose subject_code
        is empty. Drop the MATH_KEYWORDS fallback heuristic.
File: New migration in apps/curriculum/migrations/
```

This closes H3 — the silent gap where math courses skip the math protection layer.

**R5 (v3). Tighten the arithmetic-verifier algebra filter.**

```
File: apps/tutoring/llm_arithmetic_verifier.py:66-94
Action: Restrict _ALGEBRAIC_VAR_RE to drop only expressions that contain
        an algebraic variable AND no concrete numerical claim. Expressions
        like "3x = 28" (concrete numerical result) should still be verified.
```

Closes H4 — algebra-substitution errors are now caught.

### Tier 2 — Wire the Remaining Three Principles

**R6 (v3). Implement automaticity / latency tracking (Principle 6).**

```
File: apps/tutoring/conversational_tutor.py:10138, :10161
Action: Pass time_taken_seconds (compute from turn start to verdict) into
        SkillAssessmentService.record_practice().
File: apps/tutoring/personalization.py:_infer_quality
Action: Use time_taken to drive SM-2 quality more granularly.
File: apps/tutoring/conversational_tutor.py (new method)
Action: Flag skills where the student is slow-but-correct (>2× median).
        Inject a "fluency drill candidate" hint into [context].
```

**R7 (v3). Build the Daily Review session type (Principle 9, v2 R15).**

```
File: New view + template + url under apps/tutoring/
Action: Standalone session that draws 10-15 items from
        RetrievalService.get_due(student) interleaved via
        InterleavedPracticeService. No hints. Records via
        SkillAssessmentService.record_practice() with time_taken.
```

A standalone Daily Review is the canonical Math Academy delivery format and is the natural place to surface XP, streaks, and league standing.

**R8 (v3). Add a confusability edge to the skill graph (Principle 8 non-interference).**

```
File: apps/curriculum/skills_models.py
Action: Add Skill.confusable_with M2M (self-referential).
File: apps/tutoring/services/skill_extraction.py
Action: Optionally have the extraction LLM identify confusable pairs.
File: apps/tutoring/lesson_scheduler.py (new)
Action: When picking the next lesson for a student, penalise candidates
        that share a confusability edge with the most recently completed lesson.
```

A small step but enables true non-interference scheduling.

### Tier 3 — Strengthen What's Already Wired

**R9 (v3). Replace the `hints_used=0` hardcode at gamification call sites.**

```
File: apps/tutoring/conversational_tutor.py:10143
Action: Track hints_used per step in engine_state. Pass the real number.
```

Stops awarding the "no-hints bonus" universally — restores Principle 13's XP-quality calibration.

**R10 (v3). Make the remediation gate alert on coverage-check failure.**

```
File: apps/tutoring/conversational_tutor.py:_should_advance_remediation
Action: When safety-valve fires at 15 exchanges but LLM coverage check
        returns False on any concept, log a structured alert tagged
        'remediation.coverage_check_failed' so we can audit how often
        the safety valve produces unreliable re-attempts.
```

Operator visibility into the failure mode Section 3 H5 describes.

**R11 (v3). Stop silent fail-soft in personalization services.**

```
File: apps/tutoring/conversational_tutor.py (multiple)
Action: Replace try/except returning None with explicit feature-flag.
        If personalization fails to load, log a structured warning AND
        surface a banner to the teacher dashboard. Don't degrade silently.
```

Closes the architectural smell flagged in Principle 14.

**R12 (v3). Promote interactive widgets to Level B (state-aware grading).**

```
File: apps/tutoring/views.py:chat_respond
Action: Accept widget state from the frontend. Forward it into the LLM
        context as [widget_state].
File: apps/tutoring/prompts/*.py
Action: Add a widget-state-grading directive in the math_supplement.
        Allow the tutor to grade "set income=$8k, life_exp=45 — which band?"
        against the widget's current state.
```

Per `design/INTERACTIVE_VISUALIZATIONS.md:108`, widgets currently ship at Level A (explore-only). Level B unlocks active-learning compliance for parametric concepts.

**R13 (v3). Sync teacher exit-ticket overrides back into the skill graph.**

```
File: apps/dashboard/views.py (the teacher override endpoint)
Action: When a teacher flips an exit ticket from FAIL to PASS, also call
        SkillAssessmentService.record_practice(was_correct=True) for every
        skill on every failed item. This keeps StudentSkillMastery in sync
        with the source-of-truth teacher decision.
```

### Tier 4 — Larger Structural Items

**R14 (v3). Implement mastery-based phase transitions (still open from v2 R10).**

The current step-level `min_exchange_floor` is a hybrid — better than v2's pure exchange count but not the v2-target of "transition when accuracy ≥ 70%" / "advance step on demonstrated mastery". A full audit of `_should_advance_step` against the v2 R10 spec is warranted.

**R15 (v3). Build the Knowledge Frontier query.**

`Skill.prerequisites` + `StudentSkillMastery.mastery_level` is enough data to compute `knowledge_frontier(student)` per Principle 4 / Ch.13. Expose this as a service and use it to (a) recommend "what to learn next" instead of `order_index`, (b) surface "unblocked" alternatives when the student fails a lesson, (c) drive the Daily Review picker.

**R16 (v3). Implement XP penalties for blowing off tasks (Principle 13).**

Per Ch.22: *"Without XP penalties, the XP hacker strategy can be exploited indefinitely."* Detect patterns of fast random-clicking on MCQs, repeated immediate-quit-after-start, etc. Apply negative XP. Surface to the student honestly.

---

## 6. Per-Phase LLM Context Block (v3) — Single Structured Object

The v2 audit specified five separate injection blocks. The v3 prompt consolidates them into one `<context>` JSON object. Below is the producer-side contract.

```python
# apps/tutoring/conversational_tutor.py — replace scattered context-building methods
# with a single _build_v3_context() that produces this object:

context = {
    "student": {
        "name": student.first_name,
        "grade_level": student.grade_level,
        "language": session.language or "English",
    },
    "lesson": {
        "objective": lesson.objective,
        "course_is_math": lesson.unit.course.is_math,
        "step_type": current_step.step_type,            # teach|practice|quiz|worked_example|exit
        "step_index": engine_state["current_step_index"],
        "step_total": len(lesson.steps),
    },
    "step_content": current_step.educational_content,   # full structured payload
    "mastery_snapshot": [
        {"skill": s.name, "mastery": m.mastery_level,
         "state": m.state, "days_since": days_since_practice(s, m)}
        for s, m in mastery_for_lesson(student, lesson)
    ],
    "difficulty_level": engine_state["difficulty_level"],         # -1, 0, +1
    "recent_accuracy_pct": engine_state["recent_accuracy_pct"],
    "consecutive_first_try_correct": engine_state["consecutive_first_try_correct"],
    "consecutive_wrong": engine_state["consecutive_wrong"],
    "scaffolding_level": derive_scaffolding(mastery_snapshot),    # full|standard|review
    "warmup_retrieval": [
        {"q": q.question, "skill": q.skill_name,
         "days_ago": q.days_since_review,
         "expected_answer_TUTOR_ONLY": q.expected_answer}
        for q in retrieval_service.get_warmup(student, n=2)
    ] if step_type == "warmup" else [],
    "interleaved_practice": [
        {"q": q.question, "skill": q.skill_name,
         "expected_answer_TUTOR_ONLY": q.expected_answer}
        for q in interleaved_service.get_review(student, lesson, ratio=0.2)
    ] if step_type in ("practice", "quiz") else [],
    "worked_example": (
        current_step.educational_content.get("worked_example")
        if step_type in ("teach", "worked_example") else None
    ),
    "remediation_plan": (
        {"weak_skills": [...], "prerequisite_gaps": [...],
         "concepts_to_cover": [c for c in failed_concepts if not c["covered"]]}
        if session_state == REMEDIATION else None
    ),
    "media_catalog": media_catalog_with_widgets(lesson),
    "question_bank": question_bank_for_step(current_step),
    "xp_today": profile.xp_earned_today,
    "streak_days": profile.current_streak_days,
    "is_math": lesson.unit.course.is_math,
}
```

**The single structured object is easier for a smaller model to parse than five XML-tagged blocks. It also reduces prompt token count by ~15% from de-duplication.**

---

## 7. Integration Wiring Checklist (Tier 1, Tier 2)

Concrete code changes needed to deliver this audit's recommendations:

### R1 (v3) — Model rollback
```
File:   apps/llm/migrations/0029_partial_rollback_tutoring_to_opus.py
Action: New migration that reverses 0028 for 'tutoring' and 'regen' purposes only.
Test:   apps/tutoring/tests/test_math_eval_integration.py before & after.
```

### R2 (v3) — System prompt v3
```
File:   apps/tutoring/prompts/anthropic.py
Action: Replace TUTOR_SYSTEM_PROMPT with the Section 4 prompt.
File:   apps/tutoring/prompts/gemini.py
Action: Mirror the same prompt; ensure provider-specific formatting.
File:   apps/tutoring/conversational_tutor.py
Action: Replace _build_*_block() methods with _build_v3_context().
        Pass single <context> JSON, not scattered blocks.
```

### R3 (v3) — LLM concept coverage
```
File:   apps/tutoring/conversational_tutor.py:10172
Action: Add _llm_concept_coverage_check on the per-turn path.
        Keyword stays as cheap precondition.
Test:   New test_concept_coverage_llm_gate.py.
```

### R4 (v3) — Course subject_code backfill
```
File:   apps/curriculum/migrations/00XX_backfill_math_subject_code.py
Action: Set subject_code='mathematics' on every Course where content is math.
        Drop MATH_KEYWORDS fallback in apps/curriculum/models.py.
```

### R5 (v3) — Tighter algebra filter
```
File:   apps/tutoring/llm_arithmetic_verifier.py:66-94
Action: Restrict _ALGEBRAIC_VAR_RE drop to expressions without concrete numbers.
Test:   New test_algebra_filter_keeps_concrete.py with 3x=28 case.
```

### R6 (v3) — Automaticity
```
File:   apps/tutoring/conversational_tutor.py:10138, :10161
Action: Pass time_taken_seconds from turn-start timestamp.
File:   apps/tutoring/personalization.py:_infer_quality
Action: Branch on time_taken for SM-2 quality scaling.
```

### R7 (v3) — Daily Review
```
File:   apps/tutoring/views_review.py (new)
File:   templates/tutoring/daily_review.html (new)
File:   apps/tutoring/urls.py
Action: New endpoint /tutoring/review/ that runs a 10-15 item interleaved review.
```

### R8 (v3) — Confusability edge
```
File:   apps/curriculum/skills_models.py
Action: Add Skill.confusable_with = M2M(self, symmetrical=True, blank=True)
File:   apps/tutoring/lesson_scheduler.py (new)
Action: Penalise next-lesson candidates that share a confusability edge.
```

### R9 (v3) — Real hints_used
```
File:   apps/tutoring/conversational_tutor.py:10143
Action: Replace hints_used=0 with engine_state.get('hints_used_this_step', 0).
```

### R10 (v3) — Remediation coverage alerts
```
File:   apps/tutoring/conversational_tutor.py:_should_advance_remediation
Action: structured-log on safety-valve fire with LLM coverage = False.
```

### R11 (v3) — No silent fail-soft
```
File:   apps/tutoring/conversational_tutor.py (multiple try/except blocks)
Action: Replace bare except with explicit logger.exception + dashboard banner.
```

---

## 8. Quick-Start Priority Order

For the team to begin immediately, in order of impact:

| # | Recommendation | Effort | Why first |
|---|----------------|--------|-----------|
| 1 | **R1 (v3)** — Roll back tutoring to Opus 4.7 | 1 day | Largest single root cause of the regression; reversible. |
| 2 | **R2 (v3)** — Slim system prompt | 2 days | Cuts prompt tokens >50%; restores worked-example scaffolding. |
| 3 | **R3 (v3)** — LLM concept coverage on per-turn path | 1 day | Stops the remediation gate from clearing on keyword over-count. |
| 4 | **R4 (v3)** — `subject_code` backfill | 0.5 day | Closes silent math-protection gap for older courses. |
| 5 | **R5 (v3)** — Tighter algebra filter | 0.5 day | Restores arithmetic verification on algebra-substitution claims. |
| 6 | **R9 (v3)** — Real `hints_used` | 0.5 day | Restores XP-quality calibration. |
| 7 | **R11 (v3)** — No silent fail-soft | 1 day | Operator visibility for everything below. |
| 8 | **R6 (v3)** — Automaticity time-tracking | 2 days | Highest principle still unwired; unblocks fluency drills. |
| 9 | **R7 (v3)** — Daily Review session | 5 days | Canonical Math Academy delivery format; surfaces SM-2 to students. |
| 10 | **R8 (v3)** — Confusability edge + scheduler | 5 days | Closes Principle 8 (non-interference). |
| 11 | **R10, R12, R13** — Tier 3 strengthening | 3 days each | Targeted improvements on already-wired surfaces. |
| 12 | **R14, R15, R16** — Tier 4 structural | scoped per-item | Larger items; design before code. |

After R1–R5 (which can land within a week), the regression should be measurably reversed. The remaining recommendations close the principle-by-principle gaps that v2 left open and that the May 2026 work did not address (automaticity, non-interference, daily review, mastery-based transitions, knowledge frontier).

---

## Appendix A — How to Verify the Math Regression Is Fixed

A repeatable check the team can run after R1 + R2 land:

1. `apps/tutoring/tests/test_math_eval_integration.py` — run the existing math eval suite. Compare error rate pre/post.
2. `apps/tutoring/management/commands/audit_math_false_positives.py` — run against the last 7 days of sessions, then compare against the same window on Opus.
3. `apps/tutoring/management/commands/verify_math_regression.py` — confirm previously-caught math bugs still trip on the rolled-back stack.
4. Sample 20 random math sessions per week from the post-rollback period; have a teacher score them on (a) arithmetic accuracy, (b) explanation correctness, (c) appropriate scaffolding. Compare to a pre-2026-05-20 baseline.

If error rate is back within 10% of the pre-swap baseline, R1 + R2 have done their job. If not, escalate to H4 (algebra filter) and H5 (coverage gate) — R5 + R3 — and re-measure.

---

## Appendix B — Source Material Citations

All principles in Section 1 are distilled from `/Users/roy.manzi/WorldBank/AfricaTutor/References/The-Math-Academy-Way.md`, **Chapters 10–23 only** (lines 3039–5081). Chapter mapping:

| # | Chapter | Heading | Lines |
|---|---------|---------|-------|
| 1 | 10 | Active Learning | 3039–3282 |
| 2 | 11 | Direct Instruction *(In Progress)* | 3265–3282 |
| 3 | 12 | Deliberate Practice | 3283–3508 |
| 4 | 13 | Mastery Learning | 3509–3624 |
| 5 | 14 | Minimizing Cognitive Load | 3625–3693 |
| 6 | 15 | Developing Automaticity | 3694–3953 |
| 7 | 16 | Layering | 3954–4017 |
| 8 | 17 | Non-Interference | 4018–4055 |
| 9 | 18 | Spaced Repetition | 4056–4308 |
| 10 | 19 | Interleaving | 4309–4530 |
| 11 | 20 | Testing Effect (Retrieval Practice) | 4531–4724 |
| 12 | 21 | Targeted Remediation | 4725–4809 |
| 13 | 22 | Gamification | 4810–4928 |
| 14 | 23 | Leveraging Cognitive Learning Strategies Requires Technology | 4929–5081 |

No claims in this audit draw on outside research; all are grounded in those chapters of *The Math Academy Way*.
