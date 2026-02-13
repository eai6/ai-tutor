# AI Tutor — Gap Analysis

> Specification vs. Implementation: what is built, what is missing, and what needs revision.
>
> Based on the [Technical Analysis](./TECHNICAL_ANALYSIS.md), evaluated against the *AI Tutor Application — Technical & UX Specification* and the *AI Tutor Design Choices* document.

---

## How to Read This Document

Each section maps a specification requirement to the current state of the codebase. Items are classified as:

- **Implemented** — the feature exists and broadly satisfies the spec.
- **Partially Implemented** — some aspects are present but significant work remains.
- **Not Implemented** — the feature is absent from the codebase.
- **Divergent** — the implementation exists but takes a fundamentally different approach from what the spec prescribes; a deliberate decision is needed on whether to converge or to document the deviation as intentional.

---

## Part I: Feature-Level Gap Analysis

### 1. Onboarding and Profile Setup

| Requirement | Status | Notes |
|---|---|---|
| Welcome screen with name, school, grade | **Implemented** | Registration view collects username, email, password, first name, school, grade. |
| Simple signup — no complex sign-in | **Divergent** | The spec calls for minimal friction ("no complex sign-in"). The current implementation uses full Django authentication with username + password, which is heavier than what the spec envisions. The spec implies a profile creation step closer to a name/school/grade form that generates a local ID. |
| Email optional | **Partially Implemented** | Email is collected during registration but is not enforced as required. However, it is part of a traditional auth flow rather than the lightweight onboarding the spec describes. |
| Profile stored on cloud, stays logged in | **Implemented** | Django session-based auth keeps users logged in. Profile is server-side. |
| Anonymization protocols | **Not Implemented** | No anonymization layer exists. Student PII (name, school, grade) is stored in the server database alongside session transcripts. The spec and design choices document both emphasize that PII should remain on-device, with only anonymized mastery data transmitted. |

**What needs to change:** The onboarding flow should be simplified to match the spec's lightweight model. The anonymization gap is significant — the spec explicitly calls for PII to stay on-device. Either the current server-side profile model must be revised to strip PII from backend storage, or the deviation must be documented as an intentional architectural choice with a justification.

---

### 2. Subject and Module Selection

| Requirement | Status | Notes |
|---|---|---|
| Choose a subject from multiple options | **Partially Implemented** | The lesson catalog shows lessons but does not present a subject-first selection screen. Geography and Mathematics exist as courses, but the UI does not surface a subject chooser before showing modules. |
| Modules aligned with national curriculum | **Implemented** | Seychelles Geography and Mathematics curricula are seeded with detailed lesson content. |
| 4–6 modules per subject with descriptions | **Implemented** | Units contain multiple lessons, each with objectives and descriptions. |
| Recommended sequence, not locked | **Partially Implemented** | Lessons are ordered via `order_index`, but the UI does not visually recommend a sequence or indicate which module to start next. Students can access any lesson freely. |
| Icons and brief descriptions per module | **Partially Implemented** | Descriptions exist in the data model. Icons are not implemented. |
| Progress indicators on module list | **Not Implemented** | The module list does not show per-module completion status, progress bars, or checkmarks. `StudentLessonProgress` data exists server-side but is not surfaced in the UI. |
| Switch between subjects freely | **Partially Implemented** | The catalog shows all lessons but lacks an explicit subject-switching interaction (tabs, dropdown, or separate screens). |

**What needs to change:** The module selection screen needs a subject-first navigation flow, per-module progress indicators, recommended sequencing cues, and visual polish (icons, progress bars). The data layer supports most of this — the gap is in the UI/UX.

---

### 3. Conversational Tutoring Session

| Requirement | Status | Notes |
|---|---|---|
| Chat-based interface | **Implemented** | The session view provides a functional chat interface. |
| AI opens with introductory prompt | **Implemented** | The engine's `start()` method generates an opening message via the LLM. |
| Science of learning principles in system prompt | **Implemented** | The `seed_seychelles` PromptPack embeds retrieval practice, explicit instruction, guided practice, exit tickets, and spaced repetition. |
| Scaffolded, dialogic approach | **Implemented** | Both structured mode (hint ladder) and conversational mode (prompt instructions) implement scaffolding. |
| Token streaming | **Not Implemented** | The spec explicitly requires "loading indicator… in token streaming." All LLM responses are currently synchronous — the full response is returned at once. There is a typing indicator but no incremental token display. |
| Retrieval from previously covered topics | **Implemented** | The system prompt instructs the AI to begin with retrieval practice from prior modules. |
| Exit ticket: 5 MCQ, 4/5 to pass | **Implemented** | Embedded in the system prompt for conversational mode. In structured mode, QUIZ steps serve this function. |
| Student can ask tangential questions | **Partially Implemented** | Conversational mode allows free-form interaction. Structured mode is step-locked — the engine expects answers to specific questions and does not handle tangential questions gracefully. |
| Local context in examples | **Implemented** | Seychelles names, places, currency (SCR), and local scenarios are embedded in the prompts and curriculum. |
| Adaptive difficulty and pacing | **Partially Implemented** | The prompt instructs the AI to adapt, but there is no programmatic mechanism to adjust difficulty. In structured mode, the step sequence is fixed regardless of student performance. |

