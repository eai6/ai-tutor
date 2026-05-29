"""Phase 1 — skills_snapshot wiring into v2 (read side + defense writer).

Covers the contract additions, the ContextManager loader/filter, the
renderer in router_prompts + student_tutor, and the prompt prose
additions to SHARED_ROUTER_SYSTEM + CONFIRM_AND_EXTEND / WORKED_EXAMPLE
/ EXPLAIN bodies.

Plan: memory/skills_snapshot_v2_wiring_plan.md (2026-05-29).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.tutoring.v2.contracts.tutoring import RouterRequest, TutoringContext
from apps.tutoring.v2.services.context_manager import ContextManager
from apps.tutoring.v2.services.move_prompts import (
    CONFIRM_AND_EXTEND,
    EXPLAIN,
    WORKED_EXAMPLE,
)
from apps.tutoring.v2.services.router_prompts import (
    SHARED_ROUTER_SYSTEM,
    _format_skills_snapshot_block,
    render_router_user_prompt,
)


# ---------------------------------------------------------------------------
# Contract — TutoringContext carries the snapshot; RouterRequest does NOT.
# The router does not route on cross-session mastery — its job is
# counter-driven move selection. The snapshot is purely a personalization
# input for the StudentTutor prompts (EXPLAIN / WORKED_EXAMPLE).
# ---------------------------------------------------------------------------


def test_tutoring_context_carries_skills_snapshot_field() -> None:
    field = TutoringContext.model_fields["skills_snapshot"]
    assert field.default_factory is dict


def test_router_request_does_not_carry_skills_snapshot_field() -> None:
    """Skills snapshot must NOT be plumbed into the router — it has no
    routing role under the 2026-05-29 personalization-only framing."""
    assert "skills_snapshot" not in RouterRequest.model_fields


# ---------------------------------------------------------------------------
# Formatter — _format_skills_snapshot_block
# ---------------------------------------------------------------------------


def test_format_returns_empty_string_on_empty_snapshot() -> None:
    assert _format_skills_snapshot_block({}) == ""


def test_format_skips_entries_without_a_level() -> None:
    snapshot = {
        "Has level": {"level": "weak", "attempts": 1},
        "No level":  {"attempts": 1},
    }
    rendered = _format_skills_snapshot_block(snapshot)
    assert "Has level" in rendered
    assert "No level" not in rendered


def test_format_orders_weak_developing_mastered_then_unassessed() -> None:
    snapshot = {
        "Beta":  {"level": "mastered",   "attempts": 2},
        "Alpha": {"level": "weak",       "attempts": 1},
        "Delta": {"level": "developing", "attempts": 4},
        "Gamma": {"level": "unassessed", "attempts": 0},
    }
    rendered = _format_skills_snapshot_block(snapshot)
    # Weak first, then developing, then mastered, then unassessed.
    idx_alpha = rendered.find("Alpha")
    idx_delta = rendered.find("Delta")
    idx_beta = rendered.find("Beta")
    idx_gamma = rendered.find("Gamma")
    assert idx_alpha < idx_delta < idx_beta < idx_gamma


def test_format_handles_attempt_grammar() -> None:
    rendered = _format_skills_snapshot_block({
        "One":    {"level": "weak", "attempts": 1},
        "Two":    {"level": "weak", "attempts": 2},
        "Zero":   {"level": "weak", "attempts": 0},
    })
    assert "(1 attempt)" in rendered
    assert "(2 attempts)" in rendered
    # Zero-attempt entry omits the parenthetical entirely.
    zero_line = next(
        line for line in rendered.splitlines() if line.startswith("- Zero")
    )
    assert "attempt" not in zero_line


def test_format_caps_at_max_entries_with_overflow_line() -> None:
    snapshot = {
        f"Tag{i}": {"level": "developing", "attempts": 1}
        for i in range(12)
    }
    rendered = _format_skills_snapshot_block(snapshot, max_entries=5)
    # 5 explicit entries + 1 "+ N more" line + the header.
    body_lines = [
        line for line in rendered.splitlines() if line.startswith("- ")
    ]
    assert len(body_lines) == 6  # 5 + overflow
    assert "(+ 7 more)" in rendered


def test_format_section_header_matches_prompt_prose() -> None:
    """The section header is the literal string the prompt prose refers to."""
    rendered = _format_skills_snapshot_block({
        "X": {"level": "weak", "attempts": 1},
    })
    expected = "=== Your skill levels on this lesson's objectives ==="
    assert rendered.startswith(expected)


# ---------------------------------------------------------------------------
# Router user prompt + system prompt — the skills section is NOT plumbed
# into the router. Router stays counter-driven.
# ---------------------------------------------------------------------------


def _minimal_router_request() -> RouterRequest:
    return RouterRequest(
        last_n_turns=[],
        student_input="",
    )


def test_router_user_prompt_never_renders_skills_section() -> None:
    """Whether the snapshot exists or not, the router user prompt does
    not include the section. Routing is counter-driven."""
    rendered = render_router_user_prompt(_minimal_router_request())
    assert "Your skill levels" not in rendered


def test_router_system_prompt_has_no_skill_levels_instructions() -> None:
    """No routing rules / biases / hints about a skill-levels section.

    The router must not reason about cross-session mastery — that's
    the personalization input for the StudentTutor move prompts only.
    """
    assert "SKILL LEVELS SECTION" not in SHARED_ROUTER_SYSTEM
    assert "skill levels" not in SHARED_ROUTER_SYSTEM.lower()


# ---------------------------------------------------------------------------
# Move prompts — personalization-only language. No routing, no Ch.13 / Ch.16
# citations, no "lean toward" / "bias toward" verbs. Just "reference a
# mastered objective by name as something already studied."
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join(text.split())


def test_confirm_and_extend_has_no_skills_section_addition() -> None:
    """CONFIRM_AND_EXTEND should NOT carry the linking-to-prior-study
    language — the user scoped personalization to EXPLAIN +
    WORKED_EXAMPLE only."""
    body = _normalize(CONFIRM_AND_EXTEND.body)
    assert "Your skill levels on this lesson's objectives" not in body


def _extract_linking_block(body: str) -> str:
    """Pull just the linking-to-prior-study sub-block from a move prompt.

    Anchored on the "Your skill levels..." phrase the section uses;
    runs until the next blank line or the next major section header.
    Used so the "no routing language" assertions check ONLY the new
    addition, not the entire move-prompt body (which may legitimately
    cite Ch.13 / Ch.16 in unrelated sections — e.g., the EXPLAIN
    body's Testing Effect Ch.20 + Mastery Learning Ch.13 citation on
    the open-pose rule).
    """
    norm = _normalize(body)
    idx = norm.find("Your skill levels on this lesson's objectives")
    if idx < 0:
        return ""
    return norm[idx:idx + 900]  # generous window for the addition


def test_worked_example_links_mastered_objectives_only_for_personalization() -> None:
    body = WORKED_EXAMPLE.body
    assert "Your skill levels on this lesson's objectives" in _normalize(body)
    block = _extract_linking_block(body)
    assert "mastered" in block
    assert "already studied" in block
    # The block itself contains NO routing instructions / science-of-
    # learning principle citations / depth modifiers.
    assert "Ch.13" not in block
    assert "Ch.16" not in block
    assert "diagnose root cause" not in block
    assert "lean toward" not in block.lower()
    assert "bias toward" not in block.lower()
    # Explicit no-hallucination guard so the LLM doesn't invent prior study.
    assert "Do not invent prior study" in block


def test_explain_links_mastered_objectives_only_for_personalization() -> None:
    body = EXPLAIN.body
    assert "Your skill levels on this lesson's objectives" in _normalize(body)
    block = _extract_linking_block(body)
    assert "mastered" in block
    assert "already studied" in block
    assert "Ch.13" not in block
    assert "Ch.16" not in block
    assert "lean toward" not in block.lower()
    assert "bias toward" not in block.lower()
    assert "Do not invent prior study" in block


# ---------------------------------------------------------------------------
# ContextManager._load_filtered_skills_snapshot — the loader/filter
# ---------------------------------------------------------------------------


def _stub_profile(snapshot: dict | None) -> SimpleNamespace:
    return SimpleNamespace(skills_snapshot=snapshot or {})


def _stub_lesson(
    *,
    objective: str = "",
    enabling_objectives: list[str] | None = None,
    step_objectives: list[str] | None = None,
) -> SimpleNamespace:
    steps = []
    for tag in (step_objectives or []):
        steps.append(SimpleNamespace(enabling_objective=tag))
    steps_qs = SimpleNamespace(all=lambda: steps)
    return SimpleNamespace(
        objective=objective,
        enabling_objectives=(enabling_objectives or []),
        steps=steps_qs,
    )


def _stub_course(course_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(id=course_id)


def _loader() -> "ContextManager._load_filtered_skills_snapshot":
    # Use the bound method on a fresh instance — the loader doesn't
    # touch self.session.
    cm = ContextManager.__new__(ContextManager)
    return cm._load_filtered_skills_snapshot


def test_loader_returns_empty_when_profile_is_none() -> None:
    out = _loader()(
        profile=None,
        lesson=_stub_lesson(objective="X"),
        course=_stub_course(),
    )
    assert out == {}


def test_loader_returns_empty_when_lesson_is_none() -> None:
    out = _loader()(
        profile=_stub_profile({"7": {"X": {"level": "weak", "attempts": 1}}}),
        lesson=None,
        course=_stub_course(),
    )
    assert out == {}


def test_loader_returns_empty_when_course_slice_is_missing() -> None:
    out = _loader()(
        profile=_stub_profile({"99": {"X": {"level": "weak", "attempts": 1}}}),
        lesson=_stub_lesson(objective="X"),
        course=_stub_course(course_id=7),
    )
    assert out == {}


def test_loader_returns_empty_on_no_overlap() -> None:
    out = _loader()(
        profile=_stub_profile(
            {"7": {"Some Other Tag": {"level": "weak", "attempts": 1}}}
        ),
        lesson=_stub_lesson(objective="Map Scale"),
        course=_stub_course(),
    )
    assert out == {}


def test_loader_intersects_on_primary_objective() -> None:
    out = _loader()(
        profile=_stub_profile({
            "7": {
                "Map Scale": {"level": "mastered", "attempts": 3},
                "Off-lesson tag": {"level": "weak", "attempts": 1},
            },
        }),
        lesson=_stub_lesson(objective="Map Scale"),
        course=_stub_course(),
    )
    assert "Map Scale" in out
    assert "Off-lesson tag" not in out


def test_loader_intersects_on_enabling_objectives() -> None:
    out = _loader()(
        profile=_stub_profile({
            "7": {
                "Read scale ratios":   {"level": "developing", "attempts": 2},
                "Identify map types":  {"level": "mastered",   "attempts": 4},
            },
        }),
        lesson=_stub_lesson(
            objective="Map Scale and Map Types",
            enabling_objectives=["Read scale ratios", "Identify map types"],
        ),
        course=_stub_course(),
    )
    assert {"Read scale ratios", "Identify map types"} <= out.keys()


def test_loader_intersects_on_step_objectives() -> None:
    out = _loader()(
        profile=_stub_profile({
            "7": {
                "Ratio reading": {"level": "weak", "attempts": 1},
            },
        }),
        lesson=_stub_lesson(
            objective="Different",
            step_objectives=["Ratio reading"],
        ),
        course=_stub_course(),
    )
    assert "Ratio reading" in out


def test_loader_normalizes_tags_across_whitespace_drift() -> None:
    """The shared _normalize_tag strips whitespace so authoring drift on
    leading/trailing space does not break the intersection."""
    out = _loader()(
        profile=_stub_profile({
            "7": {
                "  Read scale ratios  ": {"level": "developing", "attempts": 2},
            },
        }),
        lesson=_stub_lesson(
            objective="",
            enabling_objectives=["Read scale ratios"],
        ),
        course=_stub_course(),
    )
    assert len(out) == 1
    matched_tag = next(iter(out.keys()))
    assert "scale" in matched_tag.lower()


def test_loader_failsoft_when_profile_skills_snapshot_raises() -> None:
    """Property access raise → empty dict, no propagation."""

    class _RaisingProfile:
        @property
        def skills_snapshot(self):
            raise RuntimeError("DB went sideways")

    out = _loader()(
        profile=_RaisingProfile(),
        lesson=_stub_lesson(objective="X"),
        course=_stub_course(),
    )
    assert out == {}


def test_loader_failsoft_when_steps_iteration_raises() -> None:
    """An exception during lesson.steps.all() → continues with lesson-level
    objectives only."""

    class _RaisingSteps:
        def all(self):
            raise RuntimeError("queryset blew up")

    lesson = SimpleNamespace(
        objective="Map Scale",
        enabling_objectives=[],
        steps=_RaisingSteps(),
    )
    out = _loader()(
        profile=_stub_profile({
            "7": {"Map Scale": {"level": "weak", "attempts": 1}},
        }),
        lesson=lesson,
        course=_stub_course(),
    )
    assert "Map Scale" in out


# ---------------------------------------------------------------------------
# StudentTutor — renderer parity with the router prompt's renderer
# ---------------------------------------------------------------------------


def test_student_tutor_renderer_uses_shared_formatter() -> None:
    from apps.tutoring.v2.services.student_tutor import StudentTutor

    tutor = StudentTutor.__new__(StudentTutor)
    ctx_empty = MagicMock(spec=TutoringContext)
    ctx_empty.skills_snapshot = {}
    assert tutor._render_skills_snapshot_block(ctx_empty) == ""

    ctx_full = MagicMock(spec=TutoringContext)
    ctx_full.skills_snapshot = {
        "Map Scale": {"level": "mastered", "attempts": 3},
    }
    rendered = tutor._render_skills_snapshot_block(ctx_full)
    assert "=== Your skill levels on this lesson's objectives ===" in rendered
    assert "Map Scale" in rendered
    assert "mastered" in rendered


# ---------------------------------------------------------------------------
# Defense-in-depth writer call — chat_exit_ticket view
# ---------------------------------------------------------------------------


def test_chat_exit_ticket_view_calls_refresh_student_snapshot() -> None:
    """The view calls refresh_student_snapshot AFTER the legacy submit.

    Idempotent today (legacy already calls it); becomes the sole call
    site once legacy is deleted under the 4-week deprecation gate.
    """
    import importlib
    views = importlib.import_module("apps.tutoring.views")
    # The defensive call is wired inside the view function body.
    source = (
        views.chat_exit_ticket.__wrapped__.__code__
        if hasattr(views.chat_exit_ticket, "__wrapped__")
        else views.chat_exit_ticket.__code__
    )
    # Smoke-check via source inspection — the import + call are inside
    # the try block. A live integration test would require a Django DB
    # session; this contract test confirms the wiring exists.
    import inspect
    body = inspect.getsource(views.chat_exit_ticket)
    assert "refresh_student_snapshot" in body
    assert "memory/skills_snapshot_v2_wiring_plan.md" in body
