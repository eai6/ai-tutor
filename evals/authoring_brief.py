"""Emit the authoring brief for one lesson: real content + assigned plan rows.

Single-turn scenarios must be GROUNDED in the frozen lesson content — a seeded
history that poses a question the lesson never teaches, or a
`seed_inflight_question.reference_answer` that is mathematically wrong, silently
corrupts the grader and makes the scenario unwinnable. (A prior pass had to
replace 26 placeholder reference answers for exactly this reason.)

So authors do not invent content. They get this brief: the lesson's actual steps,
its actual exit-ticket questions with model answers, and the rows the plan
assigned them.

    ./venv/bin/python -m evals.authoring_brief 1139

See memory/eval_dataset_400_plan.md.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

from evals.matrix import ARCHETYPE_BY_KEY, PLAN_PATH, SUBJECT_OF

FIXTURES = Path(__file__).parent / 'fixtures' / 'lessons.json'


def _load():
    data = json.loads(FIXTURES.read_text())
    lessons, steps, ets, etqs = {}, [], {}, []
    for x in data:
        m, f = x['model'], x['fields']
        if m == 'curriculum.lesson':
            lessons[x['pk']] = f
        elif m == 'curriculum.lessonstep':
            steps.append((x['pk'], f))
        elif m == 'tutoring.exitticket':
            ets[x['pk']] = f
        elif m == 'tutoring.exitticketquestion':
            etqs.append(f)
    return lessons, steps, ets, etqs


def brief(lesson_id: int) -> str:
    lessons, steps, ets, etqs = _load()
    out: list[str] = []
    les = lessons[lesson_id]
    subject = SUBJECT_OF[lesson_id]

    out.append(f"# LESSON {lesson_id} — {les['title']}  [subject: {subject}]")

    out.append("\n## Lesson steps (the ONLY content you may ground scenarios in)")
    mine = sorted((f for _, f in steps if f['lesson'] == lesson_id),
                  key=lambda f: int(f['order_index']))
    for f in mine:
        out.append(
            f"\n### step {f['order_index']} · phase={f['phase']} · "
            f"type={f['step_type']} · concept={f['concept_tag']}"
        )
        if f.get('enabling_objective'):
            out.append(f"objective: {f['enabling_objective']}")
        if f.get('teacher_script'):
            out.append(f"teach: {f['teacher_script'][:600]}")
        if f.get('question'):
            out.append(f"question: {f['question']}")
            if f.get('choices') and f['choices'] != 'None':
                out.append(f"choices: {f['choices']}")
            out.append(f"expected_answer: {f.get('expected_answer')!r}")

    # Exit-ticket questions carry vetted model answers — the safest source of
    # correct reference answers.
    et_ids = {pk for pk, f in ets.items() if f.get('lesson') == lesson_id}
    qs = [q for q in etqs if q.get('exit_ticket') in et_ids]
    out.append(f"\n## Exit-ticket questions ({len(qs)} available; showing 12)")
    out.append("Use these for REALISTIC questions + verified correct answers.")
    for q in qs[:12]:
        ans = q.get('correct_answer') or ''
        try:
            ad = ast.literal_eval(q['answer_data']) if q.get('answer_data') else {}
            ans = ans or ad.get('model_answer') or ad.get('computed') or ''
        except (ValueError, SyntaxError):
            ad = {}
        opts = [q.get(f'option_{c}') for c in 'abcd']
        opts = [o for o in opts if o]
        out.append(f"\n- Q [{q.get('question_type')}]: {q['question_text']}")
        if opts:
            out.append(f"  options: {opts}")
        out.append(f"  ANSWER: {ans!r}")
        if q.get('explanation'):
            out.append(f"  why: {q['explanation'][:180]}")

    # Assigned rows.
    plan = json.loads(PLAN_PATH.read_text())
    rows = [r for r in plan
            if r['mode'] == 'single_turn'
            and r['lesson_id'] == lesson_id
            and r['status'] in ('author', 'port')]
    out.append(f"\n## YOUR ASSIGNED SCENARIOS ({len(rows)})")
    for r in rows:
        a = ARCHETYPE_BY_KEY[r['kind']]
        out.append(f"\n### {r['scenario_id']}")
        out.append(f"- status: {r['status']}"
                   + (f" (PORT of an existing scenario from lesson "
                      f"{r['source_lesson_id']} — preserve its persona, archetype "
                      f"and rubric intent, re-ground the CONTENT on lesson "
                      f"{lesson_id})" if r['status'] == 'port' else ""))
        out.append(f"- persona: {r['persona']}")
        out.append(f"- archetype: {a.key}")
        out.append(f"- what it tests: {a.summary}")
        out.append(f"- grades_answer: {a.grades_answer}"
                   + ("  → MUST include seed_inflight_question"
                      if a.grades_answer else
                      "  → NO seed_inflight_question (nothing is being graded)"))
        if a.rules:
            out.append(f"- polices rules: {', '.join(a.rules)}")

    return '\n'.join(out)


if __name__ == '__main__':
    print(brief(int(sys.argv[1])))
