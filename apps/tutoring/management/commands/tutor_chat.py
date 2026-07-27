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

import sys
import time

from django.core.management.base import BaseCommand, CommandError

from apps.tutoring.cli import render
from apps.tutoring.cli.session import (
    BootstrapError, bootstrap_session, last_tutor_turn, list_lessons,
    resolve_lesson_id,
)

_QUIT = {'/quit', '/exit', '/q'}
_HELP = {'/help', '/?'}


class Command(BaseCommand):
    help = "Chat with the tutor in the terminal, driving the real engine."

    def add_arguments(self, parser):
        parser.add_argument(
            '--lesson', type=int, default=None,
            help='Lesson id to tutor. Defaults to a lesson that has steps '
                 '(announced on start). Use --list-lessons to choose.',
        )
        parser.add_argument(
            '--student', default=None,
            help='Username to run as. Defaults to the eval fixture student.',
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
        parser.add_argument('--show-timing', action='store_true',
                            help='Show wall-clock and tokens per turn.')
        parser.add_argument('--show-all', action='store_true',
                            help='Enable every --show-* flag.')
        parser.add_argument('--plain', action='store_true',
                            help='Conversation only — suppress the default '
                                 'state/timing lines.')
        # No --no-color here: BaseCommand already defines it (along with
        # --force-color), and redefining it is an argparse conflict.

    def handle(self, *args, **opts):
        # Colour off when piped, so redirected output stays clean.
        colour = not opts.get('no_color') and sys.stdout.isatty()

        if opts['list_lessons']:
            self.stdout.write(render.format_lesson_table(list_lessons()))
            return

        # Defaults: state + timing on, tools + judge opt-in. This is a
        # development tool — where you are in the lesson and how long the turn
        # took are the two things you always want. Tool calls are verbose enough
        # to drown the conversation, so they stay behind a flag.
        # --plain wins over everything; --show-all wins over the defaults.
        if opts['plain']:
            show = dict.fromkeys(('tools', 'judge', 'state', 'timing'), False)
        elif opts['show_all']:
            show = dict.fromkeys(('tools', 'judge', 'state', 'timing'), True)
        elif any(opts[f'show_{k}'] for k in ('tools', 'judge', 'state', 'timing')):
            show = {k: opts[f'show_{k}'] for k in ('tools', 'judge', 'state', 'timing')}
        else:
            show = {'tools': False, 'judge': False, 'state': True, 'timing': True}

        try:
            lesson_id, defaulted = resolve_lesson_id(opts['lesson'])
            session = bootstrap_session(
                lesson_id, student_username=opts['student'],
            )
        except BootstrapError as exc:
            raise CommandError(str(exc))

        if defaulted:
            self.stdout.write(render.paint(
                f"no --lesson given; defaulted to {lesson_id} "
                f"(--list-lessons to choose another)", 'dim', colour=colour,
            ))

        from apps.tutoring.simple_tutor.engine import (
            respond_for_view, start_for_view,
        )

        self._banner(session, colour)

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
                t0 = time.time()
                payload = respond_for_view(session, message)
                self._emit(session, payload, time.time() - t0, show, colour)
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

    def _emit(self, session, payload, elapsed, show, colour):
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

    def _banner(self, session, colour):
        lesson = session.lesson
        self.stdout.write(render.paint(
            f"session {session.pk} · lesson {lesson.pk} · {lesson.title}",
            'bold', colour=colour,
        ))
        self.stdout.write(render.paint(
            "/quit to end, /help for commands", 'dim', colour=colour,
        ))

    def _closing(self, session, colour):
        return render.paint(
            f"session {session.pk} ended (marked synthetic; excluded from analytics)",
            'dim', colour=colour,
        )
