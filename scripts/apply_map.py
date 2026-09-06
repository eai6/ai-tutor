"""Rewrite class attributes in templates from a stylesheet's class map.

    python scripts/apply_map.py --sheet ai_tutor/static/css/marketing/docs.css \
                                --templates ai_tutor/templates/docs --apply

Without --apply it reports what it would do and changes nothing.

Two things it will not do quietly:

* A class it has no mapping for is LEFT IN PLACE and reported. Those are the
  JavaScript hooks (is-open, rail-open) and the selectors a person still owes
  a decision on. Dropping them would break behaviour, not just looks.
* If two classes on one element both set the same utility family — a p-* each,
  say — it reports a conflict instead of picking. In the stylesheet the
  cascade decided; in a class attribute nothing does, because Tailwind orders
  by its own generated order and not by the order written in the markup.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scripts.sheet_to_map import parse  # noqa: E402

def _unescape(sel):
    return re.sub(r"\\(.)", r"\1", sel)


def property_families(utilities):
    """Ask Tailwind which CSS properties each utility actually sets.

    Guessing from the prefix does not work: text-md is a font size, text-text
    is a colour and text-center is an alignment. Compiling the real thing and
    reading the declarations back is the only answer that stays correct as the
    theme changes.
    """
    import subprocess
    import tempfile

    repo = pathlib.Path(__file__).resolve().parent.parent
    src = repo / "ai_tutor" / "static_src" / "app.css"
    probe_text = re.sub(
        r'@source "\.\./templates[^;]*;',
        '@source inline("' + " ".join(sorted(utilities)) + '");',
        src.read_text(), count=1,
    )
    with tempfile.TemporaryDirectory() as td:
        probe = src.parent / "_families.css"
        out = pathlib.Path(td) / "out.css"
        probe.write_text(probe_text)
        try:
            subprocess.run(["npx", "tailwindcss", "-i", str(probe), "-o", str(out)],
                           cwd=repo, check=True, capture_output=True)
            css = out.read_text()
        finally:
            probe.unlink(missing_ok=True)

    families = {}
    # A linear pattern on purpose. The obvious ((?:[^{\s]|\\.)+?) backtracks
    # catastrophically over a large generated stylesheet and simply never
    # returns — it looked like Tailwind hanging, and Tailwind takes 3 seconds.
    for sel, body in re.findall(r"\n\s*\.([^{\s]+)\s*\{([^{}]*)\}", css):
        name = _unescape(sel)
        props = frozenset(
            d.split(":", 1)[0].strip()
            for d in body.split(";")
            if ":" in d and not d.strip().startswith("--tw")
        )
        if props:
            families.setdefault(name, set()).update(props)
    return families


def make_family(families):
    def family(util):
        variant, sep, base = util.rpartition(":")
        props = families.get(base) or families.get(util)
        # An unknown utility is its own family: never merged, never dropped.
        return (variant, frozenset(props)) if props else (variant, util)
    return family


# A Django tag inside a class attribute. The tag itself is opaque, but the
# literal class names around it are not, and skipping the whole attribute
# because one is present left every component template unconverted — the stat
# tiles kept their class names and lost their styles when the sheet went.
TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)


def rewrite_attr(value, mapping, unmapped, conflicts, where, family, hooks):
    """Rewrite one class="..." value, leaving any Django tag in place."""
    # Park the tags, rewrite what is left, then put them back where they were.
    parked = []

    def park(m):
        parked.append(m.group(0))
        return f"\x00{len(parked) - 1}\x00"

    value = TAG.sub(park, value)
    literal, best, changed = [], {}, False
    for token in value.split():
        if token.startswith("\x00"):
            literal.append(token)
            continue
        if "{{" in token:
            literal.append(token)
            continue
        if token not in mapping:
            unmapped.setdefault(token, []).append(where)
            literal.append(token)
            continue
        changed = True
        if token in hooks:
            literal.append(token)   # kept: something else selects through it
        for order, util in mapping[token]:
            fam = family(util)
            prev = best.get(fam)
            if prev is None:
                best[fam] = (order, util)
            elif prev[1] != util:
                # The later rule won in the sheet; it wins here too.
                loser, winner = (prev, (order, util)) if order > prev[0] else ((order, util), prev)
                best[fam] = winner
                conflicts.setdefault(f"{winner[1]} over {loser[1]}", []).append(where)
    out = " ".join(literal + [u for _, u in best.values()])
    out = re.sub(r"\x00(\d+)\x00", lambda m: parked[int(m.group(1))], out)
    return out, changed


def classes_defined_elsewhere(converting):
    """Class names that stylesheets NOT being converted still style.

    A name has to survive if anything else styles it. .skip-link is declared
    in both marketing/landing.css and shared/base.css; converting the first
    and dropping the name took the second's rules with it and left the skip
    link unstyled. The name costs nothing to keep and is the only thing
    holding those rules on.
    """
    converting = {pathlib.Path(c).resolve() for c in converting}
    repo = pathlib.Path(__file__).resolve().parent.parent / "ai_tutor"
    names = set()
    for css in (repo / "static" / "css").rglob("*.css"):
        if css.resolve() in converting or css.name == "app.build.css":
            continue
        names.update(re.findall(r"\.([A-Za-z][\w-]*)", css.read_text()))
    # Templates carry their own <style> blocks, and a class styled there is
    # just as real as one in a stylesheet. Scanning only .css files stripped
    # names that a page's own block was still selecting on — the benchmark
    # pages lost their input, select and code styling that way.
    for tpl in (repo / "templates").rglob("*.html"):
        for block in re.findall(r"<style[^>]*>(.*?)</style>", tpl.read_text(errors="ignore"), re.S):
            names.update(re.findall(r"\.([A-Za-z][\w-]*)", block))
    return names


def run(sheets, template_dirs, apply=False):
    mapping, problems = {}, []
    base, hooks = 0, set()
    for sheet in sheets:
        m, p, h = parse(sheet)
        hooks |= h
        highest = max((o for v in m.values() for o, _ in v), default=0)
        for k, v in m.items():
            mapping.setdefault(k, []).extend((o + base, u) for o, u in v)
        base += highest
        problems += p

    hooks |= classes_defined_elsewhere(sheets) & set(mapping)

    families = property_families(
        {u for v in mapping.values() for _, u in v})
    family = make_family(families)

    files = []
    for d in template_dirs:
        d = pathlib.Path(d)
        files += sorted(d.rglob("*.html")) if d.is_dir() else [d]

    unmapped, conflicts, touched = {}, {}, []
    for f in files:
        if "email/" in f.as_posix():
            continue
        src = f.read_text()

        def repl(m):
            new, changed = rewrite_attr(m.group(1), mapping, unmapped, conflicts,
                                        f.name, family, hooks)
            return f'class="{new}"' if changed else m.group(0)

        new_src = re.sub(r'class="([^"]*)"', repl, src)
        if new_src != src:
            touched.append(f)
            if apply:
                f.write_text(new_src)

    print(f"{len(mapping)} classes mapped from {len(sheets)} sheet(s); "
          f"{len(hooks & set(mapping))} names kept (hooks, or still styled elsewhere)")
    print(f"{len(touched)} template(s) {'rewritten' if apply else 'would change'}")
    if problems:
        print(f"\n{len(problems)} rule(s) the map could not express:")
        for p in problems[:40]:
            print(f"  {p}")
    if conflicts:
        print(f"\n{len(conflicts)} property set by two classes; resolved by sheet order:")
        for c, where in conflicts.items():
            print(f"  {c}   in {sorted(set(where))[:4]}")
    interesting = {k: v for k, v in unmapped.items()
                   if not k.startswith(("{{", "{%")) and "-" in k or k.startswith("is-")}
    if interesting:
        print(f"\n{len(interesting)} class(es) with no mapping, left in place:")
        for k, where in sorted(interesting.items())[:40]:
            print(f"  {k:<34} {sorted(set(where))[:3]}")
    return touched


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", action="append", required=True)
    ap.add_argument("--templates", action="append", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    run(a.sheet, a.templates, a.apply)
