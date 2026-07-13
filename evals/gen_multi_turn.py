"""Generate the multi-turn scenario YAMLs from the plan + 12 shape templates.

Multi-turn scenarios carry no seeded history and no student turn — the student
simulator produces the turns live from the persona. What a multi-turn scenario
actually *is*, therefore, is exactly four things: persona, lesson, turn budget,
and the trajectory properties the tutor must satisfy. All four come from the
plan (evals/matrix.py) or from the shape.

So these files are generated, not hand-authored. Twelve shape templates are
reviewable; 170 near-identical hand-written YAMLs are not, and would drift.

Idempotent: rewrites only the scenarios the plan marks `author`, and never
touches the 30 existing multi-turn scenarios (status `keep`).

    ./venv/bin/python -m evals.gen_multi_turn

See memory/eval_dataset_400_plan.md.
"""
from __future__ import annotations

import json
from pathlib import Path

from evals.matrix import PLAN_PATH, SHAPE_BY_KEY

DATASET_ROOT = Path(__file__).parent / 'dataset'
OUT_DIR = DATASET_ROOT / 'multi_turn'
FIXTURES = Path(__file__).parent / 'fixtures' / 'lessons.json'

# The session-level BEA standard rubric, appended verbatim to every multi-turn
# scenario. Kept byte-identical to the block already in the 30 existing
# scenarios so the judge sees one consistent standard across all 200.
BEA_SESSION_RUBRIC = [
    "Across the session, whenever the student made a mistake the tutor recognised it rather than affirming a wrong answer, and whenever the student was correct the tutor affirmed it clearly without false hedging or unnecessary second-guessing.",
    "Whenever the student made a mistake, the tutor pointed at its specific location or nature — a particular step, a named misconception, a specific arithmetic slip — not just a generic 'try again' or 'not quite'.",
    "The tutor never revealed the final answer to a question the student had not yet solved. Partial hints calibrated to the student's level are acceptable; handing over the canonical numeric or letter answer is not.",
    "When the student needed help, the tutor offered correct and relevant guidance — an explanation, elaboration, hint, worked example, or scaffolding — calibrated to the student's apparent level and the current concept.",
    "The tutor's turns consistently made clear what the student should do next — a specific question to answer, an MCQ option to pick, a calculation to perform, or another structured prompt that hands the conversational floor back — rather than trailing off passively.",
    "The tutor stayed logically consistent across the session — it did not contradict its own earlier turns, assume facts not yet established, or ignore what the student had just said.",
    "The tutor's tone stayed warm and encouraging throughout — supportive without being condescending, honest without being harsh, and never offensive, dismissive, or impatient.",
    "The tutor's turns read as natural, conversational teaching — not robotic, templated, or padded with filler openers like 'Great question!', 'Let me think about this carefully', or 'I'm going to explain...'.",
]

# Per-shape: the scenario-specific rubric items, and the extra tags.
# These are the ONLY parts that vary by shape — everything else is mechanical.
SHAPE_RUBRIC: dict[str, dict] = {
    'baseline_full_session': {
        'tags': ['baseline'],
        'rubric': [
            "Teaches the lesson content rather than only quizzing — explanation and questioning are both present.",
            "Adapts pacing to the student's responses instead of marching through a script.",
            "Session terminates cleanly (completed, exit ticket, or max turns).",
        ],
    },
    'session_completion': {
        'tags': ['completion', 'exit_ticket'],
        'rubric': [
            "The session actually reaches the exit ticket rather than stalling in the tutoring loop.",
            "The tutor advances when the student has demonstrated the objective — it does not over-drill a student who already has it.",
            "The exit ticket is fired only after the lesson objectives were genuinely covered.",
        ],
    },
    'speedrun': {
        'tags': ['pace', 'edge_case'],
        'rubric': [
            "Keeps up with a fast, correct student — no artificial gatekeeping, no demand for working on answers the student clearly has.",
            "Does not pad turns with filler or re-explain what the student has already demonstrated.",
            "Still checks understanding at least once rather than rubber-stamping every answer.",
        ],
    },
    'help_intensive': {
        'tags': ['scaffolding', 'help_request'],
        'rubric': [
            "Scaffolds down a rung when the student asks for help — a simpler sub-question, a worked parallel example, a named tip.",
            "Never hands over the final answer, however many times the student asks for it.",
            "Does not info-dump: help is calibrated, not a wall of text.",
        ],
    },
    'refusal_chain': {
        'tags': ['refusal', 'repetition', 'banned_opener'],
        'rubric': [
            "After one or two refused 'explain your reasoning' probes, the tutor CHANGES STRATEGY rather than repeating the probe.",
            "Offers an MCQ check, a worked example, or moves on with a flagged gap rather than blocking on working.",
            "Does not loop the same banned-opener probe turn after turn.",
        ],
    },
    'self_correction': {
        'tags': ['self_correction', 'correction'],
        'rubric': [
            "When the student catches their own error, the tutor CREDITS the self-correction explicitly.",
            "Does not re-teach the concept from scratch after a student has already fixed it themselves.",
            "Confirms the corrected answer is right rather than leaving it ambiguous.",
        ],
    },
    'remediation_after_exit_ticket_fail': {
        'tags': ['remediation', 'exit_ticket'],
        'rubric': [
            "After the exit ticket fails, the tutor targets the SPECIFIC failed objectives rather than re-teaching the whole lesson.",
            "Re-fires the exit ticket once the gap has been addressed.",
            "Does not simply repeat the original explanation verbatim — remediation is a second, different attempt at the concept.",
        ],
    },
    'engagement_recovery': {
        'tags': ['engagement', 'non_answer'],
        'rubric': [
            "Keeps offering a concrete, answerable next step through the silent stretch — never trails off or gives up on the student.",
            "Varies its approach across the disengaged turns rather than repeating one prompt.",
            "When the student finally engages, the tutor picks the thread up rather than restarting the lesson.",
        ],
    },
    'straight_line': {
        'tags': ['baseline', 'edge_case'],
        'rubric': [
            "Does not invent problems where there are none — a correct student is not second-guessed into doubt.",
            "Does not over-probe: no demand for working on answers the student has plainly got.",
            "Still advances through the lesson content rather than idling on one step.",
        ],
    },
    'error_cascade': {
        'tags': ['diagnostic', 'false_accept_guard'],
        'rubric': [
            "No wrong answer is allowed to stand — every slip is caught.",
            "Each error is located specifically rather than met with a generic 'not quite'.",
            "The tutor does not spiral: repeated errors are met with a simpler rung, not with mounting pressure.",
        ],
    },
    'long_session': {
        'tags': ['long_session', 'repetition'],
        'rubric': [
            "Stays coherent over a long horizon — no contradictions with its own earlier turns.",
            "Does not drift off the lesson objective as the session stretches.",
            "Does not fall into repetition — later turns are not recycled earlier ones.",
        ],
    },
    'short_session': {
        'tags': ['short_session', 'edge_case'],
        'rubric': [
            "Prioritises under a hard turn cap — lands the core objective rather than starting everything and finishing nothing.",
            "Does not waste turns on preamble or throat-clearing.",
            "Ends with the student having actually learned or demonstrated something.",
        ],
    },
}

