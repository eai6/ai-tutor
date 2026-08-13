"""Probe a student persona with canned tutor turns. Sanity check before
wiring into the full session driver (Phase 2).

Usage:
    python manage.py probe_persona [--persona struggler] [--turns N]

Drives the persona through a hand-picked sequence of tutor utterances
representative of real teaching moments (greeting, MCQ, working request,
remediation walkthrough). Prints each tutor → student exchange so a
human can eyeball persona quality before scaling.
"""
import argparse
import textwrap

from django.core.management.base import BaseCommand

from ai_tutor.apps.tutoring.student_sim import StudentClient, PERSONAS


# Canned tutor turns covering distinct teaching moments. Order matters:
# the persona builds up history, so later turns react to earlier ones.
DEFAULT_TUTOR_SCRIPT = [
    # Opener — engages the student
    "Hi! Today we're going to look at angles on a straight line. "
    "Imagine a flat ruler with one ray sticking up from it. The ruler "
    "makes a straight line and the ray splits it into two angles. "
    "Together those two angles always add up to 180°. "
    "Quick warm-up: if one angle is 90°, what's the other?",

    # Practice question — student needs to compute
    "Good. Now try this one: if one angle is 42°, what's the other angle "
    "on the straight line?",

    # Working request after a bare answer
    "How did you get that answer? Show me the step you took.",

    # Surface-the-error moment — tutor pushes back gently
    "Hmm, let me check that. Angles on a straight line add up to 180°, "
    "and you said 42 plus your answer should equal 180. Try that addition "
    "again — what does 42 plus your answer give you?",

    # Move to a fresh problem after some struggle
    "Let's try a slightly different one. Two angles on a straight line. "
    "One is 65°. What's the other?",
]


class Command(BaseCommand):
    help = "Drive a student persona through canned tutor turns and print the dialogue."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            '--persona', default='struggler',
            choices=sorted(PERSONAS),
            help='Which persona to drive (default: struggler).',
        )
        parser.add_argument(
            '--turns', type=int, default=None,
            help='Limit to first N turns from the script (default: all).',
        )

    def handle(self, *args, persona: str, turns: int | None, **kwargs) -> None:
        script = DEFAULT_TUTOR_SCRIPT[:turns] if turns else DEFAULT_TUTOR_SCRIPT
        client = StudentClient(persona)

        self.stdout.write(self.style.SUCCESS(
            f"\n=== Probing persona '{persona}' "
            f"({client.model_config.model_name}) ===\n"
        ))

        total_in = 0
        total_out = 0
        for i, tutor_msg in enumerate(script, 1):
            self.stdout.write(self.style.HTTP_INFO(f"--- Turn {i} ---"))
            self.stdout.write("Tutor:")
            for line in textwrap.wrap(tutor_msg, width=78,
                                      initial_indent='  ',
                                      subsequent_indent='  '):
                self.stdout.write(line)

            try:
                reply = client.next_reply(tutor_msg)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(
                    f"\nLLM call failed on turn {i}: {exc}\n"
                ))
                raise

            self.stdout.write("Student:")
            for line in textwrap.wrap(reply or '<empty>', width=78,
                                      initial_indent='  ',
                                      subsequent_indent='  '):
                self.stdout.write(line)

            if client.last_response is not None:
                total_in += client.last_response.tokens_in
                total_out += client.last_response.tokens_out
            self.stdout.write("")

        self.stdout.write(self.style.SUCCESS(
            f"=== Done. {len(script)} turns. "
            f"Tokens: in={total_in}, out={total_out}. ==="
        ))