**What needs to change:** Token streaming is the most critical gap. The spec treats it as a core UX requirement for maintaining a conversational feel. Structured mode's rigidity conflicts with the spec's vision of a "free-flowing" session where the student can ask questions anytime — this may be acceptable if structured mode is reserved for specific lesson types, but the trade-off should be explicit.

---

### 4. Module Completion and Progress Tracking

| Requirement | Status | Notes |
|---|---|---|
| Module complete when core exercises done | **Implemented** | `TutorSession` is marked complete when all steps are done or mastery is achieved. |
| UI to explicitly mark module complete | **Not Implemented** | There is no explicit "mark as complete" button. Completion is determined internally by the engine. |
| Praise on completion | **Partially Implemented** | The system prompt instructs the AI to praise the student. No UI-level celebration (animation, badge, checkmark) exists. |
| Progress recorded locally | **Divergent** | Progress is stored server-side in `StudentLessonProgress`, not locally. The spec says "records… progress locally until the session ends." |
| Visual progress indicators on module list | **Not Implemented** | No progress bar, percentage, or completion badge is shown on the module list. |
| Progress bar per subject | **Not Implemented** | No aggregate subject-level progress display. |
| Revisit completed modules | **Implemented** | No lock-out mechanism; students can re-enter any lesson. |

**What needs to change:** The module list needs a progress visualization layer. A completion celebration in the UI (even minimal — a checkmark, a congratulatory banner) is specified. The local-vs-server storage question for progress is an architectural decision tied to the broader client-centric vs. server-centric question (see Part II).

---

### 5. Multi-Language Support

| Requirement | Status | Notes |
|---|---|---|
| Tutoring in multiple languages | **Not Implemented** | All content and prompts are in English only. No language selection or detection. |
| Auto-detect language from country/curriculum | **Not Implemented** | No country detection or language inference logic. |
| Flexible language switching mid-session | **Not Implemented** | No mechanism for the student to request a language change. |
| All UI text translatable (i18n) | **Not Implemented** | UI strings are hardcoded in English in Django templates. No translation dictionaries, no i18n framework integration. |
| Localized module content per language | **Not Implemented** | Curriculum content is English-only. No multilingual fields or per-language content files. |

**What needs to change:** Multi-language support is entirely absent. This is a large work item spanning: (1) i18n framework integration for static UI text, (2) multilingual curriculum content or per-language content files, (3) language detection and selection UI, (4) LLM prompt modification to instruct tutoring in the target language. For the MVP this may be deferred, but the spec treats it as a core requirement for regional deployment.

---

### 6. UX and Accessibility

| Requirement | Status | Notes |
|---|---|---|
| Mobile-responsive design | **Partially Implemented** | The chat interface has some responsive CSS, but no systematic responsive framework or media queries for a range of devices. |
| Works on low-end Android 7+ phones | **Not Verified** | No evidence of device testing or optimization for low-end hardware. |
| Offline-conscious design | **Not Implemented** | No service worker, no offline caching, no graceful degradation. The app requires connectivity for every interaction. |
| Text-focused, minimal data usage | **Implemented** | The interface is text-only with no heavy media in the tutoring flow. |
| Simple navigation (2–3 screens) | **Implemented** | The app has essentially two screens: module list and chat interface. |
| Accessibility (high contrast, screen readers, keyboard nav) | **Not Implemented** | No ARIA attributes, no accessibility auditing, no keyboard navigation support evident. |
| Cultural theming per country | **Not Implemented** | No theming system. Visual design is fixed. |
| Completion celebration UI | **Not Implemented** | No animation, badge, or visual celebration on module completion. |
| Privacy trust signals in UI | **Not Implemented** | No privacy notice, no explanation of data handling visible to students. |

