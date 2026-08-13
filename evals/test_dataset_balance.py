"""The dataset cannot silently re-skew.

The single-turn set used to have 8 of 24 persona x lesson cells empty and exactly
ONE error_prone scenario. Nothing in the repo noticed. This test is what noticing
looks like: it fails the build if the balance the 400-scenario expansion bought is
ever spent.

It is deliberately a *test*, not a lint you have to remember to run.
"""
from __future__ import annotations

import os
from collections import Counter

import django
import pytest

# Self-sufficient: this suite reads YAML and the plan, and only touches Django for
# rule_registry. Doing setup here means it runs under a bare `pytest` with no
# pytest-django and no project-wide pytest config — neither of which this repo has.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
try:
    django.setup()
except Exception:  # pragma: no cover — already configured by a plugin
    pass

from evals.lint_dataset import lint  # noqa: E402
from evals.matrix import (  # noqa: E402
    LESSONS, PERSONAS, TARGET_PER_MODE, _load_existing,
)


def test_dataset_lints_clean():
    """Structure, groundedness, balance — all of it, in one assertion."""
    errors = lint()
    assert not errors, (
        f"{len(errors)} dataset problem(s):\n\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


@pytest.mark.parametrize('mode', ['single_turn', 'multi_turn'])
def test_mode_has_target_size(mode):
    scenarios = [d for d in _load_existing().values() if d['mode'] == mode]
    assert len(scenarios) == TARGET_PER_MODE, (
        f"{mode} holds {len(scenarios)} scenarios, expected {TARGET_PER_MODE}"
    )


@pytest.mark.parametrize('mode', ['single_turn', 'multi_turn'])
def test_no_persona_is_starved(mode):
    """The bug this whole expansion existed to fix: error_prone had 1 scenario."""
    scenarios = [d for d in _load_existing().values() if d['mode'] == mode]
    counts = Counter(d['persona'] for d in scenarios)
    target = TARGET_PER_MODE / len(PERSONAS)
    starved = {p: counts[p] for p in PERSONAS if counts[p] < target * 0.6}
    assert not starved, (
        f"{mode}: persona(s) under 60% of the ~{target:.0f} target: {starved}. "
        f"Any per-persona claim drawn from this dataset would be unsupported."
    )


@pytest.mark.parametrize('mode', ['single_turn', 'multi_turn'])
def test_no_lesson_dominates(mode):
    """Lesson 1137 once carried 28 of 60 single-turn scenarios."""
    scenarios = [d for d in _load_existing().values() if d['mode'] == mode]
    counts = Counter(d['lesson_id'] for d in scenarios)
    target = TARGET_PER_MODE / len(LESSONS)
    hogs = {l: counts[l] for l in LESSONS if counts[l] > target * 2}
    assert not hogs, (
        f"{mode}: lesson(s) carrying more than 2x the ~{target:.0f} target: "
        f"{hogs}. Content-specific model weaknesses would be over-weighted."
    )
