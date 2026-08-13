"""Replay the real student transcripts that the OLD monolithic
combined_judge mis-graded, through the NEW per-domain split judges.

Cases drawn from the 5 chat transcripts Edward shared on 2026-05-04
("mistakes made with evaluation (math)"), documented in
`memory/pilot_session_plan.md` §2.A.0.

Run:
  python scripts/replay_failed_judge_transcripts.py
  python scripts/replay_failed_judge_transcripts.py --case 2

Requires ANTHROPIC_API_KEY in the environment (loaded from .env).

For each case, prints:
  - student input + tutor response
  - what OLD combined_judge labelled it (per the screenshots)
  - what NEW split judges produce (answer_correct, source, sub-results)
  - PASS / FAIL versus the human-correct verdict
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env so ANTHROPIC_API_KEY is set.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_tutor.config.settings")
django.setup()

from dataclasses import dataclass
from typing import Optional

from ai_tutor.apps.tutoring.combined_judge import run_combined_judge


@dataclass
class Case:
    name: str
    student_input: str
    tutor_response: str
    expected_answer: str
    answer_type: str  # 'free_text' | 'multiple_choice' | 'short_numeric'
    subject_is_math: bool
    bank_offered: bool
    bank_stems: list
    # Old combined_judge verdict (per the production screenshot)
    old_verdict: Optional[bool]
    # Human-correct verdict
    expected_verdict: Optional[bool]
    failure_mode: str  # description of what went wrong before


# ---------------------------------------------------------------
# Failed cases from the 5 production transcripts (2026-05-04)
# ---------------------------------------------------------------
CASES = [
    Case(
        name="A — non-answer 'help' marked correct",
        student_input="help",
        tutor_response=(
            "Perfect! You correctly calculated 130°. I can see you "
            "understood that 50° + 130° = 180°.\n\n"
            "When two lines intersect, they create adjacent angles."
        ),
        expected_answer="130",
        answer_type="short_numeric",
        subject_is_math=True,
        bank_offered=True,
        bank_stems=[
            "If one angle is 50°, what is the angle adjacent on the straight line?",
        ],
        old_verdict=True,
        expected_verdict=None,
        failure_mode=(
            "Old combined_judge marked 'help' as ✓ correct. The student "
            "was asking for help, not answering. Should be null."
        ),
    ),
    Case(
        name="B — wrong arithmetic claim '42 + 132 = 180' marked correct",
        # The student wrote this; the tutor then praised it.
        student_input="42 + 132 is equal to 180",
        tutor_response=(
            "Excellent! You used the correct method: 180° - 42° = 138°. "
            "You understood that the two angles must add to 180°, so you "
            "subtracted the known angle from 180° to find the missing one."
        ),
        expected_answer="138",
        answer_type="short_numeric",
        subject_is_math=True,
        bank_offered=True,
        bank_stems=[
            "A straight line is divided by a ray into two angles. "
            "One angle is 42°. Find the other angle.",
        ],
        old_verdict=True,
        expected_verdict=None,
        failure_mode=(
            "Student claimed 42 + 132 = 180 (actually 174) but never "
            "committed to a final answer. Old judge marked ✓ correct "
            "(false positive). New policy: arithmetic judge flags the "
            "wrong claim → engine regenerates; step_eval returns null "
            "(no final answer to grade). Either ✗ or ∅ is acceptable; "
            "the critical thing is that ✓ is fixed."
        ),
    ),
    Case(
        name="C — correct numeric answer '140' marked correct (sanity check)",
        student_input="140",
        tutor_response=(
            "Excellent! You correctly calculated x = 140°."
        ),
        expected_answer="140",
        answer_type="short_numeric",
        subject_is_math=True,
        bank_offered=True,
        bank_stems=[
            "Three angles around a point are 100°, 120°, and x°. Find x.",
        ],
        old_verdict=True,  # this one OLD got right
        expected_verdict=True,
        failure_mode=(
            "Sanity case — student correctly says 140 for x where "
            "100+120+x=360. Should remain ✓ via deterministic_numeric."
        ),
    ),
    Case(
        name="D — verbal-procedure 'subtract 65 from 180' marked incorrect",
        student_input="subtract 65 from 180",
        tutor_response=(
            "Not quite! You're thinking correctly about using 180°, but "
            "let me clarify the setup."
        ),
        expected_answer="115",
        answer_type="free_text",
        subject_is_math=True,
        bank_offered=True,
        bank_stems=[
            "Two intersecting lines form four angles. If one angle is "
            "65°, what is the angle adjacent to it on the straight line?",
        ],
        old_verdict=False,
        expected_verdict=None,
        failure_mode=(
            "Student described the correct calculation in words but did "
            "not commit to a final number. Old judge marked ✗ "
            "(false negative). New policy: step_eval returns null "
            "(method described, no final answer) — engine asks them to "
            "compute it instead of penalizing them."
        ),
    ),
    Case(
        name="E — bare answer '50' for 50° + ?° = 180° marked incorrect",
        student_input="50",
        tutor_response=(
            "How did you get 50° as your answer? If one angle is 50° and "
            "its adjacent angle on the straight line must add with it to "
            "make 180°, what calculation should you do?"
        ),
        expected_answer="130",
        answer_type="short_numeric",
        subject_is_math=True,
        bank_offered=True,
        bank_stems=[
            "If one angle is 50°, what is the angle adjacent on the "
            "straight line?",
        ],
        old_verdict=False,
        expected_verdict=False,  # 50 is wrong; 130 is right
        failure_mode=(
            "Sanity case the OLD judge got right. Student said 50 (the "
            "given angle, not the adjacent). Should remain ✗ via "
            "deterministic_numeric."
        ),
    ),
]


def _build_step_context(case: Case) -> dict:
    """Build the step_context payload that the engine would build via
    `_build_step_eval_context`. We compute the deterministic_verdict
    here too, mirroring the engine's logic."""
    deterministic_verdict = None
    deterministic_source = ""

    # Try numeric extraction via the engine's grader helper.
    try:
        from ai_tutor.apps.tutoring.grader import check_math_answer
        result = check_math_answer(
            case.student_input, case.expected_answer,
        )
        if result is not None and result.is_correct is not None:
            deterministic_verdict = bool(result.is_correct)
            deterministic_source = "numeric"
    except Exception as e:
        print(f"  [warn] check_math_answer failed: {e}")

    # MCQ letter
    if (
        deterministic_verdict is None
        and case.answer_type == "multiple_choice"
    ):
        import re
        m = re.match(
            r"^[\(\[]?\s*([A-D])\s*[\)\]\.]*\s*$",
            case.student_input.strip(), re.IGNORECASE,
        )
        if m and case.expected_answer.upper() in ("A", "B", "C", "D"):
            deterministic_verdict = (
                m.group(1).upper() == case.expected_answer.upper()
            )
            deterministic_source = "mcq_letter"

    return {
        "step_type": "practice",
        "step_index": 0,
        "exchanges_on_this_step": 1,
        "completion_criteria": (
            "Complete when the student gives the FINAL correct answer."
        ),
        "expected_answer": case.expected_answer,
        "teacher_script_excerpt": "",
        "format_hint": (
            f"Answer format: {case.answer_type}."
        ),
        "recent_conversation": (
            f"TUTOR: {case.bank_stems[0] if case.bank_stems else '(question)'}\n"
            f"STUDENT: {case.student_input}"
        ),
        "deterministic_verdict": deterministic_verdict,
        "deterministic_source": deterministic_source,
    }


