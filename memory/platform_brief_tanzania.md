# AI Tutor Platform — Architecture & Feature Reference

*Prepared as reference material for responses to the Tanzania meeting follow-up questions (open source, content validation, curriculum alignment, competencies, assessment, offline solutions, LMS integration).*

## 1. Platform Overview

**Purpose:** AI-powered personalized tutoring platform grounded in the science of learning. Delivers curriculum-aligned, one-on-one adaptive instruction at scale, with mandatory teacher oversight.

**Deployment status (Seychelles pilot):** Live at `aitutor-pixel-app.niceground-67d5237f.centralus.azurecontainerapps.io`. Running on Azure Container Apps (Dedicated D4: 4 vCPU, 8 GB RAM) provisioned via Pulumi IaC. 4,393 curriculum objects seeded; 470 vectors (175 curriculum + 295 teaching materials) indexed. Gunicorn 4 workers/4 threads, 120s timeout. CI/CD via GitHub Actions.

**Two user interfaces:**
- **Student chat** — conversational tutoring, exit ticket quiz, media artifact panel, voice input/TTS, accessibility features.
- **Teacher dashboard** — curriculum upload, lesson review/edit, live session monitor, safety/flagged chats, class management, invitation flows, exit ticket review with teacher grade override.

## 2. Technical Stack

| Layer | Technology |
|---|---|
| Web framework | Django (Python) |
| Database | PostgreSQL (prod) / SQLite (dev) |
| Vector store | ChromaDB, per-institution collection |
| Embeddings | `sentence-transformers all-MiniLM-L6-v2` (local, offline) or OpenAI (configurable via `EMBEDDING_BACKEND`) |
| LLMs (pluggable) | Anthropic Claude, OpenAI, Google Gemini — provider-agnostic client abstraction |
| Media/image gen | Gemini 3.1 Flash Image / Gemini 3 Pro Image with grounded web + image search |
| Audio | faster-whisper (STT), configurable TTS backend |
| Content safety | In-house `ContentSafetyFilter` with audit log + rate limiter |
| IaC | Pulumi (TypeScript) → Azure Container Apps, ACR, PostgreSQL Flexible Server, Storage Account (file share for media) |
| Auth | Django auth with multi-tenant Institution/Membership model, student/teacher/super-admin roles |

**Note on dependencies:** core platform depends on open-source components (Django, ChromaDB, sentence-transformers, whisper, PyTorch, Celery). LLM provider is swappable — the abstraction layer supports Anthropic, OpenAI, Google today; adding Gemma 4 or any OpenAI-compatible endpoint requires only a new client class.

## 3. Core Architectural Concepts

### 3.1 Curriculum → Lesson Pipeline

Four-stage pipeline for converting uploaded curriculum documents into structured lessons:

1. **Extract** — PDF/DOCX parsed, images extracted, figures detected and captioned.
2. **Parse** — LLM extracts unit/lesson structure, enabling objectives, and content tiers.
3. **Create lessons** — lesson shells generated with metadata (grade level, subject, duration, EOs).
4. **Generate content** — each lesson gets 5–8 ordered steps (teach / worked_example / practice / quiz / summary types) plus a 10-question exit ticket with concept tagging. Retry loop (3 attempts) with LLM correction prompt handles JSON failures.

Curriculum and teaching materials are indexed into ChromaDB with `institution_id` scope (including a `GLOBAL_INSTITUTION_ID = 0` for platform-wide content accessible to all schools).

### 3.2 Lesson Structure (5E-Aligned Step Model)

Each lesson is a sequence of steps, each tagged with a 5E phase (Engage / Explore / Explain / Elaborate / Evaluate) and a step type:
- **teach** — content delivery
- **worked_example** — fully worked problem
- **practice / quiz** — student attempts with hint ladder
- **summary** — recap

