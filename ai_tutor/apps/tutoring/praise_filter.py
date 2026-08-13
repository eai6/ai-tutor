"""Post-generation filter that strips praise words from a tutor response
when the deterministic math check determined the student's answer was
incorrect.

This is layer 3 of the math-tutor false-positive fix
(see memory/math_tutor_fix_plan.md). The system-prompt signal injection
(layer 2) should already prevent the LLM from praising a wrong answer,
but this filter is defense-in-depth for cases where the LLM defies the
prompt.

Only the FIRST sentence of the response is scanned. Later mentions of
"correct" or similar words are left alone so pedagogical phrases like
"the correct approach" or "let's figure out the correct next step" are
preserved. Praise almost always lands in the opening.
"""

import re
from typing import Optional, Tuple


# Praise patterns limited to "affirmation" uses. Avoid words like plain
# "correct" that commonly appear outside affirmations; prefer patterns
# that are clearly affirmative (e.g. "correct!" with an exclamation,
# "that's correct", etc.).
_PRAISE_PATTERNS = [
    r"\bbrilliant\b",
    r"\bperfect\b",
    r"\bexactly\b",
    r"\bexcellent\b",
    r"\bamazing\b",
    r"\bfantastic\b",
    r"\bwonderful\b",
    r"\bgreat job\b",
    r"\bnice job\b",
    r"\bgood job\b",
    r"\bgreat work\b",
    r"\bnice work\b",
    r"\bgood work\b",
    r"\bwell done\b",
    r"\bnicely done\b",
    r"\byou(?:'?ve| have)?\s+got\s+it\b",
    r"\byou got it\b",
    r"\byou(?:'?ve| have)?\s+nailed\s+it\b",
    r"\byou(?:'?re| are)\s+right\b",
    r"\bthat(?:'?s| is)\s+right\b",
    r"\bthat(?:'?s| is)\s+correct\b",
    r"\bthat(?:'?s| is)\s+it\b",
    r"\bspot on\b",
    r"\bbravo\b",
    r"\bwoo+hoo+\b",
    # Affirmative checkmarks. Catch both the literal U+2713/U+2717 and
    # common ASCII renderings like " ✓" / " ✗" the LLM uses to signal
    # "this is correct" inline. Strip them too — they leak past the
    # text-only patterns and otherwise mark wrong arithmetic as right.
    r"✓",
    r"✗",
    # Sentence-starters followed by exclamation/comma
    r"^\s*correct[!,.]",
    r"^\s*right[!,.]",
    r"^\s*yes[!,.]",
    r"^\s*indeed[!,.]",
]

_PRAISE_RE = re.compile("|".join(_PRAISE_PATTERNS), re.IGNORECASE | re.MULTILINE)

# Default fallback: kept stable for tests and any caller that doesn't
# pass a context. The "Let's check this one together — can you walk me
# through your steps?" phrasing was removed 2026-05-06 — pilot
# transcripts showed it leaking turn-after-turn even on correct
# answers because Sonnet recognised it as a stock pedagogical opener.
_NEUTRAL_OPENER = (
    "What was your first move?"
)


# Pools rotated by `rotate_index` to stop the same opener landing turn
# after turn (pilot transcripts showed "Show me your working, step by
# step" repeating across multiple turns and feeling robotic). The
# openers are still neutral on correctness — they ask for working
# without confirming or denying.
#
# Forbidden phrases (do NOT add back without sign-off):
#   "Let's check this one together — can you walk me through your steps?"
#   "Walk me through your steps"
#   "Show me your working, step by step"
# These three were the most-leaked stock openers in Martin's pilot.
_WRONG_OPENERS = [
    "What was your first move on this?",
    "Hmm, let's slow down. Where did you start?",
    "Before I weigh in, show me your working.",
    "Tell me how you set it up.",
]

_BARE_UNKNOWN_OPENERS = [
    "How did you get there?",
    "What did you do first?",
    "Show me your working before I confirm anything.",
    "What's the working behind that?",
]

