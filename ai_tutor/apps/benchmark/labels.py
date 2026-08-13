"""Label vocabulary for the tutor evaluation benchmark.

Single source of truth for the 30 labels + 19 failure categories defined in
``memory/eval_benchmark_v2_simplified.md``. Imported by models, admin
views, and the (forthcoming) auto-population + LLM-judge code.

The schema is intentionally **flat** — actions and issues coexist in one
``labels`` list on annotations. The ``ACTION_LABELS`` / ``ISSUE_LABELS``
constants below are presentation metadata (used to render labels in the
right column in the admin UI) and validation (issue labels should never
appear in ``expected_labels``).
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Action labels — what the response is doing (6)
# ---------------------------------------------------------------------------

ADVANCE = 'ADVANCE'
ASK_WORKING = 'ASK_WORKING'
PROBE = 'PROBE'
EXPLAIN = 'EXPLAIN'
SURFACE_ERROR = 'SURFACE_ERROR'
OTHER = 'OTHER'

ACTION_LABELS = (
    ADVANCE,
    ASK_WORKING,
    PROBE,
    EXPLAIN,
    SURFACE_ERROR,
    OTHER,
)

# ---------------------------------------------------------------------------
# Issue labels (24) — grouped by source so the admin UI can render sections
# ---------------------------------------------------------------------------

# From rule judge (apps/tutoring/judges/rule.py)
AUTHORED_QUESTION = 'AUTHORED_QUESTION'
UNFOUNDED_PRAISE = 'UNFOUNDED_PRAISE'

# From arithmetic judge
ARITHMETIC_ERROR = 'ARITHMETIC_ERROR'

# From factual judge
CLAIM_CONTRADICTED = 'CLAIM_CONTRADICTED'
CLAIM_UNVERIFIED = 'CLAIM_UNVERIFIED'

# From coherence judge
INCOHERENT = 'INCOHERENT'

# From figure judges
FIGURE_REF_UNATTACHED = 'FIGURE_REF_UNATTACHED'
FIGURE_MISMATCH = 'FIGURE_MISMATCH'

# From safety judge
SAFETY_HARMFUL = 'SAFETY_HARMFUL'
SAFETY_INAPPROPRIATE = 'SAFETY_INAPPROPRIATE'

# From validator (structural / format)
NO_QUESTION = 'NO_QUESTION'
INFO_DUMP = 'INFO_DUMP'
MULTI_PARAGRAPH = 'MULTI_PARAGRAPH'
BANNED_OPENER = 'BANNED_OPENER'
PADDING_FILLER = 'PADDING_FILLER'

# From validator / step eval (semantic)
VERDICT_MISMATCH = 'VERDICT_MISMATCH'
WRONG_VERDICT = 'WRONG_VERDICT'
PREMATURE_ADVANCE = 'PREMATURE_ADVANCE'

# From engine defensive strips
THINKING_LEAK = 'THINKING_LEAK'
TOOL_LEAK = 'TOOL_LEAK'

# Human-judgment only — pipeline doesn't catch
LEAKS_ANSWER = 'LEAKS_ANSWER'
IGNORES_STUDENT = 'IGNORES_STUDENT'
OFF_TOPIC = 'OFF_TOPIC'
REPEATS = 'REPEATS'

ISSUE_LABELS = (
    AUTHORED_QUESTION, UNFOUNDED_PRAISE,
    ARITHMETIC_ERROR,
    CLAIM_CONTRADICTED, CLAIM_UNVERIFIED,
    INCOHERENT,
    FIGURE_REF_UNATTACHED, FIGURE_MISMATCH,
    SAFETY_HARMFUL, SAFETY_INAPPROPRIATE,
    NO_QUESTION, INFO_DUMP, MULTI_PARAGRAPH, BANNED_OPENER, PADDING_FILLER,
    VERDICT_MISMATCH, WRONG_VERDICT, PREMATURE_ADVANCE,
    THINKING_LEAK, TOOL_LEAK,
    LEAKS_ANSWER, IGNORES_STUDENT, OFF_TOPIC, REPEATS,
)

ALL_LABELS = ACTION_LABELS + ISSUE_LABELS
LABEL_SET = frozenset(ALL_LABELS)


def is_valid_label(label: str) -> bool:
    """True if the string is one of the 30 defined labels."""
    return label in LABEL_SET


def is_action_label(label: str) -> bool:
    return label in ACTION_LABELS


def is_issue_label(label: str) -> bool:
    return label in ISSUE_LABELS


# ---------------------------------------------------------------------------
# Failure categories (19) — clustering tag for failed items
# ---------------------------------------------------------------------------

FAILURE_CATEGORIES = (
    'over_eager_working_request',
    'false_accept',
    'false_accept_with_leak',
    'false_reject',
    'incoherent_setup',
    'topic_jump',
    'bank_authoring',
    'figure_ref_broken',
    'figure_mismatch',
    'tool_leak',
    'over_explain',
    'premature_advance',
    'ignores_student_input',
    'bare_answer_chain',
    'unfounded_praise',
    'arithmetic_in_tutor',
    'ungrounded_factual',
    'safety_violation',
    'format_violation',
    'other',
)
FAILURE_CATEGORY_SET = frozenset(FAILURE_CATEGORIES)


def is_valid_failure_category(category: str) -> bool:
    if not category:
        return True  # empty = no category set (item passed)
    return category in FAILURE_CATEGORY_SET
