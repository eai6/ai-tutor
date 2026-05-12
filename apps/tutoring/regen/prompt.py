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
    conversation_history: list = None,
    history_turns: int = 6,
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
      conversation_history: list of prior {role, content} turns —
        same shape the engine maintains. The last ``history_turns``
        are formatted into a CONVERSATION_HISTORY block so the
        rewrite-LLM can fix cross-turn coherence violations (e.g.
        "you switched the equation from 5x+20=35 to 3x+20=80
        without explanation"). Without history, regen was blind to
        the prior tutor turn and converged to the same dirty
        candidate cycle after cycle.
      history_turns: how many trailing messages to include. Default 6
        (~3 student + 3 tutor exchanges before the current pair) —
        wider than the judges' window (4) since regen specifically
        needs to AVOID contradicting earlier turns.
    """
    parts: List[str] = []

    # Prior context — formatted same way the judges see history (see
    # apps/tutoring/judges/history.py). Placed up top so the LLM sees
    # what was already said BEFORE reading the response to repair.
    if conversation_history:
        from apps.tutoring.judges.history import format_history_window
        prior = format_history_window(
            conversation_history, turns=history_turns,
        )
        if prior:
            parts.append("## CONVERSATION_HISTORY (recent turns; do NOT contradict these)")
            parts.append(prior)
            parts.append("")

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
    """Translate an ISSUE_… code into a repair instruction with a
    concrete BEFORE → AFTER example.

    Prompting principles applied (see .claude/skills/{claude-,}prompting-
    fundamentals-expert): positive imperatives ("do X" not just "don't Y"),
    one short example per issue (Gemini/Claude both benefit from BEFORE→
    AFTER fix patterns), match the existing markdown bullet style.
    """
    if issue == "tutor_unsafe":
        sev = (meta.get("safety_severity") or "warning").strip()
        cats = meta.get("safety_categories") or []
        cat_str = ", ".join(str(c) for c in cats) if cats else "(unspecified)"
        reason = (meta.get("safety_reasoning") or "").strip()
        return (
            f"- TUTOR_UNSAFE (severity={sev}): the original contained "
            f"content classified as [{cat_str}]."
            + (f" Reviewer reason: {reason}." if reason else "")
            + " Fix: age-appropriate (13–16), no harmful / sexual / "
            "self-harm / substance content. If the topic itself can't "
            "be taught safely, redirect to a trusted adult and pivot "
            "to a BANK question."
        )

    if issue == "verdict_mismatch":
        direction = (meta.get("verdict_mismatch_direction") or "").strip()
        if direction == "tutor_said_wrong_was_right":
            return (
                "- VERDICT_MISMATCH: deterministic check confirms the "
                "student's answer is CORRECT, but the original said "
                "wrong. Fix: affirm the answer; move forward — do NOT "
                "ask them to walk through working.\n"
                "  Example: BEFORE: \"Not quite — try again.\" "
                "AFTER: \"Right — x = 8. Now let's try one with a "
                "different number.\""
            )
        if direction == "tutor_said_right_was_wrong":
            return (
                "- VERDICT_MISMATCH: deterministic check confirms the "
                "student's answer is INCORRECT, but the original "
                "praised them. Fix: name the specific error in one "
                "sentence and ask ONE focused question.\n"
                "  Example: BEFORE: \"Perfect!\" AFTER: \"Let's "
                "check — you said 95, but 95 + 70 + 95 = 260, not 360. "
                "What did you get when you added them?\""
            )
        return (
            "- VERDICT_MISMATCH: align the response with the "
            "deterministic verdict (correct → affirm, wrong → diagnose)."
        )

    if issue == "tutor_incoherent":
        violations = meta.get("coherence_violations") or []
        if violations:
            joined = "; ".join(str(v)[:140] for v in violations[:3])
            return (
                "- TUTOR_INCOHERENT: the response contradicted itself "
                "OR contradicted a prior turn.\n"
                f"  Specifically: {joined}.\n"
                "  Fix: pick ONE consistent framing. If a previous "
                "turn (see CONVERSATION_HISTORY above) established an "
                "equation / value / setup, KEEP IT — do not introduce "
                "a different equation just because you're rewriting.\n"
                "  Example: BEFORE: \"Solve 5x + 20 = 35. ... Now "
                "try 3x + 20 = 80.\" AFTER: \"Solve 5x + 20 = 35. "
                "What is x?\""
            )
        return (
            "- TUTOR_INCOHERENT: the response contradicted itself or a "
            "prior turn. Fix: pick ONE consistent framing; preserve "
            "any equation / value already established in CONVERSATION_"
            "HISTORY.\n"
            "  Example: BEFORE: \"Solve 5x = 35 ... Now try 3x = 80.\" "
            "AFTER: \"Solve 5x = 35. What is x?\""
        )

    if issue == "figure_ref_without_signal":
        figref = meta.get("figure_ref_issues") or []
        if figref:
            joined = "; ".join(str(v)[:140] for v in figref[:3])
            return (
                "- FIGURE_REF_WITHOUT_SIGNAL: tutor referenced a figure "
                f"with no |||MEDIA:N||| attached. Phrases flagged: {joined}.\n"
                "  Fix: either pick from MEDIA_CATALOG and emit "
                "|||MEDIA:N||| as the LAST line, OR remove every "
                "figure phrase and explain in prose.\n"
                "  Example: BEFORE: \"Look at the diagram — what's x?\" "
                "AFTER: \"Imagine three angles meeting at a point: 95°, "
                "70°, and x°. What does x equal?\""
            )
        return (
            "- FIGURE_REF_WITHOUT_SIGNAL: drop figure phrases, OR pick "
            "from MEDIA_CATALOG and emit |||MEDIA:N|||.\n"
            "  Example: BEFORE: \"Looking at the figure...\" "
            "AFTER: \"Picture a triangle with angles 60°, 70°, x°...\""
        )

    if issue == "figure_mismatch":
        reason = (meta.get("figure_mismatch_reason") or "").strip()
        summary = (meta.get("figure_summary") or "").strip()
        return (
            f"- FIGURE_MISMATCH: the attached figure does not match the "
            f"question. {('Figure shows: ' + summary + '. ') if summary else ''}"
            f"{('Mismatch reason: ' + reason + '. ') if reason else ''}"
            "Fix: change the question to match the figure, OR pick a "
            "different |||MEDIA:N|||, OR drop the figure reference.\n"
            "  Example: BEFORE: \"Find the angle in this triangle\" "
            "with a circle attached. AFTER: \"Picture a triangle with "
            "angles 95° + 70° + x° = 180°.\""
        )

    if issue == "arithmetic_violation":
        corrections = meta.get("arithmetic_corrections") or []
        if corrections:
            lines = []
            for c in corrections[:3]:
                if isinstance(c, dict):
                    lines.append(
                        f"    '{c.get('expression', '?')}' "
                        f"claimed='{c.get('claimed', '?')}' "
                        f"correct='{c.get('correct', '?')}'"
                    )
            return (
                "- ARITHMETIC_VIOLATION: replace the wrong values, OR "
                "drop the calculation entirely. Specific corrections:\n"
                + "\n".join(lines)
                + "\n  Example: BEFORE: \"95 + 70 = 175 so x = 175.\" "
                "AFTER: \"95 + 70 = 165 so x = 360 − 165 = 195.\" "
                "(or drop the calc and ask the student to compute it)"
            )
        return (
            "- ARITHMETIC_VIOLATION: drop or correct the wrong "
            "arithmetic.\n"
            "  Example: BEFORE: \"8 × 5 = 35.\" AFTER: drop that line "
            "and ask the student to multiply."
        )

    if issue == "authoring_violation":
        return (
            "- AUTHORING_VIOLATION: the original posed a numerical "
            "question NOT in the BANK. Fix: either reuse a BANK stem "
            "verbatim, OR replace with a CONCEPTUAL question (no "
            "invented numbers).\n"
            "  Example: BEFORE: \"If angles are 100°, 120°, 80°, do "
            "they sum to 360°?\" AFTER: \"What rule applies when "
            "several angles meet at a single point?\""
        )

    if issue == "rule1_violation":
        return (
            "- RULE_1: the original praised the student on a bare or "
            "wrong math answer. Fix: drop the praise and ask for "
            "reasoning — vary your wording, do NOT always default to "
            "\"walk me through your steps\".\n"
            "  Example: BEFORE: \"Perfect! You got it.\" AFTER: \"How "
            "did you get there?\" — or \"Which rule did you apply?\" "
            "— or \"What was the first thing you noticed?\""
        )

    if issue == "numeric_claim_contradicted":
        contradicted = meta.get("factual_claims_contradicted") or []
        joined = "; ".join(str(c)[:120] for c in contradicted[:3])
        return (
            "- NUMERIC_CLAIM_CONTRADICTED: the curriculum knowledge "
            "base disagrees with these claims — do NOT restate them. "
            f"Flagged claims: {joined}.\n"
            "  Fix: either replace with the curriculum-supported "
            "value, OR hedge (\"we'll need to look this up\"), OR "
            "drop the claim entirely.\n"
            "  Example: BEFORE: \"Seychelles has 50 islands.\" AFTER: "
            "drop or hedge — the curriculum says 115."
        )

    if issue == "numeric_claim_unverified":
        unverified = meta.get("factual_claims_unverified") or []
        joined = "; ".join(str(c)[:120] for c in unverified[:3])
        return (
            "- NUMERIC_CLAIM_UNVERIFIED: these claims aren't supported "
            f"by the curriculum KB: {joined}.\n"
            "  Fix: either drop the specific number / fact, OR hedge "
            "the claim (\"approximately\", \"we'll verify together\"), "
            "OR ask the student to look it up.\n"
            "  Example: BEFORE: \"Seychelles exports $237M of tuna.\" "
            "AFTER: \"Tuna is a major export — what do you think the "
            "rough figure is?\""
        )

    if issue == "no_question":
        return (
            "- NO_QUESTION: the response did not end with a question. "
            "Fix: end with ONE focused question. The literal LAST "
            "character (before any |||MEDIA:N||| signal) must be '?'.\n"
            "  Example: BEFORE: \"Algebra is useful for word "
            "problems.\" AFTER: \"Algebra is useful for word problems. "
            "What part trips you up most?\""
        )

    if issue == "info_dump_warning":
        return (
            "- INFO_DUMP_WARNING: the response packed too many named "
            "concepts / too much prose. Fix: trim to ONE core point + "
            "ONE question. Aim for ≤4 sentences total.\n"
            "  Example: BEFORE: a 5-sentence paragraph explaining "
            "three rules. AFTER: name ONE rule, give ONE concrete "
            "feel for it, then ask one targeted question."
        )

    if issue == "multi_paragraph":
        return (
            "- MULTI_PARAGRAPH: response had multiple paragraphs when "
            "one short turn is expected. Fix: collapse to a single "
            "block (≤4 sentences) + a closing question.\n"
            "  Example: BEFORE: 3 paragraphs of explanation. AFTER: "
            "one tight paragraph + one question."
        )

    if issue == "banned_opener":
        return (
            "- BANNED_OPENER: response started with banned filler "
            "(\"Great question!\", \"That's a wonderful observation!\", "
            "\"Excellent thinking!\", etc.). Fix: skip the opener; "
            "respond directly.\n"
            "  Example: BEFORE: \"Great question! Algebra is...\" "
            "AFTER: \"Algebra lets you solve when one piece is unknown. "
            "What's the unknown here?\""
        )

    if issue == "padding_filler":
        return (
            "- PADDING_FILLER: the response had conversational filler "
            "without instructional value. Fix: cut hedges / restatements"
            " and keep only the teaching content + one question.\n"
            "  Example: BEFORE: \"That's a really interesting thought. "
            "Let me see... I think what we want to do is...\" "
            "AFTER: \"Use the inverse: subtract 20 from both sides. "
            "What do you get?\""
        )

    if issue == "premature_advance":
        return (
            "- PREMATURE_ADVANCE: the original moved past a sub-step "
            "the student hasn't completed. Fix: stay on the current "
            "sub-step. Address the student's last input first, THEN "
            "(only if they're correct) advance.\n"
            "  Example: BEFORE: student wrote \"40\" to a wrong "
            "answer; tutor jumped to a new equation. AFTER: \"Let's "
            "check 40 — what's 5 × 40 + 20? Does that equal 35?\""
        )

    return f"- {issue}: fix this issue per the original validator output."
