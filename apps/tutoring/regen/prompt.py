"""Focused regen prompt builder.

The whole point of the regen ensemble is to send a SMALL, FOCUSED
prompt to the rewrite-LLM rather than the 30KB tutoring system prompt.
The previous regen path appended a `<regeneration_required>` block to
the full tutor system prompt and the LLM ignored it more often than
not (production logs, 2026-05-07).

This module returns a `(user_prompt, system_prompt)` pair where:
  - system_prompt is ~500 chars: role + non-negotiable rules
  - user_prompt is ~1-2KB: previous response + violations + bank +
    media catalog excerpt

The rewrite-LLM does ONE job: take the original response, fix the
violations listed, return ONLY the rewritten text. No teaching, no
warmth-engineering, no curriculum recall.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


_SYSTEM = (
    "You are a tutor-response repair assistant. You receive ONE tutor "
    "response that violated review rules and a list of violations to "
    "fix. Your job is to rewrite the response so it satisfies the "
    "rules — keep what was good, fix what was flagged. Output ONLY "
    "the rewritten response text. No preamble, no explanation, no "
    "markdown wrapping the whole reply.\n"
    "\n"
    "Hard rules — do NOT violate these:\n"
    "  1) Do not invent new arithmetic. If a number was wrong in the "
    "original, drop it or use the correct value supplied in the "
    "violations block.\n"
    "  2) Do not author new questions outside the BANK. If you need "
    "to pose a question, pick from the BANK section verbatim. If the "
    "BANK is empty, ask a CONCEPTUAL question (no concrete numerical "
    "values made up).\n"
    "  3) If you reference a figure (\"the diagram\", \"in the image\"), "
    "you MUST emit |||MEDIA:N||| as the LAST line, where N is the "
    "number from the MEDIA CATALOG. If no matching figure exists, "
    "DROP the figure reference entirely.\n"
    "  4) End with one focused question (or a short transition if "
    "the step is mid-explanation).\n"
    "  5) Do not contradict yourself within the response.\n"
    "  6) If the violations say the student's answer was actually "
    "correct (verdict_mismatch direction=tutor_said_wrong_was_right), "
    "the rewrite must AFFIRM the answer was right. If the violations "
    "say the student's answer was wrong, the rewrite must NOT praise "
    "them.\n"
)


def build_regen_prompt(
    *,
    previous_response: str,
    issues: List[str],
    validation_metadata: Dict,
    bank_stems: List[str],
    media_catalog_text: str = "",
    student_input: str = "",
) -> Tuple[str, str]:
    """Return (user_prompt, system_prompt) for the rewrite-LLM.

    Args:
      previous_response: the violating tutor reply (verbatim)
      issues: list of validator issue codes
      validation_metadata: extra context from the validator (specific
        violation strings, figure_ref phrases, mismatch direction, etc.)
      bank_stems: question stems the rewrite is allowed to pose
      media_catalog_text: numbered figure list — already formatted
        the same way `_build_media_catalog` produces. Empty string
        means no figures available.
      student_input: the student's last message (so the rewrite has
        the same conversational context the tutor saw)
    """
    parts: List[str] = []

    parts.append("## STUDENT_LAST_MESSAGE")
    parts.append((student_input or "(none)").strip()[:400])
    parts.append("")

    parts.append("## ORIGINAL_TUTOR_RESPONSE (the one that needs fixing)")
    parts.append("```")
    parts.append((previous_response or "").strip()[:2400])
    parts.append("```")
    parts.append("")

    parts.append("## VIOLATIONS_TO_FIX")
    if not issues:
        parts.append("(none — but a regen was triggered; check the metadata)")
    else:
        # Map each issue code to a short imperative repair instruction.
        # The metadata block carries the specific evidence strings.
        for issue in issues:
            parts.append(_violation_line(issue, validation_metadata))
    parts.append("")

    parts.append("## BANK (questions you may pose verbatim — do NOT invent new ones)")
    if bank_stems:
        for stem in bank_stems[:8]:
            parts.append(f"- {stem.strip()[:200]}")
    else:
        parts.append("(empty — ask a CONCEPTUAL question only, no numeric setup)")
    parts.append("")

    if media_catalog_text and media_catalog_text.strip():
        parts.append("## MEDIA_CATALOG (figures you may reference; emit |||MEDIA:N||| as LAST line)")
        # Already-numbered list from _build_media_catalog — pass through.
        parts.append(media_catalog_text.strip()[:1200])
        parts.append("")
    else:
        parts.append("## MEDIA_CATALOG")
        parts.append("(no figures attached this turn — DO NOT use phrases "
                     "like 'looking at the diagram', 'in the figure', or "
                     "'shown above'. Explain in plain prose instead.)")
        parts.append("")

    parts.append("## OUTPUT")
    parts.append(
        "Return ONLY the rewritten tutor response. No preamble, no "
        "explanation. End with one question (or a short transition). "
        "If you reference a figure, emit |||MEDIA:N||| as the LAST line."
    )

    user_prompt = "\n".join(parts)
    return user_prompt, _SYSTEM


# ---------------------------------------------------------------------
# Per-issue repair prescriptions
# ---------------------------------------------------------------------

def _violation_line(issue: str, meta: Dict) -> str:
    """Translate an ISSUE_… code into a one-line repair instruction
    grounded in the validator metadata.

    Keep these tight — the rewrite-LLM follows imperative instructions
    much better than abstract rule names.
    """
    if issue == "verdict_mismatch":
        direction = (meta.get("verdict_mismatch_direction") or "").strip()
        if direction == "tutor_said_wrong_was_right":
            return (
                "- VERDICT_MISMATCH: the deterministic check confirms the "
                "student's answer is CORRECT, but the original response "
                "said it was wrong. REWRITE to affirm the correct "
                "answer. Do not ask 'walk me through your working'."
            )
        if direction == "tutor_said_right_was_wrong":
            return (
                "- VERDICT_MISMATCH: the deterministic check confirms the "
                "student's answer is INCORRECT, but the original "
                "praised them. REWRITE to point at the actual error "
                "and ask one focused question."
            )
        return "- VERDICT_MISMATCH: align the response with the deterministic verdict."

    if issue == "tutor_incoherent":
        violations = meta.get("coherence_violations") or []
        if violations:
            joined = "; ".join(str(v)[:140] for v in violations[:3])
            return (
                "- TUTOR_INCOHERENT: the original contradicted itself. "
                f"Specifically: {joined}. Pick ONE consistent framing "
                "and rewrite."
            )
        return (
            "- TUTOR_INCOHERENT: the original contradicted itself. Pick "
            "ONE consistent framing and rewrite."
        )

    if issue == "figure_ref_without_signal":
        figref = meta.get("figure_ref_issues") or []
        if figref:
            joined = "; ".join(str(v)[:140] for v in figref[:3])
            return (
                "- FIGURE_REF_WITHOUT_SIGNAL: tutor referenced a figure "
                f"with no |||MEDIA:N||| attached. Phrases flagged: "
                f"{joined}. Either pick a figure from MEDIA_CATALOG "
                "and emit |||MEDIA:N||| as the last line, or REMOVE "
                "every figure reference and explain in plain prose."
            )
        return (
            "- FIGURE_REF_WITHOUT_SIGNAL: drop figure phrases, OR pick "
            "from MEDIA_CATALOG and emit |||MEDIA:N|||."
        )

    if issue == "figure_mismatch":
        reason = (meta.get("figure_mismatch_reason") or "").strip()
        summary = (meta.get("figure_summary") or "").strip()
        return (
            f"- FIGURE_MISMATCH: the attached figure does not match the "
            f"question. {('Figure shows: ' + summary + '. ') if summary else ''}"
            f"{('Mismatch reason: ' + reason + '. ') if reason else ''}"
            "Either rewrite the question to match what the figure depicts, "
            "or pick a different |||MEDIA:N||| from MEDIA_CATALOG, or "
            "drop the figure reference."
        )

    if issue == "arithmetic_violation":
        corrections = meta.get("arithmetic_corrections") or []
        if corrections:
            lines = []
            for c in corrections[:3]:
                if isinstance(c, dict):
                    lines.append(
                        f"  '{c.get('expression', '?')}' "
                        f"claimed='{c.get('claimed', '?')}' "
                        f"correct='{c.get('correct', '?')}'"
                    )
            return (
                "- ARITHMETIC_VIOLATION: replace the wrong values with "
                "the correct ones (or remove the calculation):\n"
                + "\n".join(lines)
            )
        return (
            "- ARITHMETIC_VIOLATION: drop or correct the wrong arithmetic."
        )

    if issue == "authoring_violation":
        return (
            "- AUTHORING_VIOLATION: the original posed a numerical "
            "question NOT in the BANK. Either reuse a BANK stem "
            "verbatim, or replace the numerical question with a "
            "CONCEPTUAL one (e.g. 'which rule applies?')."
        )

    if issue == "rule1_violation":
        return (
            "- RULE_1: the original praised the student on a bare or "
            "wrong math answer. Drop the praise and ask one focused "
            "question that probes their working — vary your wording, "
            "do NOT default to 'walk me through your steps'."
        )

    if issue == "numeric_claim_contradicted":
        contradicted = meta.get("factual_claims_contradicted") or []
        joined = "; ".join(str(c)[:120] for c in contradicted[:3])
        return (
            "- NUMERIC_CLAIM_CONTRADICTED: the curriculum disagrees "
            f"with these claims — DO NOT restate them: {joined}"
        )

    if issue == "no_question":
        return (
            "- NO_QUESTION: end with one focused question (must "
            "literally end with '?')."
        )

    if issue == "info_dump_warning":
        return (
            "- INFO_DUMP_WARNING: the original was too long / too many "
            "named concepts. Trim to the single most important point "
            "and one question."
        )

    return f"- {issue}: fix this issue per the original validator output."
