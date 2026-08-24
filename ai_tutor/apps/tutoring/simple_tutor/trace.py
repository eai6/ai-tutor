"""Structured per-turn trace for debugging tutoring runs.

Written because every diagnosis in the 2026-08-23 eval had to be done by
grepping unstructured console output, and several questions could not be
answered from it at all:

  * "Did the bank's <explanation> actually reach the model?" — tool-result
    bodies are never logged, so the answer was unknowable from the log. The
    27b's reply proved it arrived; the 4b's silence proved nothing either way.
  * "How far has this sweep got?" — answered by grepping `session=` ids.
  * "Why did this session deadlock?" — answered by counting occurrences of a
    substring across a 45k-line file.
  * "Is this latency real or does it include a retry?" — unanswerable.
  * "Which Ollama did this call reach?" — the laptop's or the rented box's?
    That one silently cost a whole run.

One JSON object per turn, appended to a file. Grep still works, but so does
`json.loads`, which is the point: a question about a run should be a query,
not a text search.

OFF unless TUTOR_TRACE_DIR is set, so production writes nothing and pays only
an env lookup per turn. Never raises — a broken tracer must not break a lesson.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ENV = 'TUTOR_TRACE_DIR'

# Tool-result bodies are the whole reason this exists, but a full <question_pool>
# is several KB and would bloat the file past usefulness. Enough to answer "did
# the model see it", not enough to reconstruct the prompt.
_MAX_FIELD = 2000


def enabled() -> bool:
    return bool((os.environ.get(_ENV) or '').strip())


def _path(session_id: Any) -> str | None:
    root = (os.environ.get(_ENV) or '').strip()
    if not root:
        return None
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        return None
    # One file per run, not per session: a sweep's turns interleave in time and
    # a single file keeps that order. session_id is a field, so splitting later
    # is a filter.
    name = (os.environ.get('TUTOR_TRACE_NAME') or 'turns').strip() or 'turns'
    return os.path.join(root, f'{name}.jsonl')


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD:
        return value[:_MAX_FIELD] + f'…[+{len(value) - _MAX_FIELD} chars]'
    return value


def emit(**fields: Any) -> None:
    """Append one turn record. Silent no-op when tracing is off.

    Never raises: wrapped whole, because a tracer that can break a student's
    lesson is worse than no tracer.
    """
    try:
        if not enabled():
            return
        path = _path(fields.get('session_id'))
        if not path:
            return
        record = {k: _clip(v) for k, v in fields.items()}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
    except Exception:                                        # noqa: BLE001
        logger.debug('trace emit failed', exc_info=True)