def _make_judge_client():
    """Use the platform's existing judge resolution: instantiate a
    ConversationalTutor for any active TutorSession in the local DB
    and read its `judge_client` property. That property already
    handles JUDGE → TUTORING fallback and credential lookup the same
    way production does — no need to re-implement it here.

    Also reports which ModelConfig was resolved so we can confirm we're
    hitting the same model production uses.
    """
    from ai_tutor.apps.llm.models import ModelConfig
    from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor
    from ai_tutor.apps.tutoring.models import TutorSession

    judge_cfg = ModelConfig.objects.filter(
        purpose=ModelConfig.Purpose.JUDGE, is_active=True,
    ).first()
    if judge_cfg is not None:
        print(
            f"[judge] Using PROD JUDGE ModelConfig: "
            f"institution={judge_cfg.institution.name} "
            f"provider={judge_cfg.provider} model={judge_cfg.model_name}"
        )
    else:
        print(
            "[judge] No active JUDGE ModelConfig — production fallback "
            "to the active TUTORING client will apply (same path as "
            "ConversationalTutor.judge_client)."
        )

    session = (
        TutorSession.objects
        .select_related("lesson", "lesson__unit", "lesson__unit__course")
        .order_by("-started_at")
        .first()
    )
    if session is None:
        raise SystemExit(
            "No TutorSession found in the local DB. Run the app once "
            "to create a session, or seed one."
        )
    tutor = ConversationalTutor(session)
    client = tutor.judge_client
    model_name = getattr(getattr(client, "config", None), "model_name", "?")
    return client, model_name