PERSONA_NOTE = {
    'struggler': 'A STRUGGLER (needs scaffolding, slips often, low confidence)',
    'average': 'An AVERAGE student (partial understanding, some slips)',
    'capable': 'A CAPABLE student (fast, mostly correct, pushes back)',
    'probe_resistant': 'A PROBE_RESISTANT student (answers, refuses to show working)',
    'non_responder': 'A NON_RESPONDER (monosyllabic, "idk", disengaged)',
    'error_prone': 'An ERROR_PRONE student (repeated slips and misreads)',
}


def _lesson_titles() -> dict[int, str]:
    data = json.loads(FIXTURES.read_text())
    return {x['pk']: x['fields']['title']
            for x in data if x['model'] == 'curriculum.lesson'}


def _yaml_str(s: str) -> str:
    """Double-quoted YAML scalar, escaping what needs escaping."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def render(row: dict, titles: dict[int, str]) -> str:
    shape = SHAPE_BY_KEY[row['kind']]
    tpl = SHAPE_RUBRIC[row['kind']]
    turns = row['max_turns']
    title = titles[row['lesson_id']]

    # Repetition window scales with the turn budget: a 6-turn session cannot
    # tolerate the same 5-turn window a 30-turn session can.
    window = 4 if turns <= 12 else 5
    threshold = 2 if turns <= 12 else 3

    tags = ['multi_turn', row['subject'], row['persona'], *tpl['tags']]

    lines = [
        f"id: {row['scenario_id']}",
        "description: >",
        f"  {PERSONA_NOTE[row['persona']]} drives a {turns}-turn session on",
        f"  \"{title}\".",
        f"  Shape: {shape.summary}",
        f"persona: {row['persona']}",
        f"subject: {row['subject']}",
        f"lesson_id: {row['lesson_id']}",
        f"tags: [{', '.join(tags)}]",
        "",
        "mode: multi_turn",
        f"max_turns: {turns}",
        "",
        "assertions:",
        "  expected_reason: [completed, exit_ticket, max_turns]",
        f"  max_turn_count: {turns}",
        f"  no_repeated_tutor_phrase_within_window: {{window: {window}, threshold: {threshold}}}",
        "  no_tool_syntax_in_any_turn: true",
        "",
        "rubric:",
    ]
    for item in tpl['rubric']:
        lines.append(f"  - {_yaml_str(item)}")
    lines.append("  # --- BEA-aligned standard rubric (session-level; universal across scenarios) ---")
    for item in BEA_SESSION_RUBRIC:
        lines.append(f"  - {_yaml_str(item)}")
    lines.append("pass_threshold: 0.6")
    return '\n'.join(lines) + '\n'


def main() -> None:
    plan = json.loads(PLAN_PATH.read_text())
    titles = _lesson_titles()
    rows = [r for r in plan
            if r['mode'] == 'multi_turn' and r['status'] == 'author']

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for row in rows:
        (OUT_DIR / f"{row['scenario_id']}.yaml").write_text(render(row, titles))

    print(f"Generated {len(rows)} multi-turn scenarios into {OUT_DIR}")
    total = len(list(OUT_DIR.glob('*.yaml')))
    print(f"multi_turn/ now holds {total} scenarios")


if __name__ == '__main__':
    main()
