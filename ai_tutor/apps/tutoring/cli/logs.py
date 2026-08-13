"""Logging control for the terminal tutor client.

Default is a clean transcript: the tutor and the student, nothing else. The
engine is chatty at INFO ([simple_tutor], _call_llm, [OllamaTools], [KB],
maybe_advance_step…), which is invaluable when debugging and pure noise when you
are reading pedagogy.

`--debug` reverses both halves: logs stay on screen AND the whole session —
conversation and engine logs, interleaved in order — is written to a file.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

# The conversation is emitted through logging rather than written separately so
# that it interleaves with engine logs in the debug file in true chronological
# order. Two independent writers to one file would not.
#
# propagate=False: this logger feeds the debug FILE only. The terminal already
# shows the conversation through the normal render path, so letting these
# records reach the 'apps' console handler prints every reply twice.
#
# Setting it here is necessary but NOT sufficient, which is why quiet() and
# start_debug_log() re-assert it. If this module is imported before
# django.setup(), Django's logging dictConfig runs afterwards and
# logging.config._handle_existing_loggers resets propagate=True (and clears
# handlers and level) on every already-existing logger that is a child of a
# configured one — 'apps' is configured, so this logger qualifies. The reset is
# silent and the only symptom is doubled output.
transcript = logging.getLogger('ai_tutor.apps.tutoring.cli.transcript')
transcript.propagate = False


def _isolate() -> None:
    """Re-assert the no-propagate invariant. Cheap and idempotent."""
    transcript.propagate = False

# Chatty third parties. config/settings.py gives 'apps' its own handler with
# propagate=False, so raising the root level alone does not silence it — it has
# to be named explicitly.
_NOISY = ('apps', 'httpx', 'httpcore', 'urllib3', 'chromadb', 'sentence_transformers')


def quiet() -> None:
    """Suppress engine logs so only the conversation reaches the terminal.

    ERROR, not WARNING. WARNING looks like the cautious choice but is not: the
    engine logs routine, expected conditions at warning level on nearly every
    turn — `[OllamaTools] tool_choice … NOT forwarded` fires twice per turn by
    design, and `[simple_tutor] call2_repair` is a normal repair path. Leaving
    those visible defeats the point of clean mode. Real failures still surface,
    both as ERROR records and as the engine's own fallback reply text.
    """
    logging.getLogger().setLevel(logging.ERROR)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.ERROR)
    _isolate()
    transcript.setLevel(logging.INFO)


def start_debug_log(log_dir: Path) -> Path:
    """Tee everything to a timestamped file and return its path.

    Called BEFORE the session is created so the file also captures bootstrap,
    which is why the filename is timestamp-based rather than session-based — the
    session id does not exist yet. It is logged into the file instead.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"tutor_chat_{datetime.now():%Y%m%d_%H%M%S}.log"

    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-7s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    ))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for name in _NOISY:
        log = logging.getLogger(name)
        log.setLevel(logging.INFO)
        # 'apps' does not propagate to root, so the file handler has to be
        # attached to it directly or none of the engine's own logs land here.
        if not log.propagate:
            log.addHandler(handler)
    # transcript never propagates, so it needs the file handler directly. This
    # is what puts STUDENT/TUTOR turns in the file alongside the engine logs.
    _isolate()
    transcript.setLevel(logging.INFO)
    transcript.addHandler(handler)
    return path
