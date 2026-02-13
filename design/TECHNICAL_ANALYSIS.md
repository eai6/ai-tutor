# AI Tutor — Technical Analysis

> A high-level examination of how the AI Tutor platform implements its key features, the architectural decisions behind them, and the trade-offs they introduce.

---

## 1. System Overview

AI Tutor is a Django-based intelligent tutoring system built for Seychelles secondary school students. It pairs a structured curriculum (Geography and Mathematics, S1–S5) with LLM-powered conversational tutoring grounded in the Science of Learning. The platform is organized into five Django apps — **accounts**, **curriculum**, **llm**, **media_library**, and **tutoring** — each owning a distinct domain.

The frontend is server-side rendered with Django templates and vanilla JavaScript; there is no single-page application framework. The backend exposes a small JSON API consumed by in-page scripts that drive the chat interface.

---

## 2. Dual-Mode Tutoring Engine

The most consequential design decision in the system is the **two operating modes** of the tutoring engine.

### Structured (Rich) Mode

Lessons are decomposed into an ordered sequence of `LessonStep` records — TEACH, WORKED_EXAMPLE, PRACTICE, QUIZ, SUMMARY — each carrying a teacher script, a question, an expected answer, hints, and grading criteria. The engine walks through steps sequentially: it assembles a prompt from the current step, sends it to the LLM, waits for the student's response, grades it against the expected answer, and either advances or applies the hint ladder.

**Trade-offs.** This mode gives curriculum designers tight control over what is taught and how mastery is assessed. Grading is deterministic for structured answer types (multiple choice, numeric). The cost is authoring effort: every lesson requires handcrafted steps, hints, and rubrics. It also constrains the AI's ability to adapt the lesson dynamically — the path is fixed.

### Conversational (Lightweight) Mode

A lesson carries a single TEACH step with no required answer. The LLM is given the learning objective and a detailed pedagogical playbook (retrieval, instruction, practice, exit ticket) and drives the entire session autonomously. The AI signals completion by emitting a `[SESSION_COMPLETE]` marker.

**Trade-offs.** This mode eliminates the authoring bottleneck and lets the AI tailor pacing and examples to the student. But it sacrifices predictability: grading relies on the LLM's judgment rather than pre-defined answers, the session length is variable, and there is no built-in guarantee that every required concept is covered. Observability is harder — there are no discrete steps to audit.

The coexistence of both modes is a pragmatic choice: structured mode for high-stakes or tightly scoped lessons, conversational mode for broader topics where flexibility matters. The engine detects which mode to use based on the shape of the lesson data, so the two paths share a single entry point.

---

## 3. LLM Integration Architecture

### Provider Abstraction

LLM access is mediated by a factory that returns a provider-specific client (Anthropic, OpenAI, Ollama, or a mock) based on a `ModelConfig` record. All clients implement a common interface: accept a message list and system prompt, return a response with content and token counts.

**Trade-offs.** The abstraction prevents vendor lock-in and enables local development with Ollama or cost-free testing with the mock client. However, it papers over provider differences — Anthropic and OpenAI handle system prompts, tool use, and token limits differently. The current interface is the lowest common denominator, which means provider-specific features (tool use, citations, extended thinking) are unavailable without breaking the abstraction.

### Prompt Assembly

Prompts are composed in layers:

1. **System prompt** — assembled from a `PromptPack` (persona, teaching style, safety rules, formatting guidelines) plus lesson-level context (objective, grade level).
2. **Step instruction** — injected as a specially-tagged user message containing the teacher script, question, hints revealed so far, expected answer (for the AI's reference), and retry context.
3. **Conversation history** — the full transcript of prior turns in the session.

This layered approach cleanly separates what is stable (persona, pedagogy) from what varies per interaction (step content, student state). PromptPacks are versioned and scoped to an institution, enabling A/B testing of instructional strategies without touching application code.

**Trade-offs.** Sending the full conversation history on every call simplifies state management but increases token consumption linearly with session length. There is no summarization, truncation, or sliding-window strategy — long sessions will eventually hit context limits or become expensive.

### Synchronous Responses

All LLM calls are blocking: the server waits for the complete response before returning JSON to the client. The frontend displays a typing indicator during the wait.

**Trade-offs.** Synchronous calls are simple to implement, debug, and reason about. But they create a perceptible delay (several seconds for longer responses) and tie up a Django worker thread for the duration. Streaming via Server-Sent Events would improve perceived latency significantly and is architecturally straightforward (Django supports `StreamingHttpResponse`), but it has not been implemented.

---

## 4. Curriculum Data Model

The curriculum follows a four-level hierarchy: **Course → Unit → Lesson → LessonStep**. Each level is scoped to an institution. Lessons carry metadata (objective, estimated duration, mastery rule), while steps carry content (teacher script, question, answer, hints, media attachments).

This hierarchy mirrors how educators think about curriculum — subjects contain topics, topics contain lessons, lessons contain activities — which makes the model intuitive for content authors working through the Django admin.

**Trade-offs.** The rigid hierarchy doesn't easily accommodate cross-cutting concerns: a lesson on percentages in Mathematics can't reference a Geography lesson on population statistics without duplicating content. There is no tagging, prerequisite graph, or concept map — relationships between lessons are implicit in their ordering within a unit. The mastery rules (streak-based, quiz-based, or completion-based) are simple and legible but don't capture partial understanding or concept-level diagnostics.

The entire Seychelles curriculum is seeded via a management command (`seed_seychelles`) that programmatically creates courses, units, lessons, and steps. This is efficient for initial deployment but makes ongoing curriculum maintenance a code change rather than an editorial workflow — there is no authoring UI beyond the Django admin.

---

## 5. Grading and Progress Tracking

### Grading Strategy

Grading is dispatched by answer type:

- **Multiple choice and true/false** — exact string match against the expected answer.
- **Short numeric** — parsed to a number and compared with tolerance.
- **Free text** — delegated to the LLM with the rubric and expected answer as context.

**Trade-offs.** Deterministic grading for structured types is fast, free, and reliable. LLM-based grading for free text is flexible but introduces latency, cost, and non-determinism — the same answer could be graded differently on successive attempts. There is no confidence score or human-in-the-loop fallback for ambiguous cases.

### Hint Ladder

Each step can define up to three progressive hints. On incorrect attempts, the engine reveals hints in order; after exhausting all attempts, it reveals the answer. This implements a well-researched scaffolding strategy.

**Trade-offs.** Three hint levels are sufficient for most cases but inflexible — some questions benefit from more granular scaffolding, others from none. The hints are static (authored at content creation time), so they can't adapt to the specific misconception the student demonstrated. The LLM could generate dynamic hints, but the current design favors predictability.

### Progress Persistence

`StudentLessonProgress` records track mastery per student per lesson: correct streak, total attempts, best score, and mastery status. Sessions snapshot which prompt pack and model config were active, creating an audit trail.

**Trade-offs.** Progress is lesson-granular, not concept-granular. If a student masters "Grid References" but struggles with the sub-skill of four-figure vs. six-figure references, the system has no structured way to surface that. The `SessionTurn` transcript captures the detail, but extracting actionable analytics from free-text transcripts requires additional processing.

---

## 6. Multi-Tenancy and Access Control

Every data model is scoped to an `Institution` through foreign keys. Users are linked to institutions via a `Membership` join table that carries a role (Admin, Teacher, Editor, Student). Queries are filtered by the user's active institution, ensuring data isolation.

**Trade-offs.** This institution-scoping model is simple and effective for the current use case (multiple schools on one deployment). But it uses row-level filtering rather than schema-level or database-level isolation, so a missed filter clause could leak data across institutions. There is no middleware or query-set manager that enforces scoping automatically — each view must remember to filter. Authorization checks are limited to `@login_required`; role-based access control (e.g., only teachers can view analytics, only editors can modify curriculum) is modeled in the data but not enforced in views.

---

## 7. API and Frontend Design

The API surface is small: start a session, submit an answer, advance a step, check status. Endpoints use session-based authentication (Django's default) and return JSON. The frontend consumes these endpoints with `fetch()` calls and renders tutor messages using Marked.js for Markdown support.

The client maintains lightweight state: current session ID, a heuristic learning-phase indicator (derived by scanning tutor messages for keywords), and a message counter. Phase detection drives a visual progress bar but is not authoritative — it is a UX affordance, not a system-of-record.

**Trade-offs.** Server-side rendering with vanilla JavaScript keeps the stack simple and avoids build tooling. But it limits interactivity: there is no optimistic UI, no offline support, and no component-level reactivity. The keyword-based phase detection is fragile — if the AI's phrasing changes, the progress bar may misfire. A more robust approach would have the backend emit phase metadata explicitly.

---

## 8. Security Posture

- **API keys** are stored as environment variable *names* in the database, not as raw secrets. At runtime, the LLM client resolves the name to a value via `os.getenv()`. This prevents accidental exposure through database dumps or admin interfaces.
- **CSRF protection** is disabled on some API endpoints (`@csrf_exempt`), which simplifies JavaScript integration but weakens protection against cross-site request forgery for those routes.
- **Safety prompts** are embedded in the PromptPack, instructing the AI to stay on-topic, avoid harmful content, and redirect off-topic queries. This is a prompt-level guardrail, not a programmatic filter — it depends on the LLM's compliance.

**Trade-offs.** The API-key indirection is a thoughtful security pattern. The CSRF exemptions are a pragmatic shortcut that should be revisited before production (using Django's `ensure_csrf_cookie` and sending the token from JavaScript is straightforward). The safety prompts are a reasonable first line of defense but are bypassable through prompt injection; a production deployment would benefit from output filtering or a moderation layer.

---

## 9. Transcript and Observability

Every LLM interaction is recorded as a `SessionTurn` with role, content, token counts, and arbitrary metadata (attempt number, hints revealed, grade result). This creates a complete audit trail of every tutoring session.

**Trade-offs.** Full transcript recording is invaluable for debugging, quality assurance, and research. It also enables future features like session replay, teacher review, and learning analytics. The cost is storage growth — every turn stores the full message text, and sessions can run to dozens of turns. There is no aggregation, summarization, or archival strategy yet. Token usage is tracked per turn but not surfaced in any dashboard or budget-enforcement mechanism.

---

## 10. Data Flow Summary

```
Student opens lesson
        │
        ▼
  [Start Session API]
        │
        ├── Create TutorSession record
        ├── Load first LessonStep
        ├── Assemble prompt (PromptPack + Step + History)
        ├── Call LLM (synchronous)
        ├── Save SessionTurn
        └── Return tutor greeting + step metadata
        │
        ▼
  Student submits answer
        │
        ▼
  [Submit Answer API]
        │
        ├── Grade answer (deterministic or LLM-based)
        ├── If correct → update progress, prepare next step
        ├── If incorrect → reveal next hint, allow retry
        ├── Assemble prompt with updated context
        ├── Call LLM for feedback
        ├── Save SessionTurn
        └── Return feedback + session state
        │
        ▼
  Loop until mastery or all steps complete
        │
        ▼
  Mark session complete, update StudentLessonProgress
```

---

## 11. Deployment and Scalability Considerations

The application currently runs on SQLite with Django's development server. There is no containerization, CI/CD pipeline, or infrastructure-as-code. WSGI and ASGI entry points are configured, making deployment to standard platforms (Heroku, Railway, AWS, Azure) straightforward.

**Key scaling constraints:**

- **SQLite** does not support concurrent writes, making it unsuitable for multi-user production use. Migration to PostgreSQL is a configuration change, not a code change.
- **Synchronous LLM calls** tie up worker threads. Under load, a pool of workers will be exhausted by slow LLM responses. Moving to async views (Django supports them natively) or offloading LLM calls to a task queue (Celery) would decouple request handling from LLM latency.
- **No caching layer** — every session start and answer submission results in an LLM call. Caching is difficult for generative responses, but system prompt assembly, curriculum lookups, and prompt pack resolution could benefit from in-memory caching.
- **Media files** are served by Django directly, which is inefficient at scale. A CDN or object storage (S3) integration would be needed for production media serving.

---

## 12. Summary of Key Trade-Offs

| Decision | Benefit | Cost |
|---|---|---|
| Dual-mode engine (structured + conversational) | Flexibility for different lesson types | Two code paths to maintain; conversational mode harder to audit |
| LLM provider abstraction | Vendor independence; local dev with Ollama | Lowest-common-denominator interface; provider-specific features inaccessible |
| Full conversation history per call | Stateless server; simple implementation | Token cost grows linearly with session length |
| Synchronous LLM calls | Simple request/response model | Perceptible latency; worker threads blocked |
| Server-side rendering + vanilla JS | No build tooling; simple deployment | Limited interactivity; no offline capability |
| Institution-scoped multi-tenancy | Data isolation without schema complexity | Relies on correct query filtering; no automatic enforcement |
| Static hint ladder | Predictable scaffolding; no extra LLM calls | Cannot adapt to specific misconceptions |
| SQLite for development | Zero-config local setup | Cannot be used in production |
| Prompt-level safety guardrails | Easy to author and iterate | Bypassable; no programmatic enforcement |
| Full transcript recording | Complete audit trail; enables analytics | Storage growth; no aggregation strategy |

---

## 13. Architectural Strengths

- **Pedagogical grounding.** The Science of Learning principles (retrieval practice, explicit instruction, scaffolded hints, exit tickets, spaced repetition) are not afterthoughts — they are structural to the prompt system and engine flow.
- **Clean domain separation.** The five-app structure maps directly to the problem domain, making the codebase navigable and each app independently testable.
- **Content-code separation.** Curriculum content lives in the database (seeded via management commands), prompt strategies live in PromptPacks, and application logic lives in Python. Changes to what is taught, how the AI behaves, and how the engine works can be made independently.
- **Audit-ready design.** Session snapshots (prompt pack version, model config, full transcript) make it possible to reproduce any tutoring interaction after the fact.
- **Localization depth.** The Seychelles context is not superficial — it permeates example data, school lists, currency references, and geographic examples in the curriculum and prompts.

---

*Generated February 2026. Based on analysis of the ai-tutor repository at commit `45f829a`.*