**What needs to change:** Mobile responsiveness and accessibility need systematic attention. Offline capability is a larger architectural decision (PWA vs. current SSR). Privacy trust signals are a relatively easy addition. The completion celebration is a small but spec-explicit UX element.

---

### 7. Technical Architecture

| Requirement | Status | Notes |
|---|---|---|
| Progressive Web App (PWA) | **Not Implemented** | The app is a traditional Django server-rendered application. No service worker, no web app manifest, no installability. |
| React or modern JS framework | **Divergent** | The spec recommends React. The implementation uses Django templates with vanilla JavaScript. This is a fundamental architectural divergence. |
| Client-centric architecture | **Divergent** | The spec calls for a "client-centric architecture with a lightweight backend." The current architecture is server-centric: Django handles routing, rendering, state, and all business logic. |
| Browser local storage / IndexedDB for profile and progress | **Not Implemented** | All data is stored server-side in Django ORM models (SQLite). No client-side persistence. |
| Content in structured JSON config files | **Divergent** | Content is stored in Django database models, seeded via Python management commands. The spec envisions JSON files per country (`curriculum_KE.json`, `curriculum_NG.json`) that the app loads at startup. |
| Config directory per country/locale | **Not Implemented** | No country-specific configuration system. Content is Seychelles-specific and hardcoded in the seed command. |
| Open source on GitHub | **Implemented** | The repository is on GitHub. |
| Compatible with multiple LLMs | **Implemented** | The provider abstraction supports Anthropic, OpenAI, and Ollama. |
| Response streaming from LLM | **Not Implemented** | Spec mentions streaming; all responses are synchronous. |
| Analytics backend with anonymized events | **Not Implemented** | No analytics event system. No event types (module_started, message_submitted, etc.) are emitted. No analytics dashboard. |
| Feature flags | **Not Implemented** | No feature flag system. `ModelConfig` partially serves this role for LLM provider selection, but there is no general mechanism for toggling features like analytics, debug mode, or mock data. |
| <3s initial load on 3G | **Not Verified** | No performance testing or optimization for slow connections. |
| CORS enforcement | **Not Applicable** | Django serves both frontend and API; CORS is not relevant in the current architecture. Would become relevant if the frontend were separated. |

**What needs to change:** This section reveals the most significant architectural divergences. The spec envisions a **client-centric PWA with React**, where the backend is a thin API layer and the client owns state, storage, and rendering. The current implementation is a **server-centric Django monolith** where the backend owns everything. These are fundamentally different architectures. Resolving this divergence is the single most consequential decision for the project. See Part II for analysis.

---

### 8. Content Management and Scalability

| Requirement | Status | Notes |
|---|---|---|
| JSON-based content files per country | **Divergent** | Content is in the database, managed via seed commands. |
| Adding a new country requires no code changes | **Not Met** | Adding a new country currently requires writing a new seed command in Python. |
| Educators can update content without code changes | **Not Met** | Content can only be modified via the Django admin or by editing Python seed scripts. No authoring UI. |
| Module content with optional pedagogical scripts | **Implemented** | LessonSteps carry teacher scripts, questions, hints, and rubrics. |

**What needs to change:** The spec envisions content portability — drop in a JSON file for a new country and the app adapts. The current database-backed approach is more powerful but less portable. A content import/export layer or a JSON-to-database loader would bridge this gap.

---

### 9. Privacy and Security

| Requirement | Status | Notes |
|---|---|---|
| No PII transmitted to servers | **Not Met** | Student names, schools, and grades are stored server-side. The spec says "personal info stays on the device." |
| All network traffic encrypted (HTTPS) | **Deployment Dependent** | The dev server does not use HTTPS. Production deployment would need TLS. |
| No conversation text sent to analytics | **Met by Absence** | No analytics system exists, so no conversation text is transmitted. However, session transcripts are stored server-side. |
| Anonymized user IDs only | **Not Implemented** | User records use Django's auto-increment IDs and contain full PII. |
| Privacy notice in UI | **Not Implemented** | No privacy information displayed to students. |
| Content safety / safeguards | **Partially Implemented** | Prompt-level safety instructions exist. No programmatic moderation, no content filter API, no banned-topic list. |
| Opt-out toggle for analytics | **Not Applicable** | No analytics system exists. |

**What needs to change:** The PII-on-server issue is the most significant privacy gap. The spec is explicit that personal information should stay on-device. If the server-centric architecture is retained, an anonymization layer is needed (e.g., hashing identifiers, stripping names from server records). Programmatic safeguards beyond prompt instructions should be added before deployment in schools.

