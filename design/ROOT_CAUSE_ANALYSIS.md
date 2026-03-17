# Root Cause Analysis — Human Evaluation Feedback (2026-03-11)

> Feedback from Vaani Chopra (World Bank) after testing "Place Value and Number Recognition" in the student interface.

---

## A. Learning Effectiveness

### 1. Lessons not mapped to clear learning objectives

**Feedback:** Each lesson should state the learning objective at the beginning so students know what they are expected to learn.

**Root cause:** The `Lesson` model has an `objective` field and it is included in the system context (`_build_lesson_context()`), but the opening prompt in `conversational_tutor.py:1669-1675` never instructs the LLM to **state the objective to the student**. It is marked as "for reference" only. Students never hear *"Today you will learn to…"*.

**Files:** `apps/tutoring/conversational_tutor.py:1669-1675`, `apps/curriculum/models.py:96-98`

---

### 2. Questions are unclear/vague and lack conceptual depth

**Feedback:** Phrasing of questions is unclear (e.g. "When you see a price like 500 SCR at the market, how do you know it's different from 50 SCR?"). Most questions are procedural and don't encourage application or deeper conceptual understanding.

**Root cause (content generation):** The content generation prompt in `content_generator.py:345-391` has no quality constraints requiring questions to be conceptual rather than procedural. No guidance on clarity, unambiguous framing, or matching question intent with explanations.

**Root cause (delivery):** The tutor system prompt instructs to "ask the EXACT question provided" — so if the generated question is vague, the tutor faithfully delivers it as-is. There is no validation gate or quality check on generated questions.

**Files:** `apps/curriculum/content_generator.py:345-391`, `apps/tutoring/conversational_tutor.py:259-266`

---

### 3. No recall of prior knowledge

**Feedback:** If a chapter assumes foundational knowledge, review it before beginning. Suggested structure: Intro/Hook → Review what we've learned before → New Material → Practice → Quiz.

**Root cause:** Prerequisite review infrastructure exists (`_build_retrieval_block()` at line 1616) but it is **optional and fragile** — it requires personalization to be initialized and interleaved practice questions to exist. When it returns `None` (common case), the fallback at line 1651 is a generic *"what do you already know about…"* question instead of a structured review of specific prerequisites. The suggested lesson structure (Intro → Review → New Material → Practice → Quiz) is not reflected in the step generation.

**Files:** `apps/tutoring/conversational_tutor.py:1616-1640, 1646-1651`

---

### 4. Errors in assessing student responses

**Feedback:** (a) Inaccuracies caused by question framing — asks "which number is smallest" but explanation focuses on hundreds-place digit. (b) Correct answers assessed as incorrect.

**Root cause (keyword fallback):** `_keyword_evaluate_response()` at line 2798-2806 checks the **tutor's response** (not the student's answer) for success keywords like `"correct"`, `"right"`, `"great"`. A tutor response saying *"That's not quite right"* contains `"right"` → false positive. This is a fundamental design flaw.

**Root cause (LLM evaluation):** `_llm_evaluate_response()` at line 2768-2775 has incomplete context — no `answer_type` validation, no check that the `expected_answer` semantically matches the question, and the rubric from the step is not consistently included in the evaluation prompt.

**Root cause (question-explanation mismatch):** The screenshot in the feedback shows a question asking "which number is smallest" but the explanation focuses on hundreds-place comparison. This originates from the content generation step — no structural validation that question framing and `expected_answer` are semantically consistent.

**Files:** `apps/tutoring/conversational_tutor.py:2798-2806, 2768-2775, 2289-2291`

---

## B. User Interface / Experience

### 1. Dashboard — no unit/lesson structure explanation

**Feedback:** Provide a short explanation of how each unit and lesson is structured.

**Root cause:** `Unit.description` field exists in the DB (`curriculum/models.py:54`) but `catalog.html:160-163` only renders `unit.title` and lesson count — never the description.

**Files:** `apps/curriculum/models.py:54`, `templates/tutoring/catalog.html:160-163`

---

### 2. Dashboard — units not collapsible

**Feedback:** Allow units to be collapsible/minimizable.

**Root cause:** No collapse/expand JavaScript or CSS exists for unit cards in `catalog.html`. The `.lessons-list` is always rendered inline. Contrast with the trophy case which *does* have `toggleTrophyCase()`.

**Files:** `templates/tutoring/catalog.html:165-202`

---

### 3. Dashboard — units/lessons not numbered

**Feedback:** Number units and lessons, ideally consistent with textbook sequencing.

**Root cause:** Both `Unit` and `Lesson` models have `order_index` fields used for ordering, but `catalog.html` never renders them. Template shows `{{ unit.title }}` and `{{ lesson.title }}` with no numbering prefix.

