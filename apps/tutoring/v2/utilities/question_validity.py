"""Boundary validator for posable lesson questions.

Single source of truth for "is this LessonStep something the tutor can
legitimately put in front of a student?". Run at the slot-builder
boundary in ``build_anthropic_pose_question_tool`` so the LLM's tool
surface NEVER contains incomplete questions — the tutor cannot pose
what it cannot see.

Scope (deliberate):

* ``multiple_choice`` — requires ``choices`` to be a list of ≥2
  non-empty strings. An MCQ stem with no rendered options is
  unanswerable; the most common P1.3 source observed across runs.
* ``true_false`` — requires the stem to cue True/False, OR the
  ``expected_answer`` to be a canonical T/F token so the renderer's
  cue suffix is guaranteed to apply. An unsuffixed T/F statement is
  an incomplete question (MATHS-S1 2026-05-27 T1442 P1).
* ``none`` — never posable (engage / explain steps are not assessment
  items).
* ``short_numeric`` / ``free_text`` / unknown types — *conservative
  pass*. These are hard to validate deterministically (the stem may
  be phrased many ways) and the historical P1 surface for them is
  small. Pose them; the downstream conformance / grader layer
  catches the residual cases.

Design role (CLAUDE.md guidance — "deterministic gates as safety
floors, not flow controllers"):

  - This validator is the *boundary* check. It runs once when the
    slot menu is built; the LLM never sees the filtered steps.
  - Renderer (``_render_bank_stem_with_options``) handles canonical
    transformation from typed fields to visible text. The two
    together produce a guarantee: every posable step renders into
    an answerable stem.
  - Existing safety floors (``_looks_like_mcq_stem_without_options``,
    move-prompt answer-shape directives) become belt-and-braces; the
    boundary filter is the primary line.

(Science of learning principle: Active Learning Ch.10 — the student
must be able to act on the question this turn; missing answer-shape
signal breaks the retrieval loop before it starts. Principle #11
Testing Effect Ch.20 — retrieval only consolidates when the question
is answerable.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PosabilityResult:
    """Outcome of ``is_posable_question``.

    ``reason`` is a short slug ("mcq_choices_missing", "tf_no_cue",
    "no_question_text", "answer_type_none") suitable for telemetry
    grouping. Empty when ``passes`` is True.
    """

    passes: bool
    reason: str = ""


_TF_STEM_CUES = ("true or false", "true/false", "t/f")
_TF_CANONICAL_ANSWERS = frozenset({"true", "false", "t", "f"})


def is_posable_question(step) -> PosabilityResult:
    """Return whether a ``LessonStep`` can be put in front of a student.

    Pass criteria:
      * ``question`` text is non-empty.
      * Answer-type-specific structural requirements (see module
        docstring).

    Validators OUT of scope (conservative pass): ``short_numeric``,
    ``free_text``, and any unknown type. Their incompleteness modes
    are hard to validate deterministically and rarely surface in
    observed P1.3 incidents; the downstream conformance + grader
    stack handles the residual.
    """
    question = (getattr(step, "question", "") or "").strip()
    if not question:
        return PosabilityResult(passes=False, reason="no_question_text")

    answer_type = (getattr(step, "answer_type", "") or "").strip().lower()
    if answer_type in ("", "none"):
        return PosabilityResult(passes=False, reason="answer_type_none")

    if answer_type == "multiple_choice":
        return _check_multiple_choice(step)

    if answer_type == "true_false":
        return _check_true_false(step, question_lower=question.lower())

    # short_numeric / free_text / unknown — conservative pass.
    # The renderer + downstream stack handle these.
    return PosabilityResult(passes=True)


def _check_multiple_choice(step) -> PosabilityResult:
    """MCQ requires a list of ≥2 non-empty string choices.

    The renderer (``_render_bank_stem_with_options``) appends the
    choices block; without it, the student sees a stem that asks
    them to pick but offers nothing to pick from.
    """
    choices = getattr(step, "choices", None)
    if not isinstance(choices, list):
        return PosabilityResult(passes=False, reason="mcq_choices_missing")
    valid = [c for c in choices if isinstance(c, str) and c.strip()]
    if len(valid) < 2:
        return PosabilityResult(passes=False, reason="mcq_choices_too_few")
    return PosabilityResult(passes=True)


def _check_true_false(step, *, question_lower: str) -> PosabilityResult:
    """True/False requires either a stem-level cue OR a canonical
    expected answer.

    The renderer appends ``(True or False?)`` when the stem doesn't
    already cue T/F — but only when the answer-type is set. The
    *minimum* signal we need to trust that suffix is the answer-key
    actually being a T/F value; otherwise the step is mis-typed and
    we'd be suggesting a binary answer for a non-binary question.
    """
    for cue in _TF_STEM_CUES:
        if cue in question_lower:
            return PosabilityResult(passes=True)

    expected = (getattr(step, "expected_answer", "") or "").strip().lower()
    if expected in _TF_CANONICAL_ANSWERS:
        return PosabilityResult(passes=True)

    return PosabilityResult(passes=False, reason="tf_no_cue")