---

### 10. Analytics and Evaluation

| Requirement | Status | Notes |
|---|---|---|
| Anonymized analytics events | **Not Implemented** | No event system. |
| Events: module_started, message_submitted, api_response_received, module_completed, session_start/end, module_dropout, error_occurred | **Not Implemented** | None of these events are emitted or collected. |
| Events batched and throttled | **Not Implemented** | No event pipeline. |
| Offline event caching for later upload | **Not Implemented** | No offline support at all. |
| Admin/teacher dashboard | **Not Implemented** | No dashboard for educators or administrators. `SessionTurn` data could power one, but no views exist. |

**What needs to change:** The analytics system is entirely missing. This is a significant gap for evaluation and impact measurement, which are key goals of the project. The `SessionTurn` transcript data provides a rich foundation, but a structured event system, aggregation layer, and dashboard UI all need to be built.

---

## Part I Summary: Gap Severity Matrix

| Area | Severity | Nature of Gap |
|---|---|---|
| Token streaming | **High** | Missing core UX feature; spec-explicit |
| Multi-language support | **High** | Entirely absent; spec treats as essential for regional deployment |
| Analytics and evaluation | **High** | No event system, no dashboard; critical for project goals |
| Privacy / PII handling | **High** | Architectural divergence from spec's on-device model |
| Client-centric architecture (PWA/React) | **Critical — Architectural** | Fundamental divergence; see Part II |
| Progress visualization in UI | **Medium** | Data exists server-side; UI layer missing |
| Module selection UX | **Medium** | Needs subject-first navigation, icons, progress cues |
| Content portability (JSON per country) | **Medium** | Need import/export layer for multi-country scaling |
| Offline capability | **Medium** | No offline support; spec calls for offline-conscious design |
| Mobile responsiveness / accessibility | **Medium** | Needs systematic testing and remediation |
| Completion celebrations | **Low** | Small UI addition |
| Feature flags | **Low** | Nice to have for operational flexibility |
| Simplified onboarding | **Low** | Current auth works; spec prefers lighter approach |

---

## Part II: Architectural Divergence — The Central Question

The most consequential finding in this gap analysis is not any single missing feature but a **fundamental architectural divergence** between the specification and the implementation.

### What the Spec Prescribes

A **client-centric PWA** built with React (or similar), where:
- The frontend is the primary application — it handles rendering, state, local storage, and direct LLM API calls.
- The backend is a lightweight, optional analytics service.
- Student data lives on-device (localStorage / IndexedDB).
- Curriculum content is loaded from static JSON files.
- The app is installable and functions offline for non-LLM features.

### What Is Built

A **server-centric Django monolith**, where:
- The backend handles rendering (Django templates), business logic (TutorEngine), state (ORM models), authentication (Django auth), and LLM orchestration.
- The frontend is a thin layer of vanilla JavaScript consuming a JSON API.
- All data — including student PII, progress, and transcripts — lives in the server database.
- Curriculum content is stored in database models, managed via Python seed commands.

### Implications

These are not two implementations of the same design — they are **different architectures** with different trade-off profiles:

| Dimension | Spec (Client-Centric PWA) | Current (Server-Centric Django) |
|---|---|---|
| Privacy | PII stays on device | PII on server |
| Offline capability | Natural (PWA + local storage) | Requires fundamental rework |
| Scalability | Backend is stateless, cheap | Backend bears all load |
| LLM API keys | Exposed to client or needs proxy | Safely server-side |
| Content management | JSON files, easy portability | Database, more powerful but less portable |
| Multi-country scaling | Drop in new JSON config | Write new seed command |
| Development complexity | Needs React build tooling, state management | Simpler stack, Django handles everything |
| Deployment | Static hosting + API proxy | Traditional server deployment |
| Audit trail | Harder (data on client) | Natural (data on server) |
| Teacher dashboards | Harder (no server-side data) | Natural (all data server-side) |

Neither architecture is objectively better — they optimize for different priorities. The spec optimizes for **privacy, offline access, and minimal infrastructure**. The current implementation optimizes for **auditability, content control, and development simplicity**.

**A decision is required:** converge toward the spec's client-centric model, explicitly choose to retain the server-centric model with documented justifications, or find a hybrid approach (e.g., keep the Django backend for tutoring orchestration and analytics but add a PWA shell with local-first profile storage).

---

## Part III: What Needs Revision in the Current Implementation