**Files:** `apps/curriculum/models.py:55,108`, `templates/tutoring/catalog.html:162,192`

---

### 4. Dashboard — no analytics in gamification section

**Feedback:** Add analytics (hours studied, lessons completed, average quiz accuracy).

**Root cause:** `total_practice_time_minutes` and `total_sessions` are tracked in `StudentKnowledgeProfile` but never displayed. Quiz accuracy is never calculated. The `get_gamification_data()` view returns `mastered_lessons_count` but the frontend does not use it.

**Files:** `apps/tutoring/skills_models.py:624-631`, `apps/tutoring/views.py:936-1028`, `templates/tutoring/catalog.html:54-68`

---

### 5-7. Too much text / simpler language / alternative formatting

**Feedback:** (5) Too much text per lesson — negatively impacts engagement. (6) Use simpler language, shorter text, break into multiple messages. (7) Use symbols, icons, variety in structuring text.

**Root cause (prompt):** The system prompt *does* instruct `"~50 words total"` and `"never produce a wall of text"` (lines 89-98, 296-298), but these are soft LLM suggestions with no enforcement.

**Root cause (no splitting):** A single `TutorMessage` is returned per student input (line 1108). There is no post-processing to split long responses into multiple shorter chat bubbles.

**Root cause (formatting):** The system prompt gives no guidance on using emoji/symbols, bullet points, bold/italics for key terms, or varying visual structure. Markdown rendering is supported (`marked.parse()`) but the LLM is not prompted to use it.

**Files:** `apps/tutoring/conversational_tutor.py:89-98, 296-298, 1108`, `templates/tutoring/chat_tutor.html`

---

## C. Bugs

### 1. TTS lag

**Feedback:** Noticeable lag after clicking the speaker icon.

**Root cause:** `speakResponse()` in `chat_tutor.html:1714` makes a synchronous fetch to `/tutor/api/speak/`, which calls ElevenLabs API in real-time (`audio_service.py:39-79`) with **no caching**. Every click re-synthesizes the full text.

**Files:** `templates/tutoring/chat_tutor.html:1714`, `apps/tutoring/audio_service.py:39-79`, `apps/tutoring/views.py:833-892`

---

### 2. TTS reads wrong message

**Feedback:** Sometimes reads the previous message even when the user has moved to the next one.

**Root cause:** Race condition in the auto-play patch at `chat_tutor.html:1900-1909`. When a new message arrives, `querySelectorAll('.message.tutor .bubble')[msgs.length - 1]` grabs the last bubble, but an in-flight TTS fetch from a previous message may still complete and play. The `speakGeneration` counter does not fully prevent this because multiple fetches can overlap.

**Files:** `templates/tutoring/chat_tutor.html:1900-1909, 1691`

---

### 3. Audio continues after mute

**Feedback:** Audio continues playing even after clicking the mute button.

**Root cause:** `toggleAudioMode()` at `chat_tutor.html:1596-1607` sets `audioMode = false` and updates UI, but **never calls `stopCurrentAudio()`**. The currently playing `<audio>` element keeps playing.

**Files:** `templates/tutoring/chat_tutor.html:1596-1607`

---

### 4. Chat freezing

**Feedback:** The chat interface occasionally became stuck and required a page refresh to continue.

**Root cause:** The TTS fetch in `speakResponse()` has **no timeout and no `AbortController`**. If the ElevenLabs API hangs, the browser waits indefinitely. Combined with potential memory leaks from un-revoked blob URLs and the RAF-based `highlightLoop()` that can become a zombie loop if the audio element is GC'd before cleanup.

**Files:** `templates/tutoring/chat_tutor.html:1714-1837, 1747-1776`

---

## Priority Map

| # | Issue | Category | Severity | Fix Complexity |
|---|-------|----------|----------|----------------|
| C3 | Audio continues after mute | Bug | High | **Low** — add `stopCurrentAudio()` to toggle |
| C4 | Chat freezing | Bug | Critical | **Medium** — add AbortController + timeout to TTS fetch |
| C2 | TTS reads wrong message | Bug | High | **Medium** — bind TTS to specific message ID |
| C1 | TTS lag | Bug | High | **Medium** — add server-side TTS caching |
| A4 | Answer assessment errors | Learning | Critical | **High** — fix keyword fallback, improve LLM eval context |
| A1 | No stated learning objectives | Learning | High | **Low** — add instruction to opening prompt |
| A3 | No prior knowledge recall | Learning | High | **Medium** — structured prerequisite review step |
| A2 | Weak question quality | Learning | High | **High** — content generation prompt + validation |
| B1-4 | Dashboard UX items | UX | Medium | **Low-Medium** — template changes + expose existing data |
| B5-7 | Response verbosity/formatting | UX | Medium | **Medium** — prompt tuning + response post-processing |
