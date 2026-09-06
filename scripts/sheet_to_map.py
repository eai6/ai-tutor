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
    m = re.match(r"^\.([\w-]+)(.*)$", sel)
    if not m:
        return None, f"not a single class selector: {sel}"
    base, rest = m.group(1), m.group(2).strip()
    if not rest:
        return base, ""
    for pseudo, variant in PSEUDO.items():
        if rest == pseudo:
            if variant is None:
                return None, f"no variant for {pseudo}: {sel}"
            return base, f"{variant}:"
    if rest.startswith(">"):
        child = rest[1:].strip()
        return base, f"[&>{child.replace(' ', '_')}]:"
    if rest.startswith((".", "#", ":", "[")):
        return None, f"compound selector needs a person: {sel}"
    return base, f"[&_{rest.replace(' ', '_')}]:"


def parse(path):
    css = pathlib.Path(path).read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
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
    local.update(re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+);", css))
    css_to_tailwind.LOCAL_VARS = local
    out, problems = {}, []

    def handle(block, prefix=""):
        for sel_group, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
            sel_group = sel_group.strip()
            if sel_group.startswith("@") or not sel_group:
                continue
            for sel in sel_group.split(","):
                base, variant = _selector_to_variant(sel)
                if base is None:
                    problems.append(variant)
                    continue
                try:
                    utils = decls_to_utilities(body)
                except Unconvertible as e:
                    problems.append(f"{sel.strip()} -> {e}")
                    continue
                if not utils:
                    continue
                full = " ".join(f"{prefix}{variant}{u}" for u in utils.split())
                out.setdefault(base, []).append(full)

    # media blocks first, then strip them so the rest is the top level
    for query, inner in re.findall(r"@media([^{]+)\{((?:[^{}]*\{[^{}]*\}\s*)+)\}", css):
        variant = _media_variant(query)
        if variant is None:
            problems.append(f"unmapped media query: @media{query.strip()}")
            continue
        handle(inner, prefix=f"{variant}:")
    handle(re.sub(r"@media[^{]+\{(?:[^{}]*\{[^{}]*\}\s*)+\}", "", css))

    return {k: " ".join(v) for k, v in out.items()}, problems


if __name__ == "__main__":
    mapping, problems = parse(sys.argv[1])
    for name, utils in sorted(mapping.items()):
        print(f".{name}\n    {utils}\n")
    if problems:
        print(f"\n--- {len(problems)} rule(s) need a person ---")
        for p in problems:
            print(f"  {p}")