def _verdict_str(v):
    if v is True:
        return "✓ correct"
    if v is False:
        return "✗ incorrect"
    if v is None:
        return "∅ null"
    return str(v)


def _print_case(case: Case, idx: int, total: int, llm_client, model_name: str):
    print(f"\n{'=' * 70}")
    print(f"Case {idx}/{total}: {case.name}")
    print('=' * 70)
    print(f"  bank stem: {case.bank_stems[0][:80] if case.bank_stems else '—'}")
    print(f"  expected_answer: {case.expected_answer}  ({case.answer_type})")
    print(f"  student input:   {case.student_input!r}")
    print(f"  tutor response:  {case.tutor_response[:120]}…")
    print(f"  failure mode:    {case.failure_mode}")
    print(f"  OLD judge verdict:      {_verdict_str(case.old_verdict)}")
    print(f"  HUMAN-CORRECT verdict:  {_verdict_str(case.expected_verdict)}")

    step_context = _build_step_context(case)
    print(f"  deterministic verdict:  "
          f"{step_context['deterministic_verdict']} "
          f"({step_context['deterministic_source'] or 'none'})")

    print(f"\n  Running split judges (Sonnet model={model_name})...")

    result = run_combined_judge(
        case.tutor_response,
        lesson=None,  # factual judge skips without a lesson
        llm_client=llm_client,
        bank_stems=case.bank_stems,
        student_input=case.student_input,
        answer_was_bare=True,
        answer_was_wrong=False,
        step_context=step_context,
        subject_is_math=case.subject_is_math,
        bank_offered=case.bank_offered,
    )

    print(f"\n  NEW split-judge result:")
    print(f"    answer_correct: {_verdict_str(result.answer_correct)}")
    print(f"    step_eval source: {result.step_eval_source!r}")
    print(f"    arithmetic_corrections: {len(result.arithmetic_corrections)}")
    for c in result.arithmetic_corrections[:3]:
        print(f"      - {c.get('expression')!r} claimed={c.get('claimed')!r} "
              f"correct={c.get('correct')!r}")
    print(f"    rule_violations: "
          f"{[v.rule for v in result.rule_violations]}")
    print(f"    fact_claims: {len(result.fact_claims)}")
    for fc in result.fact_claims[:2]:
        print(f"      - {fc.claim[:60]!r} → {fc.status}")
    print(f"    step_eval_reasoning: {result.step_eval_reasoning[:140]}")
    print(f"    sub_skipped: {result.sub_skipped}")

    # Compare verdict
    new_v = result.answer_correct
    pass_fail = "✅ PASS" if new_v == case.expected_verdict else "❌ FAIL"
    print(f"\n  → NEW verdict matches HUMAN: {pass_fail} "
          f"({_verdict_str(new_v)} vs {_verdict_str(case.expected_verdict)})")

    return new_v == case.expected_verdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", type=int, default=None,
        help="Run only one case by 1-based index (default: all)",
    )
    args = parser.parse_args()

    cases = CASES if args.case is None else [CASES[args.case - 1]]

    llm_client, model_name = _make_judge_client()

    print(f"\nReplaying {len(cases)} failed transcript(s) through the "
          f"NEW split judges. Judge model: {model_name}\n")

    pass_count = 0
    for i, case in enumerate(cases, 1):
        ok = _print_case(case, i, len(cases), llm_client, model_name)
        if ok:
            pass_count += 1

    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {pass_count}/{len(cases)} cases match human-correct verdict")
    print('=' * 70)


if __name__ == "__main__":
    main()
