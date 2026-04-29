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
| **1** | App scaffold, KB indexer, single-shot answer endpoint (no conversation persistence yet), basic widget tab integration with one user→assistant→user→assistant flow. Ship behind a feature flag. | **2** |
| **2** | Conversation persistence + escalation. Citations rendered as chips with deep-link anchors on the help page. | **1** |
| **3** | Audience-aware filtering + teacher-tagged docs. Polish the modal UX (loading states, retry, "I don't know — escalate" handling). | **0.75** |
| **4** | Cron / hook to rebuild the `help_docs` collection on git push to main (or on a daily schedule). Audit dashboard for support staff to read past conversations. | **1** |

Total: ~4.75 focused days.

## Open questions

1. **Default LLM choice** for the help_assistant purpose. Recommend
   Anthropic Haiku 4.5 (fast, cheap, good at structured retrieval-
   augmented Q&A). Can be swapped via the existing ModelConfig admin.
2. **Where does the index get built?** Recommend a Django management
   command (`manage.py build_help_index`) hooked into the deploy
   pipeline (`.github/workflows/deploy.yml`) so the index refreshes
   on every push to main. Alternative: a periodic cron — slower to
   reflect docs changes.
3. **Should the assistant have any "tool use" / structured action
   capability** (e.g. "I'll set your default duration to 20 min for
   you")? Recommend NO for v1 — pure information retrieval. Adding
   actions opens an audit/permissions can-of-worms.
4. **Per-institution help customisation?** Recommend NO for v1.
   The help docs are platform-wide. Phase 5+ if a school needs custom
   FAQ entries.
5. **Anchors on the help page** — the FAQ template needs `id="..."`
   attributes on each `<details class="help-section">` so citations
   can deep-link. Recommend slugifying the summary text into ids
   on render.

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
