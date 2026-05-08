# Recent platform updates

`[STAFF]` Auto-generated from git history. Window: last 30 days · regenerated on every deploy. Last refresh: 2026-05-08.

The help assistant indexes this file so it knows what shipped recently. If a doc elsewhere disagrees with an entry here, this file wins for time-sensitive answers.

## 2026 · week 18

- **2026-05-08** · `5838317` — Align Exchanges metric across pages + drop redundant Exit Quiz column
- **2026-05-07** · `c0e2496` — Class readiness = avg of avg competency + show all lessons (not just published)
- **2026-05-07** · `37900e5` — Pulumi: enable HTTP-concurrency autoscaling on the Container App
- **2026-05-07** · `96ad27d` — Class detail: include platform-wide courses for teachers
- **2026-05-07** · `c83e339` — Summative student-scores table + class competency cleanup + 1/0 lessons fix
- **2026-05-07** · `d9c8b0f` — Class competency: 1 row per lesson + collapse to single "Average competency"
- **2026-05-07** · `a58e123` — Student detail: drop misleading "Completed" stat + Competency Breakdown widget
- **2026-05-07** · `88ce06e` — Lock teachers out of editing + image regen with vision context + flagged page safety-only
- **2026-05-07** · `7abbefb` — Safety judge + flagged dashboard scoped to safety-only + UI cleanup
- **2026-05-07** · `4e3a8e3` — Seed Sonnet REGEN ModelConfig + drop tutor temperature to 0.2
- **2026-05-07** · `d6d73f2` — Regen ensemble + ISSUE_VERDICT_MISMATCH
- **2026-05-07** · `278579c` — Drop "Enabling Objectives" section from live monitor chat history
- **2026-05-07** · `87fee60` — B.1: move Help / report-issue button into the chat header
- **2026-05-07** · `ab23ad2` — UI cleanup — drop EO surface area, dedupe student rows, hide judge tags
- **2026-05-07** · `560f2c6` — Split monolithic judge into per-domain inspectors + add tutor-side checks
- **2026-05-06** · `0a7d59e` — Fix: media catalog was silently dropping every generated figure
- **2026-05-06** · `7f98ebc` — Disable praise stripping — post-process rewrite kept leaking stock phrases
- **2026-05-06** · `39b49de` — Tier 1.6: surface media catalog state + ungate figure_facts + nudge LLM
- **2026-05-06** · `8fdb9fc` — Tier 1.5: exit-ticket triggers + drop matching + programmatic RULE_1 + cap=10
- **2026-05-06** · `6a9bacf` — Tier 1 — non-MCQ renderer + RULE_1 math gate + figure-regen + distractor safety
- **2026-05-06** · `047c6df` — Polish: shown-questions dedup + figure-signal rule + MCQ value-form + chat math kbd
- **2026-05-06** · `9f4bc0a` — Fix bank visibility + drop media inference + kill leaked opener phrases
- **2026-05-06** · `9740014` — Lazy media + stop drip-feeding "show your working"
- **2026-05-06** · `c724724` — Fix tutor cascade + EO drop + bank grader + add per-turn logs
- **2026-05-05** · `93c6212` — Admin-initiated staff password reset (show + email) + force-change flow
- **2026-05-05** · `a2e8ea9` — Bump file share quota to 100 GiB + plan Azure Blob migration
- **2026-05-05** · `a8002f3` — Universal vision + bank tool + judge across subjects + 2 UX fixes
- **2026-05-05** · `838a001` — Hotfix: strip leaked pose_question(slot=N) syntax from text blocks
- **2026-05-05** · `d80c811` — Vision: tutor sees the step's figure (not just metadata)
- **2026-05-05** · `cc43c15` — Step advancement: deterministic-only fast-path + tighter criteria + vision payload
- **2026-05-05** · `7640779` — Batched exit-ticket grading + per-step-type caps + correct-answer fast-path
- **2026-05-05** · `6524800` — Recap (warmup) pulls only numeric exit-ticket types — no diagram dependence
- **2026-05-05** · `dde8462` — Tutor → Sonnet 4 + structural messages-array refactor + anti-loop directives
- **2026-05-05** · `a36a42d` — Switch tutoring → OpenAI gpt-4o (3.3× faster than Opus 4.7)
- **2026-05-05** · `422c6bc` — Fix bank-scope holes: skip slot 0 for TEACH, prereq-lesson recap, edit-with-context regen
- **2026-05-05** · `bee85ef` — Tutor cleanup: stop editing messages, judges→Sonnet, EO-first bank, drop PROBE/ARTIFACT/GENERATE
- **2026-05-05** · `0c7826c` — Pose-question Anthropic tool — structural fix for LLM authoring
- **2026-05-05** · `bbc9783` — Remove force-inject of bank questions on persistent authoring_violation
- **2026-05-05** · `48b0821` — HOTFIX: Opus 4.7 compatibility — drop temperature, bump instructor max_tokens
- **2026-05-04** · `149609f` — Force-inject + per-turn final_reminder + tutoring → Opus 4.7
- **2026-05-04** · `82f03cd` — Consolidate math_teaching: 8 rules → 4 orthogonal ones
- **2026-05-04** · `85f2d09` — Math tutor: don't reveal the answer until 5 wrong attempts
- **2026-05-04** · `c6dc991` — Bank-only enforcement: extend to all phases + include all lesson steps
- **2026-05-04** · `85de6b5` — Bank-only math + verifier-driven regen + EO-targeted question signal
- **2026-05-04** · `7fe123a` — Lesson competency: latest-attempt semantics, not historical best
- **2026-05-04** · `28df9ff` — Chat header: surface the kebab menu on desktop so Restart is reachable

