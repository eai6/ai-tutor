#!/usr/bin/env python
"""Bring the variable names in templates' own <style> blocks up to date.

    python scripts/inline_styles.py --apply

These blocks were written against css/shared/tokens.css. That file is gone, so
every var(--surface), var(--space-4) and var(--focus-ring) in them resolves to
nothing and the page keeps its layout while losing every colour at once.

It renames and NOTHING else. An earlier version of this script tried to
convert the blocks into utilities the way the stylesheets were converted, and
it deleted the per-institution theme block in dashboard/base.html — a
documented exception that must never be touched. The blocks carry CSP nonces,
Django tags and element selectors that a class map has no business rewriting;
they are left for a person.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scripts import css_to_tailwind  # noqa: E402

TEMPLATES = pathlib.Path("ai_tutor/templates")
STYLE = re.compile(r"(<style[^>]*>)(.*?)(</style>)", re.S)


def rename(css):
    def sub(m):
        name = m.group(1)
        if name in css_to_tailwind.COLOURS:
            return f"var(--color-{css_to_tailwind.COLOURS[name]})"
        if name in css_to_tailwind.SPACE_TOKENS:
            return f"calc(var(--spacing)*{css_to_tailwind.SPACE_TOKENS[name]})"
        if name in css_to_tailwind.SHADOW_RENAMES:
            return f"var({css_to_tailwind.SHADOW_RENAMES[name]})"
        return m.group(0)
    return re.sub(r"var\((--[\w-]+)\)", sub, css)


def main(apply=False):
    touched = renamed = 0
    for tpl in sorted(TEMPLATES.rglob("*.html")):
        if "email/" in tpl.as_posix():
            continue
        src = tpl.read_text()
        if "<style" not in src:
            continue

        def swap(m):
            nonlocal renamed
            new = rename(m.group(2))
            renamed += sum(1 for _ in re.finditer(r"var\(--", m.group(2))) if new != m.group(2) else 0
            return m.group(1) + new + m.group(3)

        out = STYLE.sub(swap, src)
        if out != src:
            touched += 1
            if apply:
                tpl.write_text(out)
    print(f"  {touched} template(s) with <style> blocks {'updated' if apply else 'would change'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    main(ap.parse_args().apply)
