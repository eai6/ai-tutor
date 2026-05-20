"""Socratic tutor response validator.

Assumes the tutor is wrong until proven otherwise. Every tutor response
runs through this pipeline before it is saved to DB or sent to the
student. Issues are either soft-fixed (strip the offending fragment)
or logged for teacher visibility.

V1 layers (this module):
  L1 STRUCTURAL — does the response end with a question (on practice/quiz)?
  L2 PEDAGOGICAL — praise present + correctness signal said wrong/bare?
                   strip the praise (extends the math-only fix to ALL
                   subjects).

Future layers (V2-V4, see memory/socratic_validator_plan.md):
  L3 CORRECTNESS — extend LLM evaluator to all subjects (already in place)
  L4 FACTUAL — RAG-verify numeric/named claims against curriculum KB
  L5 REGENERATE — retry once with validator issues injected as constraints
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from apps.tutoring.praise_filter import strip_praise_if_wrong, _PRAISE_RE
from apps.tutoring.fact_verifier import verify_response, FactCheckResult


# Issues we record. Strings rather than enums so they serialize cleanly
# into SessionTurn.metadata JSONField.
ISSUE_NO_QUESTION = "no_question"
ISSUE_UNFOUNDED_PRAISE_STRIPPED = "unfounded_praise_stripped"
ISSUE_NUMERIC_CLAIM_UNVERIFIED = "numeric_claim_unverified"
ISSUE_NUMERIC_CLAIM_CONTRADICTED = "numeric_claim_contradicted"
# P5 — rule-compliance violations (LLM-as-judge). See
# memory/tutor_no_authoring_plan.md.
ISSUE_AUTHORING_VIOLATION = "authoring_violation"
ISSUE_ARITHMETIC_VIOLATION = "arithmetic_violation"
# ISSUE_RULE1_VIOLATION ("praise on bare answer") REMOVED 2026-05-17.
# Rationale: the rule was added when the grader was unreliable and the
# tutor could praise a bare answer that was actually wrong. With the
# bank grader + chat-authored grader now authoritative on correctness
# (deterministic for MCQ/numeric, grounded LLM for the rest), praise
# on a grader-confirmed-correct bare answer is justified. The pedagogy
# concern ("but ask for working anyway") is a separate teaching pattern
# handled elsewhere, not a regen-worthy violation. The RULE_RULE_1
# category in `apps/tutoring/rule_compliance.py` + the LLM judge prompt
# schema still exist for backward compat with stored judge outputs;
# the validator just no longer translates them into a validator-level
# issue or surfaces them in validator_issues.
# Tutor referenced a figure ("the diagram", "in the figure") but no
# |||MEDIA:N||| signal was emitted, so the student saw the reference
# without the visual. Soft issue for now — surfaced in [TurnSummary]
# so we can see frequency before deciding whether to escalate to regen.
ISSUE_FIGURE_REF_WITHOUT_SIGNAL = "figure_ref_without_signal"
# Tutor self-contradicted within the same response. Source: coherence
# judge. Production example (Savy Eva, 2026-05-04): "let's explore
# three angles" then immediately posed a TWO-angle question; student
# replied "YOU SAID 3 ANGLES".
ISSUE_TUTOR_INCOHERENT = "tutor_incoherent"
# Tutor attached a figure that doesn't match the question being asked.
# Source: figure_vision judge (LLM vision call). Catches mid-conversation
# figure misalignment that the deterministic figure_ref check can't see.
ISSUE_FIGURE_MISMATCH = "figure_mismatch"
# Tutor's text disagrees with the deterministic verdict. Production
# example (Edward, 2026-05-07): student answered "B" to a 4-option
# MCQ where B was correct; deterministic_mcq returned True; but the
# tutor's text said "That's not quite right. Let me help you understand
# what went wrong." This must trigger regen so the student doesn't
# see "you got it wrong" when they got it right.
ISSUE_VERDICT_MISMATCH = "verdict_mismatch"
# Safety judge flagged the tutor response (HARMFUL or INAPPROPRIATE
# content). Triggers regen so unsafe text never reaches the student.
# Student-side safety findings (HARMFUL / INAPPROPRIATE / MANIPULATION
# from a student message) are NOT routed through this issue — they
# go directly to SessionTurn.is_flagged via the safety judge call
# in apps/tutoring/views.py and surface at /dashboard/flagged/.
ISSUE_TUTOR_UNSAFE = "tutor_unsafe"
# Tutor revealed the canonical answer to a question the student
# hasn't yet resolved. Detected by the W1 answer-leak guard
# (apps/tutoring/answer_leak.py — single LLM judge as of 2026-05-17,
# was deterministic + LLM + arbiter; see
# memory/tutor_state_drift_and_leak_simplification_plan.md). Triggers
# regen with the canonical answer SCRUBBED from the regen context
# so the rewrite can only produce a concept-level hint.
ISSUE_ANSWER_LEAK = "answer_leak"
# Tutor re-asked a question already asked (cross-turn authored
# repeat, or paraphrased re-ask of the active bank Q). Detected by
# the W14 repeated-question guard (apps/tutoring/repeated_question.py
# — Jaccard signature match + LLM judge on borderline). Triggers
# regen with a "rephrase from a different angle or advance" directive.
ISSUE_REPEATED_QUESTION = "repeated_question"
# Tutor posed a NEW question in prose without using pose_question /
# pose_inline_question. The engine has no way to track or grade
# untooled prose questions, which produces stale-awaiting bugs
# (grader graded against the wrong question). Pilot 2026-05-17 task
# #197: re-enabled pose_inline_question with optional answer_key,
# made tools mandatory for new question posing. Detected only when
# `_awaiting_answer is None` (no active Q being scaffolded) AND
# `tool_use_count == 0` AND an MCQ pattern (A) ... B) ... C) ... D))
# is present in the response. Hint-probes inside a hint don't count —
# they're scoped to an active awaiting record.
ISSUE_NO_QUESTION_TOOL = "no_question_tool"
# Literal tool-call markup leaked into the rendered tutor text.
# Observed 2026-05-20 in browser e2e of Gemini Flash family:
#   - "|||tool_call:pose_question{slot: 1}|||"  (3.5 Flash variant)
#   - "tool_use: pose_question(slot=4)"         (Flash Lite Preview)
# The student sees raw protocol markup instead of a real tool call —
# breaks the affordance + confuses the UI. Detected by a single
# regex; triggers regen (the candidate is unsalvageable as text).
ISSUE_TOOL_CALL_LEAK = "tool_call_leak"

# Deictic figure references — phrases that strongly imply "I am
# pointing at a visual right now". Used by the figure-ref-without-signal
# validator check. Tighter than a substring match: requires "the/this/
# that/our + figure-noun" or "shown above/below/here" — won't fire on
# figurative "Picture yourself" / "imagine".
_FIGURE_DEICTIC_RE = re.compile(
    r"\b(?:the|this|that|these|those|our|in the|on the|"
    r"look at the|see the)\s+"
    r"(?:diagram|figure|image|picture|illustration|chart|graph|map)s?\b"
    r"|\bshown\s+(?:above|below|here|in (?:the )?(?:diagram|figure|image))\b",
    re.IGNORECASE,
)


@dataclass
class ValidationResult:
    """Outcome of validating a tutor response."""
    content: str
    issues: List[str] = field(default_factory=list)
    layers_run: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    # Issues considered "soft" — logged for teacher review but don't
    # flip `passed` to False. Numeric_claim_unverified is soft because
    # the LLM judge often defaults to "unverified" when retrieved
    # evidence is sparse, not because the tutor is wrong.
    _SOFT_ISSUES = frozenset({
        ISSUE_NUMERIC_CLAIM_UNVERIFIED,
    })

    # Issues that should trigger regeneration (V3) — strong evidence
    # the tutor is wrong, can't be patched in place.
    #
    # Removed 2026-05-16 per pilot directive (regen rate was too high,
    # destroying tool calls + producing incoherent turns):
    #   - ISSUE_AUTHORING_VIOLATION: the grader now handles
    #     tutor-authored questions (with or without answer key via
    #     pose_inline_question). Tool-driven authoring is legitimate.
    #   - ISSUE_RULE1_VIOLATION: "praise on bare answer" rule. The
    #     grader is the source of truth on correctness; if grader
    #     says correct, "Perfect!" is justified. Was firing on every
    #     warmup turn.
    # Both still appear in validator_issues for analytics; they just
    # no longer trigger the regen ensemble.
    _REGEN_ISSUES = frozenset({
        ISSUE_NUMERIC_CLAIM_CONTRADICTED,
        ISSUE_ARITHMETIC_VIOLATION,
        # "Looking at the diagram…" with no |||MEDIA:N||| emitted —
        # student sees the deictic reference without the visual.
        # Regen with explicit instruction to either signal or rephrase.
        ISSUE_FIGURE_REF_WITHOUT_SIGNAL,
        # Tutor self-contradicted within the same turn — needs a clean
        # rewrite, not a patch.
        ISSUE_TUTOR_INCOHERENT,
        # Attached figure doesn't match the question — regen with
        # instruction to either fix the question or pick a different
        # figure from the catalog.
        ISSUE_FIGURE_MISMATCH,
        # Tutor said "not quite" when the answer was correct (or
        # vice versa) — regen with instruction to align the text
        # with the verdict.
        ISSUE_VERDICT_MISMATCH,
        # Safety judge flagged the tutor response — regen so the
        # student never sees the unsafe text.
        ISSUE_TUTOR_UNSAFE,
        # Tutor leaked the canonical answer before the student earned
        # reveal (wrong_attempts < 3). Regen scrubs canonical from
        # the regen context — see apps/tutoring/regen/prompt.py.
        ISSUE_ANSWER_LEAK,
        # Tutor re-asked a question already in flight or already
        # answered. Regen prompts for a different angle.
        ISSUE_REPEATED_QUESTION,
        # Tutor ended without a question or a call-to-action — the
        # student is left without direction. Pilot directive 2026-05-17:
        # every turn must hand the floor back with an explicit next step.
        ISSUE_NO_QUESTION,
        # Tutor posed a new question in prose without using the
        # pose_question / pose_inline_question tool. Regen instructs
        # the LLM to re-pose via a tool so the engine state is
        # authoritative (task #197).
        ISSUE_NO_QUESTION_TOOL,
        # Tutor typed literal tool-call markup into the response
        # ("|||tool_call:pose_question{...}|||", "tool_use: pose_..."
        # etc.). Surfaced by the Gemini Flash family in browser e2e
        # 2026-05-20. Always regen — markup must never reach UI.
        ISSUE_TOOL_CALL_LEAK,
    })

    @property
    def passed(self) -> bool:
        return all(i in self._SOFT_ISSUES for i in self.issues)

    @property
    def needs_regeneration(self) -> bool:
        return any(i in self._REGEN_ISSUES for i in self.issues)


# A response qualifies as ending-in-question only when there's an
# actual '?' on the tail line. The regex used to accept imperative
# phrases ("let's check", "show me", "walk me through") as questions
# without a '?', which let bland transitions slip through and made
# the validator rubber-stamp the regen-prescribed stock phrases. The
# Socratic approach is valid — but it should always be expressed as
# an actual question.
_QUESTION_RE = re.compile(r"\?")

# Mirrors `_INLINE_MCQ_RE` in apps/tutoring/conversational_tutor.py —
# detects a multi-choice question authored in prose (two consecutive
# lettered options on their own lines). Used by the
# NO_QUESTION_TOOL check to flag clear "LLM typed an MCQ instead of
# calling pose_inline_question" violations.
_PROSE_MCQ_RE = re.compile(
    r'(?m)^\s*A[\.\)]\s+\S.*(?:\r?\n|\r)\s*B[\.\)]\s+\S',
)

# Tool-call markup leak — the LLM types protocol syntax into prose
# instead of emitting a real tool_use block. The student sees the
# raw markup. Observed variants from Gemini Flash family 2026-05-20:
#   - "|||tool_call:pose_question{slot: 1}|||"     (3.5 Flash)
#   - "tool_use: pose_question(slot=4)"            (Flash Lite Preview)
#   - "<tool_use><invoke name='...'>"              (earlier 2026-05-17)
#   - "pose_inline_question(question=...)"         (earlier function form)
# Triggers ISSUE_TOOL_CALL_LEAK + regen. Kept broad — false positives
# here are cheap (regen produces a clean turn) and the alternative
# (shipping raw markup) breaks the UI.
_TOOL_CALL_LEAK_RE = re.compile(
    # Triple-pipe fence form: |||tool_call:NAME{...}||| or |||tool_use:NAME(...)|||
    r'\|{2,}\s*tool[_ ]?(?:call|use|code)\s*:\s*\w+'
    # Prefix-and-call form: "tool_use:" or "tool_call:" followed by a
    # function-style invocation (`pose_question(`, `pose_inline_question(`,
    # generic `name(`).
    r'|\btool[_ ]?(?:call|use|code)\s*:\s*pose_(?:inline_)?question\b'
    # Bare function-call form for our pose tools (matches the
    # self-retry detector pattern). Covers e.g. `pose_question(slot=N)`
    # that escaped the engine's narrower strip regex.
    r'|\bpose_(?:inline_)?question\s*\('
    # XML-tag form (kept for parity with self-retry detector).
    r'|<\s*(?:tool_use|invoke|antml:function_calls|function_calls)\b',
    re.IGNORECASE,
)

# Verdict-mismatch detection (ISSUE_VERDICT_MISMATCH).
# Phrases the tutor uses to tell the student they got it WRONG:
_NEGATIVE_VERDICT_RE = re.compile(
    r"\b(?:not\s+(?:quite|right|correct)|"
    r"that's\s+(?:not\s+(?:quite\s+)?right|incorrect|wrong)|"
    r"that\s+(?:isn't|isn'?t\s+quite)\s+right|"
    r"incorrect|wrong\s+answer|let me help (?:you )?(?:understand|see) what went wrong|"
    r"you got (?:it )?wrong)\b",
    re.IGNORECASE,
)
# Phrases the tutor uses to tell the student they got it RIGHT.
# Kept tight — generic "good" / "nice" don't count as a correctness
# claim; we only flag mismatch when the tutor explicitly says the
# answer is correct.
_POSITIVE_VERDICT_RE = re.compile(
    r"\b(?:exactly\s+(?:right|correct)?|"
    r"that's\s+(?:exactly\s+)?(?:right|correct)|"
    r"correct(?:!)?(?:\s+answer)?|"
    r"you('?ve)?\s+got\s+it|"
    r"perfect(?:!|,)|"
    r"(?:exactly|absolutely|spot\s+on)(?:!|,))",
    re.IGNORECASE,
)


def _ends_with_question(text: str) -> bool:
    if not text:
        return False
    # Look at last sentence-ish chunk.
    tail = text.strip().splitlines()[-1] if "\n" in text else text.strip()
    return bool(_QUESTION_RE.search(tail))


# 2026-05-17 — call-to-action regex + _has_call_to_action helper
# REMOVED. The structural check was brittle (dangling colons +
# multi-sentence questions slipped past). Handoff detection is now
# delegated to the dedicated `handoff` LLM judge in
# apps/tutoring/judges/handoff.py, which runs concurrently with the
# other judges and semantically classifies whether the turn hands
# the floor back to the student. The validator consumes its verdict
# below via combined_result.handed_off.


def validate_tutor_response(
    response: str,
    is_correct: Optional[bool],
    bare_answer: bool,
    step_type: Optional[str] = None,
    lesson=None,
    llm_client=None,
    fact_check: bool = True,
    student_input: Optional[str] = None,
    rule_check: bool = True,
    bank_stems: Optional[List[str]] = None,
    arithmetic_corrections: Optional[List[Dict]] = None,
    bank_signal_used: Optional[bool] = None,
    combined_result=None,
    media_attached: bool = True,
    step_has_media: bool = True,
    tool_use_count: int = 0,
    awaiting_answer_is_set: bool = False,
) -> ValidationResult:
    """Run V1+V2 validator layers over a tutor response.

    Args:
      response: the cleaned tutor reply (after media-signal parsing).
      is_correct: result of the most recent answer evaluation, or None
                  when no evaluation was performed (e.g. teach step,
                  warmup, no expected answer).
      bare_answer: True when the student replied with a naked numeric
                   answer on a practice/quiz step.
      step_type: 'teach' | 'worked_example' | 'practice' | 'quiz' | 'summary'.
      lesson: Lesson model instance — required for L4 fact-check.
      llm_client: BaseLLMClient — required for L4 LLM judge.
      fact_check: when False, skips L4 entirely (used in tests / when
                  callers want fast-path validation).

    Returns:
      ValidationResult with the (possibly modified) content, the list
      of issues encountered, and the layer trace.
    """
    issues: List[str] = []
    layers_run: List[str] = []
    content = response or ""
    # Stash the awaiting_answer state so downstream consumers (regen
    # prompt builder, telemetry) can branch on whether the tutor was
    # mid-question. Anti-smuggle path uses this to repair `no_question`
    # by restating the active question rather than authoring a new one.
    extra_meta: dict = {"awaiting_answer_is_set": bool(awaiting_answer_is_set)}

    # L1 — structural
    layers_run.append("structural")
    # Practice/quiz steps require a literal '?' (the student must be
    # asked the next attempt question). The broader handoff check
    # (does the response actually hand the floor back to the student
    # via question OR clear next-step directive?) is delegated to the
    # `handoff` LLM judge, which runs concurrently in run_all_judges
    # and surfaces via combined_result.handed_off. The validator
    # consumes that verdict below in the combined_result merge.
    # Pilot 2026-05-17 (revised): regex CTA check was brittle —
    # "Now let me ask:" with a dangling colon slipped past. The LLM
    # judge sees the whole turn semantically.
    # Anti-smuggle (2026-05-17, see
    # memory/tutor_state_drift_and_leak_simplification_plan.md): when an
    # answer is already awaiting, the tutor is on a hint/remediation
    # turn for the existing question. The tutor MUST NOT author a new
    # question — it should restate the active one. So the
    # "must-end-with-?" structural check (which was prodding regen to
    # smuggle a new question in) is suppressed in that case. The
    # handoff LLM judge below still catches truly dangling turns.
    if step_type in {"practice", "quiz"} and not awaiting_answer_is_set:
        if not _ends_with_question(content):
            issues.append(ISSUE_NO_QUESTION)

    # Verdict-mismatch — only fires when we have a high-confidence
    # verdict (deterministic numeric / mcq) and the tutor's text
    # contradicts it. Production case (Edward, 2026-05-07): student
    # answered B, deterministic_mcq said correct, tutor said "not
    # quite". Skip when is_correct is None — too risky on an LLM-only
    # verdict to flag mismatch (the LLM judge could be wrong, the
    # tutor could be reasonable).
    eval_layer = (extra_meta.get("eval_layer") or "")
    high_conf_verdict = (
        combined_result is not None
        and getattr(combined_result, "step_eval_source", "").startswith("deterministic")
    )
    if high_conf_verdict and is_correct is True and _NEGATIVE_VERDICT_RE.search(content):
        issues.append(ISSUE_VERDICT_MISMATCH)
        extra_meta["verdict_mismatch_direction"] = "tutor_said_wrong_was_right"
    elif high_conf_verdict and is_correct is False and _POSITIVE_VERDICT_RE.search(content):
        issues.append(ISSUE_VERDICT_MISMATCH)
        extra_meta["verdict_mismatch_direction"] = "tutor_said_right_was_wrong"

    # NO_QUESTION_TOOL — tutor authored a new question in prose without
    # using pose_question / pose_inline_question. Only fires when no
    # active awaiting record (a question already in flight scopes any
    # `?` in the response as a hint/probe, not a new question). Uses
    # the deterministic MCQ-in-prose pattern for high precision —
    # a non-MCQ false-positive on a probing rhetorical `?` would be
    # too costly. Catches the common case (LLM types out A/B/C/D in
    # prose) without overfiring on hint phrases. Task #197.
    if (
        not awaiting_answer_is_set
        and tool_use_count == 0
        and _PROSE_MCQ_RE.search(content)
    ):
        issues.append(ISSUE_NO_QUESTION_TOOL)

    # TOOL_CALL_LEAK — literal protocol markup in the rendered text
    # (e.g. "|||tool_call:pose_question{slot: 1}|||" or
    # "tool_use: pose_question(slot=4)"). Always a defect — the
    # student should never see protocol syntax. Regen.
    if _TOOL_CALL_LEAK_RE.search(content):
        issues.append(ISSUE_TOOL_CALL_LEAK)

    # Figure reference without |||MEDIA:N||| signal: tutor said "the
    # diagram"/"in the figure" but no media was attached for this turn.
    # The student sees a deictic reference to a visual that isn't there
    # ("Looking at the diagram, you can see…" → "where is the diagram?").
    #
    # 2026-05-20: Gated on `step_has_media` after audit found this was
    # the dominant false-positive trigger — 57% of all regens, 144 of
    # those on L540 alone where the lesson is ABOUT maps so "the map"
    # matches the deictic regex constantly but there's no figure for
    # the step. Only fire when the step actually has a figure that
    # COULD have been signaled; otherwise "the map" is a conceptual
    # reference, not a deictic pointer at a visual.
    if step_has_media and not media_attached and _FIGURE_DEICTIC_RE.search(content):
        issues.append(ISSUE_FIGURE_REF_WITHOUT_SIGNAL)

    # L2 — pedagogical praise gate (universal; previously math-only)
    layers_run.append("pedagogical")
    should_strip = False
    if is_correct is False:
        should_strip = True
    elif bare_answer:
        # Bare answers must not be praised regardless of correctness
        # (math_teaching Rule 1 generalized).
        should_strip = True

    if should_strip and _PRAISE_RE.search(content):
        # By the time the validator runs, the engine's deterministic
        # math-check pass has already stripped praise on bare-correct
        # turns. Anything reaching this layer with `is_correct=False`
        # is genuinely wrong; bare answers without any canonical
        # correctness signal use the "bare_unknown" opener.
        praise_context = "wrong" if is_correct is False else "bare_unknown"
        new_content, stripped = strip_praise_if_wrong(
            content,
            is_correct=False,
            context=praise_context,
            student_input=student_input,
        )
        if stripped:
            content = new_content
            issues.append(ISSUE_UNFOUNDED_PRAISE_STRIPPED)

    # L4 + L5 — factual claims and rule compliance.
    #
    # Preferred path: caller already ran apps.tutoring.combined_judge,
    # passes the result via `combined_result`, and we just consume the
    # verdicts. This is what conversational_tutor.py does on math turns
    # — collapses three serial LLM calls (arithmetic + fact + rule) into
    # one.
    #
    # Legacy path (no combined_result): run the two judges separately.
    # Used for non-math callers and tests that haven't migrated.
    if combined_result is not None:
        layers_run.append("fact_check_combined")
        layers_run.append("rule_check_combined")
        extra_meta.update(combined_result.to_metadata())
        if combined_result.contradicted_claims:
            issues.append(ISSUE_NUMERIC_CLAIM_CONTRADICTED)
        if combined_result.unverified_claims:
            issues.append(ISSUE_NUMERIC_CLAIM_UNVERIFIED)
        if combined_result.has_violations:
            from apps.tutoring.rule_compliance import (
                RULE_ARITHMETIC,
                RULE_NO_AUTHORING,
                # RULE_RULE_1 removed 2026-05-17 — see top of file.
            )
            # NO_AUTHORING suppression 2026-05-16: the rule_compliance
            # LLM judge sees the question text in the chat content and
            # flags it as authoring, but the tutor may have used the
            # pose_inline_question tool LEGITIMATELY (the question
            # text comes from the tool input, with an answer_key the
            # grader will use). When bank_signal_used=True we trust
            # the tool path and suppress this rule.
            if (
                RULE_NO_AUTHORING in combined_result.violated_rules
                and not bank_signal_used
            ):
                issues.append(ISSUE_AUTHORING_VIOLATION)
            if RULE_ARITHMETIC in combined_result.violated_rules:
                issues.append(ISSUE_ARITHMETIC_VIOLATION)
            # RULE_RULE_1 ("praise on bare answer") FULLY REMOVED
            # 2026-05-17 — dropped from VALID_RULES + judge prompt +
            # validator + regen handler + tests. Grader is now
            # authoritative on correctness.
        # Coherence judge findings (2026-05-08).
        if getattr(combined_result, "coherence_violations", None):
            issues.append(ISSUE_TUTOR_INCOHERENT)
            extra_meta["coherence_violations"] = list(
                combined_result.coherence_violations
            )
        # Figure-reference judge: tutor said "the diagram" with no
        # figure attached. The existing programmatic `_FIGURE_DEICTIC_RE`
        # check below already raises ISSUE_FIGURE_REF_WITHOUT_SIGNAL —
        # this judge gives a structured list of phrases for the regen
        # prompt. Surface the issues into metadata; the existing
        # ISSUE_FIGURE_REF_WITHOUT_SIGNAL flag continues to drive regen.
        if getattr(combined_result, "figure_ref_issues", None):
            extra_meta["figure_ref_issues"] = list(
                combined_result.figure_ref_issues
            )
            extra_meta["figure_ref_in_question"] = bool(
                getattr(combined_result, "figure_ref_in_question", False)
            )
        # Figure-vision judge: attached figure mismatched the question.
        if getattr(combined_result, "figure_aligned", None) is False:
            issues.append(ISSUE_FIGURE_MISMATCH)
            extra_meta["figure_mismatch_reason"] = (
                combined_result.figure_mismatch_reason
            )
            extra_meta["figure_summary"] = combined_result.figure_summary
        # Safety judge: tutor response flagged for harmful or
        # inappropriate content. Always trigger regen so unsafe text
        # never reaches the student. Categories + reasoning go into
        # metadata so the regen prompt can name what to fix.
        sev = getattr(combined_result, "safety_severity", "safe") or "safe"
        if sev in ("warning", "critical"):
            issues.append(ISSUE_TUTOR_UNSAFE)
            extra_meta["safety_severity"] = sev
            extra_meta["safety_categories"] = list(
                getattr(combined_result, "safety_categories", []) or []
            )
            extra_meta["safety_reasoning"] = (
                getattr(combined_result, "safety_reasoning", "") or ""
            )
        # Answer-leak judge (task #202, 2026-05-17): the detector runs
        # concurrent with the other judges via run_all_judges. When
        # the tutor stated the canonical answer or paraphrased it,
        # answer_leaked=True surfaces here and we raise the same
        # regen-triggering issue the old post-regen branch raised.
        if getattr(combined_result, "answer_leaked", False) is True:
            if ISSUE_ANSWER_LEAK not in issues:
                issues.append(ISSUE_ANSWER_LEAK)
            extra_meta["answer_leak_reason"] = getattr(
                combined_result, "answer_leak_reason", "",
            ) or ""
            extra_meta["answer_leak_sources"] = list(getattr(
                combined_result, "answer_leak_sources", [],
            ) or [])
        # Handoff judge (task #183): the LLM verdict on whether the
        # tutor turn hands the floor back to the student. Catches
        # dangling promises ("Now let me ask:" with no question),
        # pure acknowledgements, and other parting lines that leave
        # the student without direction. Default is handed_off=True
        # (skipped / errored judge doesn't false-positive).
        #
        # Anti-smuggle (2026-05-17): when an answer is already awaiting,
        # the active question above IS the handoff. The handoff judge
        # only sees the turn text and doesn't know about state, so it
        # may flag handed_off=False even when the tutor restated the
        # active question. Suppressing the issue (but still recording
        # the reason in metadata for analysis) prevents the regen loop
        # that drove the smuggle pattern on prod session 265.
        if (
            getattr(combined_result, "handed_off", True) is False
            and not awaiting_answer_is_set
        ):
            if ISSUE_NO_QUESTION not in issues:
                issues.append(ISSUE_NO_QUESTION)
            extra_meta["handoff_reason"] = (
                getattr(combined_result, "handoff_reason", "") or ""
            )
        elif getattr(combined_result, "handed_off", True) is False:
            # Record but don't trigger regen — see comment above.
            extra_meta["handoff_reason_suppressed"] = (
                getattr(combined_result, "handoff_reason", "") or ""
            )
    else:
        if fact_check and lesson is not None and llm_client is not None:
            layers_run.append("fact_check")
            fc: FactCheckResult = verify_response(
                content, lesson=lesson, llm_client=llm_client,
            )
            extra_meta.update(fc.to_metadata())
            if fc.contradicted_claims:
                issues.append(ISSUE_NUMERIC_CLAIM_CONTRADICTED)
            unverified = [c for c in fc.claims if c.status == "unverified"]
            if unverified:
                issues.append(ISSUE_NUMERIC_CLAIM_UNVERIFIED)

        if (
            rule_check
            and lesson is not None
            and llm_client is not None
        ):
            try:
                is_math_lesson = lesson.unit.course.is_math
            except Exception:
                is_math_lesson = False
            if is_math_lesson:
                layers_run.append("rule_check")
                from apps.tutoring.rule_compliance import (
                    RULE_ARITHMETIC,
                    RULE_NO_AUTHORING,
                    # RULE_RULE_1 removed 2026-05-17 — see top of file.
                    check_rule_compliance,
                )
                rc = check_rule_compliance(
                    content,
                    llm_client=llm_client,
                    bank_stems=bank_stems or [],
                    student_input=student_input or "",
                    answer_was_bare=bool(bare_answer),
                    answer_was_wrong=(is_correct is False),
                )
                extra_meta.update(rc.to_metadata())
                if rc.has_violations:
                    # NO_AUTHORING suppressed when bank_signal_used —
                    # see combined_result branch above for rationale.
                    if (
                        RULE_NO_AUTHORING in rc.violated_rules
                        and not bank_signal_used
                    ):
                        issues.append(ISSUE_AUTHORING_VIOLATION)
                    if RULE_ARITHMETIC in rc.violated_rules:
                        issues.append(ISSUE_ARITHMETIC_VIOLATION)

    # L6 — deterministic gates that don't rely on the LLM judge.
    #
    # (a) arithmetic_corrections from the LLM arithmetic verifier:
    #     when the verifier flagged ANY claim as wrong, force regen.
    #     The verifier was previously logging-only; now it's a hard
    #     trigger so bad math never ships even if the rule_compliance
    #     judge is lenient.
    if arithmetic_corrections:
        layers_run.append("arithmetic_corrections")
        issues.append(ISSUE_ARITHMETIC_VIOLATION)
        extra_meta["arithmetic_corrections"] = list(arithmetic_corrections)

    # (b) AUTHORING gate: math + ANY phase + the response contains a
    #     question with specific numerical values + the LLM did NOT
    #     emit a bank pull signal (|||QUESTION:N||| or |||QUESTION_EO:N|||).
    #
    #     Was previously gated on practice/quiz only. Field bug: the
    #     LLM was authoring during engage/warmup phases (where the
    #     gate didn't fire) — paraphrasing later steps' questions
    #     instead of pulling from the bank. Extending to all phases
    #     means the LLM has to use |||QUESTION:N||| to pose a
    #     numerical question regardless of which step it's on.
    #
    #     Conceptual / non-numerical questions ("which rule applies?")
    #     pass _has_numerical_question and are not blocked.
    try:
        is_math_for_authoring = lesson.unit.course.is_math
    except Exception:
        is_math_for_authoring = False
    if (
        is_math_for_authoring
        and bank_signal_used is False
        and _has_numerical_question(content)
    ):
        layers_run.append("authoring_gate")
        if ISSUE_AUTHORING_VIOLATION not in issues:
            issues.append(ISSUE_AUTHORING_VIOLATION)
        extra_meta["authoring_gate_fired"] = True
        extra_meta["authoring_gate_step_type"] = step_type or ""

    return ValidationResult(
        content=content,
        issues=issues,
        layers_run=layers_run,
        metadata={
            "ends_with_question": _ends_with_question(content),
            **extra_meta,
        },
    )


# A "math question signature" — at least one '?' AND at least two
# digit groups (numbers separated by ops, units, words) inside the
# 200 chars before the question mark. Tuned to catch authored
# practice questions ("if angles are 60° and x°, find x?") while
# letting purely conceptual questions ("which rule applies?") through.
_NUMERICAL_QUESTION_RE = re.compile(
    r"(?:\d[\d.]*\s*[°%a-zA-Z]?[\s,].*?){2,}\?",
    re.DOTALL,
)


def _has_numerical_question(text: str) -> bool:
    if not text:
        return False
    return bool(_NUMERICAL_QUESTION_RE.search(text))
