"""Curriculum-fidelity check: detect verifiable questions in tutor prose.

Per ``memory/curriculum_fidelity_principle.md``, all assessable questions
must go through the ``pose_question`` tool against bank-authored
``LessonStep`` rows. The tutor must never author verifiable questions in
prose (the EXPLAIN opener failure mode that produced the Map Scale
preview regression on 2026-05-28). This module owns the detection:
given a tutor response's text, classify whether it ends with a
verifiable (closed-set canonical) question.

The detector is precision-favoring: false positives that block
legitimate reflective questions are acceptable because the gate's
retry mechanism allows recovery; false negatives that let through
assessable prose questions corrupt the assessment chain and must be
avoided. Default verdict when the trailing question cannot be
classified is ``True`` (block).

Consumed by ``safety_gates.run_curriculum_fidelity_check``.
"""

from __future__ import annotations

import re


# Reflective patterns — when the trailing question matches one of these,
# it's a legitimate conversational scaffold (no single canonical answer).
# Return False from the detector (do not block).
_REFLECTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Possessive subjective state ("your intuition", "your guess", ...)
    re.compile(
        r"\byour\s+(intuition|view|guess|hunch|sense|feeling|impression|"
        r"first\s+thought|first\s+guess|starting|opinion|experience|"
        r"reasoning|gut|take)\b"
    ),
    # Direct "do you think / feel / believe / ..."
    re.compile(
        r"\b(do|did)\s+you\s+(think|feel|believe|imagine|suppose|wonder|"
        r"reckon|expect|recall|remember)\b"
    ),
    # Experiential "have you seen / noticed / experienced ..."
    re.compile(
        r"\bhave\s+you\s+(seen|noticed|experienced|come\s+across|tried|"
        r"heard|encountered|met|observed)\b"
    ),
    # Subjunctive "what would you / how might you / where could you ..."
    re.compile(
        r"\b(what|where|how|which|when|why)\s+"
        r"(would|do|might|could|should|can)\s+you\b"
    ),
    # "feels clearest / familiar / right / tricky / natural / obvious"
    re.compile(
        r"\bfeels?\s+(most\s+)?(clearest|familiar|right|tricky|natural|"
        r"obvious|hardest|easiest|surprising|new)\b"
    ),
    # "matches your X"
    re.compile(r"\bmatches?\s+your\b"),
    # "for you" / "to you" (asking the student's relation to something)
    re.compile(r"\b(for|to)\s+you\b"),
    # "tell me what you ... / share what you ..."
    re.compile(r"\b(tell|share|describe|explain)\s+me\s+what\s+you\b"),
    # "what do(es) that bring to mind"
    re.compile(r"\bbring\s+to\s+mind\b"),
    # "in your view / opinion / words / experience"
    re.compile(
        r"\bin\s+your\s+(view|opinion|words|experience|world|"
        r"understanding)\b"
    ),
    # "what you already know / what you remember"
    re.compile(r"\bwhat\s+you\s+(already\s+)?(know|remember|recall)\b"),
    # "what comes to mind"
    re.compile(r"\bcomes?\s+to\s+mind\b"),
    # "where (have/do) you (start|begin)" — open-ended starting point
    re.compile(r"\bwhere\s+(do|would|might|could|have)\s+you\b"),
)


