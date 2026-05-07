"""Figure-reference judge — flags tutor responses that reference a
figure / diagram / image when no figure was actually attached this turn.

Mostly DETERMINISTIC: regex-detect figure-reference phrases in the
tutor response, then check whether the engine emitted a media signal
this turn (via `attached_media_count`). No LLM call needed in the
common case.

Production transcripts showed the tutor saying "Looking at the
diagram, you can see…" with no diagram attached — frequent and
confusing for students.

Note: an existing programmatic check in apps/tutoring/validator.py
(ISSUE_FIGURE_REF_WITHOUT_SIGNAL) already raises this as a SOFT
issue. This judge produces structured findings the orchestrator can
surface so the validator can upgrade it to a HARD (regen-triggering)
issue when a question depends on the missing figure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class FigureRefResult:
    # Each issue is a short string naming what's wrong.
    # Example: "tutor said 'looking at the diagram' but no figure was
    # attached this turn"
    issues: List[str] = field(default_factory=list)
    # Whether any of the references appear in a question (vs purely
    # narrative). Question-context references are more critical because
    # the student literally cannot answer without seeing the figure.
    in_question: bool = False
    skipped: bool = False
    skip_reason: str = ""


# Phrases that count as figure references.
_FIGURE_PHRASES = (
    "looking at the diagram",
    "looking at the figure",
    "looking at the image",
    "see in the diagram",
    "see in the figure",
    "see in the image",
    "in the diagram",
    "in the figure",
    "in the image",
    "from the diagram",
    "from the figure",
    "shown in the diagram",
    "shown in the figure",
    "shown in the image",
    "as shown",
    "as the diagram shows",
    "the diagram above",
    "the figure above",
    "the image above",
    "the diagram below",
    "the figure below",
    "this diagram",
    "this figure",
    "the diagram",
    "the figure",
)
_FIGURE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _FIGURE_PHRASES) + r")\b",
    re.IGNORECASE,
)


def _is_in_question_context(response_text: str, match_pos: int) -> bool:
    """Is the figure-reference embedded in a question? Heuristic: the
    sentence containing the match ends with `?` or fill-in markers
    (`___°.`, `___.`)."""
    end = response_text.find(".", match_pos)
    end_q = response_text.find("?", match_pos)
    if end_q != -1 and (end == -1 or end_q < end):
        return True
    # Look for fill-in markers within the next ~120 chars
    window = response_text[match_pos : match_pos + 200]
    if "___°" in window or "___ " in window or window.rstrip().endswith("___"):
        return True
    return False


def run_figure_ref_judge(
    response_text: str,
    *,
    attached_media_count: int = 0,
) -> FigureRefResult:
    """Deterministic figure-reference check.

    Args:
      response_text: the (cleaned, post media-signal parsing) tutor reply
      attached_media_count: number of media items the engine attached
        to THIS turn (computed from the parsed media signal). 0 means
        no figure was emitted this turn.
    """
    result = FigureRefResult()
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result

    if attached_media_count > 0:
        # A figure WAS attached this turn — references are legitimate.
        # (The figure_vision judge handles whether the figure matches.)
        result.skipped = True
        result.skip_reason = "figure_attached"
        return result

    matches = list(_FIGURE_RE.finditer(response_text))
    if not matches:
        result.skipped = True
        result.skip_reason = "no_figure_reference"
        return result

    # Collect distinct phrases (cap at 3 to keep the issue list short).
    seen = set()
    for m in matches:
        phrase = m.group(0).lower()
        if phrase in seen:
            continue
        seen.add(phrase)
        if _is_in_question_context(response_text, m.start()):
            result.in_question = True
            result.issues.append(
                f"tutor said '{phrase}' inside a question, but no "
                f"figure was attached this turn"
            )
        else:
            result.issues.append(
                f"tutor said '{phrase}' but no figure was attached "
                f"this turn"
            )
        if len(result.issues) >= 3:
            break
    return result
