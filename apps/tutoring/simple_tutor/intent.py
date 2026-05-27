"""Deterministic student-message intent classifier.

Added 2026-05-27 per user direction:

    "I think my understanding here is that we need to make the tutor
    more responsive to students. Not all messages from the student
    is an answer. Perhaps we should update the system to first
    identify if a students message is an answer or request or some
    inquiry for explanation."

The simple_tutor used to assume every student message was an answer
attempt whenever ``InFlightQuestion`` was populated — the slot's mere
existence implied GRADE mode. That assumption broke on three real
patterns observed in the phase-5 eval:

  - **Pushback / correction**: capable student says "actually you're
    wrong because…" — slot grader marks 'incorrect', tutor pivots
    away from the substantive correction.
  - **Clarification**: student says "wait, what does X mean?" mid-
    question — same pivot.
  - **Emotional / off-topic**: "i hate this i'm so stupid" — grader
    fails on the placeholder ref, tutor composes a wrong-answer
    response instead of engaging with the distress.

The classifier runs BEFORE the LLM call. Its output:
  - flows into the system prompt as ``<message_intent>X</message_intent>``
  - drives a hint to the LLM about which path to take (GRADE vs
    CONVERSATIONAL)

The LLM still has final say (it can call ``record_answer`` even when
the classifier said 'clarification' — useful for borderline cases).
The classifier just biases the model toward the right path.

Design: regex + heuristics. No LLM call. ~30 µs per classification.
For ambiguous cases the classifier returns ``'answer_or_other'`` and
the LLM decides — matching today's behavior on those cases.
"""
from __future__ import annotations

import re
from typing import Literal

IntentLabel = Literal[
    'answer',
    'clarification',
    'pushback',
    'off_topic',
    'non_engagement',
    'answer_or_other',
]


# Single letter A-D, optionally followed by ")" or "." or trivia.
_LETTER_ONLY = re.compile(r'^\s*[(\[]?\s*([A-Da-d])\s*[)\].:]?\s*$')

# Pure numeric answer (optionally signed / decimal / with simple unit).
_NUMERIC_ONLY = re.compile(
    r'^\s*[(\[]?'
    r'-?\d+(?:[.,]\d+)?'                          # integer or decimal
    r'\s*'
    r'(?:°|degrees?|cm|km|m|%|mm|kg|g|ml|l)?'      # optional unit
    r'\s*[)\].]?\s*$',
    re.IGNORECASE,
)

# Math-shaped answers with explicit work: "60+75+80=215", "x = 360-215 = 145"
_NUMERIC_WITH_WORK = re.compile(
    r'(?i)(=\s*-?\d|^\s*-?\d+\s*[+\-*x/×÷·]\s*-?\d)'
)

# "A) Map 1 and Map 4" — MCQ letter + the option label text.
_LETTERED_WITH_TAIL = re.compile(r'^\s*[(\[]?([A-Da-d])\s*[)\].:]\s+\S+', re.MULTILINE)

# Clarification patterns. The student is asking for explanation,
# definition, or restatement of the question.
_CLARIFICATION_PATTERNS = [
    re.compile(r"(?i)\bwhat\s+(does|is)\s+\w+\s+mean\b"),
    re.compile(r"(?i)\bcan\s+you\s+(explain|repeat|clarify|rephrase)\b"),
    re.compile(r"(?i)\bi\s+(don'?t|do not)\s+(understand|get\s+it|follow)\b"),
    re.compile(r"(?i)\b(what|how)\s+(do you mean|are you asking)\b"),
    re.compile(r"(?i)\bi'?m\s+confused\b"),
    re.compile(r"(?i)\b(help|hint|show\s+me)\b\s*[!?.]?\s*$"),
    re.compile(r"(?i)^\s*(hold on|wait\b|hang on|just a sec)"),
]

# Pushback / correction patterns. Student is contesting the framing
# or asserting a counter-point.
_PUSHBACK_PATTERNS = [
    re.compile(r"(?i)\b(but|however|actually|wait,?)\b.{0,80}\?"),
    re.compile(r"(?i)\b(that'?s|that is)\s+not\s+(right|correct|true|quite)\b"),
    re.compile(r"(?i)\b(no|nope),?\s+(it'?s|it is|you|that)\b"),
    re.compile(r"(?i)\bi\s+(think|believe)\s+(you|that|it)\s+\w+\s+(wrong|mistake|incorrect)\b"),
    # "what if all six are equal AND one turns out to be reflex?"
    re.compile(r"(?i)\bwhat\s+if\b.{0,100}\?"),
    re.compile(r"(?i)\bwell\b,?\s+(actually|technically)\b"),
]

