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
from typing import Tuple


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
    r"\bwell done\b",
    r"\bnicely done\b",
    r"\byou(?:'?ve| have)?\s+got\s+it\b",
    r"\byou got it\b",
    r"\byou(?:'?re| are)\s+right\b",
    r"\bthat(?:'?s| is)\s+right\b",
    r"\bthat(?:'?s| is)\s+correct\b",
    r"\bthat(?:'?s| is)\s+it\b",
    r"\bspot on\b",
    r"\bbravo\b",
    r"\bwoo+hoo+\b",
    # Sentence-starters followed by exclamation/comma
    r"^\s*correct[!,.]",
    r"^\s*right[!,.]",
    r"^\s*yes[!,.]",
    r"^\s*indeed[!,.]",
]

_PRAISE_RE = re.compile("|".join(_PRAISE_PATTERNS), re.IGNORECASE | re.MULTILINE)

_NEUTRAL_OPENER = (
    "Let's check this one together — can you walk me through your steps?"
)


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
) -> Tuple[str, bool]:
    """Return (possibly-rewritten text, was_modified).

    Only acts when is_correct is explicitly False. When is_correct is True
    or None (unknown), returns the input unchanged.

    Strategy:
      1. Strip praise patterns across the ENTIRE response text. Praise is
         never appropriate when we know the student was wrong, and the LLM
         sometimes sprinkles it across multiple sentences ("Brilliant!
         You've got it — ...").
      2. If the first sentence was heavy in praise (> 2 hits) or nearly
         entirely praise, replace it with a neutral opener so the output
         doesn't read as a mangled fragment.
      3. Tidy whitespace and punctuation artifacts from the stripping.
    """
    if is_correct is True or is_correct is None:
        return response_text, False
    if not response_text:
        return response_text, False

    first_orig, rest_orig = _split_first_sentence(response_text)
    first_hits = _PRAISE_RE.findall(first_orig)
    rest_hits = _PRAISE_RE.findall(rest_orig) if rest_orig else []

    if not first_hits and not rest_hits:
        return response_text, False

    heavy_praise = len(first_hits) > 2 or (
        len(first_hits) >= 1 and len(first_orig.strip()) < 40
    )

    if heavy_praise:
        # Replace the mangled opening with a neutral opener and strip any
        # stray praise from the rest (preserves original rest content).
        rest_stripped = _tidy(_PRAISE_RE.sub("", rest_orig)) if rest_orig else ""
        cleaned = _NEUTRAL_OPENER + (" " + rest_stripped if rest_stripped else "")
    else:
        # Light strip across the entire text.
        cleaned = _tidy(_PRAISE_RE.sub("", response_text))

    # Re-capitalize first letter.
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]

    return cleaned.strip(), True


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
