"""Drive one synthetic-student tutoring session end-to-end.

Usage:
    python manage.py simulate_session --lesson <id> [--persona struggler]
                                      [--max-turns 40]
                                      [--institution <id>]
                                      [--quiet]

Creates a TutorSession tagged is_synthetic=True, runs the LLM-as-student
loop until completion / exit ticket / max-turns / deadlock, and prints a
formatted transcript plus result summary.

See memory/llm_student_simulator_plan.md (Phase 2).
"""
import argparse
import textwrap

from django.core.management.base import BaseCommand, CommandError

from ai_tutor.apps.tutoring.student_sim import PERSONAS, simulate_session


class Command(BaseCommand):
    help = "Run one end-to-end synthetic student session against a real lesson."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--lesson', type=int, required=True,
                            help='Lesson ID to teach (must have content_status=ready).')
        parser.add_argument('--persona', default='struggler',
                            choices=sorted(PERSONAS),
                            help='Persona key. Default: struggler.')
        parser.add_argument('--max-turns', type=int, default=40,
                            help='Hard cap on tutor↔student exchanges (default: 40).')
        parser.add_argument('--institution', type=int, default=None,
                            help='Institution ID. Defaults to the lesson\'s institution.')
        parser.add_argument('--quiet', action='store_true',
                            help='Suppress per-turn transcript; only print summary.')

    def handle(self, *args, lesson, persona, max_turns, institution,
               quiet, **kwargs) -> None:
        self.stdout.write(self.style.SUCCESS(
            f"\n=== Simulating session: lesson={lesson} persona={persona} "
            f"max_turns={max_turns} ===\n"
        ))

        try:
            result = simulate_session(
                lesson_id=lesson,
                persona=persona,
                max_turns=max_turns,
                institution_id=institution,
            )
        except Exception as exc:
            raise CommandError(f"simulate_session blew up: {exc}") from exc

        if not quiet:
            for turn in result.transcript:
                if turn.role == 'tutor':
                    label = self.style.HTTP_INFO(f"Tutor [t{turn.turn_number}, "
                                                  f"phase={turn.phase or '-'}]:")
                else:
                    label = self.style.WARNING(f"Student [t{turn.turn_number}]:")
                self.stdout.write(label)
                for line in textwrap.wrap(turn.content or '<empty>', width=78,
                                          initial_indent='  ',
                                          subsequent_indent='  '):
                    self.stdout.write(line)
                if turn.role == 'tutor' and turn.is_correct:
                    self.stdout.write(self.style.SUCCESS('  ✓ tutor judged student correct'))
                self.stdout.write('')

        # Summary
        verdict = {
            'completed':   self.style.SUCCESS,
            'exit_ticket': self.style.SUCCESS,
            'max_turns':   self.style.WARNING,
            'deadlock':    self.style.WARNING,
            'error':       self.style.ERROR,
        }[result.reason]
        self.stdout.write(verdict(
            f"\n=== Result: reason={result.reason} turns={result.turns} "
            f"session_id={result.session_id} ==="
        ))
        self.stdout.write(
            f"Student tokens: in={result.student_tokens_in} "
            f"out={result.student_tokens_out}"
        )
        if result.error:
            self.stdout.write(self.style.ERROR(f"Error: {result.error}"))
