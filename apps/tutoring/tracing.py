"""Per-turn trace logging — Phase 1 of memory/agentic_platform_architecture_plan.md.

Records one TurnSpan per LLM call / tool call / judge fire / regen pass that
contributes to producing a tutor turn. Spans are accumulated in a
ContextVar-bound buffer during the turn's generation, then flushed to
``TurnSpan`` rows after the parent ``SessionTurn`` is saved.

Design choices:

- **ContextVar** (not ``threading.local``) so the buffer can be propagated
  to ``ThreadPoolExecutor`` workers via ``contextvars.copy_context()``.
  Judge fan-out depends on this — without context propagation, spans from
  judge worker threads would be lost.

- **Buffered, flushed once.** Spans are appended in memory until the parent
  ``SessionTurn`` is created, then bulk-inserted. Avoids the chicken/egg of
  emitting spans before the parent row exists.

- **No-op when no buffer is active.** Safe to call ``emit_span()`` from any
  context — curriculum generation, skill extraction, background tasks etc.
  will simply pass through without recording. Only tutor-turn paths set the
  buffer.

- **Never block the operation.** A failure to persist spans logs a warning
  and continues. Tracing is observability, not correctness.

Reference for span shape: OpenTelemetry GenAI semantic conventions —
``gen_ai.system``, ``gen_ai.request.model``, ``gen_ai.usage.*``. We adopt
the *shape* without depending on the full OTel SDK.

See ``memory/agentic_platform_architecture_plan.md`` (Phase 1) and
``CLAUDE.md`` (Architecture section).
"""
from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from django.utils import timezone

logger = logging.getLogger(__name__)

# Buffer of pending-span dicts. When ``None``, ``emit_span()`` is a no-op.
_span_buffer: "contextvars.ContextVar[Optional[List[Dict[str, Any]]]]" = (
    contextvars.ContextVar('turn_span_buffer', default=None)
)


def start_span_buffer() -> contextvars.Token:
    """Begin collecting spans for the current tutor turn.

    Returns a token to be passed to ``reset_span_buffer`` in a finally block.
    """
    return _span_buffer.set([])


def reset_span_buffer(token: contextvars.Token) -> None:
    """Clear the span buffer. Pair with ``start_span_buffer`` in try/finally."""
    _span_buffer.reset(token)


def flush_spans(turn_id: int) -> int:
    """Persist accumulated spans with the given ``SessionTurn`` as parent.

    Called after the tutor turn's SessionTurn row is saved (its ID is then
    known). Returns the number of spans persisted. Safe to call when no
    buffer is active — returns 0. Clears the buffer on success so a
    subsequent flush doesn't double-write.
    """
    buffer = _span_buffer.get()
    if not buffer:
        return 0

    # Import inside the function to avoid circular imports at module load.
    from apps.tutoring.models import TurnSpan

    rows: List[TurnSpan] = []
    for s in buffer:
        rows.append(TurnSpan(
            turn_id=turn_id,
            kind=s.get('kind', 'other'),
            name=(s.get('name') or '')[:100],
            model=(s.get('model') or '')[:80],
            purpose=(s.get('purpose') or '')[:40],
            started_at=s.get('started_at') or timezone.now(),
            duration_ms=int(s.get('duration_ms') or 0),
            tokens_in=s.get('tokens_in'),
            tokens_out=s.get('tokens_out'),
            payload=s.get('payload') or {},
            error=(s.get('error') or '')[:5000],
        ))

    try:
        TurnSpan.objects.bulk_create(rows)
    except Exception as exc:  # belt-and-braces — never block the turn
        logger.warning("[TurnSpan] failed to flush %d spans: %s", len(rows), exc)
        return 0

    buffer.clear()
    return len(rows)


def traced_judge(judge_name: str):
    """Decorator: wrap a judge function in a ``kind='judge'`` span.

    Usage::

        @traced_judge('arithmetic')
        def run_arithmetic_judge(...): ...

    The span captures total judge duration (including pre-gates and any
    LLM calls inside). LLM calls inside still emit their own
    ``kind='llm_call'`` spans via ``BaseLLMClient.generate()``, giving a
    two-level trace: judge → underlying LLM call. No parent-child
    relationship is recorded in v1; both spans share the same parent
    turn and ordering by ``started_at``.

    If the judge returns an object with a ``skipped`` attribute, it's
    recorded in the span payload along with ``skip_reason`` when present.
    """
    import functools

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with emit_span('judge', judge_name) as span:
                result = fn(*args, **kwargs)
                if span is not None:
                    payload: Dict[str, Any] = {}
                    if hasattr(result, 'skipped'):
                        payload['skipped'] = bool(getattr(result, 'skipped', False))
                    if hasattr(result, 'skip_reason'):
                        payload['skip_reason'] = str(getattr(result, 'skip_reason', '') or '')[:200]
                    if payload:
                        span['payload'] = payload
                return result
        return wrapper
    return decorator


@contextmanager
def emit_span(kind: str, name: str, *,
              model: str = '', purpose: str = '',
              payload: Optional[Dict[str, Any]] = None):
    """Record a span if a buffer is active; no-op otherwise.

    Usage::

        with emit_span('llm_call', 'generate', model=cfg.model_name) as span:
            response = call_llm(...)
            if span is not None:
                span['tokens_in'] = response.tokens_in
                span['tokens_out'] = response.tokens_out

    The yielded ``span`` is a mutable dict the caller can populate with
    ``tokens_in``, ``tokens_out``, or extra payload keys. On exception the
    error is recorded and re-raised. Duration is captured automatically.
    """
    buffer = _span_buffer.get()
    if buffer is None:
        # No active turn context — execute the body without recording.
        yield None
        return

    start_monotonic = time.monotonic()
    span_data: Dict[str, Any] = {
        'kind': kind,
        'name': name,
        'model': model,
        'purpose': purpose,
        'payload': dict(payload) if payload else {},
        'started_at': timezone.now(),
        'error': '',
    }
    try:
        yield span_data
    except Exception as exc:
        span_data['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        span_data['duration_ms'] = int((time.monotonic() - start_monotonic) * 1000)
        buffer.append(span_data)
