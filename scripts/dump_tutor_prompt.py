#!/usr/bin/env python
"""Render the tutor's system prompt exactly as the model receives it.

The prompt is assembled at request time from a Block-0 template plus per-turn
renderers, so reading the Python string constants alone shows you neither the
final wording nor the order the model sees things in. This dumps the real
assembly for a real session.

    python scripts/dump_tutor_prompt.py                       # newest session
    python scripts/dump_tutor_prompt.py --session 40
    python scripts/dump_tutor_prompt.py --family qwen --mode picker
    python scripts/dump_tutor_prompt.py --out /tmp/p.txt

`--family qwen --mode picker` is the offline device path; the defaults follow
whatever the session actually resolves to. Edit the templates in
family_prompts.py / prompts.py, re-run this, and diff the two dumps.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django                                                    # noqa: E402
django.setup()                                                   # noqa: E402


def main() -> int:
    from apps.curriculum.models import LessonStep
    from apps.tutoring.models import InFlightQuestion, TutorSession
    from apps.tutoring.simple_tutor.engine import _uses_answer_picker
    from apps.tutoring.simple_tutor.prompts import (
        ANSWER_MODE_FREE_TEXT, ANSWER_MODE_PICKER, build_system_prompt,
    )
    from apps.tutoring.simple_tutor.tools import build_question_pool

    ap = argparse.ArgumentParser()
    ap.add_argument('--session', type=int, default=None,
                    help='TutorSession id. Default: the most recent one.')
    ap.add_argument('--family', default=None,
                    help="Prompt family: qwen (offline), gemini, kimi, or "
                         "omit for the base XML template (Anthropic).")
    ap.add_argument('--mode', choices=('picker', 'free_text'), default=None,
                    help='Answer surface. Default: whatever the session uses.')
    ap.add_argument('--out', default=None, help='Write here instead of stdout.')
    args = ap.parse_args()

    qs = TutorSession.objects.filter(engine='simple')
    session = (qs.get(pk=args.session) if args.session
               else qs.order_by('-id').first())
    if session is None:
        print('no simple-tutor session in this database', file=sys.stderr)
        return 1

    slot = InFlightQuestion.objects.filter(session=session).first()
    step = LessonStep.objects.filter(
        lesson=session.lesson, order_index=session.current_step_index).first()
    if args.mode:
        mode = (ANSWER_MODE_PICKER if args.mode == 'picker'
                else ANSWER_MODE_FREE_TEXT)
    else:
        mode = (ANSWER_MODE_PICKER if _uses_answer_picker(session, slot)
                else ANSWER_MODE_FREE_TEXT)

    blocks, tools = build_system_prompt(
        session=session, step=step,
        question_pool=build_question_pool(session),
        in_flight_question=slot,
        recent_window=list(session.turns.order_by('-id')[:6])[::-1],
        family=args.family, answer_mode=mode,
    )

    header = (
        f"# session={session.pk} lesson={session.lesson_id} "
        f"step={session.current_step_index} family={args.family or '(base XML)'} "
        f"mode={mode} in_flight={'yes' if slot else 'no'}\n"
        f"# {len(blocks)} blocks, "
        f"{sum(len(b['text']) for b in blocks):,} chars, "
        f"tools={[t['name'] for t in tools]}\n"
        "# Blocks are concatenated in this order and the model reads them "
        "top to bottom;\n# the student's message arrives AFTER the last one.\n"
    )
    sep = "\n\n" + "=" * 30 + " BLOCK BOUNDARY " + "=" * 30 + "\n\n"
    text = header + sep.join(b['text'] for b in blocks)

    if args.out:
        with open(args.out, 'w') as fh:
            fh.write(text)
        print(f'wrote {len(text):,} chars to {args.out}')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