Even if the server-centric architecture is retained, several aspects of the current implementation need revision to better align with the spec's intent:

1. **Anonymization layer.** Strip or hash PII before storing server-side. Store names and school only in an encrypted, access-controlled table separate from session data. Transmit only anonymized IDs in analytics events.

2. **Streaming responses.** Implement SSE or WebSocket-based token streaming. This is spec-explicit and materially impacts UX. Django supports `StreamingHttpResponse`; the LLM client abstraction needs a `generate_stream()` method.

3. **Progress UI.** Surface `StudentLessonProgress` data on the module list: completion checkmarks, progress bars per subject, recommended next module.

4. **Subject-first navigation.** Add a subject selection screen or tab system before the module list. The current flat list does not match the spec's two-level browse pattern.

5. **Analytics event system.** Define and emit the spec's event types (`module_started`, `message_submitted`, `module_completed`, etc.). Store them in a lightweight events table or forward them to an external service. Build a minimal dashboard for educators.

6. **Content portability.** Add a JSON import/export mechanism so that new country curricula can be onboarded via data files rather than Python code. This does not require abandoning the database — a `load_curriculum` command that reads a standard JSON schema would suffice.

7. **i18n foundation.** Even if full multi-language support is deferred, lay the groundwork: extract hardcoded strings into a translation-ready format, add a language field to the student profile, and include a language directive in the LLM prompt.

8. **Privacy notice.** Add a visible privacy statement in the UI explaining what data is stored and where, as the spec requires for trust-building with minor students.

9. **Programmatic safeguards.** Supplement prompt-level safety with at least basic output filtering (keyword blocklist, moderation API call) before deploying in schools.

10. **Responsive design audit.** Test on low-end Android devices and implement CSS media queries or a responsive framework for mobile usability.

---

## Part IV: Evaluation of the Five Design Choices

The *AI Tutor Design Choices* document proposes five architectural design options. This section evaluates each against the gap analysis findings, the technical specification, and the practical realities of the current codebase.

---

### Design Choice 1: Multi-Agent System (Agent-per-Subject)

**Proposal:** Each subject (Mathematics, Geography, etc.) is handled by a dedicated Tutor Agent with its own prompt engineering and pedagogical strategies. Agents access curricula via MCP (Model Context Protocol) and are decoupled from content.

**Current State:** A single `TutorEngine` handles all subjects. Subject-specific behavior is driven entirely by the `PromptPack` and lesson content, not by separate agents. There is no MCP integration.

#### Pros

- **Pedagogical fit.** Mathematics tutoring (problem-solving, step-by-step scaffolding, numeric grading) genuinely differs from Geography tutoring (conceptual discussion, map interpretation, descriptive answers). Separate agents allow each to be optimized independently.
- **Prompt isolation.** Keeps system prompts focused and shorter. A single "universal tutor" prompt that handles every subject becomes unwieldy as subjects scale.
- **Independent iteration.** A Mathematics agent can be improved or swapped without risking regressions in Geography. Different subjects can use different models if cost/quality trade-offs warrant it.
- **Aligns with spec.** The spec describes per-module system prompts. Agent-per-subject is a natural extension.

#### Cons

- **Premature at current scale.** With only two subjects and one country, the overhead of managing separate agents (deployment, configuration, testing per agent) is not yet justified. The current `PromptPack` approach achieves subject-specific behavior with simpler machinery.
- **MCP adds complexity.** MCP is an emerging protocol with limited tooling maturity. Introducing it requires infrastructure (MCP server, tool definitions, protocol handling) that the current codebase does not have. The specification does not mention MCP.
- **Shared logic duplication.** Many tutoring behaviors (hint ladder, progress tracking, session management) are identical across subjects. Agent-per-subject risks duplicating this shared logic unless a strong base-agent abstraction is maintained.
- **Operational overhead.** Each agent needs its own prompt management, testing, and monitoring. With 6+ subjects across multiple countries, this multiplies operational complexity.

**Recommendation relative to the gaps:** The current single-engine approach with PromptPack-driven subject variation is adequate for the near term. The gap analysis does not identify agent separation as a blocking issue. However, if the platform scales to 10+ subjects with fundamentally different interaction patterns (e.g., language arts with essay feedback vs. mathematics with equation solving), per-subject agents become valuable. **Design for it but don't build it yet** — ensure the PromptPack and engine interfaces could support a future split.

---

### Design Choice 2: Knowledge Graph — Curriculum