Steps carry: `teacher_script`, `question`, `enabling_objective`, `concept_tag`, `hint_ladder`, `common_mistakes`, `key_vocabulary`, `media` (linked images/figures). Steps are the **single source of truth** for lesson flow — there is no separate phase-transition state machine.

### 3.3 Tutor Engine (`ConversationalTutor`)

The engine is a stateful orchestrator with three session states: `TUTORING`, `EXIT_TICKET`, `COMPLETED`. Each student message triggers:

1. **Load state** from `engine_state` JSON (exchange counts, covered EOs, cognitive load, difficulty level, media shown, remediation flags)
2. **Retrieve** relevant curriculum chunks from ChromaDB via RAG
3. **Build prompt** with: system instructions, current step directive, 5E phase instructions, student profile (skill profile + struggles/strengths), enabling-objective coverage, cognitive-load adjustment block, difficulty-signal block (ZPD), teacher guidance block, worked-example context
4. **Generate response** via LLM — may emit inline signals (`|||MEDIA:N|||` for image selection from catalog, `|||GENERATE:category:description|||` for on-the-fly image gen, `|||ARTIFACT:html|||` for sandboxed iframe charts/tables)
5. **Evaluate step** — merged LLM evaluator returns answer correctness + step completion in one call, step-type-specific prompts
6. **Advance / remediate** — if step complete, advance index with concept-boundary gating; if exit ticket fails, enter remediation
7. **Save state**, persist turn

### 3.4 Exit Ticket & Remediation

- Fixed 10-question exit ticket per lesson, randomized from a larger question bank, concept-balanced.
- Question types: MCQ, fill-in-blank, matching, short-answer, data-interpretation.
- Passing threshold: **8/10**. LLM-based grading for non-MCQ types.
- On failure: targeted remediation loop focused on the failed **enabling objectives** (not just replaying step 1). Remediation walks through relevant steps until either all failed concepts are re-covered (keyword signal) and minimum exchange floor met (≥3 per failed EO, hard floor of 6), or safety valve at 15 exchanges.
- Teachers can **override** any question's correct/incorrect mark in the review UI; the override recomputes score, promotes session to COMPLETED if it flips to passing, and updates `mastery_achieved`.

### 3.5 Adaptive Instruction Signals

Two independent signals adapt the tutor's behavior on the next turn:

1. **Cognitive load (0.0–1.0)** — derived from correctness streaks, confusion patterns, consecutive wrongs. HIGH (>0.7): simpler language, smaller steps, immediate hints, worked examples before practice. LOW (<0.3): skip scaffolding, harder variants, request reasoning.
2. **Difficulty level (-2 to +2)** — explicit student signal via "Too hard?" / "Too easy?" buttons. Negative triggers expert→novice slowdown; positive triggers expertise-reversal (skip intermediate explanations, harder problems, no follow-ups).

Both flow into a `[DIFFICULTY ADJUSTMENT]` + `[COGNITIVE LOAD]` block inserted into the system prompt every turn.

### 3.6 Competency & Skill Tracking

Enabling objectives are modeled as first-class `Skill` records. Every practice attempt and exit-ticket response records to the `SkillAssessment` service with `was_correct`, `hints_used`, `practice_type` (initial/remediation), and `lesson_step`. Mastery is computed per-EO from aggregate practice. The **class readiness report** aggregates this across a class to surface weak EOs, stuck students, and ready-to-advance cohorts.

### 3.7 Teacher Oversight

- **Live Monitor** — per-lesson real-time view: active/idle/completed/struggling counts, phase, step progress (X/Y), exchanges, active engagement duration, exit score (PASS/FAIL), view chat, review exit ticket.
- **Monitor AI** — debounced (every 5 min) auto-scan of active sessions; injects guidance into the tutor prompt for students with cognitive load >0.7. Teachers can also manually send guidance.
- **Flagged Chats** — safety filter flags (profanity, PII, self-harm, off-topic) with 2-strike auto-suspend escalation and teacher review queue.
- **Editable content** — teachers edit every AI-generated artifact: step scripts, questions, hint ladders, media, exit ticket questions, explanations, difficulty tags.
- **Curriculum management** — per-institution or platform-wide uploads, material tiering, per-grade filtering, re-parse controls, knowledge-base reindex.