# Verifiable patterns — when the trailing question matches one of these,
# it has a closed-set canonical answer derivable from the lesson content.
# Return True from the detector (block).
_VERIFIABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Disjunction with hyphenated named options ("large-scale or small-scale")
    re.compile(r"\b\w+(?:-\w+)+\s+or\s+\w+(?:-\w+)+\b"),
    # Disjunction with two named single-word options inside a "which" Q
    # ("which is larger, X or Y?", "is the answer X or Y?")
    re.compile(
        r"\b(which|is\s+(the|it)|or)\b[^.?!]*\b\w{3,}\s+or\s+\w{3,}\b"
    ),
    # True/false framing
    re.compile(r"\btrue\s+or\s+false\b"),
    re.compile(r"\bt\s*/\s*f\b"),
    # MCQ option listing inside the trailing block
    re.compile(r"\b[A-D]\)\s+\S+"),
    # Compute-value framing with a digit nearby
    re.compile(r"\bwhat\s+(is|are|equals|does)\b[^.?!]*\d"),
    re.compile(r"\bwhat\s+do(es)?\s+\d"),
    # "what is the value / answer / result / ..."
    re.compile(
        r"\bwhat\s+(is|are)\s+the\s+"
        r"(value|answer|result|outcome|product|sum|difference|"
        r"quotient|remainder|next\s+term|missing|total|number)\b"
    ),
    # Imperative compute / solve verbs
    re.compile(
        r"\b(solve|find|calculate|compute|simplify|evaluate|work\s+out)\b"
    ),
    # Ordered-sequence framing ("rank from", "put in order", "arrange by")
    re.compile(
        r"\b(rank|order|arrange|put|list|sort)\b[^.?!]*"
        r"\b(from|in\s+order|by|smallest|largest|first|last|highest|lowest)\b"
    ),
    # Closed-set discriminator ("which (of these) is X")
    re.compile(
        r"\bwhich\s+(of\s+(these|the\s+following)\s+)?"
        r"(is|are|shows|gives|comes|equals|best|correctly|matches|"
        r"contains|represents|describes|explains)\b"
    ),
    # "which X — Y or Z" pattern (the Map Scale shape)
    re.compile(r"\bwhich\b.*\b—\s*\w+"),
    # Yes/no with a verifiable predicate
    re.compile(
        r"\bis\s+\w+\s+"
        r"(true|false|larger|smaller|greater|less|equal|the\s+same|"
        r"correct|right|wrong|positive|negative)\b"
    ),
    # "does X equal Y" / "do these match"
    re.compile(r"\b(does|do)\s+\w+\s+(equal|match|equate)\b"),
    # Direct numeric question ending — "X + Y = ?", "what's 5+3?"
    re.compile(r"\d\s*[+\-*/×÷]\s*\d"),
    # "how many / how much" with a digit nearby (counting questions)
    re.compile(r"\bhow\s+(many|much)\b[^.?!]*\d"),
    # "name the X" / "what's the name of"
    re.compile(r"\bname\s+(the|a|one|all)\b"),
    re.compile(r"\bwhat'?s\s+the\s+name\b"),
)


def _last_question_sentence(text: str) -> str:
    """Extract the last sentence ending with '?' from ``text``.

    Walks back from the end of the text to find the most recent '?'.
    Returns the sentence containing that '?'. Empty string if no '?'
    is found at the end of the text (after stripping whitespace).
    """
    text = (text or "").strip()
    if not text.endswith("?"):
        return ""
    last_q = len(text) - 1
    # Walk back to find the previous sentence boundary.
    # A boundary is '.', '!', '?', or a newline. We allow internal '?'
    # inside the trailing sentence only if there is no sentence
    # boundary between the previous '?' and the trailing '?' — i.e.
    # the trailing sentence may itself be the question.
    start = 0
    for i in range(last_q - 1, -1, -1):
        ch = text[i]
        if ch in ".!?\n":
            start = i + 1
            break
    return text[start:last_q + 1].strip()


def is_verifiable_prose_question(response_text: str) -> bool:
    """True if the response's trailing question has a closed-set canonical.

    Used by the curriculum-fidelity gate to detect when a tutor move
    ended with an assessable question typed in prose (a curriculum
    violation per ``memory/curriculum_fidelity_principle.md``).

    Pipeline:
      1. If the response does not end with '?', return False.
      2. Extract the trailing question sentence.
      3. If it matches a reflective pattern, return False.
      4. If it matches a verifiable pattern, return True.
      5. Default: True (precision-favoring — block when uncertain).

    The gate combines this detector with ``posed_via_tool`` to decide
    whether to actually block. A tool-posed question whose stem also
    matches here is allowed — the gate skips when the tool fired.
    """
    text = (response_text or "").strip()
    if not text.endswith("?"):
        return False

    trailing = _last_question_sentence(text)
    if not trailing:
        return False

    lower = trailing.lower()

    # Reflective signals override — return False
    for pat in _REFLECTIVE_PATTERNS:
        if pat.search(lower):
            return False

    # Verifiable signals — return True
    for pat in _VERIFIABLE_PATTERNS:
        if pat.search(lower):
            return True

    # Precision-favoring default: when the classifier cannot place the
    # trailing question into either bucket, treat it as verifiable and
    # block. The retry mechanism in the gate gives the LLM one more
    # attempt to author a reflective alternative or call the
    # pose_question tool.
    return True
