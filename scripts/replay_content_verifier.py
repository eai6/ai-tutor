"""Replay realistic LLM-generated math content through Layers 1+2+3.

Synthetic-but-realistic samples drawn from production transcripts +
documented LLM failure modes. Confirms each verifier catches the
intended error class and produces actionable audit entries.

Sections:
  A) Layer 1 — step-content arithmetic auto-correct
  B) Layer 1 — step detect-only (answer-key-bound fields)
  C) Layer 1 — exit-ticket question explanation auto-correct
  D) Layer 2 — answer-key cross-check (Patterns B/C/D)
  E) Layer 3 — constraint-block builder shape
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

from apps.curriculum.content_verifier import (  # noqa: E402
    build_arithmetic_constraint_block,
    has_unresolved_corrections,
    verify_exit_ticket_question,
    verify_lesson_step,
)
from apps.tutoring.question_validator import cross_check_question  # noqa: E402


_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_GREY = "\033[90m"
_RESET = "\033[0m"


def section(title: str) -> None:
    print(f"\n{_CYAN}{'=' * 78}{_RESET}")
    print(f"{_CYAN}{title}{_RESET}")
    print(f"{_CYAN}{'=' * 78}{_RESET}\n")


def show_step_result(label: str, original: dict, audit: list) -> None:
    """Compact diff display for a Layer 1 step run."""
    print(f"{_YELLOW}— {label}{_RESET}")
    if not audit:
        print(f"  {_GREEN}✓ no findings{_RESET}")
        return
    for e in audit:
        marker = (
            f"{_GREEN}auto-correct{_RESET}"
            if e.get("auto_corrected")
            else f"{_RED}DETECT-ONLY{_RESET}"
        )
        print(f"  step {e['step_order']} field={e['field']} [{marker}]")
        for fix in e.get("corrections", []):
            print(f"    {fix['expression']} = {fix['claimed']}  →  = {fix['correct']}")


def show_question_result(label: str, audit: list, cross_check: dict) -> None:
    print(f"{_YELLOW}— {label}{_RESET}")
    if not audit and not cross_check:
        print(f"  {_GREEN}✓ no findings{_RESET}")
        return
    for e in audit:
        marker = (
            f"{_GREEN}auto-correct{_RESET}"
            if e.get("auto_corrected")
            else f"{_RED}DETECT-ONLY{_RESET}"
        )
        print(f"  Q{e['question_index']} field={e['field']} [{marker}]")
        for fix in e.get("corrections", []):
            print(f"    {fix['expression']} = {fix['claimed']}  →  = {fix['correct']}")
    if cross_check:
        print(
            f"  {_RED}ANSWER-KEY MISMATCH{_RESET} pattern={cross_check['pattern']} "
            f"computed={cross_check['computed']} stored={cross_check['claimed']}"
        )
        print(f"  {_GREY}{cross_check['reason']}{_RESET}")


# ============================================================================
# A — Layer 1 step-content auto-correct
# ============================================================================


def section_a():
    section("A — Layer 1 step content (AUTO-CORRECT fields)")

    # Sample 1: classic worked-example with wrong sum check
    step1 = {
        "order_index": 4,
        "teacher_script": (
            "Let's verify: 60° + 80° + 75° + 70° + 75° = 220° ✓\n"
            "Looks right!"
        ),
    }
    audit1 = []
    verify_lesson_step(step1, audit=audit1)
    show_step_result("worked example with wrong N-term sum", step1, audit1)
    print(f"  {_GREY}corrected text → {step1['teacher_script']!r}{_RESET}")

    # Sample 2: hint with bad arithmetic
    step2 = {
        "order_index": 2,
        "hints": [
            "Add the angles first: 95 + 70 = 175",  # wrong, 165
            "Then add 110 to get the running total.",
            "Subtract from 360 to find x.",
        ],
    }
    audit2 = []
    verify_lesson_step(step2, audit=audit2)
    show_step_result("hint with wrong addition", step2, audit2)
    print(f"  {_GREY}corrected hint[0] → {step2['hints'][0]!r}{_RESET}")

    # Sample 3: educational_content nested list
    step3 = {
        "order_index": 5,
        "educational_content": {
            "common_mistakes": [
                "Don't forget order of operations: 3 + 4 × 2 = 14 (right answer is 11)",
                "Always check your sum: 50 + 50 = 110 (should warn the student about errors).",
            ],
        },
    }
    audit3 = []
    verify_lesson_step(step3, audit=audit3)
    show_step_result("educational_content list with wrong arithmetic", step3, audit3)


# ============================================================================
# B — Layer 1 step detect-only (answer-key-bound)
# ============================================================================


def section_b():
    section("B — Layer 1 step content (DETECT-ONLY fields)")

    # Wrong arithmetic embedded in question stem — must NOT auto-rewrite
    step1 = {
        "order_index": 3,
        "question": (
            "If the four angles 95° + 70° + 110° + x° = 360° and we know "
            "95 + 70 + 110 = 285, what is x?"
        ),
        "expected_answer": "75",  # bug-aligned with the wrong stem claim
    }
    audit1 = []
    original_q = step1["question"]
    verify_lesson_step(step1, audit=audit1)
    show_step_result("question with wrong arithmetic claim", step1, audit1)
    # Confirm stem unchanged.
    if step1["question"] == original_q:
        print(f"  {_GREEN}✓ stem text NOT auto-rewritten (correct){_RESET}")
    else:
        print(f"  {_RED}✗ stem was rewritten — DESYNC RISK{_RESET}")
    print(f"  {_GREY}detect-only flag → Layer 3 retry required{_RESET}")


# ============================================================================
# C — Layer 1 exit-ticket question explanation
# ============================================================================


def section_c():
    section("C — Layer 1 exit-ticket question (EXPLANATION auto-correct)")

    q = {
        "question_type": "mcq",
        "question_text": "What is x if 95° + 70° + 110° + x° = 360°?",
        "explanation": (
            "First add the known angles: 95 + 70 + 110 = 285. "
            "Then subtract from 360: 360 - 285 = 85. So x = 85°."
        ),
        # 285 is wrong — should be 275 — but the rest of the explanation
        # then computes 360-285=75 (which the student would see).
        "option_a": "75",
        "option_b": "85",
        "option_c": "100",
        "option_d": "175",
        "correct": "B",
    }
    audit = []
    verify_exit_ticket_question(q, question_index=2, audit=audit)
    show_question_result(
        "explanation with wrong intermediate sum",
        audit, cross_check=None,
    )
    print(f"  {_GREY}corrected explanation → {q['explanation']!r}{_RESET}")


# ============================================================================
# D — Layer 2 answer-key cross-check
# ============================================================================


def section_d():
    section("D — Layer 2 answer-key cross-check (Patterns B/C/D)")

    # Pattern B — pure additive sum
    q_sum = {
        "question_type": "short_answer",
        "question": "Calculate 95 + 70 + 110.",
        "correct_answer": "165",  # wrong — should be 275
    }
    ck = cross_check_question(q_sum)
    show_question_result("Pattern B (sum) — wrong stored answer", [], ck)

    # Pattern C — multiplication chain
    q_mult = {
        "question_type": "short_answer",
        "question": "Compute 8 × 7 × 3.",
        "correct_answer": "120",  # wrong — should be 168
    }
    ck = cross_check_question(q_mult)
    show_question_result("Pattern C (mult) — wrong stored answer", [], ck)

    # Pattern D — linear equation
    q_lin = {
        "question_type": "short_answer",
        "question": "Solve x + 5 = 12.",
        "correct_answer": "17",  # wrong — should be 7
    }
    ck = cross_check_question(q_lin)
    show_question_result("Pattern D (linear) — wrong stored answer", [], ck)

    # MCQ with wrong correct letter
    q_mcq = {
        "question_type": "mcq",
        "question_text": "What is 25 + 30 + 45?",
        "option_a": "75",
        "option_b": "100",
        "option_c": "165",
        "option_d": "120",
        "correct": "B",  # B says 100, real answer 100. Wait. 25+30+45=100. Correct!
    }
    ck = cross_check_question(q_mcq)
    show_question_result(
        "MCQ where stored letter happens to point to right value (passes)",
        [], ck,
    )

    # MCQ with wrong correct letter
    q_mcq_wrong = {
        "question_type": "mcq",
        "question_text": "What is 25 + 30 + 45?",
        "option_a": "75",
        "option_b": "100",
        "option_c": "165",
        "option_d": "120",
        "correct": "A",  # A=75 but stem computes 100
    }
    ck = cross_check_question(q_mcq_wrong)
    show_question_result("MCQ — wrong correct-letter (75 ≠ 100)", [], ck)

    # Word problem — should NOT trigger (out of pattern)
    q_word = {
        "question_type": "short_answer",
        "question": (
            "Pierre has 15 mangoes and gives 7 to his sister. "
            "How many mangoes does he have left?"
        ),
        "correct_answer": "10",  # actually wrong, but unverifiable
    }
    ck = cross_check_question(q_word)
    show_question_result(
        "Word problem (Pierre & mangoes) — unverifiable, passes through",
        [], ck,
    )

    # Geometry word problem — also unverifiable
    q_geom = {
        "question_type": "short_answer",
        "question": (
            "Three angles around a point are 95°, 70°, and 110°. Find x."
        ),
        "correct_answer": "85",  # right, but unverifiable by Layer 2 v1
    }
    ck = cross_check_question(q_geom)
    show_question_result(
        "Geometry word problem (no '+' connector) — unverifiable",
        [], ck,
    )


# ============================================================================
# E — Layer 3 constraint-block builder
# ============================================================================


def section_e():
    section("E — Layer 3 constraint block (full sample)")

    step_audit = [
        {
            "step_order": 4,
            "field": "question",
            "auto_corrected": False,
            "corrections": [
                {"expression": "60 + 80 + 75 + 70 + 75",
                 "claimed": "220", "correct": "360"},
            ],
        },
    ]
    answer_key_mismatches = [
        {
            "question_index": 2,
            "pattern": "sum",
            "computed": 275.0,
            "claimed": 165.0,
            "reason": "mcq: stem computes 275, stored answer is 165",
        },
        {
            "question_index": 7,
            "pattern": "linear",
            "computed": 7.0,
            "claimed": 17.0,
            "reason": "short_answer: stem (linear) computes 7, stored is 17",
        },
    ]
    block = build_arithmetic_constraint_block(step_audit, answer_key_mismatches)
    print(block)


def main():
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    print()


if __name__ == "__main__":
    main()