### 3.8 Accessibility & Localization

- Voice input (STT) + TTS read-aloud for all tutor responses
- Artifact panel for images on desktop (split-pane) with thumbnail strip; inline images on mobile
- Adjustable difficulty via student signal
- Language adaptation via model choice (base models support 100+ languages; grade-calibrated delivery)
- Sandboxed HTML artifacts for data tables, comparison charts, SVG diagrams (incl. mandatory gridlines for math plots)

## 4. Direct Responses to the Seven Technical Topics

### 4.1 Open Source
The platform is **built on an open-source foundation** (Django, ChromaDB, sentence-transformers, whisper, PostgreSQL, Pulumi). The proprietary application code is not currently open-sourced, but:
- LLM provider is **pluggable** — adding Gemma 4 (open-weights), Qwen, or Llama via an OpenAI-compatible endpoint requires only a new client class in the provider abstraction.
- Embeddings already run **locally** (sentence-transformers `all-MiniLM-L6-v2`), fully offline, no API dependency. 470 vectors indexed today prove the viability of the local pipeline.
- Could migrate to fully-open stack: Gemma 4 for LLM + sentence-transformers for embeddings + ChromaDB for vector store. Zero proprietary dependency.

### 4.2 Content Validation
Multi-layer validation:
1. **Curriculum grounding (RAG)** — every generated step and exit ticket question is generated against retrieved curriculum chunks, not from model pretraining alone.
2. **Teacher review & edit** — nothing deploys to students without teacher approval. Every step, question, explanation, and media asset is editable.
3. **Subject-specific verifiers** — math responses go through `verify_calculations` which parses and re-computes every `a op b = c` chain in the response; incorrect arithmetic is auto-corrected before delivery.
4. **Safety filter** — pre-LLM and post-LLM content safety check on every message, with audit log.
5. **Retry + JSON repair** — content generation has a 3-attempt retry loop with LLM self-correction for structural failures.
6. **Confidence routing** — exit-ticket grading for free-text uses LLM evaluation; low-confidence cases can be flagged to teacher override (the override UI is live).

### 4.3 Curriculum Alignment
- **Ingestion pipeline** parses uploaded national curriculum PDFs/DOCX, extracts units/lessons/enabling objectives, and indexes them into a per-institution ChromaDB collection.
- **Teaching materials** (textbooks, worksheets, past exams) are uploaded separately and indexed alongside — they supplement the curriculum without replacing it. Currently 175 curriculum + 295 teaching-material vectors for the Seychelles pilot.
- **Lessons are generated *from* the curriculum** — a lesson is not a free-form AI creation; its steps, enabling objectives, and exit-ticket questions are grounded in retrieved curriculum content with explicit EO tagging.
- **Institution scope** — `institution_id` segregates curriculum per school/country. A `GLOBAL_INSTITUTION_ID = 0` allows platform-wide content (e.g., shared across a region).
- **Teacher validation step** after generation closes the alignment loop.

### 4.4 Competencies
- **Enabling objectives** are first-class `Skill` records with mastery levels.
- Every student action (practice attempt, exit-ticket answer, remediation attempt) records a `SkillAssessment` event with correctness, hints used, and context.
- **Mastery** is computed per-EO from aggregate assessments, not from a single test.
- **Competency report** reads EOs directly from the curriculum (not inferred from chat), shows per-student and per-class proficiency, identifies weak objectives, and categorizes students (BE/AE/ME/EE tiers).
- Aligned with **competency-based education** — the platform tracks what students *can do*, not just what they've been exposed to. Remediation is EO-targeted, not generic.

