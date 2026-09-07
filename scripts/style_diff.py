#!/usr/bin/env python
"""Compare computed styles element-by-element between two servers.

The screenshot gate says a page moved; this says which element and which
property. The migration only ever rewrites `class` attributes, so the element
trees on both sides are identical and can be walked in parallel by position.

    python scripts/style_diff.py /dashboard/ --role teacher

Reports every property whose computed value differs, grouped by how often it
occurs, because one wrong utility usually shows up on dozens of elements.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import shoot  # noqa: E402

# Properties worth comparing. The full computed set is ~340 per element and
# most of it is noise that never differs.
PROPS = [
    "display", "position", "width", "height", "marginTop", "marginRight",
    "marginBottom", "marginLeft", "paddingTop", "paddingRight", "paddingBottom",
    "paddingLeft", "color", "backgroundColor", "fontSize", "fontWeight",
    "fontFamily", "lineHeight", "letterSpacing", "textAlign", "textTransform",
    "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
    "borderTopColor", "borderRightColor", "borderBottomColor", "borderLeftColor",
    "borderTopLeftRadius", "borderTopRightRadius", "borderBottomRightRadius",
    "borderBottomLeftRadius", "boxShadow", "flexDirection", "alignItems",
    "justifyContent", "gap", "flexGrow", "flexShrink", "flexBasis", "opacity",
    "overflowX", "overflowY", "whiteSpace", "zIndex", "gridTemplateColumns",
    "maxWidth", "minWidth", "maxHeight", "minHeight", "textDecorationLine",
    # Two elements can share font-family, size and letter-spacing and still
    # measure differently: tabular figures are a different advance width from
    # proportional ones. Without these a dropped `tabular-nums` shows up only
    # as an unexplained 0.2px per digit.
    "fontVariantNumeric", "fontFeatureSettings", "fontStyle", "wordSpacing",
]

WALK = """
(() => {
  const PROPS = %s;
  const out = [];
  const walk = (el, path) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    // An element nobody can see cannot be a visual defect. The mobile rail
    // toggle is display:none at this width and reports a different font on
    // both sides for reasons that never reach a screen.
    const shown = s.display !== 'none' && s.visibility !== 'hidden'
                  && s.opacity !== '0' && r.width > 0 && r.height > 0;
    const v = {};
    for (const p of PROPS) v[p] = s[p];
    out.push([path, el.tagName, v, shown]);
    let i = 0;
    for (const c of el.children) walk(c, path + '/' + (i++) + ':' + c.tagName);
  };
  walk(document.body, 'body');
  return JSON.stringify(out);
})()
""" % json.dumps(PROPS)


def normalise(prop, value):
    """Drop differences that cannot reach a screen.

    Tailwind composes box-shadow out of five slots and leaves the unused ones
    fully transparent, so its shadow string differs from a hand-written one
    that says the same thing.
    """
    if prop == "boxShadow":
        # Split on commas that are NOT inside rgba(...). Splitting on the
        # literal "), " missed every separator and collapsed the whole value
        # into one layer, which then looked transparent and reported a shadow
        # that was in fact rendering perfectly as missing.
        layers, depth, cur = [], 0, ""
        for ch in value:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                layers.append(cur.strip())
                cur = ""
                continue
            cur += ch
        if cur.strip():
            layers.append(cur.strip())
        kept = [l for l in layers if "rgba(0, 0, 0, 0)" not in l and l != "none"]
        return ", ".join(kept) if kept else "none"
    return value


def reaches_a_screen(prop, style):
    """Whether a difference in *prop* can actually be seen.

    A border colour on a zero-width border is the common case: the old sheets
    left it at `currentColor` and Tailwind's preflight sets it to the border
    token, so every element in the tree reports a difference that no one can
    ever see. Filtering it here rather than in the eye keeps the report about
    real defects.
    """
    if prop.startswith("border") and prop.endswith("Color"):
        side = prop[len("border"):-len("Color")]
        width = style.get(f"border{side}Width")
        try:
            return float(str(width).replace("px", "")) > 0
        except (TypeError, ValueError):
            return True
    return True


def snapshot(chrome, url, settle=2.5):
    chrome.send("Page.navigate", url=url)
    time.sleep(settle)
    chrome.send("Runtime.evaluate", expression=(
        "document.getAnimations?.().forEach(a=>{a.pause();a.currentTime=0});"
        "document.fonts.ready"), awaitPromise=True)
    r = chrome.send("Runtime.evaluate", expression=WALK, returnByValue=True)
    return json.loads(r["result"]["value"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--role", default="anon")
    ap.add_argument("--old", default="http://127.0.0.1:8001")
    ap.add_argument("--new", default="http://127.0.0.1:8000")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--inspect", help="dump both sides' class attribute at this element path")
    a = ap.parse_args()

    cookies = shoot.session_cookies(
        {"teacher": "teacher_daniel", "student": "student_daniel", "admin": "superadmin_daniel"})

    with shoot.Chrome() as c:
        c.send("Runtime.enable")
        c.send("Emulation.setDeviceMetricsOverride",
               width=1440, height=900, deviceScaleFactor=1, mobile=False)
        if a.role in cookies:
            c.send("Network.setCookie", name="sessionid", value=cookies[a.role],
                   domain="127.0.0.1", path="/")
        if a.inspect:
            js = ("(() => { let e = document.body;"
                  "for (const seg of %r.split('/').slice(1))"
                  "  e = e.children[parseInt(seg)];"
                  "return e.tagName + '  ' + e.className; })()")
            for label, base in (("baseline", a.old), ("converted", a.new)):
                snapshot(c, base + a.path)
                r = c.send("Runtime.evaluate", expression=js % a.inspect, returnByValue=True)
                print(f"{label}: {r['result']['value']}\n")
            return 0
        old = snapshot(c, a.old + a.path)
        new = snapshot(c, a.new + a.path)

    if len(old) != len(new):
        print(f"element counts differ: baseline {len(old)}, converted {len(new)}")
        print("the trees are not comparable — a template changed structure, not just classes")

    counts = collections.Counter()
    examples = {}
    hidden = 0
    for (po, to, vo, so), (pn, tn, vn, sn) in zip(old, new):
        if po != pn:
            print(f"tree diverged at {po} vs {pn}")
            break
        if not (so or sn):
            hidden += 1
            continue
        for prop, ov in vo.items():
            nv = normalise(prop, vn[prop])
            ov = normalise(prop, ov)
            if ov != nv:
                key = f"{to}.{prop}: {ov!r} -> {nv!r}"
                counts[key] += 1
                examples.setdefault(key, po)

    if not counts:
        print(f"{a.path}: computed styles identical across {len(old)} elements")
        return 0
    print(f"{a.path}: {sum(counts.values())} property differences over "
          f"{len(old) - hidden} visible elements ({hidden} hidden, skipped)\n")
    for key, n in counts.most_common(a.top):
        print(f"  {n:>4}x  {key}")
        print(f"        at {examples[key][:110]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
