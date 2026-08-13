"""Chat with the tutor from a terminal, driving the real engine.

    python manage.py tutor_chat --list-lessons
    python manage.py tutor_chat --lesson 42
    python manage.py tutor_chat --lesson 42 --show-all

Calls ``start_for_view`` / ``respond_for_view`` — the SAME adapters
apps/tutoring/views.py uses (views.py:1214) — so what you see here is what the
chat UI would render. The raw ``respond()`` was deliberately not used: it skips
the step-progress, is_correct, media and exit-ticket projection that the browser
actually consumes, and would test a different surface than production serves.

FIDELITY GAP — this runs in-process, not over HTTP, so the view layer is NOT
exercised: ContentSafetyFilter PII redaction, RateLimiter, SafetyAuditLog and
session-ownership auth (views.py:1016) are all skipped. Debugging anything in
that layer needs the browser. Running in-process is the point: no dev server
means no extra ~200-400 MB on a box where the model already needs ~4.0 GB.

Plan: memory/terminal_tutor_client_plan.md
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ai_tutor.apps.tutoring.cli import logs as cli_logs
from ai_tutor.apps.tutoring.cli import render
from ai_tutor.apps.tutoring.cli.session import (
    BootstrapError, bootstrap_session, last_tutor_turn, list_lessons,
    resolve_lesson_id,
)

_QUIT = {'/quit', '/exit', '/q'}
_HELP = {'/help', '/?'}

# Friendly names for the local tags worth chatting against. Each maps to a spec
# with an exact entry in apps/llm/model_profiles.py — that matters, because a
# tag without one falls through to a CLOUD family profile and gets sized at
# num_ctx=24192, the window that does not fit this box.
#
# qwen3:8b and qwen3.5:9b are deliberately absent: 4.9 GB and 6.1 GB of weights
# against ~5.5 GB free. The fit preflight refuses them anyway, but offering them
# here would invite the attempt.
#
# No thinking checkpoints. They spend the box's scarce tokens/s on a monologue
# the student never sees, and on the Ollama path a tool call emitted into the
# reasoning channel has no salvage — _adapt_ollama_response never calls
# _recover_reasoning_tool_call. qwen3-4b-jetson is built FROM qwen3:4b-instruct
# precisely to avoid this.
# The cloud model --cloud resolves to, for comparing turn latency against the
# local tags above. Configurable: export TUTOR_CLOUD_MODEL to point it elsewhere
# without editing this file.
#
# Opus 4.7 rather than a 5-series model, deliberately. Two properties make it the
# one that needs no new plumbing:
#   - AnthropicClient._supports_temperature already knows 4.7 rejects
#     `temperature` with a 400 and omits it. Sonnet 5 and Opus 5 reject it too,
#     but the guard does not match them yet, so they 400 on the first turn.
#   - Omitting `thinking` runs 4.7 WITHOUT thinking, so a turn here is the same
#     shape of work as a local turn. On Sonnet 5 and Opus 5 the same omission
#     runs adaptive thinking, which would make the latency comparison measure a
#     reasoning pass the local model never does.
# Pointing TUTOR_CLOUD_MODEL at either of those is fine once the guard is
# widened and thinking is disabled explicitly — not before.
CLOUD_MODEL = os.environ.get('TUTOR_CLOUD_MODEL') or 'anthropic/claude-opus-4-7'

MODEL_ALIASES = {
    'qwen3-4b': 'local_ollama/qwen3-4b-jetson',
    'qwen3.5-4b': 'local_ollama/qwen3.5-4b-jetson',
    'qwen3.5-2b': 'local_ollama/qwen3.5-2b-jetson',
    # The num_ctx caveat below is local-only: the fit preflight lives in
    # OllamaClient, so a cloud spec never reaches it and no profile entry is
    # needed here.
    'cloud': CLOUD_MODEL,
    # Every alias points at a Modelfile-pinned tag, never a bare one. A bare tag
    # does not pin num_ctx, so the grader's verifier loads a separate
    # 4096-context runner and evicts the tutor on every graded turn. Adding a
    # model here means building it a tag first — copy an
    # infra/ollama/Modelfile.*-jetson and change the FROM line.
}


def _log_dir() -> Path:
    """Where --debug writes session logs. Gitignored."""
    return Path(getattr(settings, 'BASE_DIR', '.')) / 'logs' / 'tutor_chat'


class _StreamPrinter:
    """Prints stream snapshots to the terminal as they arrive.

    Passed to the engine as ``on_delta``. The engine hands it progressively
    longer SAFE SNAPSHOTS (see simple_tutor/stream_filter.py); this converts
    them to the tail-only writes a terminal needs, via render.stream_delta.

    Two details the buffered path never had to care about:

    * ``ending=''`` — Django's OutputWrapper appends a newline to every
      write, which would put each fragment on its own line.
    * ``flush()`` — the command never flushed before because every write
      ended in a newline and a TTY is line-buffered. A partial line is not
      flushed on its own, so without this the reply appears in 4 KB lumps
      when stdout is a pipe, i.e. exactly the wait streaming exists to
      remove.
    """

    def __init__(self, out, colour: bool):
        self._out = out
        self._colour = colour
        self.text = ''
        self.started = False

    def __call__(self, snapshot: str) -> None:
        tail, restarted = render.stream_delta(self.text, snapshot)
        if restarted:
            # A transient retry replayed the generation; what was printed
            # belongs to a dead attempt.
            self._out.write(
                '\n' + render.paint('  ↻ restarted:', 'dim',
                                    colour=self._colour), ending='')
        if not self.started:
            self._out.write('\n', ending='')
            self.started = True
        self._out.write(
            render.format_reply_chunk(tail, colour=self._colour), ending='')
        try:
            self._out.flush()
        except Exception:
            pass
        self.text = snapshot


class Command(BaseCommand):
    help = "Chat with the tutor in the terminal, driving the real engine."

    def add_arguments(self, parser):
        # --lesson and the subject selectors answer the same question, so
        # argparse rejects passing both rather than silently picking a winner.
        picker = parser.add_mutually_exclusive_group()
        picker.add_argument(
            '--lesson', type=int, default=None,
            help='Lesson id to tutor. Defaults to a lesson that has steps '
                 '(announced on start). Use --list-lessons to choose.',
        )
        picker.add_argument(
            '--subject', default=None, choices=('math', 'geography', 'science'),
            help='Pick a random lesson from this subject.',
        )
        picker.add_argument(
            '--math', dest='subject', action='store_const', const='math',
            help='Shorthand for --subject math.',
        )
        picker.add_argument(
            '--geography', dest='subject', action='store_const', const='geography',
            help='Shorthand for --subject geography.',
        )
        parser.add_argument(
            '--student', default=None,
            help='Username to run as. Defaults to the eval fixture student.',
        )

        model = parser.add_mutually_exclusive_group()
        model.add_argument(
            '--model', default=None,
            help=f"Tutor model: an alias ({', '.join(MODEL_ALIASES)}) or a raw "
                 f"provider/name spec. Defaults to qwen3-4b.",
        )
        for alias in MODEL_ALIASES:
            model.add_argument(
                f'--{alias}', dest='model', action='store_const', const=alias,
                help=f'Shorthand for --model {alias}.',
            )
        parser.add_argument(
            '--list-lessons', action='store_true',
            help='List lessons that have steps, then exit.',
        )
        parser.add_argument('--show-tools', action='store_true',
                            help="Show the engine's tool calls each turn.")
        parser.add_argument('--show-judge', action='store_true',
                            help='Show judge output persisted on the turn.')
        parser.add_argument('--show-state', action='store_true',
                            help='Show step position and grading verdict.')
        parser.add_argument(
            '--keep-progress', action='store_true',
            help='Keep this lesson\'s mastery state instead of clearing it. By '
                 'default every run starts the lesson fresh, so two models can '
                 'be compared on equal footing.',
        )
        parser.add_argument('--no-timing', action='store_true',
                            help='Hide the per-turn response time (shown by '
                                 'default).')
        parser.add_argument('--show-all', action='store_true',
                            help='Enable every --show-* flag.')
        stream = parser.add_mutually_exclusive_group()
        stream.add_argument(
            '--stream', dest='stream', action='store_true', default=None,
            help='Print the reply as it decodes instead of waiting for the '
                 'whole turn. Defaults to the TUTOR_STREAMING env var. Only '
                 'the second LLM call streams — the grader has to run first.',
        )
        stream.add_argument(
            '--no-stream', dest='stream', action='store_false',
            help='Force buffered output even when TUTOR_STREAMING is set.',
        )
        parser.add_argument(
            '--debug', action='store_true',
            help='Show engine logs and all diagnostics, and save the whole '
                 'session (conversation + logs) to logs/tutor_chat/.',
        )
        # No --no-color here: BaseCommand already defines it (along with
        # --force-color), and redefining it is an argparse conflict.

    def handle(self, *args, **opts):
        # Colour off when piped, so redirected output stays clean.
        colour = not opts.get('no_color') and sys.stdout.isatty()

        # Logging is configured before anything else runs, so bootstrap and the
        # first LLM call are already governed by it.
        log_path = None
        if opts['debug']:
            log_path = cli_logs.start_debug_log(_log_dir())
        else:
            cli_logs.quiet()

        if opts['list_lessons']:
            self.stdout.write(render.format_lesson_table(list_lessons()))
            return

        # Set before the engine runs — model_profiles and ModelConfig.get_for
        # both read TUTOR_MODEL_OVERRIDE from os.environ at call time, so this
        # takes effect for every turn in the session. Overwrites rather than
        # setdefault: chat.py has already installed its default, and an explicit
        # --model must win over it.
        if opts['model']:
            os.environ['TUTOR_MODEL_OVERRIDE'] = MODEL_ALIASES.get(
                opts['model'], opts['model'],
            )
        tutor_spec = os.environ.get('TUTOR_MODEL_OVERRIDE') or '(DB default)'

        # Clean by default: tutor, student, and the response time. The engine's
        # INFO commentary and the [step/tool/judge] annotations are debugging
        # instruments — useful when reading the machinery, noise when reading the
        # pedagogy. Timing is the exception and stays on: turn latency is the
        # headline number for a local model on this box (~19 s resident vs ~90 s
        # cold), it is how you notice a reload or a slow path, and one dim
        # bracket per turn does not intrude on the transcript.
        keys = ('tools', 'judge', 'state', 'timing')
        if opts['debug'] or opts['show_all']:
            show = dict.fromkeys(keys, True)
        else:
            show = {k: opts.get(f'show_{k}', False) for k in keys}
            show['timing'] = not opts['no_timing']

        # --stream / --no-stream override the env default, so the streaming
        # path is reachable here without exporting anything. The terminal has
        # none of the constraints the env var exists to protect (no Azure, no
        # gunicorn worker to pin), so an explicit flag is safe.
        from ai_tutor.apps.tutoring.simple_tutor.stream_filter import streaming_enabled
        streaming = (
            streaming_enabled() if opts.get('stream') is None
            else opts['stream']
        )

        try:
            lesson_id, note = resolve_lesson_id(opts['lesson'], opts['subject'])
            session = bootstrap_session(
                lesson_id, student_username=opts['student'],
                fresh=not opts['keep_progress'],
            )
        except BootstrapError as exc:
            raise CommandError(str(exc))

        if note:
            self.stdout.write(render.paint(note, 'dim', colour=colour))
        if log_path:
            self.stdout.write(render.paint(
                f"debug log: {log_path}", 'dim', colour=colour))
        cli_logs.transcript.info(
            "session=%s lesson=%s (%s)",
            session.pk, session.lesson.pk, session.lesson.title,
        )

        from ai_tutor.apps.tutoring.simple_tutor.engine import (
            respond_for_view, start_for_view,
        )

        self._banner(session, tutor_spec, colour, streaming=streaming)

        # Opening turn — the same warmup the browser fires on session start.
        try:
            t0 = time.time()
            payload = start_for_view(session)
            self._emit(session, payload, time.time() - t0, show, colour)
        except Exception as exc:                      # noqa: BLE001 — surface, don't crash
            self.stderr.write(f"opening turn failed: {type(exc).__name__}: {exc}")
            return

        while True:
            try:
                message = input(render.paint('\nyou> ', 'bold', colour=colour)).strip()
            except (EOFError, KeyboardInterrupt):
                self.stdout.write('\n' + self._closing(session, colour))
                return

            if not message:
                continue
            if message.lower() in _QUIT:
                self.stdout.write(self._closing(session, colour))
                return
            if message.lower() in _HELP:
                self.stdout.write(
                    "  /quit  end the session\n"
                    "  /help  this message\n"
                    "  Anything else is sent to the tutor as a student turn."
                )
                continue

            try:
                cli_logs.transcript.info("STUDENT: %s", message)
                t0 = time.time()
                printer = _StreamPrinter(self.stdout, colour) if streaming else None
                payload = respond_for_view(session, message, on_delta=printer)
                self._emit(session, payload, time.time() - t0, show, colour,
                           printer=printer)
            except Exception as exc:                  # noqa: BLE001
                # respond() is documented as never raising, so anything landing
                # here is from the view-adapter projection. Keep the loop alive
                # — losing a whole session to one bad turn wastes the model load.
                self.stderr.write(
                    render.paint(f"  turn failed: {type(exc).__name__}: {exc}",
                                 'red', colour=colour)
                )
                continue

            if payload.get('show_exit_ticket'):
                self.stdout.write(render.paint(
                    "\n  Exit ticket reached — the terminal client stops here. "
                    "Use the browser to submit it.", 'magenta', colour=colour,
                ))
                self.stdout.write(self._closing(session, colour))
                return

    # -- output -----------------------------------------------------------

    def _emit(self, session, payload, elapsed, show, colour, printer=None):
        cli_logs.transcript.info(
            "TUTOR (%.1fs): %s", elapsed, payload.get('message') or '',
        )
        if printer is not None and printer.started:
            # Something already streamed. Reconcile against the authoritative
            # final text rather than printing it a second time.
            #
            # A continuation is the NORMAL case, not an edge case: the last
            # sentence of a reply usually has no trailing boundary, so the
            # gate never emits it as a snapshot and it arrives only here.
            tail, restarted = render.stream_delta(
                printer.text, payload.get('message') or '')
            if restarted:
                # The batch pipeline rewrote text the student already read —
                # _dedupe_reply prepends, the exit-ticket branch replaces.
                # Say so instead of silently printing a second copy.
                self.stdout.write(
                    '\n' + render.paint('  ↻ revised:', 'dim', colour=colour))
                self.stdout.write(render.format_reply(payload, colour=colour))
            elif tail:
                self.stdout.write(render.format_reply_chunk(tail, colour=colour))
            else:
                self.stdout.write('')
        else:
            self.stdout.write('\n' + render.format_reply(payload, colour=colour))
        if show['state']:
            self.stdout.write(render.format_state(payload, colour=colour))
        if show['tools'] or show['judge'] or show['timing']:
            turn = last_tutor_turn(session)
            if show['tools']:
                self.stdout.write(
                    render.format_tools(getattr(turn, 'metadata', None), colour=colour)
                )
            if show['judge']:
                self.stdout.write(
                    render.format_judge(getattr(turn, 'judge_outputs', None), colour=colour)
                )
            if show['timing']:
                self.stdout.write(render.format_timing(elapsed, turn, colour=colour))

    def _banner(self, session, tutor_spec, colour, streaming=False):
        lesson = session.lesson
        self.stdout.write(render.paint(
            f"session {session.pk} · lesson {lesson.pk} · {lesson.title}",
            'bold', colour=colour,
        ))
        # Say which mode is live. Streaming changes what a slow turn LOOKS
        # like, so a reader comparing two transcripts needs to know which
        # they are looking at.
        mode = f"model {tutor_spec}" + (" · streaming" if streaming else "")
        self.stdout.write(render.paint(mode, 'dim', colour=colour))
        cli_logs.transcript.info("model=%s", tutor_spec)
        # Deleting rows silently is not something a dev tool should do — say so
        # when there was actually prior state to clear.
        reset = getattr(session, '_cli_reset', None) or {}
        if any(reset.values()):
            self.stdout.write(render.paint(
                f"fresh start · cleared {reset.get('progress', 0)} progress and "
                f"{reset.get('skills', 0)} skill-mastery record(s) for this lesson "
                f"(--keep-progress to retain)",
                'dim', colour=colour,
            ))
        self.stdout.write(render.paint(
            "/quit to end, /help for commands", 'dim', colour=colour,
        ))

    def _closing(self, session, colour):
        return render.paint(
            f"session {session.pk} ended (marked synthetic; excluded from analytics)",
            'dim', colour=colour,
        )
