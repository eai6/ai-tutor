# LLM Configuration Issue — Tutor Fallback Regression

**Date:** 2026-03-17
**Severity:** Critical — all tutoring sessions affected
**Branch:** `fix/human-eval-feedback-2026-03-11`

---

## Symptoms

1. **Tutor assumes prior work on new lessons.** Opening message says "Good effort! Let's explore this together" to a student who has never started the lesson.
2. **Tutor ignores student input.** Student says "I have not started this lesson" and tutor replies with the same generic message.
3. **Resumed lessons get garbage messages.** Previously working sessions return fallback text on resume, which gets persisted to the database as real conversation turns.

All three symptoms appear on every lesson, for every student.

---

## Root Cause

There are two independent issues that combine to produce the regression.

### Issue 1: LLM provider mismatch (infrastructure)

The `ModelConfig` record for `tutoring` in the database is configured as:

| Field | Value |
|-------|-------|
| `provider` | `google` |
| `model_name` | `gemini-3.1-pro-preview` |
| `api_key_env_var` | `GOOGLE_API_KEY` |

The `.env` file only contains:

```
ANTHROPIC_API_KEY=sk-ant-api03-...
```

`GOOGLE_API_KEY` is not set. When the tutor initializes the LLM client:

1. `ModelConfig.get_for('tutoring')` returns the Google config.
2. The Google client constructor calls `os.environ['GOOGLE_API_KEY']` — not found.
3. It raises `ValueError("API key not found. Set GOOGLE_API_KEY environment variable")`.
4. The `llm_client` property catches the exception and returns `None`.
5. `_generate_response()` sees `llm_client is None` and returns `_fallback_response()`.
6. Every tutor message is a random fallback string.

### Issue 2: Fallback responses are inappropriate (code — introduced in PR #2)

The `_fallback_response()` method returns hardcoded strings that assume the student has been actively working:

- *"Good effort! Let's explore this together."*
- *"That's interesting! Let me think about that."*

These are used as opening messages, resume messages, and mid-conversation responses without any context awareness. Additionally:

- `resume()` persists fallback messages to the database as real tutor turns.
- The opening prompt unconditionally instructs the LLM to "recall prior knowledge from earlier lessons" even when the student has none.
- `_get_retrieval_context()` lists previous lessons as "learned" without checking `StudentLessonProgress`.

The silent `except Exception` in `_generate_response()` logged a generic error with no session context, making the provider mismatch invisible.

---

## Fixes Applied

### Infrastructure fix

Update the `ModelConfig` to match the available API key. Run in Django shell:

```python
from apps.llm.models import ModelConfig
config = ModelConfig.get_for('tutoring')
config.provider = 'anthropic'
config.model_name = 'claude-sonnet-4-6-20250514'
config.api_key_env_var = 'ANTHROPIC_API_KEY'
config.save()
```

Or add `GOOGLE_API_KEY=<key>` to `.env` if Google Gemini is the intended provider.

### Code fixes (all in `apps/tutoring/conversational_tutor.py`)

| # | Fix | Description |
|---|-----|-------------|
| 1 | Context-aware fallbacks | `_fallback_response()` now takes a `context` parameter (`opening`, `resume`, `conversation`). Opening and resume fallbacks pull real practice questions from lesson steps instead of generic text. |
| 2 | Don't persist fallback resume messages | `resume()` checks `_last_response_was_fallback` and skips `_save_turn()` to prevent garbage from polluting conversation history. |
| 3 | Conditional prior knowledge in opening | Opening prompt instruction #3 only asks the LLM to recall prior knowledge when the student actually has completed prior lessons. For first lessons, it says "do NOT reference prior lessons." |
| 4 | Verify student completion in retrieval context | `_get_retrieval_context()` now queries `StudentLessonProgress` to confirm the student actually started or completed previous lessons before listing them. |
| 5 | Soften grade calibration principle | Changed "acknowledge their prior knowledge" to "if the student demonstrates prior knowledge, acknowledge it." |
| 6 | Fix word limit inconsistency | Standardized all word-limit references to ~60 words (three stale "50 words" references from PR #2). |
| 7 | Improved error logging | `_generate_response()` now logs session ID, lesson title, and full traceback on failure. Logs a WARNING when `llm_client` is `None`. |

### Test fix

Updated `test_opening_prompt_recalls_prior_knowledge` in `test_r9_system_prompt.py` to expect the new conditional behavior (first lesson should say "do NOT reference prior lessons").

---

## Verification

All 122 unit tests pass. Manual verification confirmed:

- Opening fallback: *"Welcome! Before we start What is Geography?, let's review what you already know — Which of the following activities do you think is a great example of studying geography?"*
- Resume fallback: *"Welcome back! Let's continue with What is Geography?. Let us review what we covered last time — Which of the following is the most complete definition of Geography?"*
- Resume fallback was NOT persisted to the database (turn count unchanged).
- First lesson retrieval context correctly returns "no previous topics to review."
- Error log now clearly shows: `No LLM client available for session=13 lesson='What is Geography?'`
