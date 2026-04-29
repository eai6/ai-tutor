# Help Assistant — Plan (2026-04-29)

## Problem

The in-app Help/Feedback widget today only does one thing: take a
text message + optional screenshot and queue it for human support
on `/dashboard/feedback/`. Most pilot questions are how-to / where-do-I
questions ("how do I take the baseline?", "where's the retake
button?", "the math symbol picker isn't showing on this lesson —
why?") that the platform docs already answer. Routing every one of
those to humans is slow + creates a backlog.

We want the widget to also offer an LLM-backed help assistant that
can answer common questions instantly, cite the doc it pulled from,
and escalate cleanly to humans when it doesn't know.

## Audience

Both students and teachers. Same chat surface, but the retrieval
filters by audience tag so a student doesn't get a teacher-only
"how to publish a summative" answer and vice versa.

## Current state (audit)

- `apps/curriculum/knowledge_base.py` — `CurriculumKnowledgeBase`
  class wraps a per-institution ChromaDB collection
  (`curriculum_{institution_id}`) using sentence-transformers
  embeddings via `EMBEDDING_BACKEND=local`. ChromaDB lives at
  `VECTORDB_ROOT` (`/tmp/vectordb` on Azure, see CLAUDE.md). Pattern
  is reusable for a separate help-assistant collection.
- `apps/llm/client.py` — `BaseLLMClient` abstraction with
  Anthropic / OpenAI / Google / Ollama implementations + a
  `get_llm_client(config)` factory. `ModelConfig.get_for(purpose)`
  picks a model per purpose.
- `apps/dashboard/models.py::FeedbackReport` — existing bug-report
  model with screenshot capture (just shipped).
- `templates/_includes/feedback_button.html` — existing modal with
  text + opt-out screenshot capture.
- `templates/help/index.html` — the canonical FAQ rendered as
  collapsible sections; ~360 lines of authoritative copy.
- `CLAUDE.md` + `memory/*.md` — operational rules + plan docs.

## Target design

Single chat surface that opens from the existing Help/Feedback
button. Default tab is "💬 Ask the AI" with the LLM assistant. A
secondary "📩 Send to support" tab keeps the existing form for when
the user wants a human. The assistant offers a "Send this to
support →" CTA at the bottom of every response so escalation is
one click.

Conversation lives in a new `HelpAssistantConversation` model so
support can audit, train future indexing on common asks, and join
escalations to their original chat.

### Knowledge base

New ChromaDB collection: `help_docs` (single platform-wide
collection — help docs aren't institution-scoped). Lives in the
same VECTORDB_ROOT under a separate institution slot
(`institution_help`) to keep the `apps/curriculum/knowledge_base`
code shape reusable.

Document sources, chunked + indexed at build time:

1. `templates/help/index.html` — strip HTML, split per `<details
   class="help-section">` block. Each chunk gets metadata:
     - source: 'help_faq'
     - section_title: the summary text
     - audience: 'all' | 'staff' (derived from `class="help-tag staff"`)
     - anchor: a fragment id we'll add for deep-linking
2. `CLAUDE.md` — operational rules. Audience: 'staff' (super-admin
   only). Chunked per heading.
3. `memory/*.md` — plan docs. Audience: 'staff'. Chunked per heading.
4. `README.md` — architecture overview. Audience: 'staff'.

Embedding: same `sentence-transformers/all-MiniLM-L6-v2` already
running on prod. No new model dependency.

Retrieval: top-5 similarity, threshold 0.35, audience filter
applied as ChromaDB metadata predicate.

### LLM call

New `ModelConfig` purpose: `'help_assistant'`. Defaults to a small
fast model (Anthropic Haiku or Gemini Flash) for low latency.
Configurable per institution like other purposes.

Prompt structure:

```
SYSTEM:
You are the AI Tutor platform's help assistant. Answer ONLY using
the documentation snippets provided. If the snippets don't cover
the question, say so plainly and suggest sending the question to
human support.

Tone: warm, terse, action-oriented. 2-4 sentences max + a clear
next step. Cite the section title in parentheses at the end.

Audience: {student | teacher | super_admin}

USER: {their question}

DOCS:
[1] (Help FAQ → Exit tickets & mastery)
{snippet 1}

[2] (Help FAQ → Choosing how long a lesson takes)
{snippet 2}

[3] (CLAUDE.md → Critical rules)
{snippet 3 — only included if audience is staff}
```

Output is plain text; the citations are a JSON sidecar so the UI
can render the anchor links.

### UI changes

`templates/_includes/feedback_button.html` becomes a tabbed modal:

  Tab 1: 💬 Ask the AI       [default for everyone]
  Tab 2: 📩 Send to support  [the existing form]

Tab 1 surface:
  - Chat bubbles (user / assistant)
  - Input textarea + Send button
  - "Send this to support →" button under each assistant response
    (pre-fills the support form with the conversation transcript)
  - Citations rendered as small chips: "📖 Exit tickets section"
    that scroll to the help page anchor when clicked.

Tab 2 surface: existing form unchanged. Pre-fills
`message` and `page_url` if user came from Tab 1.

### Conversation persistence

New model in a new app `apps/support`:

```python
class HelpAssistantConversation(models.Model):
    user = FK(User, CASCADE)
    started_at = DateTimeField(auto_now_add)
    last_message_at = DateTimeField(auto_now)
    audience_tag = CharField(20)  # 'student' / 'teacher' / 'super_admin'
    page_url_at_start = CharField(500)
    user_agent = CharField(500)

class HelpAssistantMessage(models.Model):
    conversation = FK(HelpAssistantConversation, CASCADE)
    role = CharField(choices=['user', 'assistant'])
    content = TextField()
    citations = JSONField(default=list)  # [{section_title, anchor, source}]
    created_at = DateTimeField(auto_now_add)
    # Optional escalation pointer
    escalated_to_feedback = FK('dashboard.FeedbackReport', SET_NULL, null=True)
```

Retention: keep indefinitely; retired conversations go to a
read-only audit view. Pilot scale, no GDPR concern at this stage.

### Auth + rate limiting

- `@login_required` on the new endpoints.
- Reuse `apps/safety/rate_limit.py::RateLimiter` (already used by
  `chat_start_session`). Cap at 30 messages / 5 min / user — generous
  enough for a real conversation, low enough to bound cost.

### App boundary

New app `apps/support/` with:
  - `models.py` — HelpAssistantConversation, HelpAssistantMessage
  - `kb.py` — `HelpKB` class (mirrors `CurriculumKnowledgeBase` shape
    but on the `help_docs` collection)
  - `services.py` — `answer(question, audience, history) -> dict`
    pipeline: retrieve → format prompt → LLM call → parse citations
  - `views.py` — DRF-style endpoints:
      POST `/support/chat/start/`         → returns conversation_id
      POST `/support/chat/<conv>/message/` → sends user msg, returns assistant reply + citations
      POST `/support/chat/<conv>/escalate/` → spawns a FeedbackReport with the transcript
  - `urls.py` — wired under `apps.support.urls`
  - `management/commands/build_help_index.py` — re-indexes all docs
    into the `help_docs` collection. Idempotent. Run on deploy.

## Phased delivery

| Phase | Work | Solo-dev days |
|-------|------|---------------|
| **1** | App scaffold (`apps/support/`), KB indexer (`build_help_index`), single-shot Q&A endpoint with citations, basic chat tab in the widget. Conversation NOT persisted yet. Tools NOT live yet. Behind a feature flag. | **2** |
| **2** | Conversation persistence (`HelpAssistantConversation`, `HelpAssistantMessage`). Multi-turn chat. Escalation pipeline ("Send to support →" pre-fills the form with the transcript). Citation chips with deep-link anchors on the help page. | **1** |
| **3** | Tool layer foundation: `HelpAssistantToolCall` model, tool catalog, Anthropic tool-use loop, confirmation card UI for write actions, audit logging. Initial tools: `find_help_doc`, `recommend_next_lesson`, `start_lesson`, `take_baseline`. | **1.5** |
| **4** | Navigation tools (read-only, no confirmation): `open_student_chat_history`, `open_class_competency_map`, `open_class_readiness`, `open_session_history`, `open_lesson_detail`, `open_summative_review`. Each resolves fuzzy natural-language queries to course/student/session IDs with permission filtering. | **0.75** |
| **5** | Teacher-side write tools: `assign_lesson_for_week`, `set_default_lesson_duration`, `regenerate_lesson`. Audience filter on the tool catalog. Permission re-checks audited end-to-end on a smoke test. | **1** |
| **5** | Index rebuild hook in deploy pipeline. Audit dashboard for support staff to read past conversations + tool-call logs. UX polish (loading states, retries, model misbehaviour handling). | **1** |

Total: ~6.5 focused days.

## Decisions confirmed (2026-04-29)

1. **Default LLM**: Anthropic Haiku 4.5 (`claude-haiku-4-5-20251001`)
   for the `help_assistant` purpose. Fast, cheap, strong at
   retrieval-augmented Q&A.
2. **Index rebuild**: `manage.py build_help_index` hooked into
   `.github/workflows/deploy.yml` so the index refreshes on every
   push to main.
3. **Tool use: YES.** Scoped to actions the user already has
   permission for. See "Tool layer" section below for the v1
   tool catalog and confirmation flow.

## Hard safety rule — no destructive tools, ever

The assistant CANNOT perform destructive actions, full stop.
This is the load-bearing safety constraint and applies regardless
of the calling user's actual permissions. Even a super-admin
cannot use the assistant to delete things — they have to use the
proper dashboard buttons (where the destructive action is gated
by an explicit confirmation modal owned by the human, not the
LLM).

**Forbidden tool categories** (must never appear in the tool
catalog):

- Deleting students, teachers, accounts, or memberships
- Deleting courses, units, lessons, or lesson steps
- Deleting summative banks, exit tickets, or attempt history
- Deleting student progress, mastery records, or competency
  transcripts
- Deleting feedback reports, sessions, transcripts, or
  flagged-chat records
- Modifying user permissions, role assignments, or institution
  membership
- Overriding student grades / mastery state
- Editing or deleting prompts, model configs, or platform settings
- Touching anything in `safety/` (rate limits, audit logs)
- Sending bulk emails or notifications
- Anything that touches authentication / sessions / tokens

**Enforcement layers** (defense in depth):

1. **Tool catalog allowlist** in `apps/support/tools.py`. Tools
   are explicitly registered. There is NO generic
   `execute_django_orm` or `run_management_command` tool. If a
   tool isn't in the allowlist, the LLM literally cannot call it.
2. **Per-handler permission re-check**. Every handler validates
   the calling user's role against what it's about to do, even
   though the tool catalog already filtered by audience. A
   compromised tool spec or model jailbreak can't bypass this.
3. **Read vs write separation in handlers**. Read-only tools run
   inline without a confirmation step; write tools (the small
   set we DO allow) gate on an explicit user-click confirmation
   so a hallucinated "I'll just go ahead and..." can't fire a
   write without the user seeing the proposed action.
4. **Audit log on every tool call**. `HelpAssistantToolCall`
   captures proposed/confirmed/executed/error states so a
   reviewer can spot patterns of bad LLM behaviour.
5. **System prompt constraint**. The assistant's system prompt
   ends with: *"You may NEVER suggest deleting, removing, or
   destroying anything. If a user asks to delete something,
   direct them to the dashboard's delete buttons; do not propose
   a tool call for it."* Belt-and-braces — the catalog is the
   real enforcer, but the prompt also tells the LLM to refuse.

When in doubt, the rule is: **the assistant gives information
and proposes constructive actions. Destructive actions belong to
the human-driven dashboard.**

## Tool layer (added 2026-04-29 per user direction)

The assistant doesn't just answer questions — it can propose
ACTIONS it can take on the user's behalf. Example exchange:

> Student: "What lesson should I do next?"
> Assistant: "Based on your skills_snapshot for Math S3, I'd
>   recommend 'Angles around a point' next. Want me to start it
>   for you?"
> Student: "Yes"
> Assistant: [calls `start_lesson(lesson_id=1137)` → opens chat]

Or (teacher):

> Teacher: "Assign that lesson to my Math S3 class for next week."
> Assistant: "I'll add 'Angles around a point' to next week's
>   assignment for Math S3 (week of May 5). Confirm?"
> Teacher: [clicks Confirm]
> Assistant: [calls `assign_lesson_for_week(course_id=15,
>   lesson_id=1137, week_start='2026-05-05')` → done]

### Architecture

- **Tool catalog**: Anthropic tool-use compatible JSON schemas
  defined in `apps/support/tools.py`. Each tool has:
    - `name`, `description`, `input_schema`
    - `audience` filter (which roles can see / call it)
    - `requires_confirmation`: bool — write actions are gated on
      explicit user click; reads run inline.
    - `handler(user, **inputs) -> dict` — server-side function
      that RE-CHECKS permissions (defense in depth) and returns
      a result the assistant can summarise.
- **Anthropic tool-use loop** in `services.answer()`:
    1. LLM call with tools provided.
    2. If response is a tool_use block, dispatch:
        - `requires_confirmation=False` → run handler immediately,
          feed result back into LLM, get next response.
        - `requires_confirmation=True` → return a "pending
          confirmation" structure to the UI; UI shows a card with
          "Confirm" / "Cancel"; on Confirm the client POSTs
          `/support/chat/<conv>/confirm/<msg_id>/`, server runs
          the handler, feeds result back into the LLM.
    3. Continue until LLM emits a plain-text response.
- **Audit**: every tool call (proposed, confirmed/rejected,
  executed, errored) logged to a new `HelpAssistantToolCall` model.

### v1 tool catalog

| Tool | Audience | Confirm? | What it does |
|------|----------|----------|--------------|
| `find_help_doc(topic)` | all | no | Same doc retrieval as the regular Q&A — explicit tool form for when the LLM wants more context. |
| `recommend_next_lesson(course_id?)` | student | no | Returns the engine's existing recommendation (mirrors catalog logic). Read-only. |
| `start_lesson(lesson_id)` | student | yes | Returns a deep-link to the lesson chat; UI navigates on confirm. No DB write. |
| `assign_lesson_for_week(course_id, lesson_id, week_start)` | teacher | yes | Creates / updates a `WeeklyAssignment`. Permission re-checked: must be staff at the course's institution. |
| `set_default_lesson_duration(course_id, minutes)` | teacher | yes | Bulk-updates `Lesson.estimated_minutes` for the course. Same permission check as the dashboard form. |
| `regenerate_lesson(lesson_id)` | teacher | yes | Queues `generate_complete_lesson` like the ⚡ button. Long-running; returns "queued, refresh in ~2 min". |
| `take_baseline(course_id)` | student | yes | Deep-link to the summative baseline page. |
| `open_student_chat_history(student_query)` | teacher | no | Resolve "student X" / username / "first name last name" → `/dashboard/students/<id>/`. Returns URL only — no DB write. |
| `open_class_competency_map(course_query)` | teacher | no | Resolve "S3 geography" / "math s3" / partial title → `/dashboard/curriculum/course/<id>/competencies/`. |
| `open_class_readiness(course_query)` | teacher | no | Same shape, → `/dashboard/class/<id>/readiness/`. |
| `open_session_history(session_query)` | teacher | no | Resolve "Aaliyah's last Math S3 session" → `/dashboard/session/<id>/chat-history/`. |
| `open_lesson_detail(lesson_query)` | teacher | no | Partial-title resolve → `/dashboard/curriculum/lesson/<id>/`. |
| `open_summative_review(course_query)` | teacher | no | → `/dashboard/curriculum/course/<id>/summative/`. |

Navigation tools all return `{ok: true, url: "...", label: "..."}`
so the UI can render a clickable "Take me there →" button rather
than dumping a raw URL into the chat. The LLM's job is to map
fuzzy natural-language references ("the math S3 readiness page",
"@student3's chat") to the right course / student / session id;
the tool handler does the actual DB lookup with permission
filters applied. Ambiguous queries return
`{ok: false, candidates: [...]}` and the assistant lists the
candidates back to the user to disambiguate.

Tools that are notably OUT of v1 scope: course delete, lesson
delete, prompt edits, model config changes, anything that touches
permissions / users. These are admin-only actions whose blast
radius isn't worth conversational dispatch.

### Permission model

Every handler does its own check — never trusts the LLM. Pattern:

```python
def assign_lesson_for_week_handler(user, course_id, lesson_id, week_start):
    # 1. Re-check permission via existing decorator helper.
    course = Course.objects.get(id=course_id)
    if not user_can_manage_course(user, course):
        return {"error": "permission_denied", "human_msg": "..."}
    # 2. Validate inputs.
    lesson = Lesson.objects.filter(id=lesson_id, unit__course=course).first()
    if not lesson:
        return {"error": "lesson_not_in_course"}
    # 3. Run the same code path the dashboard form uses.
    wa, _ = WeeklyAssignment.objects.update_or_create(...)
    # 4. Audit log row written by the caller.
    return {"ok": True, "summary": f"Assigned for week of {week_start}"}
```

The `audience` filter on the tool catalog is a UX layer — it
controls which tools the LLM sees. The permission re-check is the
load-bearing security layer. A teacher who somehow gets the LLM
to call `assign_lesson_for_week` for a different institution
still gets `permission_denied` because the handler enforces it.

### Audit model

```python
class HelpAssistantToolCall(models.Model):
    conversation = FK(HelpAssistantConversation, CASCADE)
    message = FK(HelpAssistantMessage, CASCADE)  # the assistant turn that proposed it
    user = FK(User, SET_NULL, null=True)
    tool_name = CharField(50)
    inputs = JSONField()
    proposed_at = DateTimeField(auto_now_add)
    confirmed_at = DateTimeField(null=True)
    cancelled_at = DateTimeField(null=True)
    executed_at = DateTimeField(null=True)
    result = JSONField(null=True)
    error = TextField(blank=True)
```

Surface in the audit dashboard alongside conversation transcripts.

## Other open questions

1. **Per-institution help customisation?** No for v1. Help docs
   are platform-wide. Phase 5+ if a school needs custom FAQ.
2. **Anchors on the help page** — the FAQ template needs
   `id="..."` attributes on each `<details class="help-section">`
   so citations can deep-link. Slugify the summary text into ids
   on render. Add as part of Phase 2.
3. **Confirmation card UI** — render the proposed action as a
   compact card inside the chat (e.g. "📅 Assign 'Angles around a
   point' to Math S3 for week of May 5 — [Confirm] [Cancel]"). Use
   the existing modal styling.

## Out of scope (this iteration)

- Multi-language help docs. English only at pilot stage.
- Conversation export / download.
- Per-conversation feedback (thumbs up/down on AI answers) — useful
  for tuning but Phase 5+.
- Voice / speech input on the help chat (the tutor chat has it; the
  help widget can stay text-only).
- Indexing past resolved feedback reports as "common asks" data —
  good idea for Phase 5 but creates PII handling work.

## Risks

- **Hallucination**: assistant says "yes you can do X" when X
  doesn't exist. Mitigation: strict "answer only from docs" system
  prompt + always include citations + always offer escalation.
- **Cost runaway**: a chatty student can rack up calls. Mitigation:
  rate limit + small fast model.
- **Stale index**: docs ship faster than the index refreshes,
  assistant gives outdated answers. Mitigation: rebuild on every
  deploy (Phase 4).
- **Auth bypass**: an unauthenticated user could query the help
  endpoint. Mitigation: `@login_required` on every endpoint.

## Next step

Phase 1 sequenced first commit: scaffold `apps/support/`, write the
`HelpKB` class + `build_help_index` command, ship a single-shot
answer endpoint that ignores conversation history. Validate the
retrieval quality on a handful of real student questions before
investing in conversation persistence.
