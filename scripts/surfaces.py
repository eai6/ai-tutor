#!/usr/bin/env python
"""Group templates by the stylesheets their shell actually loaded.

One global class map is wrong. `.card` is defined in dashboard/components/
surfaces.css AND in student/components.css, with different rules, and each
surface only ever loaded one of them. Merging the two hands dashboard pages
the student definition — which is how the settings form-group became a flex
column and grew 22px.

A template's sheet set is the set its shell linked at the pre-migration
commit. Following {% extends %} to the root shell gives that.
"""

from __future__ import annotations

import collections
import pathlib
import re
import subprocess
import sys

BASE_COMMIT = "1d1617c"
TEMPLATES = pathlib.Path("ai_tutor/templates")
EXTENDS = re.compile(r"""{%\s*extends\s+['"]([^'"]+)['"]""")
LINK = re.compile(r"{%\s*static\s+'(css/[^']+)'\s*%}")


def at_base(rel):
    try:
        return subprocess.check_output(
            ["git", "show", f"{BASE_COMMIT}:ai_tutor/templates/{rel}"], text=True,
            stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return ""


def root_shell(rel, seen=None):
    seen = seen or set()
    if rel in seen:
        return rel
    seen.add(rel)
    m = EXTENDS.search(at_base(rel))
    return root_shell(m.group(1), seen) if m else rel


INCLUDE = re.compile(r"""{%\s*include\s+['"]([^'"]+)['"]""")


def sheets_for(rel):
    """The stylesheets this template renders under.

    The shell's links plus any the page adds itself — a page sheet like
    student/catalog.css is linked by the page, not the shell.
    """
    src = at_base(root_shell(rel)) + at_base(rel)
    return tuple(sorted({s for s in LINK.findall(src) if "vendor" not in s}))


def resolve_partials(out):
    """A partial renders under whoever includes it.

    Give it the intersection of its includers' sheet sets: a class the
    partial uses is only safe to convert if every surface that renders it
    agrees on what that class means.
    """
    includers = collections.defaultdict(set)
    for sheets, tpls in out.items():
        for t in tpls:
            for inc in INCLUDE.findall(at_base(t)):
                includers[inc].add(sheets)
    moved = {}
    for sheets, tpls in list(out.items()):
        if sheets:
            continue
        for t in list(tpls):
            sets = includers.get(t)
            if not sets:
                continue
            common = tuple(sorted(set.intersection(*(set(x) for x in sets))))
            moved.setdefault(common, []).append(t)
            tpls.remove(t)
    for k, v in moved.items():
        out[k].extend(v)

    # Two kinds are reached by a template tag rather than {% include %}, so
    # nothing points at them: the generated playbook fragments, which the docs
    # view pulls in by slug, and the dashboard_ui inclusion tags.
    by_prefix = [
        ("docs/sections/", "docs/base.html"),
        ("dashboard/_components/", "dashboard/base.html"),
        ("_includes/icon.html", "dashboard/base.html"),
    ]
    for prefix, shell in by_prefix:
        target = sheets_for(shell)
        for sheets, tpls in list(out.items()):
            if sheets:
                continue
            for t in list(tpls):
                if t.startswith(prefix):
                    out[target].append(t)
                    tpls.remove(t)
    return out


def groups():
    out = collections.defaultdict(list)
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        if rel.startswith("email/"):
            continue
        out[sheets_for(rel)].append(rel)
    return resolve_partials(out)


if __name__ == "__main__":
    for sheets, tpls in sorted(groups().items(), key=lambda kv: -len(kv[1])):
        label = ", ".join(s.replace("css/", "") for s in sheets) or "(no stylesheet)"
        print(f"\n{len(tpls):>3} templates <- {label}")
        for t in tpls[:3]:
            print(f"      {t}")
        if len(tpls) > 3:
            print(f"      ... and {len(tpls) - 3} more")