**Proposal:** National curricula are modeled as Knowledge Graphs with interconnected topics, prerequisite dependencies, and a unified API. One graph per country-subject pair. Agents query the graph for content and sequencing decisions.

**Current State:** The curriculum uses a flat four-level hierarchy (Course → Unit → Lesson → LessonStep) with no prerequisite links, no cross-lesson dependencies, and no concept-level interconnection. Sequencing is implicit in `order_index`.

#### Pros

- **Addresses a real gap.** The gap analysis identifies the lack of prerequisite relationships and concept-level diagnostics as a limitation. A Knowledge Graph directly solves this — the system could determine that a student must master "fractions" before attempting "ratios" and surface that in the UI.
- **Enables adaptive pathways.** The spec's "possible adjustments" section calls for adaptive learning pathways based on performance. A graph of topic dependencies is a prerequisite for any meaningful adaptive system.
- **Multi-country scalability.** "One graph per country-subject pair" with a unified API is exactly what the spec envisions for scaling across countries. It replaces the current "write a new Python seed command per country" approach.
- **Content portability.** A standardized graph format could be the JSON-based content system the spec calls for — each country contributes a graph file rather than code.
- **Aligns with spec.** The spec references "The Math Academy Way" chapters 10–23, which are heavily centered on knowledge graphs, mastery learning, and prerequisite-based sequencing.

#### Cons

- **High implementation effort.** Building a Knowledge Graph system — data model, graph traversal algorithms, API, authoring tooling, and integration with the tutoring engine — is a major engineering undertaking. It requires expertise in graph data modeling and potentially a graph database (Neo4j) or a custom graph-over-relational implementation.
- **Content authoring burden.** Defining thousands of topics with prerequisite links, tagging resources, and maintaining graph integrity across countries requires significant curriculum expertise and tooling. Who builds and maintains these graphs?
- **Over-engineering risk at current scale.** With two subjects in one country, the flat hierarchy works. A Knowledge Graph pays off at scale (many subjects, many countries, adaptive algorithms) but adds complexity before that scale is reached.
- **Graph quality determines system quality.** If the graph has incorrect prerequisites or missing links, the adaptive decisions it drives will be wrong. Graph maintenance becomes a critical-path dependency.

**Recommendation relative to the gaps:** The Knowledge Graph is the most impactful design choice for long-term vision alignment. It directly addresses the gaps in prerequisite modeling, adaptive learning, and multi-country scalability. However, it is also the highest-effort choice. A pragmatic path: **add prerequisite links to the existing Lesson model now** (a lightweight version of graph edges), and plan a full Knowledge Graph migration for the phase when multiple countries are onboarded. The gap of "content portability" can be partially closed with a simpler JSON import/export layer in the interim.

---

### Design Choice 3: Lesson Plans (Personalized, Per Session)

**Proposal:** At the start of every session, the Tutor Agent generates an internal, personalized Lesson Plan based on three inputs: student profile and mastery levels, the Knowledge Graph, and Science of Learning principles. Plans are auditable and serve as a primary evaluation metric.

**Current State:** In structured mode, the "lesson plan" is the pre-authored sequence of `LessonStep` records — static, not personalized. In conversational mode, the LLM implicitly plans the session based on the system prompt, but no explicit plan is generated, stored, or auditable.

#### Pros

- **Closes the personalization gap.** The gap analysis notes that the current system doesn't adapt difficulty or sequencing to individual students. An explicit lesson plan generated from mastery data would enable real personalization.
- **Audit and evaluation.** The spec calls for transparency and accountability. An explicit, stored lesson plan is a concrete artifact that educators and evaluators can review. This directly supports the project's evaluation goals.
- **Bridges both modes.** Rather than having two separate engine modes (structured vs. conversational), a generated lesson plan could unify them: the LLM produces a plan, the engine executes it, and the plan can be as rigid or flexible as needed.
- **Aligns with spec.** The spec's session flow (retrieval → instruction → practice → exit ticket) is essentially a lesson plan template. Formalizing it makes the implicit explicit.

#### Cons

- **Depends on Knowledge Graph.** A meaningful lesson plan requires knowing what the student has and hasn't mastered, which requires mastery tracking at the topic level and a prerequisite structure. Without the Knowledge Graph (Design Choice 2), the plan's inputs are impoverished.
- **LLM-generated plans are hard to validate.** If the LLM generates the plan, how do we verify it covers the required content? The plan could skip important topics or misjudge difficulty. Quality assurance becomes a challenge.
- **Added latency at session start.** Generating a plan before the first interaction adds a planning step (another LLM call) to session startup. This conflicts with the spec's 2–5 second response target.
- **Plan rigidity vs. adaptivity tension.** A pre-generated plan that is "adapted in real time based on student responses" is effectively two systems: a planner and an adapter. The interaction between them (when does the adapter override the plan?) introduces complexity.