### 4.5 Assessment
- **Diagnostic** — entry-point readiness check (prerequisite gating) before a new lesson.
- **Formative (in-session)** — merged LLM evaluator on each practice/quiz step; correctness + step-completion in one call with step-type-specific prompts.
- **Summative (exit ticket)** — 10 questions, 5 question types, concept-balanced, 8/10 passing, LLM-graded for free-text.
- **Remediation** — adaptive, EO-targeted, triggered on failure. Walks through failed objectives with minimum-exchange floors and a 15-exchange safety valve.
- **Teacher override** — teachers can reclassify any question's correctness in the review UI; score and pass/fail recompute, session promotes to completed if it crosses 8/10.
- **Mastery tracking** — per-EO skill mastery aggregated across sessions; class readiness report surfaces patterns.

### 4.6 Offline Solutions
**Already supported:**
- Local embeddings (sentence-transformers, no external API).
- SQLite fallback for database.
- Media served from local file share (or Azure File Share mount).

**Gap / roadmap:** the LLM is currently cloud-hosted. Moving to fully offline requires a local LLM:
- **Option A — Edge device (student tablet/laptop):** limited to small models (1–3B params). Sufficient for simple Q&A, insufficient for the full tutoring experience (Sidy's point).
- **Option B — On-premise desktop AI server:** NVIDIA Spark / DGX-class workstation running Gemma 4, Qwen 2.5 72B, or similar. Sidy has tested this successfully. Serves a classroom or school over local network. Recommended path for low-connectivity deployments.
- **Option C — Hybrid cache-and-forward:** local SLM handles common queries; cloud LLM used opportunistically when connectivity exists; teacher-coach feedback generated async when a connection appears (already the model for the AI Coach).

The platform's provider abstraction means swapping to a local LLM endpoint is a configuration change, not a rewrite.

### 4.7 Integration with LMS (Moodle, etc.)
Not yet implemented. Recommended integration surface:
- **LTI 1.3** (Learning Tools Interoperability) — industry-standard launch + grade-passback for Moodle, Canvas, Blackboard. Teacher assigns a lesson; student launches into the tutor; score returns to the LMS gradebook.
- **SCORM / xAPI** — for completion reporting and activity analytics pushed to a learning record store.
- **REST API** — the platform already has authenticated APIs for sessions, progress, and exit-ticket attempts. Exposing a documented subset for an LMS adapter is straightforward.
- **Roster sync** — SIS/LMS → institution `Membership` via OneRoster or CSV import (the invitation + bulk-upload flow already exists; just needs the OneRoster adapter).

Integration with a teacher-coaching tool (the AI Coach that Sidy maintains) is the natural companion — tutoring data flows can inform which teachers need which PD modules.

## 5. What's Deliberately Not Built (and Why)

- **No attempt to replace the teacher.** Teachers edit, approve, monitor, and override. The system surfaces effort; pedagogical judgment stays with them.
- **No generic chatbot.** The tutor is constrained to the current lesson step — it cannot "skip ahead" or go off-curriculum. Off-topic attempts trigger redirection (safety filter) or flagging.
- **No unreviewed AI content in front of students.** Pipeline outputs are always pending teacher review before publish.
- **No hardcoded LLM vendor.** Provider-agnostic abstraction lets the system follow cost, policy, or performance decisions without code churn.

## 6. Pilot Context

- **Seychelles (live):** full deployment, multi-school, with platform-wide + per-school curriculum. Subject focus includes geography ("world pattern of development" etc.) and math, with math-specific content generation, mandatory SVG diagrams for graph/geometry questions, calculation verification, and working-before-evaluation pedagogy.
- **Adjacent tooling in the same family** (same team, shared philosophy): AI Coach for teacher PD (audio lesson recording → evidence-based feedback against TEACH-framework rubric; offline-capable), Career Coach (skills elicitation, CV/interview prep, LMIC-calibrated job matching — Zambia pilot), AI Assessment Agent (21st-century skills rubric-based feedback with human-in-the-loop for low-confidence cases — Zimbabwe pilot).
