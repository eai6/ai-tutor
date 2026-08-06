#!/usr/bin/env python
"""Render the DYNAMIC block (Block 2) once per turn mode, for review.

Block 0 is stable and readable on its own. Block 2 is the one that changes
every turn — KB chunks, history, recent turns, the in-flight slot, the answer
surface, the server-picked mode, the length budget — and it is where most of
the recent bugs lived. Reading one rendered example per mode is the only way
to see what the model actually receives.

    python scripts/dump_dynamic_blocks.py                     # newest session
    python scripts/dump_dynamic_blocks.py --session 40 --out blocks.txt
"""
from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django                                                    # noqa: E402
django.setup()                                                   # noqa: E402


def main() -> int:
    from apps.curriculum.models import LessonStep
    from apps.tutoring.models import InFlightQuestion, TutorSession
    from apps.tutoring.simple_tutor.prompts import (
        ANSWER_MODE_PICKER, build_system_prompt,
    )
    from apps.tutoring.simple_tutor.tools import build_question_pool

    ap = argparse.ArgumentParser()
    ap.add_argument('--session', type=int, default=None)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    qs = TutorSession.objects.filter(engine='simple')
    session = (qs.get(pk=args.session) if args.session
               else qs.order_by('-id').first())
    if session is None:
        print('no simple-tutor session in this database', file=sys.stderr)
        return 1

    step = LessonStep.objects.filter(
        lesson=session.lesson, order_index=session.current_step_index).first()
    pool = build_question_pool(session)
    real_slot = InFlightQuestion.objects.filter(session=session).first()
    slot = real_slot or SimpleNamespace(
        question_text='In the four-figure grid reference 3947, which digits '
                      'represent the easting value?',
        question_type='mcq', reference_answer='A', source='catalog',
        attempt_count=0, options=['39', '47', '93', '74'],
        catalog_question_id=12,
    )
    review = {
        'score': 2, 'total': 10, 'passed': False,
        'missed_objectives': [{
            'enabling_objective': 'Calculate the distance between two locations',
            'asked': 3, 'correct': 0,
            'sample_question': 'Two locations at 3641 and 3645 — how far apart?',
            'student_answer': 'A', 'reference': 'B',
        }],
        'mastered_objectives': ['Read a four-figure grid reference'],
    }

    cases = [
        ('POSE / TEACH — nothing in flight',
         dict(in_flight_question=None, student_intent='answer')),
        # One GRADE sample, not two. A first-vs-second attempt pair differed
        # by exactly one character — <attempt_count>0</attempt_count> against
        # 1 — because the hint ladder that branches on it lives in Block 0,
        # which this file does not dump. 51 lines of duplication for a digit
        # buries the sections that do differ.
        ('GRADE — answering the live question',
         dict(in_flight_question=slot, student_intent='answer')),
        # No CONVERSATIONAL case: it was cut from the offline prompt because a
        # live question means the picker is showing and the typing box is not,
        # so the student can only send a letter. This case rendered a GRADE
        # block under a CONVERSATIONAL heading, which is worse than absent.
        ('REMEDIATION — failed the quiz, answering',
         dict(in_flight_question=slot, student_intent='answer',
              exit_ticket_review=review)),
        ('REMEDIATION — failed the quiz, nothing in flight',
         dict(in_flight_question=None, student_intent='answer',
              exit_ticket_review=review)),
    ]

    out = [
        f'# DYNAMIC BLOCK (Block 2) — session {session.pk}, '
        f'lesson {session.lesson_id}, step {session.current_step_index}',
        '# One rendering per turn mode. Block 0 is the same in all of them and',
        '# is dumped separately by scripts/dump_tutor_prompt.py.',
        '# The student\'s message arrives immediately AFTER this block.',
    ]
    for title, kw in cases:
        blocks, _ = build_system_prompt(
            session=session, step=step, question_pool=pool,
            family='qwen', answer_mode=ANSWER_MODE_PICKER, **kw)
        dyn = blocks[-1]['text']
        out += [
            '', '', '=' * 78,
            f'  {title}',
            f'  {len(dyn):,} chars   (whole prompt: '
            f'{sum(len(b["text"]) for b in blocks):,})',
            '=' * 78, '', dyn,
        ]
    text = '\n'.join(out)
    if args.out:
        with open(args.out, 'w') as fh:
            fh.write(text)
        print(f'wrote {len(text):,} chars to {args.out}')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
