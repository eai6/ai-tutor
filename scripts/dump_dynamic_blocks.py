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
    # A real DB row is not a SimpleNamespace, so the old copy-with-override
    # silently fell through to `slot` and the two GRADE samples rendered
    # byte-identical. Build the variant from the fields the renderer reads,
    # whatever the slot's type.
    wrong_slot = SimpleNamespace(
        question_text=getattr(slot, 'question_text', ''),
        question_type=getattr(slot, 'question_type', 'mcq'),
        reference_answer=getattr(slot, 'reference_answer', ''),
        source=getattr(slot, 'source', 'catalog'),
        options=list(getattr(slot, 'options', None) or []),
        catalog_question_id=getattr(slot, 'catalog_question_id', None),
        attempt_count=1,
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
        ('GRADE — first attempt',
         dict(in_flight_question=slot, student_intent='answer')),
        ('GRADE — second attempt (hint ladder rung 1)',
         dict(in_flight_question=wrong_slot, student_intent='answer')),
        ('CONVERSATIONAL — student asked something instead',
         dict(in_flight_question=slot, student_intent='clarification')),
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