## 2026 · week 17

- **2026-05-02** · `8aa8de5` — Fix silent exit-ticket regen crash + better runtime logging
- **2026-05-02** · `3d54ae0` — Course detail: fix multi-line {# #} comments rendering as visible text
- **2026-05-02** · `3b99bd2` — Exit-ticket gen: format-mix retry to break short_numeric anchoring
- **2026-05-02** · `cb587f9` — Math exit-ticket prompt: name the failure modes + force format mix
- **2026-05-02** · `904e1bc` — Pilot permissions polish: hide regenerate from teachers, expose weekly assigns + back-to-dashboard
- **2026-05-02** · `b17d809` — Feedback reports: super-admin only
- **2026-05-02** · `d2efb75` — Pilot permissions + step_edit 404 fix
- **2026-05-02** · `279c915` — LLM-based verifiers: arithmetic + EO snap (replacing regex layers)
- **2026-05-02** · `2a276bf` — Tests: regression guards for fake-scaffolding judge fix
- **2026-05-02** · `85e7289` — No-authoring: tighten judge + bank block to catch fake-scaffolding
- **2026-05-02** · `65ce5a5` — Math tutor: forbid premises that violate the lesson rule + drop class mastery widget
- **2026-05-02** · `c4c31c5` — Promote + demote: per-student buttons, bulk demote on class page
- **2026-05-02** · `95e6a76` — Class list: collapse to compact grade tiles, drop inline promote
- **2026-05-02** · `2fc3f6e` — build_help_index: enable --with-source on every deploy
- **2026-05-02** · `93bd448` — Sidebar simplification + assistant context doc + source-code indexer
- **2026-05-02** · `6dea650` — Help assistant: lock catalog to docs + navigation only
- **2026-05-02** · `e0bae2c` — Dedicated class pages: courses + class mastery + scoped roster
- **2026-05-02** · `d798983` — Class competency: roster scoped to course's grade level
- **2026-05-02** · `5e29a2a` — Student detail: untaken lessons show UN, not BE
- **2026-05-02** · `e069be6` — Sessions chart: fix bars not varying — flex alignment was collapsing barWrap
- **2026-05-02** · `9c2dd08` — Diagnostic: trace EO source at exit-ticket gen + dump-state command
- **2026-05-02** · `f2c726a` — Dashboard home: drop Course Progress, promote Sessions chart with readable Y-axis
- **2026-05-02** · `ac7c942` — Lesson detail: stop mislabelling enabling objectives as "Terminal Objectives"
- **2026-05-02** · `a70034d` — Math exit-ticket prompt: ban premises that violate the lesson rule
- **2026-05-02** · `eeee0f7` — Add MIT LICENSE file
- **2026-05-02** · `578f32c` — EO expansion: fix ImportError that made it silently fail every time
- **2026-05-02** · `8ab68f3` — Exit-ticket EO tagging: fix prompt + validator drop-everything bug
- **2026-05-02** · `d21f7f1` — Lesson regen: live banner + auto-reload; admin back-to-dashboard link
- **2026-05-02** · `1f2ca0a` — Lesson regenerate: scope flags so exit-ticket-only is one click
- **2026-05-02** · `4c094d7` — Help page: make public + link from landing footer
- **2026-05-02** · `725b651` — P4 + P5: EO-driven remediation walkthrough + re-quiz with weighted sampling
- **2026-05-02** · `02e0244` — P3: deterministic grading for ALL bank-pulled questions
- **2026-05-02** · `986109d` — P2d + P2e: typed templates wired into content-gen + math prompt
- **2026-05-02** · `e259fc5` — P2c: validate_template_typed for typed parametric templates

