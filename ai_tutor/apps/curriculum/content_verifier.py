"""Layer 1 — content-time arithmetic verification.

Runs `apps.tutoring.math_tools.verify_calculations` over LLM-generated
lesson content (steps + exit-ticket questions) BEFORE it is persisted
to the DB, catching LLM arithmetic errors at generation time rather
than letting them reach students.

Two modes per field:

  AUTO-CORRECT — for fields where rewriting the arithmetic doesn't
                 desync anything else: `teacher_script`,
                 `educational_content.*`, `hint_1/2/3`. The verifier
                 silently corrects these in place.

  DETECT-ONLY — for fields where the arithmetic is bound to an answer
                key: `question` text, `expected_answer`, MCQ
                `option_a/b/c/d`, `correct_answer`. Rewriting only
                one side (e.g. fixing the stem to `=360` while the
                answer key still says `=220`) creates a mismatch
                worse than the original. The verifier flags these
                for Layer 3 retry instead.

All corrections (auto-applied or detect-only) are appended to a
per-lesson audit list which the caller persists to
`lesson.metadata['verification_audit']` for teacher review.

Math-only by design: the regex matches "60 + 80 = 220" patterns that
occur incidentally in geography prose ("Seychelles has 115 islands;
22 + 32 + 61 = 115") and would generate noisy false positives on
non-math content. Caller gates with `course.is_math`.

See `memory/llm_arithmetic_defense_plan.md` (Layer 1 section).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ai_tutor.apps.tutoring.math_tools import verify_calculations


# Symbols that adorn numbers in lesson content (degrees on angles,
# units, currency) but aren't arithmetic. We strip them in a working
# copy before running verify_calculations so e.g.
# "95° + 70° + 110° = 285°" gets detected as wrong arithmetic, then
# map the correction back into the ORIGINAL text — preserving the
# ° symbols around the unaffected operands.
_NOISE_CHAR_RE = re.compile(r"[°$%]")
_UNIT_WORD_RE = re.compile(
    r"\b(kg|cm|mm|km|m|s|g|l|ml)\b",
    re.IGNORECASE,
)


def _strip_noise(text: str) -> str:
    """Replace arithmetic-noise chars with spaces (preserves length).

    Keeping length stable matters because we map corrections from the
    stripped text back into the original by literal claimed-value
    matching — shifting positions would break that mapping.
    """
    out = _NOISE_CHAR_RE.sub(" ", text)
    out = _UNIT_WORD_RE.sub(lambda m: " " * (m.end() - m.start()), out)
    return out


def _apply_corrections_to_original(
    original: str, corrections: List[Dict],
) -> str:
    """Map corrections (computed from a noise-stripped copy) back into
    the original text. We replace only the claimed value after `=`,
    leaving operands + their unit suffixes untouched.

    Example:
      original   = "Step 5: Check 60° + 80° + 75° + 70° + 75° = 220° ✓"
      correction = {'claimed': '220', 'correct': '360'}
      output     = "Step 5: Check 60° + 80° + 75° + 70° + 75° = 360° ✓"

    Each correction is applied at most once. If we can't find the
    `= <claimed>` pattern in the original (e.g. the claim was inside
    a chained sum that the stripped match split differently), we
    skip it — the audit still records the correction so Layer 3 can
    decide whether to retry.
    """
    out = original
    for c in corrections:
        claimed = c.get("claimed", "")
        correct = c.get("correct", "")
        if not claimed or not correct:
            continue
        # Find `= <claimed>` and replace only when the trailing
        # context confirms `claimed` is a complete number — not a
        # prefix of a longer one (`285` inside `2856`) and not the
        # integer part of a decimal (`285` inside `285.7`).
        # Critically, we DO match when followed by a sentence-
        # terminator period (`= 285. Then`) — the period is not
        # part of the number iff it isn't followed by a digit.
        pattern = re.compile(
            r"(=\s*)" + re.escape(claimed) + r"(?!\d)(?!\.\d)"
        )
        out, n = pattern.subn(rf"\g<1>{correct}", out, count=1)
    return out


# ============================================================================
# Field categorisation
# ============================================================================

# Fields rewritten in place when arithmetic is wrong.
_AUTO_CORRECT_FIELDS = (
    "teacher_script",
)
# Hint fields are special — generated as a flat list `step['hints']`
# but persisted as hint_1/2/3. We verify the source list.
_HINT_LIST_KEY = "hints"

# educational_content sub-fields to verify. String values get
# rewritten; list-of-string values walk each element.
_EDU_CONTENT_FIELDS = (
    "worked_example",
    "common_mistakes",
    "key_points",
    "real_world_connections",
)

# Fields where rewriting one side without the other would desync the
# question stem from its answer key. Layer 1 reports but does NOT
# rewrite. Layer 3 retries the LLM call to reconcile them.
_DETECT_ONLY_STEP_FIELDS = (
    "question",
    "expected_answer",
)

# Exit-ticket question fields.
_AUTO_CORRECT_QUESTION_FIELDS = (
    "explanation",
)
_DETECT_ONLY_QUESTION_FIELDS = (
    "question_text",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_answer",
)


# ============================================================================
# Per-field helpers
# ============================================================================


def _verify_text_field(text: str) -> tuple[str, list[dict]]:
    """Wrap verify_calculations with noise-stripping + back-mapping.

    Returns `(corrected_text, corrections)` where corrected_text has
    only the claimed result-values rewritten — operands and any
    trailing °/units are preserved.
    """
    if not text or not isinstance(text, str):
        return text, []
    stripped = _strip_noise(text)
    if stripped == text:
        # No noise to strip — fast path.
        return verify_calculations(text)
    _, corrections = verify_calculations(stripped)
    if not corrections:
        return text, []
    # The corrections list comes from the noise-stripped copy, so
    # `expression` may have double-spaces where degrees / units used
    # to be. Collapse to single spaces for display.
    for c in corrections:
        if "expression" in c:
            c["expression"] = " ".join(c["expression"].split())
    corrected = _apply_corrections_to_original(text, corrections)
    return corrected, corrections


def _audit_entry(
    step_order: Optional[int],
    field: str,
    corrections: List[Dict],
    *,
    auto_corrected: bool,
) -> Dict:
    return {
        "step_order": step_order,
        "field": field,
        "auto_corrected": auto_corrected,
        "corrections": corrections,
    }


def _question_audit_entry(
    question_index: int,
    field: str,
    corrections: List[Dict],
    *,
    auto_corrected: bool,
) -> Dict:
    return {
        "question_index": question_index,
        "field": field,
        "auto_corrected": auto_corrected,
        "corrections": corrections,
    }


# ============================================================================
# Public API — called from content_generator.py
# ============================================================================


def verify_lesson_step(step_data: Dict, *, audit: List[Dict]) -> Dict:
    """Run verify_calculations over the verifiable fields of a single
    LessonStep dict (as emitted by the LLM, before persistence).

    Mutates `step_data` in place for AUTO_CORRECT fields. Appends
    audit entries to `audit` for every field that had at least one
    correction.

    Returns `step_data` for convenience (caller can inline).
    """
    step_order = step_data.get("order_index")

    # 1. Top-level auto-correct fields.
    for field in _AUTO_CORRECT_FIELDS:
        original = step_data.get(field) or ""
        corrected, corrections = _verify_text_field(original)
        if corrections:
            step_data[field] = corrected
            audit.append(_audit_entry(
                step_order, field, corrections, auto_corrected=True,
            ))

    # 2. Hints — list of strings. Walk each.
    hints = step_data.get(_HINT_LIST_KEY) or []
    if isinstance(hints, list):
        new_hints = []
        for i, hint in enumerate(hints):
            if not isinstance(hint, str):
                new_hints.append(hint)
                continue
            corrected, corrections = _verify_text_field(hint)
            if corrections:
                audit.append(_audit_entry(
                    step_order,
                    f"hints[{i}]",
                    corrections,
                    auto_corrected=True,
                ))
            new_hints.append(corrected)
        step_data[_HINT_LIST_KEY] = new_hints

    # 3. educational_content nested fields.
    edu = step_data.get("educational_content") or {}
    if isinstance(edu, dict):
        for field in _EDU_CONTENT_FIELDS:
            val = edu.get(field)
            if isinstance(val, str):
                corrected, corrections = _verify_text_field(val)
                if corrections:
                    edu[field] = corrected
                    audit.append(_audit_entry(
                        step_order,
                        f"educational_content.{field}",
                        corrections,
                        auto_corrected=True,
                    ))
            elif isinstance(val, list):
                new_list = []
                for i, item in enumerate(val):
                    if not isinstance(item, str):
                        new_list.append(item)
                        continue
                    corrected, corrections = _verify_text_field(item)
                    if corrections:
                        audit.append(_audit_entry(
                            step_order,
                            f"educational_content.{field}[{i}]",
                            corrections,
                            auto_corrected=True,
                        ))
                    new_list.append(corrected)
                edu[field] = new_list
        step_data["educational_content"] = edu

    # 4. Detect-only fields — answer-key dependent. Record but DON'T
    # rewrite. Layer 3 retry handles these.
    for field in _DETECT_ONLY_STEP_FIELDS:
        original = step_data.get(field) or ""
        if not isinstance(original, str):
            continue
        _, corrections = _verify_text_field(original)
        if corrections:
            audit.append(_audit_entry(
                step_order, field, corrections, auto_corrected=False,
            ))

    # 5. MCQ choices — same detect-only treatment.
    choices = step_data.get("choices")
    if isinstance(choices, list):
        for i, choice in enumerate(choices):
            if not isinstance(choice, str):
                continue
            _, corrections = _verify_text_field(choice)
            if corrections:
                audit.append(_audit_entry(
                    step_order,
                    f"choices[{i}]",
                    corrections,
                    auto_corrected=False,
                ))

    return step_data


def verify_exit_ticket_question(
    question_data: Dict,
    question_index: int,
    *,
    audit: List[Dict],
) -> Dict:
    """Run verify_calculations over the verifiable fields of a single
    exit-ticket question dict. Same auto-correct vs detect-only rules
    as `verify_lesson_step`.

    Mutates `question_data` in place for auto-correct fields. Appends
    audit entries indexed by `question_index`.

    Returns `question_data` for convenience.
    """
    # 1. Auto-correct fields.
    for field in _AUTO_CORRECT_QUESTION_FIELDS:
        original = question_data.get(field) or ""
        corrected, corrections = _verify_text_field(original)
        if corrections:
            question_data[field] = corrected
            audit.append(_question_audit_entry(
                question_index, field, corrections, auto_corrected=True,
            ))

    # 2. Detect-only fields — stem + options + answer key.
    for field in _DETECT_ONLY_QUESTION_FIELDS:
        original = question_data.get(field) or ""
        if not isinstance(original, str):
            continue
        _, corrections = _verify_text_field(original)
        if corrections:
            audit.append(_question_audit_entry(
                question_index, field, corrections, auto_corrected=False,
            ))

    # 3. answer_data — type-specific shapes. Most variants carry
    # numeric content somewhere (data_description, blanks, pairs).
    # We verify the string fields we recognise; unknown shapes pass
    # through.
    ad = question_data.get("answer_data") or {}
    if isinstance(ad, dict):
        # Fill-in-blank: text_template is a stem template; figure_svg
        # / data_description carry numbers; blanks is the answer key.
        for sub_field in ("data_description", "figure_description",
                          "text_template"):
            val = ad.get(sub_field)
            if isinstance(val, str) and val:
                _, corrections = _verify_text_field(val)
                if corrections:
                    audit.append(_question_audit_entry(
                        question_index,
                        f"answer_data.{sub_field}",
                        corrections,
                        auto_corrected=False,
                    ))

    return question_data


def has_unresolved_corrections(audit: List[Dict]) -> bool:
    """Return True if any audit entry has `auto_corrected=False`.
    Detect-only entries can't be silently fixed; Layer 3 retry is the
    correct response.
    """
    return any(not e.get("auto_corrected", True) for e in audit)


# ============================================================================
# Layer 3 — constraint-prompt builder for verify-then-retry
# ============================================================================


def build_arithmetic_constraint_block(
    step_audit: Optional[List[Dict]] = None,
    answer_key_mismatches: Optional[List[Dict]] = None,
) -> str:
    """Construct a constraint preamble to prepend to the original
    generation prompt for a Layer 3 retry.

    Lists every detect-only step correction (Layer 1) and every
    cross-check answer-key mismatch (Layer 2) in plain prose, with
    explicit fix instructions. Returns an empty string when nothing
    needs constraining.

    Trade-off: we paste the wrong claims back to the LLM, which can
    bias it toward repeating them. Mitigated by stating both the
    wrong value AND the correct value, and by capping at one retry.
    """
    detect_only_steps = [
        c for c in (step_audit or []) if not c.get("auto_corrected")
    ]
    mismatches = answer_key_mismatches or []
    if not detect_only_steps and not mismatches:
        return ""

    lines = [
        "=" * 72,
        "ARITHMETIC ERRORS IN PREVIOUS RESPONSE — FIX BEFORE RESPONDING",
        "=" * 72,
        "",
        "The following arithmetic claims in your previous output were wrong.",
        "Regenerate with these corrections applied:",
        "",
    ]

    if detect_only_steps:
        lines.append("In step content (question stems / expected answers):")
        for c in detect_only_steps:
            step_num = c.get("step_order")
            step_label = (
                f"Step {step_num + 1}" if step_num is not None else "a step"
            )
            for fix in c.get("corrections", []):
                lines.append(
                    f"  - {step_label} {c.get('field')}: "
                    f"'{fix['expression']} = {fix['claimed']}' is wrong "
                    f"(correct value is {fix['correct']})"
                )
        lines.append("")

    if mismatches:
        lines.append(
            "In exit-ticket questions (stem-vs-answer-key mismatches):"
        )
        for c in mismatches:
            qi = c.get("question_index", 0)
            lines.append(
                f"  - Q{qi + 1} (pattern {c.get('pattern')}): "
                f"the stem computes {c.get('computed')}, but the "
                f"stored answer was {c.get('claimed')}. Fix the stem "
                f"OR the answer so they agree."
            )
        lines.append("")

    lines.extend([
        "Re-verify EVERY arithmetic claim before responding.",
        "If unsure of a calculation, omit the explicit numeric step",
        "and use a placeholder like 'Step N: solve for x' instead.",
        "",
        "Original brief follows:",
        "",
    ])
    return "\n".join(lines)