**Recommendation relative to the gaps:** The concept is valuable but should be staged. **Phase 1:** Formalize the current implicit plan — have the LLM output a brief structured plan at session start (topics to cover, question types, estimated difficulty) and store it as metadata on the `TutorSession` record. This closes the auditability gap with minimal effort. **Phase 2:** Once mastery tracking and the Knowledge Graph are in place, enrich the plan with personalized topic selection and adaptive sequencing.

---

### Design Choice 4: Student Profiles — Local-First with Mastery Levels

**Proposal:** Student profiles (PII) are stored locally on the frontend (IndexedDB). Only anonymized mastery levels are transmitted to the backend. Mastery is tracked per-topic with states like "not ready to learn," "ready to learn," and "learned."

**Current State:** All student data — PII and progress — is stored server-side. Mastery is tracked at the lesson level (not topic level) with states of "in_progress" and "mastered." There is no local storage, no anonymization, and no per-topic granularity.

#### Pros

- **Directly addresses the privacy gap.** The gap analysis identifies PII-on-server as a significant divergence from the spec. Local-first storage resolves this by design.
- **Spec-aligned.** Both the spec and the design choices document explicitly prescribe this approach.
- **Reduces backend scope.** If the backend only handles anonymized mastery data and analytics, it becomes simpler, cheaper to operate, and less of a data-protection liability.
- **Per-topic mastery is more useful.** Lesson-level mastery ("you passed Grid References") is less actionable than topic-level mastery ("you understand four-figure references but not six-figure"). Per-topic tracking enables better personalization and clearer progress visualization.

#### Cons

- **Data loss risk.** Browser storage (localStorage, IndexedDB) is volatile — clearing the browser, switching devices, or using incognito mode wipes the student's profile and progress. The spec acknowledges this ("if a student switches phones… progress is lost") but defers the solution. For a school deployment, this is a significant usability risk.
- **No multi-device support.** Students who use school computers and personal phones cannot carry progress across devices. The spec mentions this as a known limitation and a future enhancement area.
- **Conflicts with audit trail.** The current implementation's strength is its complete server-side audit trail (session transcripts, progress history). Moving profiles client-side makes it harder to correlate analytics data with learning outcomes, because the backend cannot link anonymized mastery to specific students.
- **Conflicts with teacher dashboard.** Teachers need to see per-student progress. If PII is only on-device, the teacher dashboard cannot show student names alongside their mastery data without a sync mechanism.
- **Implementation complexity.** Requires building a client-side storage layer (IndexedDB), a sync protocol for mastery data, conflict resolution for offline/online transitions, and a frontend architecture capable of managing state (which the current vanilla JS approach does not support).

**Recommendation relative to the gaps:** The privacy intent is correct and should be honored. However, pure local-first storage creates practical problems for a school deployment where teachers need visibility and students share devices. A **hybrid approach** is more pragmatic: store PII in an encrypted, access-controlled server table that is separate from analytics and session data; transmit only anonymized identifiers in analytics events; and add per-topic mastery tracking to the server model. If the project later moves to a PWA architecture, add IndexedDB caching as a progressive enhancement. This closes the privacy gap meaningfully without sacrificing auditability or teacher visibility.

---

### Design Choice 5: WebSocket API

**Proposal:** Replace the RESTful API with a persistent WebSocket connection for frontend-backend communication. Benefits cited: real-time updates, reduced latency, and async/non-blocking messaging.

**Current State:** All communication is synchronous HTTP (POST/GET). No WebSocket, no Server-Sent Events, no streaming. The frontend sends a request, shows a spinner, and waits for the complete response.

#### Pros

- **Enables token streaming.** The gap analysis identifies missing token streaming as a high-severity gap. WebSockets naturally support streaming LLM output token by token, which dramatically improves perceived responsiveness.
- **Real-time status updates.** The design choices document mentions status messages like "Fetching examples…" and "Generating quiz…" — these require a push channel that HTTP request-response cannot provide.
- **Aligns with async LLM calls.** WebSockets decouple the request from the response, which is natural for LLM interactions where the server initiates processing and streams results over an unpredictable time window.
- **Future-ready.** If multi-agent systems or background tasks (Knowledge Graph queries, lesson plan generation) are added, a persistent connection supports notifying the client when asynchronous operations complete.

