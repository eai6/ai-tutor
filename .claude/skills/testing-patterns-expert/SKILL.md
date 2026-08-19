---
name: testing-patterns-expert
description: Testing patterns that catch the bugs unit tests miss — concurrency races, fail-soft swallowing, prod-only failures. Auto-loads when writing or modifying test files. Grounded in real incidents on this codebase (notably the 2026-05-12 contextvars-shared-Context bug where 7 of 8 judges silently failed in prod for ~24h while passing every local test).
paths:
  - "apps/**/tests/**/*.py"
  - "**/test_*.py"
  - "**/tests.py"
  - "pytest.ini"
  - "conftest.py"
---

# Testing Patterns — AI Tutor

When local tests pass but prod still bites you, the test wasn't actually exercising the failure path. This skill is a checklist of patterns that close those gaps. Grounded in real incidents on this codebase.

## The reflex: prove your test catches the bug

**Always**: before declaring a regression test "done", run it against the BROKEN code and confirm it fails. If the test passes on both broken and fixed code, it isn't testing what you think.

Quick recipe:
```bash
git stash -- <changed_file>      # un-apply the fix
python manage.py test <new_test> # MUST fail
git stash pop                    # restore the fix
python manage.py test <new_test> # MUST pass
```

If you can't repro the failure with the test reverted, your test is theatre.

---

## Concurrency: mocks lie about timing

### The trap

Every existing judge / regen / agent test in this codebase uses `MagicMock` LLM clients that return in microseconds. When mocks return instantly:
- A `ThreadPoolExecutor` submits a job, the worker runs it before the next submission, no two workers are ever in-flight simultaneously.
- Race conditions that need overlap (lock contention, shared-resource entry, `contextvars.Context` re-entry) never fire.
- Your test passes. Production fails.

### Real incident (2026-05-12)

`apps/tutoring/judges/__init__.py::run_all_judges` shared one `contextvars.Context` across 8 thread-pool submissions:
```python
ctx = contextvars.copy_context()  # ONE copy
ex.submit(ctx.run, run_arithmetic_judge, ...)
ex.submit(ctx.run, run_factual_judge, ...)   # same ctx
# ... 8 judges total
```

A `Context` cannot be entered concurrently by multiple threads — Python raises `RuntimeError: cannot enter context: ... is already entered`. **7 of 8 judges silently failed for ~24 hours in prod** before anyone noticed (the fail-soft `_safe_result` wrapper hid it; sessions kept working with empty `judge_outputs`).

Every local test passed. Why? Mocked judges returned instantly → no two threads ever in `ctx.run` simultaneously → no race.

### The fix in tests

Inject realistic latency in mocks for parallel code paths:

```python
import time
from unittest.mock import patch

def _sleepy(result_cls):
    def _impl(*args, **kwargs):
        time.sleep(0.05)  # 50ms — enough for real overlap
        return result_cls()
    return _impl

@patch("ai_tutor.apps.tutoring.judges.run_arithmetic_judge")
def test_no_race_in_orchestrator(self, m_arith):
    m_arith.side_effect = _sleepy(ArithmeticResult)
    # ... patch the others similarly
    result = run_all_judges(...)
    # Verify NONE of them landed in sub_skipped:
    self.assertEqual(
        [n for n, r in result.sub_skipped.items()
         if 'RuntimeError' in r], []
    )
```

50ms × N parallel mocks ≈ 50ms total when concurrency works (the point of the thread pool) and ~N×50ms when it doesn't — the latency difference is itself a useful signal.

See `apps/tutoring/tests/test_judges_concurrency.py` for the canonical pattern.

### When to apply

Any test that exercises:
- `concurrent.futures.ThreadPoolExecutor` / `ProcessPoolExecutor`
- `asyncio.gather` / `asyncio.wait`
- Database transactions across threads
- `contextvars.Context.run()` (the bug above)
- File-locking or signal handlers
- Any "fan out N calls, collect results" pattern

If the code under test has more than one `submit()` or `gather()`, write a latency-injected variant of the test.

---

## Fail-soft wrappers must surface enough signal to debug

### The trap

Patterns like:
```python
try:
    return future.result()
except Exception as e:
    logger.warning("X failed: %s", e)
    return DefaultResult(skipped=True, skip_reason=f"exception: {type(e).__name__}")
```

look responsible — they prevent one judge crashing from poisoning the others. But when the persisted artefact (DB row, JSON blob) only carries the **type name** ("RuntimeError"), debugging requires correlating against logs, which may already be rotated.

