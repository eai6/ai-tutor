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


def _question_sentences(text: str) -> list[str]:
    """Return every '?'-ending sentence in ``text``, in order.

    Sentences are split on '.', '!', '?', or newline boundaries.
    Each returned sentence is the substring from the prior boundary
    (exclusive) through the '?' itself (inclusive), stripped of
    surrounding whitespace. Sentences ending in '.' or '!' are
    skipped. Empty list if ``text`` contains no '?'.
    """
    s = (text or "").strip()
    if not s or "?" not in s:
        return []
    out: list[str] = []
    start = 0
    for i, ch in enumerate(s):
        if ch in ".!\n":
            start = i + 1
            continue
        if ch == "?":
            sent = s[start:i + 1].strip()
            if sent:
                out.append(sent)
            start = i + 1
    return out


def _classify_question(sentence: str) -> str:
    """Return ``verifiable``, ``reflective``, or ``unclassified``.

    Reflective patterns are checked first — when both pattern groups
    would match, the reflective intent wins (a closed-set 'which'
    embedded inside a 'matches your intuition' framing is still
    conversational, not an assessment).
    """
    if not sentence:
        return "unclassified"
    lower = sentence.lower()
    for pat in _REFLECTIVE_PATTERNS:
        if pat.search(lower):
            return "reflective"
    for pat in _VERIFIABLE_PATTERNS:
        if pat.search(lower):
            return "verifiable"
    return "unclassified"


def is_verifiable_prose_question(response_text: str) -> bool:
    """True if the response's trailing question has a closed-set canonical.

    Used by the curriculum-fidelity gate (no-tool path) — when no
    ``pose_question`` tool call fired this turn, the trailing
    question is the LLM's authored assessment. A verifiable trailing
    Q is a curriculum violation per
    ``memory/curriculum_fidelity_principle.md``.

    Pipeline:
      1. If the response does not end with '?', return False.
      2. Extract the trailing question sentence.
      3. Classify reflective / verifiable / unclassified.
      4. Reflective → False. Verifiable → True. Unclassified → True
         (precision-favoring — block when uncertain).
    """
    text = (response_text or "").strip()
    if not text.endswith("?"):
        return False
    trailing = _last_question_sentence(text)
    if not trailing:
        return False
    classification = _classify_question(trailing)
    if classification == "reflective":
        return False
    if classification == "verifiable":
        return True
    # Precision-favoring default for the trailing-only / no-tool path.
    return True


def find_verifiable_prose_questions(text: str) -> list[str]:
    """Return every explicitly-verifiable '?' sentence in ``text``.

    One question per turn is the curriculum-fidelity rule
    (``memory/curriculum_fidelity_principle.md``). When the gate
    fires, the recovery loop needs to surface ALL offending sentences
    so the retry reminder names them all and the degrade pass can
    strip them all in one go — single-match detection lets later
    violations slip past a one-attempt retry budget.

    Scans every '?'-ending sentence in order. ``unclassified``
    sentences are NOT treated as verifiable here (precision-favoring
    becomes recall-degrading on a multi-sentence scan — many
    rhetorical / conversational prose Qs would false-positive). The
    no-tool trailing-only check in ``is_verifiable_prose_question``
    handles the precision-favoring fallback for the unclassified
    trailing case.
    """
    if not text or "?" not in text:
        return []
    return [
        sent
        for sent in _question_sentences(text)
        if _classify_question(sent) == "verifiable"
    ]


def strip_trailing_tool_stem(response_text: str, tool_stem: str) -> str:
    """Return ``response_text`` with the appended tool stem removed.

    The engine assembles every tool-posed response as
    ``f"{lead_in}\\n\\n{rendered_stem}"`` in
    ``student_tutor._assemble_response_text``. This helper undoes that
    join so the gate can scan only the LLM-authored prose.

    Returns ``response_text`` unchanged when:
      - ``tool_stem`` is empty or whitespace, OR
      - the stem doesn't appear at the end of ``response_text``
        (e.g. the engine altered the assembly format).
    """
    text = (response_text or "").rstrip()
    stem = (tool_stem or "").strip()
    if not stem:
        return text
    # The stem might have been altered (e.g. legacy "True or False" prefix
    # vs. new "(True or False?)" suffix). Match the longest contiguous
    # suffix that aligns with the stem's trailing portion.
    if text.endswith(stem):
        return text[: -len(stem)].rstrip()
    # Fallback: try matching on the stem's first line — bank stems often
    # span multiple lines (MCQ options), and the stem-as-stored may have
    # different whitespace than the assembled response.
    first_line = stem.split("\n", 1)[0].strip()
    if first_line and first_line in text:
        idx = text.rfind(first_line)
        return text[:idx].rstrip()
    return text