# bare_correct gets a templated opener (echoes student input) plus a
# tail. The tail rotates so it doesn't read like a stuck record.
_BARE_CORRECT_TAILS = [
    "How did you get there?",
    "Show me your working and I'll confirm.",
    "What was your first move?",
    "What's the working behind that?",
]


def _opener_for_context(
    context: Optional[str],
    student_input: Optional[str] = None,
    rotate_index: int = 0,
) -> str:
    """Pick the right neutral opener for the situation.

    The single hardcoded opener was producing the contradictory pattern
    visible in production transcripts: praise filter strips "Perfect!",
    swaps in "Let's check this together — can you walk me through your
    steps?", and the rest of the response then says "✓ correct, now do
    the next step". The student sees a tutor that asked for working
    *after* it already affirmed the answer.

    `rotate_index` lets the caller rotate through opener variations to
    stop the same line repeating turn-after-turn (pilot UX feedback).
    Typical caller passes the bare-answer counter for the current step.

    Three contexts:
      - "wrong"          — student's answer is wrong. Asks them to walk
                           through their working.
      - "bare_correct"   — student gave a bare numeric answer that
                           happens to match. Per math_teaching Rule 1
                           we still must not praise; ask for working
                           but echo the answer so the tutor doesn't
                           sound like it ignored what they said.
      - "bare_unknown"   — student gave a bare numeric answer with no
                           expected-answer to check against. Ask for
                           working without implying anything about
                           correctness.
    """
    idx = max(0, int(rotate_index or 0))
    if context == "bare_correct":
        echo = (student_input or "").strip()
        tail = _BARE_CORRECT_TAILS[idx % len(_BARE_CORRECT_TAILS)]
        if echo:
            # Truncate to keep the opener tight — long pasted answers
            # (e.g. "1/2 + 1/3 = 5/6") fit, full essays do not.
            echo = echo if len(echo) <= 60 else echo[:60].rstrip() + "…"
            return f"I see you wrote {echo}. {tail}"
        return tail
    if context == "bare_unknown":
        return _BARE_UNKNOWN_OPENERS[idx % len(_BARE_UNKNOWN_OPENERS)]
    # Default / "wrong"
    return _WRONG_OPENERS[idx % len(_WRONG_OPENERS)]


def _split_first_sentence(text: str) -> Tuple[str, str]:
    """Split text into (first_sentence, rest). Preserves the separator
    inside first_sentence.

    Heuristic: first `.`, `!`, or `?` followed by whitespace or EOS.
    Avoids splitting on decimals/ellipses by requiring a following space
    or end-of-string.
    """
    if not text:
        return "", ""
    m = re.search(r"[.!?](?:\s|$)", text)
    if not m:
        return text, ""
    end = m.end()
    return text[:end].rstrip(), text[end:].lstrip()


def strip_praise_if_wrong(
    response_text: str,
    is_correct: bool,
    context: Optional[str] = None,
    student_input: Optional[str] = None,
    rotate_index: int = 0,
) -> Tuple[str, bool]:
    """Disabled 2026-05-06 per user direction. Returns input unchanged.

    History: the post-process praise stripper was injecting stock
    opener phrases ("Let's check this one together…", "What was your
    first move on this?") that Sonnet then echoed turn-after-turn.
    Each new opener pool became the next leak. Cleaner architecture:
    handle praise-on-bare upstream (combined_judge RULE_1 → regen on
    math turns; non-math praise is allowed). The function signature
    is kept for backward compatibility with all existing call sites.
    """
    return response_text, False


def _tidy(text: str) -> str:
    """Clean up whitespace + punctuation artifacts from regex stripping."""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[,!?.\s]+", "", text)
    # Close up " ," -> "," and remove orphan leading commas after strip.
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    # Collapse repeated terminal punctuation: "!!" -> "!", ".." -> "."
    text = re.sub(r"([!?.,])\1+", r"\1", text)
    # Strip dangling leading commas/dashes like ", — " or "— "
    text = re.sub(r"^[\s,—–-]+", "", text)
    return text.strip()
