"""Formatting for the terminal tutor client.

Pure functions: dict in, string out. No printing, no TTY access, no Django. That
keeps them unit-testable without a terminal, and keeps the management command a
thin I/O loop.

Standard library only — no `rich`, no `textual`. `rich` is not a dependency of
this project and adding one to a memory-constrained box for colour is a bad
trade (memory/terminal_tutor_client_plan.md).
"""
from __future__ import annotations

import json
from typing import Any

# ANSI codes. Callers pass colour=False to get plain text (not a TTY, piped
# output, --no-color), in which case every helper below emits nothing extra.
_CODES = {
    'reset': '\033[0m', 'dim': '\033[2m', 'bold': '\033[1m',
    'cyan': '\033[36m', 'green': '\033[32m', 'yellow': '\033[33m',
    'red': '\033[31m', 'blue': '\033[34m', 'magenta': '\033[35m',
}


def paint(text: str, *styles: str, colour: bool = True) -> str:
    if not colour or not styles:
        return text
    prefix = ''.join(_CODES.get(s, '') for s in styles)
    return f"{prefix}{text}{_CODES['reset']}"


def format_reply(payload: dict, *, colour: bool = True) -> str:
    """The tutor's visible message — what a student would read in the browser."""
    return paint(payload.get('message') or '', 'cyan', colour=colour)


def format_reply_chunk(text: str, *, colour: bool = True) -> str:
    """One streamed fragment, styled like `format_reply`.

    Separate from format_reply because that one takes the whole payload dict
    and a stream has no payload yet — only text.
    """
    return paint(text, 'cyan', colour=colour)


def stream_delta(previous: str, current: str) -> tuple[str, bool]:
    """Diff two cumulative stream snapshots into something a terminal can print.

    The stream gate emits progressively longer SNAPSHOTS, not deltas, because
    the browser re-renders a whole bubble. A terminal cannot un-print, so it
    needs the new tail instead.

    Returns ``(text_to_print, restarted)``. ``restarted`` is True when
    ``current`` is not a continuation of ``previous`` — which happens when a
    transient retry replays the generation (the gate's begin_attempt drops the
    dead attempt), or when the final batch pipeline rewrites text that was
    already shown (``_dedupe_reply`` prepends; the exit-ticket branch of
    ``respond_for_view`` replaces outright). The caller is expected to start a
    fresh line and reprint rather than append.

    Also used at end-of-turn to append whatever the stream never reached: the
    last sentence usually has no trailing boundary, so it is never emitted as
    a snapshot and arrives only in the final payload.
    """
    if not previous:
        return current, False
    if current.startswith(previous):
        return current[len(previous):], False
    return current, True


def format_state(payload: dict, *, colour: bool = True) -> str:
    """Step position and grading verdict for the turn.

    ``is_correct`` is None on turns where the tutor taught rather than graded,
    which is a different thing from "graded and wrong" — rendered distinctly so
    the two are not confused while debugging.
    """
    step = payload.get('step_number')
    total = payload.get('total_steps')
    bits = []
    if step is not None and total:
        bits.append(f"step {step}/{total}")
    if payload.get('phase'):
        bits.append(str(payload['phase']))
    verdict = payload.get('is_correct')
    if verdict is True:
        bits.append(paint('correct', 'green', colour=colour))
    elif verdict is False:
        bits.append(paint('incorrect', 'red', colour=colour))
    else:
        bits.append(paint('not graded', 'dim', colour=colour))
    if payload.get('show_exit_ticket'):
        bits.append(paint('EXIT TICKET', 'magenta', 'bold', colour=colour))
    if payload.get('is_complete'):
        bits.append(paint('COMPLETE', 'magenta', 'bold', colour=colour))
    if payload.get('media'):
        urls = [m.get('url') for m in payload['media'] if m.get('url')]
        if urls:
            bits.append(f"media={' '.join(urls)}")
    return paint('  [' + ' · '.join(bits) + ']', 'dim', colour=colour)


def format_tools(metadata: dict | None, *, colour: bool = True) -> str:
    """Tool calls the engine made this turn.

    The browser hides these entirely; they are the main reason to use this
    client over the chat UI when debugging tool-protocol compliance on a small
    local model.
    """
    calls = (metadata or {}).get('tool_calls') or []
    if not calls:
        return paint('  (no tool calls)', 'dim', colour=colour)
    lines = []
    for entry in calls:
        tool = entry.get('tool') or '?'
        result = entry.get('result') or {}
        verdict = result.get('verdict')
        summary = f"verdict={verdict}" if verdict else _compact(result)
        lines.append(
            '  ' + paint(f"→ {tool}", 'yellow', colour=colour) + f"  {summary}"
        )
    return '\n'.join(lines)


def format_judge(judge_outputs: dict | None, *, colour: bool = True) -> str:
    """Judge output persisted on the turn.

    Often empty for simple_tutor: the per-turn multi-axis judge belongs to the
    legacy conversational_tutor engine. What does appear here is grading-tier
    output from apps/tutoring/simple_tutor/grader.py. Says so explicitly rather
    than rendering a blank, so an empty judge block is not read as a bug.
    """
    if not judge_outputs:
        return paint(
            '  (no judge output — simple_tutor grades via grader.py tiers, '
            'not a per-turn combined judge)', 'dim', colour=colour,
        )
    lines = []
    for key, value in judge_outputs.items():
        lines.append('  ' + paint(f"{key}:", 'blue', colour=colour) + f" {_compact(value)}")
    return '\n'.join(lines)


def format_timing(seconds: float, turn: Any = None, *, colour: bool = True) -> str:
    """Wall-clock for the turn, plus token counts when the turn recorded them."""
    bits = [f"{seconds:.1f}s"]
    if turn is not None:
        tin = getattr(turn, 'tokens_in', None)
        tout = getattr(turn, 'tokens_out', None)
        if tin or tout:
            bits.append(f"in={tin or 0} out={tout or 0}")
            if tout and seconds > 0:
                bits.append(f"{tout / seconds:.1f} tok/s")
    return paint('  [' + ' · '.join(bits) + ']', 'dim', colour=colour)


def format_lesson_table(rows: list[tuple[int, str, str]]) -> str:
    """Lesson picker for --list-lessons."""
    if not rows:
        return ("No lessons with steps found. Load fixtures:\n"
                "  python manage.py loaddata evals/fixtures/institution.json "
                "evals/fixtures/lessons.json")
    width = max(len(r[1]) for r in rows)
    width = min(width, 34)
    out = [f"{'ID':>7}  {'COURSE':<{width}}  LESSON"]
    for lesson_id, course, title in rows:
        out.append(f"{lesson_id:>7}  {course[:width]:<{width}}  {title}")
    return '\n'.join(out)


def _compact(value: Any, limit: int = 110) -> str:
    """One-line, length-capped repr for nested tool/judge payloads."""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, default=str, separators=(',', ':'))
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + '…'