### The fix

Include the message in the persisted artefact, truncated to bound size:

```python
msg = str(e).strip().replace("\n", " ")[:200]
skip_reason = f"exception: {type(e).__name__}: {msg}" if msg else f"exception: {type(e).__name__}"
logger.warning("X failed", exc_info=True)  # full traceback to logs
```

### When to apply

Any time you write `except Exception` and persist the result. Particularly important for:
- LLM call wrappers (rate limits, timeouts, content filters all surface differently)
- Background task results
- Judge / validator / verifier results that store to `metadata`
- Webhook handlers that persist to a queue

---

## Production log archaeology

### The trap

`az containerapp logs show` is a **streaming slice**, not historical query. `--tail` caps at 300, and most of those will be uninteresting startup spam (sentence-transformers batch loaders, etc.).

### The fix

Use `az monitor log-analytics query` against the workspace ID:

```bash
# Find the workspace ID
az containerapp env show --query 'properties.appLogsConfiguration.logAnalyticsConfiguration.customerId' -o tsv

# Then query KQL
az monitor log-analytics query \
  --workspace <workspace-id> \
  --analytics-query "
    ContainerAppConsoleLogs_CL
    | where TimeGenerated > ago(6h)
    | where Log_s contains 'YOUR_FILTER'
    | project TimeGenerated, Log_s
    | order by TimeGenerated desc
    | take 100
  " -o tsv
```

KQL beats grep on streamed logs every time. Use it.

---

## "All N things fail with the same error" is an orchestrator bug

### The signal

When you see N parallel things all fail with the **same exception type**, suspect the orchestrator, not the individual things.

Per-thing bugs diversify across exception types (one judge has a JSON parse bug, another has a missing kwarg, another has a network timeout). When 7 of 8 raise `RuntimeError` with the same message, that's a pattern.

The 2026-05-12 incident displayed this perfectly:
```
[Judges] coherence judge raised: cannot enter context: ... is already entered
[Judges] factual judge raised: cannot enter context: ... is already entered
[Judges] rule judge raised: cannot enter context: ... is already entered
[Judges] step_eval judge raised: cannot enter context: ... is already entered
[Judges] safety judge raised: cannot enter context: ... is already entered
[Judges] figure_ref judge raised: cannot enter context: ... is already entered
[Judges] figure_vision judge raised: cannot enter context: ... is already entered
```

`figure_ref` is deterministic — no LLM call. If a "no-LLM" thing fails the same way as 6 LLM things, the cause is upstream of LLM calls.

---

## Testing fixtures: read the model field names

Pylance doesn't catch these. When constructing test fixtures, the field names matter:

| Model | Correct field | Common mistake |
|---|---|---|
| `Institution` | `slug` | `subdomain` |
| `Unit`, `Lesson` | `order_index` | `order` |
| `BenchmarkAnnotation` | `annotator_user` (FK) | `user` |
| `TutorSession` | `student` (FK to User) | `user` |

Mirror an existing test file before inventing fixture shapes. See `apps/tutoring/tests/test_question_bank.py::QuestionBankHelpersTest.setUpTestData` for the canonical Course → Unit → Lesson chain.

---

## When tests pass but you're suspicious

A few smells:

| Smell | What to check |
|---|---|
| "It works on my machine" | Are mocks returning instantly? Try with `time.sleep(0.05)` in the side_effect. |
| Test asserts on `len(result)` but never on content | The mock probably matches its own output. Add `assertNotEqual(result, default)`. |
| `setUp` runs in <10ms | If your code under test does I/O, your test probably isn't either. |
| One test fails when run alone but passes in the suite | State leak from a previous test. Add `TransactionTestCase` or `setUpTestData`. |
| All N things fail with the same error | Orchestrator bug, not per-thing bug. |

---

## Sources

- `apps/tutoring/tests/test_judges_concurrency.py` — canonical latency-injection pattern
- `~/.claude/projects/.../memory/feedback_concurrency_testing_patterns.md` — incident write-up
- Python docs: `contextvars.Context.run()` — note re-entry rule
- Anthropic blog: [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) — similar lessons on observability

## When this skill auto-loads

`paths` in frontmatter:
- `apps/**/tests/**/*.py`
- `**/test_*.py`
- `**/tests.py`
- `pytest.ini`, `conftest.py`

Pairs naturally with `django-expert` and `tutoring-engine-expert`.
