"""Turn a stylesheet into a {class name: utility string} map.

Phases 1-3 each replace a set of stylesheets with utilities on the markup that
used their class names. Doing that by hand over ~6,400 lines invites exactly
the kind of quiet transcription error the screenshot gate then has to hunt, so
the mechanical part is mechanised and what is left is review.

Selectors become Tailwind variants rather than being flattened:

    .card:hover              -> hover:...
    .card .title             -> [&_.title]:...
    .card > li               -> [&>li]:...
    .card::before            -> before:...
    @media (min-width:62rem) -> xl:...

A rule the tool cannot express is reported, not dropped, so it gets a person.

    python scripts/sheet_to_map.py ai_tutor/static/css/marketing/docs.css
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scripts import css_to_tailwind  # noqa: E402
from scripts.css_to_tailwind import decls_to_utilities, Unconvertible  # noqa: E402

# Breakpoints as declared in the theme, so a media query maps to the same
# variant the rest of the migration uses.
BREAKPOINTS = {"30rem": "xs", "40rem": "sm", "56rem": "md", "60rem": "lg", "62rem": "xl"}

def _sel(text):
    """Encode a selector fragment for use inside a Tailwind arbitrary variant.

    Tailwind reads _ as a space, so a BEM name has to escape its own
    underscores FIRST — otherwise [&_.lp-hero__lede] compiles to
    `.lp-hero lede`, a descendant selector for an element type that does not
    exist, and the rule silently matches nothing.
    """
    return text.replace("_", "\\_").replace(" ", "_")


PSEUDO = {
    ":hover": "hover", ":focus": "focus", ":focus-visible": "focus-visible",
    ":focus-within": "focus-within", ":active": "active", ":disabled": "disabled",
    ":checked": "checked", ":first-child": "first", ":last-child": "last",
    ":only-child": "only", ":empty": "empty", ":required": "required",
    "::before": "before", "::after": "after", "::placeholder": "placeholder",
    "::-webkit-scrollbar": None,  # no variant; needs a person
}


def _media_variant(query):
    q = query.strip()
    m = re.fullmatch(r"\(min-width:\s*([\d.]+rem|\d+px)\)", q)
    if m and m.group(1) in BREAKPOINTS:
        return BREAKPOINTS[m.group(1)]
    m = re.fullmatch(r"\(max-width:\s*([\d.]+rem|\d+px)\)", q)
    if m:
        v = m.group(1)
        return f"max-{BREAKPOINTS[v]}" if v in BREAKPOINTS else f"max-[{v}]"
    if m := re.fullmatch(r"\(min-width:\s*([\d.]+rem|\d+px)\)", q):
        return f"min-[{m.group(1)}]"
    return {
        "print": "print",
        "(prefers-reduced-motion: reduce)": "motion-reduce",
        "(prefers-contrast: more)": "contrast-more",
        "(forced-colors: active)": "forced-colors",
    }.get(q)


def _selector_to_variant(sel):
    """(base class, variant prefix) or (None, reason) if a person is needed."""
    sel = sel.strip()
    if sel in (":root", "html", "body", "*"):
        # Custom-property carriers and the document reset. resolve_local has
        # already followed anything they declare.
        return None, ""
    # `select.form-control` — a tag narrowing a class. The class is the base;
    # the tag becomes a condition on the element itself.
    tag = re.match(r"^([a-z][a-z0-9]*)\.([\w-]+)$", sel)
    if tag:
        return tag.group(2), f"[&:is({tag.group(1)})]:"
    m = re.match(r"^\.([\w-]+)(.*)$", sel)
    if not m:
        return None, f"not a single class selector: {sel}"
    base, raw_rest = m.group(1), m.group(2)
    # The separator distinguishes ".card .title" (a descendant, expressible as
    # an arbitrary variant) from ".card.title" (both classes on one element,
    # which no variant on a single class can reproduce). Stripping first would
    # collapse the two.
    descendant = raw_rest[:1].isspace()
    rest = raw_rest.strip()
    if not rest:
        return base, ""
    for pseudo, variant in PSEUDO.items():
        if rest == pseudo:
            if variant is None:
                return None, f"no variant for {pseudo}: {sel}"
            return base, f"{variant}:"
    if rest.startswith(">"):
        child = rest[1:].strip()
        return base, f"[&>{_sel(child)}]:"
    if not descendant:
        if rest.startswith("["):
            # An attribute on the same element: .card[hidden]
            return base, f"[&{rest}]:"
        if rest.startswith(":"):
            # A pseudo-class not in the table, e.g. :not(...)
            return base, f"[&{rest.replace(' ', '_')}]:"
        if rest.startswith("."):
            # .stat-change.positive — a modifier on the same element. The
            # variant asks for the second class alongside the first, which is
            # exactly what the cascade was doing.
            return base, f"[&{rest}]:"
        return None, f"two classes on one element needs a person: {sel}"
    return base, f"[&_{_sel(rest)}]:".replace('"', "'")


def parse(path):
    css = pathlib.Path(path).read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # @keyframes steps are `from`, `to` and percentages, not selectors. The
    # frames themselves live in app.css; here they are only noise.
    css = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\}\s*)*\}", "", css)
    # Custom properties the sheet declares for itself. They disappear with the
    # sheet, so they must be resolved to their values rather than referenced.
    # The sheet's own custom properties, plus the two token sheets it was
    # written against — an alias declared in tokens.css and read here would
    # otherwise convert to a var() with nothing behind it once both are gone.
    local = {}
    for extra in ("shared/tokens.css", "student/brand.css"):
        f = pathlib.Path("ai_tutor/static/css") / extra
        if f.exists():
            local.update(re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+);",
                                    re.sub(r"/\*.*?\*/", "", f.read_text(), flags=re.S)))
    # First definition wins for the sheet's own properties. A custom property
    # redefined inside a media query is an override of a base value, and a flat
    # last-wins dict would apply the small-screen value at every width.
    for name, val in re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+);", css):
        local.setdefault(name, val)
    css_to_tailwind.LOCAL_VARS = local
    # url()s in this sheet were written relative to its own directory.
    css_to_tailwind.URL_BASE = pathlib.Path(path).parent.relative_to(
        pathlib.Path("ai_tutor/static")).as_posix()
    out, problems = {}, []
    order = [0]
    # Class names a selector mentions in any position other than "the whole
    # selector". They have to survive the rewrite: an ancestor's arbitrary
    # variant ([&_.lp-hero_h1]:) targets them, and JavaScript looks some of
    # them up. Stripping them would break structure, not just appearance.
    hooks = set()

    def handle(block, prefix=""):
        for sel_group, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
            order[0] += 1
            sel_group = sel_group.strip()
            if sel_group.startswith("@") or not sel_group:
                continue
            for sel in sel_group.split(","):
                names = re.findall(r"\.([\w-]+)", sel)
                simple = re.fullmatch(r"\.([\w-]+)", sel.strip())
                if not simple:
                    hooks.update(names)
                base, variant = _selector_to_variant(sel)
                if base is None:
                    if variant:
                        problems.append(variant)
                    continue
                # A custom-property declaration defines a value for other
                # rules to read; resolve_local has already followed it, so it
                # is not something the markup needs to carry.
                body = "; ".join(d for d in body.split(";")
                                 if not d.strip().startswith("--"))
                try:
                    utils = decls_to_utilities(body)
                except Unconvertible as e:
                    problems.append(f"{sel.strip()} -> {e}")
                    continue
                if not utils:
                    continue
                for u in utils.split():
                    out.setdefault(base, []).append((order[0], f"{prefix}{variant}{u}"))

    # media blocks first, then strip them so the rest is the top level
    for query, inner in re.findall(r"@media([^{]+)\{((?:[^{}]*\{[^{}]*\}\s*)+)\}", css):
        variant = _media_variant(query)
        if variant is None:
            problems.append(f"unmapped media query: @media{query.strip()}")
            continue
        handle(inner, prefix=f"{variant}:")
    handle(re.sub(r"@media[^{]+\{(?:[^{}]*\{[^{}]*\}\s*)+\}", "", css))

    # Values keep the index of the rule they came from. Two classes on one
    # element can set the same property, and CSS gave that to whichever rule
    # sat LATER in the sheet — a class attribute has no order of its own, so
    # the index is the only way to reproduce the cascade.
    return out, problems, hooks


def flat(mapping):
    return {k: " ".join(u for _, u in v) for k, v in mapping.items()}


if __name__ == "__main__":
    mapping, problems, _hooks = parse(sys.argv[1])
    for name, utils in sorted(flat(mapping).items()):
        print(f".{name}\n    {utils}\n")
    if problems:
        print(f"\n--- {len(problems)} rule(s) need a person ---")
        for p in problems:
            print(f"  {p}")
