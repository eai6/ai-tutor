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
    bank_context: Dict = None,
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

    # Bank context FIRST — ground truth the rewrite must anchor to.
    # When the previous tutor turn posed a bank question and the
    # student replied, the grader's verdict + the bank's explanation
    # are the SOURCE OF TRUTH. Without this block, the regen LLM
    # invented contradicting answers (pilot 2026-05-16: student
    # picked C=Human geography for q3760, grader said CORRECT, regen
    # rewrote with "cultural or social geography" — a non-option that
    # contradicted the bank's own explanation). Top placement so the
    # LLM internalises it before reading the broken response.
    if bank_context:
        suppress_reason = (bank_context.get('suppress_reason') or '').strip()
        leak_mode = (suppress_reason == 'answer_leak_regen')

        if leak_mode:
            # W3 leak-aware regen: the canonical answer has been
            # INTENTIONALLY REMOVED from your context because the
            # previous response leaked it. Output a concept-level
            # hint only — you literally don't know the answer.
            parts.append("## LEAK_AWARE_REGEN — ANSWER INTENTIONALLY HIDDEN")
            parts.append(
                "The previous tutor response LEAKED the canonical answer. "
                "The student answered wrong and is still allowed to retry. "
                "The canonical answer has been REMOVED from your context "
                "so you cannot leak it.\n"
                "\n"
                "OUTPUT: ONE concept-level hint that names what the "
                "question is testing. Do NOT describe what any option "
                "says. Do NOT name the correct option letter. Let the "
                "student attempt again. ONE short scaffolding question "
                "is OK ('what's the key thing you need before X?')."
            )
        else:
            parts.append("## BANK_GROUND_TRUTH (the question this turn responds to — DO NOT CONTRADICT)")
        q_text = (bank_context.get('question_text') or '').strip()
        if q_text:
            parts.append(f"question: {q_text[:300]}")
        opts = bank_context.get('options') or {}
        if opts:
            for letter in ('A', 'B', 'C', 'D'):
                opt_text = (opts.get(letter) or '').strip()
                if opt_text:
                    parts.append(f"  {letter}. {opt_text[:120]}")
        if not leak_mode:
            ca = (bank_context.get('correct_answer') or '').strip()
            if ca:
                parts.append(f"correct_answer: {ca}")
            expl = (bank_context.get('explanation') or '').strip()
            if expl:
                parts.append(f"explanation: {expl[:400]}")
        stud_ans = (bank_context.get('student_answer') or '').strip()
        if stud_ans:
            parts.append(f"student_answer: {stud_ans[:120]}")
        verdict = bank_context.get('verdict')
        if leak_mode:
            # Don't re-state verdict-tied rewrite directives — the
            # LEAK_AWARE_REGEN block above is the only directive.
            pass
        elif verdict is True:
            parts.append("verdict: CORRECT")
            parts.append(
                "REWRITE MUST: confirm the student's answer + explain "
                "WHY using the explanation field above. Do NOT invent "
                "a different category or contradict the explanation. "
                "Do NOT ask the student to show working on a correct "
                "answer. Then advance via a bank question (do NOT "
                "author a new question in prose)."
            )
        elif verdict is False:
            parts.append("verdict: INCORRECT")
            parts.append(
                "REWRITE MUST: gently say their answer wasn't right, "
                "use the explanation field above to show the actual "
                "correct answer is the one labelled by `correct_answer`. "
                "Do NOT invent a different correct option."
            )
        parts.append("")

    # Prior context — formatted same way the judges see history (see
    # apps/tutoring/judges/history.py). Placed up top so the LLM sees
    # what was already said BEFORE reading the response to repair.
    if conversation_history:
        from ai_tutor.apps.tutoring.judges.history import format_history_window
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
            # Distinguish parallel-questions case so the LLM sees a
            # focused fix pattern. Heuristic: the coherence violation
            # mentions "two", "parallel", or "questions".
            is_dual_question = any(
                kw in str(joined).lower()
                for kw in ("two parallel", "two distinct", "parallel question")
            )
            if is_dual_question:
                return (
                    "- TUTOR_INCOHERENT (parallel questions): the "
                    "response asked TWO distinct questions in one "
                    f"turn. Specifically: {joined}.\n"
                    "  Fix: pick ONE question — the one that moves "
                    "the student forward at THIS step. Drop the "
                    "other. A focused turn has ONE acknowledgment "
                    "(at most) + ONE question.\n"
                    "  Example: BEFORE: \"You're thinking about the "
                    "operations. What was the first thing you "
                    "noticed? [MCQ: If x + 15 = 40, what is x?]\" "
                    "AFTER: \"You're thinking about the operations — "
                    "good. [MCQ: If x + 15 = 40, what is x?]\" "
                    "(acknowledgment kept, conceptual probe dropped, "
                    "MCQ stands alone)"
                )
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

    # rule1_violation handler REMOVED 2026-05-17 — RULE_1 is gone.
    # Grader is authoritative on correctness; praise on a verified-
    # correct bare answer is justified.

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

    if issue == "no_question_tool":
        # Task #197 — LLM typed an MCQ in prose instead of using a tool.
        # Engine has no way to grade un-tooled prose questions, so we
        # force a re-pose via tool.
        return (
            "- NO_QUESTION_TOOL: you posed a NEW question in your prose "
            "response (multiple-choice options A/B/C/D detected) instead "
            "of using a tool call. The engine cannot track or grade "
            "questions that aren't tool-posed — the student's reply will "
            "get graded against the wrong question.\n"
            "  Fix: remove the prose question from your response text, "
            "then re-pose it via a tool call. Use pose_question if the "
            "question is in the lesson bank (preferred — canonical "
            "curriculum coverage). Otherwise use pose_inline_question "
            "with the question text, optional answer_key (supply for "
            "objective questions like numeric / true-false / fact-recall; "
            "omit for conceptual / open-ended), and the appropriate "
            "type. The tool RENDERS the question into the chat bubble — "
            "you do NOT need to also type it in prose.\n"
            "  Allowed in prose: hints, explanations, probing rhetorical "
            "questions tied to the active question ('what made you pick "
            "A?'). NOT allowed in prose: any new multi-choice / "
            "fact-recall / short-answer question the student is "
            "expected to answer."
        )

    if issue == "no_question":
        # The handoff LLM judge surfaces a specific reason via
        # validation.metadata['handoff_reason'] when it flags
        # handed_off=False (task #183). Include it so the regen LLM
        # sees the exact failure mode (dangling colon, mid-sentence
        # truncation, pure ack...) instead of generic "end with ?".
        handoff_reason = (meta.get("handoff_reason") or "").strip()
        head = (
            "- NO_QUESTION: the response did not hand the floor back "
            "to the student.\n"
        )
        if handoff_reason:
            head += f"  Specifically: {handoff_reason[:200]}\n"
        # Anti-smuggle (2026-05-17): when the validator passed an
        # awaiting_answer_is_set=True signal AND we have bank_context
        # above, the fix is to RESTATE the active question — NOT
        # invent a new one. Inventing creates state drift (engine's
        # bank_question_ref points at the active Q, but student sees
        # the smuggled prose Q; grader then mismatches). See
        # memory/tutor_state_drift_and_leak_simplification_plan.md.
        if meta.get("awaiting_answer_is_set"):
            return (
                head
                + "  Fix: RESTATE the active question (see "
                "BANK_GROUND_TRUTH above) verbatim as the last "
                "lines of your turn, so the student can re-attempt "
                "it. Do NOT author a NEW question. Do NOT call "
                "pose_question or pose_inline_question in your "
                "response. The active question already lives in the "
                "engine's state — your job is to put it back on "
                "screen for the student.\n"
                "  Example: BEFORE: \"Here's the key idea — larger "
                "scales show more detail. Want to try again?\" "
                "AFTER: \"Here's the key idea — larger scales show "
                "more detail.\\n\\nWhich statement is true?\\n"
                "A) ...\\nB) ...\\nC) ...\\nD) ...\" (the four "
                "options come from the BANK_GROUND_TRUTH block above)"
            )
        return (
            head
            + "  Fix: end with ONE focused question OR a clear "
            "next-action directive. The LAST sentence MUST be either "
            "(a) a complete question ending in '?', OR (b) an "
            "imperative invitation (\"try this one\", \"pick one\", "
            "\"tell me what you notice\"). Never dangle with \"Now "
            "let me ask:\" + no question, never end mid-sentence, "
            "never end on a pure acknowledgement (\"Great work!\").\n"
            "  Example: BEFORE: \"Algebra is useful for word "
            "problems.\" AFTER: \"Algebra is useful for word problems. "
            "What part trips you up most?\""
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

    if issue == "answer_leak":
        reason = (meta.get("answer_leak_reason") or "").strip()
        return (
            "- ANSWER_LEAK: the original response REVEALED the correct "
            "answer to the student. Pilot directive 2026-05-17: reveal "
            "is NEVER allowed regardless of wrong_attempts. Past the "
            "move-on threshold the tutor MUST pivot to a different "
            "easier question on the same concept WITHOUT stating the "
            "canonical. The canonical answer has been REMOVED from "
            "your context (see LEAK_AWARE_REGEN block above) — output "
            "ONE concept-level hint or a brief concept re-explanation "
            "that names what the question is testing WITHOUT "
            "describing what any option says.\n"
            + (f"  Detector reason: {reason[:200]}\n" if reason else "")
            + "  Example: BEFORE: \"A compass rose helps you figure "
            "out which direction to travel.\" AFTER: \"Think about "
            "what a compass rose actually shows on a map — and what "
            "you need before you can pick a route.\""
        )

    if issue == "repeated_question":
        reason = (meta.get("repeated_question_reason") or "").strip()
        return (
            "- REPEATED_QUESTION: the original re-asked a question the "
            "student already saw (either a previous tutor probe or the "
            "active bank Q paraphrased). Fix: choose ONE — (a) advance "
            "to the next concept/step, (b) ask about a DIFFERENT "
            "angle of the same concept, or (c) give a hint on the "
            "still-pending question if there is one. Do NOT re-state "
            "the same question.\n"
            + (f"  Detector reason: {reason[:200]}\n" if reason else "")
            + "  Example: BEFORE: \"What is the function of a compass "
            "rose?\" (already asked). AFTER: \"Why is a compass rose "
            "especially important for navigating between islands like "
            "Mahé and Praslin?\""
        )

    if issue == "same_template_repeat":
        return (
            "- SAME_TEMPLATE_REPEAT: the original posed a question with "
            "the SAME structural template as the previous tutor turn "
            "(only the numbers changed). Mid-tier interleaving failure. "
            "Fix: pose a question on the same concept but with a "
            "structurally DIFFERENT shape — change the question type "
            "(reverse problem, word-problem framing, MCQ instead of "
            "open-ended), the kind of unknown being solved for, or "
            "switch to a different bank slot. If the bank has no "
            "structurally-different slot for this concept, ADVANCE to "
            "the next step.\n"
            "  Example: BEFORE (just asked): \"Three angles around a "
            "point are 80°, 80°, and y°. Find y.\" AFTER: \"A pie is "
            "cut into 6 equal slices. What angle does each slice make "
            "at the center?\" (different structural shape — equal "
            "division rather than missing-angle subtraction)."
        )

    if issue == "truncated":
        tail = (meta.get("truncated_tail") or "").strip()
        return (
            "- TRUNCATED: the original tutor prose was cut off mid-"
            "sentence (no terminal punctuation on the last line). The "
            "student would see a dangling fragment. Fix: rewrite the "
            "turn fully so the prose ends with a complete sentence "
            "and terminal punctuation before the question tool is "
            "invoked. Keep the total under the word limit so this "
            "doesn't recur.\n"
            + (f"  Truncated tail: {tail[:120]!r}\n" if tail else "")
            + "  Example: BEFORE: \"Yes — 360° is correct. In\" "
            "AFTER: \"Yes — 360° is correct, because the four angles "
            "fill a full turn around a single point.\""
        )

    if issue == "numeric_mutation":
        invented = meta.get("numeric_mutation_invented") or []
        stem = (meta.get("numeric_mutation_stem") or "").strip()
        return (
            "- NUMERIC_MUTATION: the original tutor turn introduced "
            "numeric values that are NOT in the active problem's stem "
            "(or in any accepted intermediate calculation from the "
            "previous tutor turn). This means the model invented or "
            "swapped numbers mid-feedback — students will be confused "
            "because the new numbers don't match the question they're "
            "trying to solve. Fix: rewrite using ONLY the numbers from "
            "the active problem stem. If you need to show an "
            "intermediate result, derive it explicitly from a stated "
            "stem number.\n"
            + (f"  Active stem: {stem[:160]!r}\n" if stem else "")
            + (f"  Invented numbers: {invented}\n" if invented else "")
            + "  Example: stem says \"scale 1:10,000, distance 4 cm\". "
            "BEFORE: \"Since the scale is 1:40,000…\" (40,000 not in "
            "stem). AFTER: \"Since the scale is 1:10,000, 4 cm "
            "represents 4 × 10,000 = 40,000 cm = 400 m.\""
        )

    if issue == "filler_reply_teach":
        filler = (meta.get("filler_reply_input") or "").strip()
        return (
            "- FILLER_REPLY_TEACH: the student's last reply was content-"
            "free filler (\"ok\", \"yes\", \"got it\", \"...\") — NOT an "
            "answer attempt. The original tutor turn responded with a "
            "fresh teach block, which buries the student under more "
            "content they did not ask for. Fix: do one of these instead "
            "— (a) re-pose the currently-active question (use the bank "
            "tool with the active slot), OR (b) ask a short check-in "
            "(\"Ready to continue?\" / \"Want to try another?\") before "
            "advancing. Do not deliver new instructional content.\n"
            + (f"  Filler reply: {filler!r}\n" if filler else "")
            + "  Example: STUDENT: \"ok\". BEFORE: \"Now let's explore "
            "the next concept...\" AFTER (via the question tool): "
            "\"Ready to try one? Which of these angles is acute: "
            "30°, 90°, or 120°?\""
        )

    return f"- {issue}: fix this issue per the original validator output."