# Disengagement / emotional / non-answer fillers.
_NON_ENGAGEMENT_PATTERNS = [
    re.compile(r"(?i)^\s*(idk|i don'?t know|dunno|i'?m lost|no idea)\s*[.!]?\s*$"),
    re.compile(r"(?i)^\s*(thanks|thank you|cheers|ok|okay|sure|yes|no)\s*[.!]?\s*$"),
    re.compile(r"(?i)^\s*(skip|next|pass|nope)\s*[.!]?\s*$"),
    # Thanks/gratitude at the head of the message — covers "thanks for
    # being patient" / "thank you so much" / "cheers, that helps".
    re.compile(r"(?i)^\s*(thanks|thank\s+you|cheers)\b"),
    # Emotional outbursts.
    re.compile(r"(?i)\bi\s+(hate|love)\s+(this|maths?|geography)\b"),
    re.compile(r"(?i)\bi'?m\s+(so\s+)?(stupid|dumb|terrible|bad)\b"),
    re.compile(r"(?i)\b(give\s+up|gave\s+up|done\s+with\s+this)\b"),
]

# Off-topic markers — explicit non-curriculum references.
_OFF_TOPIC_PATTERNS = [
    re.compile(r"(?i)\b(football|soccer|basketball|tiktok|instagram|youtube|netflix|game|gaming)\b"),
    re.compile(r"(?i)\b(my (friend|brother|sister|mom|dad))\b"),
    re.compile(r"(?i)\bwhat\s+time\s+(is|does)\b"),
]


def classify_student_message(
    text: str,
    *,
    has_inflight_question: bool,
) -> IntentLabel:
    """Classify the student's last message.

    Order of checks matters — most-specific patterns first. A clean
    numeric/letter answer beats a clarification hit; a clarification
    beats a pushback (the "wait, what if…?" case); pushback beats
    non-engagement.

    Args:
        text: the student's message text.
        has_inflight_question: True when an InFlightQuestion row
            exists for the session. When False, the classifier
            collapses 'answer' to 'answer_or_other' since there's
            nothing to answer.

    Returns:
        IntentLabel — one of 'answer', 'clarification', 'pushback',
        'off_topic', 'non_engagement', 'answer_or_other'.
    """
    s = (text or '').strip()
    if not s:
        return 'non_engagement'

    # 1. Clear answer shape — single letter / pure number / numeric+work.
    if has_inflight_question:
        if _LETTER_ONLY.match(s) or _LETTERED_WITH_TAIL.match(s):
            return 'answer'
        if _NUMERIC_ONLY.match(s) or _NUMERIC_WITH_WORK.search(s):
            return 'answer'
        # Short non-question textual fragment without pushback / clarification
        # markers — likely a short_answer attempt.
        if len(s) < 80 and not s.rstrip().endswith('?'):
            # Drop down to the more specific checks before deciding.
            pass

    # 2. Clarification — explicit ask for explanation.
    for p in _CLARIFICATION_PATTERNS:
        if p.search(s):
            return 'clarification'

    # 3. Pushback — explicit correction or counter-question. Order
    # matters: "what if all six are equal..." is pushback even though
    # it ends with "?" (would otherwise trip the "?" check).
    for p in _PUSHBACK_PATTERNS:
        if p.search(s):
            return 'pushback'

    # 4. Off-topic.
    for p in _OFF_TOPIC_PATTERNS:
        if p.search(s):
            return 'off_topic'

    # 5. Non-engagement / emotional / short filler.
    for p in _NON_ENGAGEMENT_PATTERNS:
        if p.search(s):
            return 'non_engagement'

    # 6. Long student message ending with "?" — clarification by default
    # unless we have an in-flight question and it looks short-answer-ish.
    if s.rstrip().endswith('?'):
        if has_inflight_question and len(s) < 80:
            return 'answer_or_other'
        return 'clarification'

    # 7. Fallback — defer to the LLM.
    return 'answer_or_other'
