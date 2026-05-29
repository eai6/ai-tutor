"""Content-regression test for the WORKED_EXAMPLE Subgoal → Step relabel.

Per 2026-05-28 design call: the worked-example move should label its
walkthrough sections as "Step 1, Step 2, ..." not "Subgoal 1,
Subgoal 2, ...". Student-facing label change; pedagogy is identical.

The Cognitive Load Ch.14 principle citation (technical vocabulary
"labelled subgoals are the load-reducer") is preserved on the comment
attribution line — it is not a student-facing label.
"""

from __future__ import annotations

from apps.tutoring.v2.services.move_prompts import (
    SCAFFOLD_HINT,
    NAME_MISCONCEPTION,
    WORKED_EXAMPLE,
)


def test_worked_example_uses_step_labels_not_subgoal() -> None:
    """The WORKED_EXAMPLE body labels walkthrough sections as 'Step N'."""
    body = WORKED_EXAMPLE.body
    # Step labels present in the example shape.
    assert '"Step 1: …"' in body
    assert '"Step 2: …"' in body
    assert '"Step 3: …"' in body
    # No leftover "Subgoal" labels anywhere in the body.
    assert "Subgoal" not in body
    assert "subgoal" not in body


def test_worked_example_checklist_uses_step_terminology() -> None:
    """The response-quality checklist mirrors the Step terminology."""
    body = WORKED_EXAMPLE.body
    checklist_start = body.find("RESPONSE QUALITY CHECKLIST")
    assert checklist_start > -1
    checklist = body[checklist_start:]
    # Checklist references "step" not "subgoal".
    assert "step" in checklist.lower()
    assert "subgoal" not in checklist.lower()


def test_worked_example_open_question_guard_preserves_pedagogy() -> None:
    """The 'last step poses, not states' guard survives the relabel."""
    body = WORKED_EXAMPLE.body
    # Concrete acceptable shape — labelled Step 3 with a question.
    assert "Step 3 — Putting it together" in body
    # Counter-shape — also relabelled to Step.
    assert "Step 3 (bad)" in body


def _normalize(text: str) -> str:
    """Collapse whitespace so substring assertions are robust to line wraps."""
    return " ".join(text.split())


def test_scaffold_hint_uses_step_not_subgoal_terminology() -> None:
    """SCAFFOLD_HINT uses 'step' terminology, never 'subgoal' (the relabel
    invariant). Post-consolidation the move references decomposing into a
    smaller 'step', not the old 'worked-example steps' cross-reference."""
    body = SCAFFOLD_HINT.body.lower()
    assert "subgoal" not in body
    assert "step" in body


def test_name_misconception_consistent_with_step_labels() -> None:
    """NAME_MISCONCEPTION's fallback worked-example walkthrough uses 'step'."""
    body = NAME_MISCONCEPTION.body
    # The fallback line ~713 was "the relevant subgoal" → "the relevant step".
    assert "the relevant step" in body
    # No leftover subgoal mention.
    assert "the relevant subgoal" not in body


def test_principle_attributions_preserve_subgoals_in_internal_vocabulary() -> None:
    """Cognitive Load Ch.14's 'labelled subgoals' principle citation is preserved.

    Code-comment principle attributions reference the science-of-learning
    literature's technical term and are NOT student-facing labels. We do
    not change these; only the LLM's emitted output labels were relabelled.
    """
    # The WORKED_EXAMPLE principle tuple comment still cites subgoals.
    import apps.tutoring.v2.services.move_prompts as mp
    src = open(mp.__file__).read()
    assert "Cognitive Load (worked-example + subgoals)" in src
