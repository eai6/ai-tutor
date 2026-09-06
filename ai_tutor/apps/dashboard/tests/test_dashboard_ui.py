"""The tone lookup is what keeps the status vocabulary visible to Tailwind."""

import pytest

from ai_tutor.apps.dashboard.templatetags.dashboard_ui import (
    TONES, TONE_UTILITIES, tone_class,
)


def test_tone_class_returns_literal_utilities_not_a_modifier():
    out = tone_class("badge", "success")
    assert "--" not in out
    assert "bg-success-surface" in out and "text-success" in out


def test_every_component_covers_every_tone():
    """A missing entry renders an unstyled badge rather than raising."""
    for prefix, table in TONE_UTILITIES.items():
        assert set(table) == set(TONES), f"{prefix} is missing a tone"


def test_an_unknown_tone_falls_back_to_neutral():
    assert tone_class("badge", "banana") == tone_class("badge", "neutral")


def test_neutral_adds_nothing_because_the_base_class_is_neutral():
    assert tone_class("badge", "neutral") == ""


def test_an_unknown_component_is_empty_rather_than_an_error():
    assert tone_class("not-a-component", "success") == ""


@pytest.mark.parametrize("prefix", sorted(TONE_UTILITIES))
def test_no_entry_smuggles_in_a_modifier_name(prefix):
    for tone, utils in TONE_UTILITIES[prefix].items():
        assert "--" not in utils, f"{prefix}/{tone} still names a modifier"
