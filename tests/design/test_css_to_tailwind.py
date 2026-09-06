"""The declaration->utility helper is what makes the rewrite pixel-faithful.

Its contract is narrow on purpose: match a theme token when the value is
exactly a theme value, emit an arbitrary value when it is not, and refuse
anything it cannot express. The refusal matters most — a helper that silently
dropped a declaration would produce markup that looks converted and renders
differently.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scripts.css_to_tailwind import Unconvertible, decls_to_utilities  # noqa: E402


class TestSpacing:
    def test_a_value_on_the_scale_uses_the_scale(self):
        # --spacing is 0.25rem, so 1rem is exactly p-4 (the old --space-4)
        assert decls_to_utilities("padding: 1rem") == "p-4"
        assert decls_to_utilities("margin-top: 0.5rem") == "mt-2"

    def test_a_value_off_the_scale_is_exact_not_nearest(self):
        """0.6rem is between p-2 and p-3. Rounding would move the pixel."""
        assert decls_to_utilities("padding: 0.6rem") == "p-[0.6rem]"

    def test_shorthand_splits_into_axes(self):
        assert decls_to_utilities("padding: 0.6rem 0.85rem") == "py-[0.6rem] px-[0.85rem]"

    def test_four_value_shorthand_splits_into_sides(self):
        got = decls_to_utilities("margin: 1rem 0.5rem 0.25rem 0")
        assert got == "mt-4 mr-2 mb-1 ml-0"

    def test_zero_is_zero_not_an_arbitrary_value(self):
        assert decls_to_utilities("padding: 0") == "p-0"


class TestColour:
    def test_a_token_reference_becomes_the_token_utility(self):
        assert decls_to_utilities("color: var(--text-muted)") == "text-text-muted"
        assert decls_to_utilities("background: var(--surface)") == "bg-surface"

    def test_a_hex_that_matches_a_token_uses_the_token(self):
        """The one rule: no literal colour survives the migration."""
        assert decls_to_utilities("color: #A83B00") == "text-primary-ink"

    def test_a_hex_that_matches_nothing_is_still_exact(self):
        assert decls_to_utilities("color: #123456") == "text-[#123456]"

    def test_an_ambiguous_hex_resolves_by_the_job_it_is_doing(self):
        """#A83B00 is both --primary-dark and --primary-ink.

        css/dashboard/README.md is explicit that the ink is the orange that may
        carry text and --primary (3.6:1) is the one that may not. Picking
        whichever name happened to be declared first would erase that.
        """
        assert decls_to_utilities("color: #A83B00") == "text-primary-ink"
        assert decls_to_utilities("border-color: #E3DFD8") == "border-border"


class TestTypography:
    def test_font_size_token(self):
        assert decls_to_utilities("font-size: var(--text-sm)") == "text-sm"

    def test_font_weight_token(self):
        assert decls_to_utilities("font-weight: var(--weight-medium)") == "font-medium"

    def test_raw_font_weight(self):
        assert decls_to_utilities("font-weight: 700") == "font-bold"


class TestSkinnableTokens:
    def test_shadows_keep_their_variable_so_a_scope_can_re_skin_them(self):
        """shadow-sm bakes its literal; shadow-[var(--shadow-sm)] does not.

        The student skin re-declares --shadow-* under [data-surface="student"]
        with warmer, softer values. A baked utility would ignore that and put
        the dashboard's shadow on every student card.
        """
        assert decls_to_utilities("box-shadow: var(--shadow-sm)") == "shadow-[var(--shadow-sm)]"

    def test_colour_and_radius_need_no_such_care(self):
        """These already compile to var(), so the scope reaches them."""
        assert decls_to_utilities("border-radius: var(--radius-lg)") == "rounded-lg"
        assert decls_to_utilities("background: var(--canvas)") == "bg-canvas"


class TestLayout:
    def test_display_and_flex(self):
        got = decls_to_utilities("display: flex; align-items: center; gap: 0.5rem")
        assert got == "flex items-center gap-2"

    def test_several_declarations_keep_source_order(self):
        got = decls_to_utilities("display: block; padding: 1rem; color: var(--text)")
        assert got == "block p-4 text-text"


class TestRefusal:
    def test_an_unmappable_property_raises(self):
        with pytest.raises(Unconvertible):
            decls_to_utilities("counter-reset: section")

    def test_the_exception_names_the_declaration(self):
        with pytest.raises(Unconvertible) as e:
            decls_to_utilities("counter-increment: step")
        assert "counter-increment" in str(e.value)

    def test_nothing_is_silently_dropped(self):
        """Every declaration in, at least one utility out, or an exception."""
        with pytest.raises(Unconvertible):
            decls_to_utilities("padding: 1rem; counter-reset: section")
