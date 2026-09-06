"""Guards that hold the migration's invariants once each surface lands.

Each one fails today and is expected to: they describe the finished state, and
are marked xfail(strict=True) so that the moment a phase makes one true, the
suite says so and the marker comes off. A guard that quietly passed early would
be worse than none.
"""

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "ai_tutor" / "templates"
STATIC_CSS = REPO / "ai_tutor" / "static" / "css"
STATIC_JS = REPO / "ai_tutor" / "static" / "js"

HEX = re.compile(r"#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{2})?)?\b")

# The only places a literal colour may appear after the migration.
ALLOWED_HEX = {
    # The theme itself. This is the whole point of the rule.
    "ai_tutor/static_src/app.css",
    # An HTML email. Mail clients strip <link> and most strip <style>, so the
    # inline style attributes are the only thing that renders it.
    "ai_tutor/templates/email/verify_email.html",
}


def _sources():
    for p in list(TEMPLATES.rglob("*.html")) + list(STATIC_CSS.rglob("*.css")) + list(
        STATIC_JS.rglob("*.js")
    ):
        rel = p.relative_to(REPO).as_posix()
        if "vendor" in rel or rel.endswith("app.build.css") or "Sortable" in rel:
            continue
        yield rel, p


@pytest.mark.xfail(strict=True, reason="true once phase 4 deletes the last stylesheet")
def test_no_literal_colour_outside_the_theme():
    """`css/dashboard/README.md`'s one rule, carried across to @theme.

    A hex in a template is a colour nobody can re-theme: the per-institution
    brand override in base.html works by redefining a custom property, and a
    literal never sees it.
    """
    offenders = sorted(
        rel for rel, p in _sources()
        if rel not in ALLOWED_HEX and HEX.search(p.read_text(errors="ignore"))
    )
    assert offenders == [], f"{len(offenders)} files carry a literal colour"


@pytest.mark.xfail(strict=True, reason="true once every surface uses literal utility strings")
def test_no_runtime_class_name_concatenation():
    """Tailwind's scanner only ever sees literal strings in source text.

    A class assembled at runtime — `pill-{{ status }}`, or an f-string in a
    template tag — is invisible to it. The failure mode is nasty: it works in
    development, because the class was in an earlier build's scan, and ships
    with no styles at all.
    """
    pattern = re.compile(r'class="[^"]*[\w-]-\{\{|class="[^"]*\{\{\s*\w+\s*\}\}[\w-]')
    offenders = sorted(
        rel for rel, p in _sources()
        if rel.endswith(".html") and pattern.search(p.read_text(errors="ignore"))
    )
    assert offenders == [], f"{len(offenders)} templates build a class name at runtime"


@pytest.mark.xfail(strict=True, reason="true once phase 4 deletes the last stylesheet")
def test_only_the_built_stylesheet_is_linked():
    """One stylesheet, not twenty-three."""
    linked = set()
    for rel, p in _sources():
        if rel.endswith(".html"):
            linked |= set(re.findall(r"static '(css/[^']+)'", p.read_text(errors="ignore")))
    assert linked <= {"css/app.build.css"}, f"still linked: {sorted(linked - {'css/app.build.css'})}"


def test_the_email_template_is_never_touched():
    """Not a ratchet — an invariant that must hold at every point.

    Mail clients strip <link> and most strip <style>. If this file ever loses
    its inline styles the verification email renders as unstyled text, and
    nothing in the screenshot gate would notice, because it has no URL.
    """
    email = REPO / "ai_tutor" / "templates" / "email" / "verify_email.html"
    assert 'style="' in email.read_text(), "verify_email.html lost its inline styles"