#### Cons

- **Infrastructure complexity.** Django's default deployment model (WSGI) does not support WebSockets. WebSockets require Django Channels with ASGI, a channel layer backend (Redis), and a compatible deployment setup (Daphne or Uvicorn). This is a significant infrastructure addition.
- **Connection management.** WebSockets require handling connection lifecycle (connect, disconnect, reconnect), heartbeats, and state recovery after drops. On flaky African mobile networks — which the spec explicitly calls out — WebSocket connections will drop frequently. Robust reconnection logic is essential but non-trivial.
- **Overengineered for current needs.** The current API surface is small (4 endpoints). A WebSocket protocol adds framing, message types, routing, and error handling complexity for a use case that SSE (Server-Sent Events) could mostly satisfy. SSE is simpler: it streams server-to-client over a standard HTTP connection and works with WSGI, requires no Redis, and degrades gracefully.
- **Mobile battery and data.** Persistent connections consume battery and keep radios active on mobile devices — a concern for the spec's low-end Android target demographic.
- **Debugging difficulty.** WebSocket interactions are harder to inspect, replay, and debug than HTTP request/response pairs. Standard tools (browser dev tools, curl, logging middleware) work less well.

**Recommendation relative to the gaps:** The core need is **token streaming**, not a full WebSocket API. **Server-Sent Events (SSE)** close this gap with far less infrastructure overhead: Django's `StreamingHttpResponse` supports SSE natively, no Redis or Channels is needed, SSE auto-reconnects on drop, and it works over standard HTTP. Reserve WebSockets for a future phase where bidirectional real-time communication is genuinely needed (e.g., collaborative features, multi-agent orchestration with progress notifications). For the current architecture, SSE is the right tool.

---

### Design Choices: Summary Comparison

| Design Choice | Gap It Addresses | Implementation Effort | Recommended Approach |
|---|---|---|---|
| 1. Multi-Agent System | Subject-specific tutoring optimization | Medium | Defer; current PromptPack approach is sufficient for 2 subjects. Design interfaces to support future split. |
| 2. Knowledge Graph | Prerequisite modeling, adaptive learning, multi-country scaling | High | Add prerequisite links to existing models now. Plan full graph migration for multi-country phase. |
| 3. Lesson Plans | Personalization, auditability, evaluation | Medium | Formalize the implicit plan as stored metadata immediately. Enrich with mastery data later. |
| 4. Local-First Profiles | Privacy, PII handling | Medium–High | Hybrid approach: encrypted server-side PII with anonymized analytics. Add IndexedDB caching if/when PWA is adopted. |
| 5. WebSocket API | Token streaming, real-time updates | High | Use SSE for streaming (low effort, high impact). Reserve WebSockets for future bidirectional needs. |

---

## Part V: Recommended Prioritization

Based on the gap analysis and design choice evaluation, the following prioritization balances impact, effort, and spec alignment:

### Immediate (Close critical UX and compliance gaps)

1. **Token streaming via SSE** — Highest-impact UX improvement. Spec-explicit. Achievable without architectural overhaul.
2. **Progress visualization on module list** — Data exists; needs UI layer. High visibility to users.
3. **Privacy notice and anonymization foundation** — Required before school deployment. Add privacy UI text, begin separating PII from analytics data.
4. **Subject-first navigation** — Simple UI restructure that matches the spec's browse pattern.

### Near-Term (Build toward spec's vision)

5. **Analytics event system** — Define events, emit and store them. Critical for evaluation goals.
6. **Lesson plan metadata** — Formalize the session plan as stored, auditable data.
7. **i18n foundation** — Extract strings, add language field, prepare for multi-language phase.
8. **Prerequisite links on Lesson model** — Lightweight graph edges. Foundation for adaptive pathways.
9. **Responsive design audit and remediation** — Test on target devices, fix breakpoints.

### Later (Requires architectural decisions)

10. **PWA / client-centric architecture decision** — The fundamental architectural question. Must be resolved before scaling.
11. **Knowledge Graph** — Full implementation when multi-country onboarding begins.
12. **Multi-language content** — Requires both i18n infrastructure and curriculum translation effort.
13. **Multi-agent system** — When subject count and pedagogical divergence justify it.
14. **WebSocket API** — When real-time bidirectional communication is needed beyond streaming.

---

*Generated February 2026. Based on the AI Tutor Technical Analysis, the AI Tutor Application Technical & UX Specification, and the AI Tutor Design Choices document.*
