"""Turn a CSS declaration block into Tailwind utilities, exactly.

The migration's promise is that nothing moves by a pixel. This helper keeps it
by preferring a theme token only when the value is *exactly* a theme value, and
falling back to an arbitrary value otherwise — never to the nearest token. A
0.6rem padding becomes p-[0.6rem], not p-2.

It refuses what it cannot express. That is the important half: a helper that
silently dropped `counter-reset` would hand back markup that looks converted
and renders differently, and the screenshot gate would be left to catch a bug
the tool could have refused to write.

    >>> decls_to_utilities("padding: 0.6rem 0.85rem; color: var(--text-muted)")
    'py-[0.6rem] px-[0.85rem] text-text-muted'
"""

from __future__ import annotations

import pathlib
import re

APP_CSS = pathlib.Path(__file__).resolve().parent.parent / "ai_tutor" / "static_src" / "app.css"


class Unconvertible(Exception):
    """A declaration with no faithful utility form. Handle it by hand."""


# ---------------------------------------------------------------------------
# The theme, read from app.css so the two can never drift
# ---------------------------------------------------------------------------

def _parse_theme(path=APP_CSS):
    text = path.read_text()
    block = re.search(r"@theme[^{]*\{(.*?)\n\}", text, re.S)
    if not block:
        raise RuntimeError(f"no @theme block in {path}")
    out = {}
    for line in block.group(1).splitlines():
        line = re.sub(r"/\*.*?\*/", "", line).strip()
        m = re.match(r"(--[\w-]+)\s*:\s*(.+?);", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


THEME = _parse_theme()


def _resolve(value, depth=0):
    """Follow var() chains inside the theme down to a literal."""
    if depth > 8:
        return value
    m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    if m and m.group(1) in THEME:
        return _resolve(THEME[m.group(1)], depth + 1)
    return value.strip()


def _same_name(prefix):
    """{old-var-name: suffix} where the old and new names are identical.

    --radius-md was --radius-md before the migration too, so the key keeps its
    full name. Only the colour namespace gained a prefix.
    """
    return {k: k[len(prefix):] for k in THEME if k.startswith(prefix)}


# Colours are the exception: the old --surface became --color-surface, so the
# key has to be rebuilt from the suffix.
COLOURS = {f"--{k[len('--color-'):]}": k[len("--color-"):]
           for k in THEME if k.startswith("--color-")}
FONT_SIZES = _same_name("--text-")
RADII = _same_name("--radius-")
SHADOWS = _same_name("--shadow-")
LEADINGS = _same_name("--leading-")
TRACKINGS = _same_name("--tracking-")
FONTS = _same_name("--font-")
WEIGHTS = {"--weight-normal": "normal", "--weight-medium": "medium", "--weight-bold": "bold"}
RAW_WEIGHTS = {"400": "normal", "600": "medium", "700": "bold", "500": "[500]",
               "800": "[800]", "300": "[300]", "900": "black",
               "bold": "bold", "normal": "normal", "bolder": "[bolder]",
               "lighter": "[lighter]"}
# The student sheets say --weight-regular where the token is --weight-normal.
WEIGHTS["--weight-regular"] = "normal"

# The old sheet's legacy aliases, so a --gray-500 in an un-migrated page
# converts to the warm ramp it already points at rather than refusing.
COLOURS.update({f"--gray-{n}": f"warm-{n}" for n in
                (50, 100, 200, 300, 400, 500, 600, 700, 900)})
COLOURS["--white"] = "surface"

# Old focus tokens were named for the ring; the theme names them for the shadow.
SHADOWS.update({"--focus-ring": "focus", "--focus-halo": "halo"})
SHADOW_RENAMES = {"--focus-ring": "--shadow-focus", "--focus-halo": "--shadow-halo",
                  "--focus-ring-inset": "--inset-shadow-focus"}

# hex -> every token holding it, so a literal colour converts to the token that
# already carries that value. Several hexes have more than one name on purpose:
# --primary-dark and --primary-ink are both #A83B00, and which one is correct
# depends on the job. css/dashboard/README.md is explicit that the ink is the
# one that may carry text, so the lookup has to know the role it is filling.
HEX_TO_TOKENS = {}
for _k, _v in THEME.items():
    if _k.startswith("--color-"):
        _lit = _resolve(_v)
        if _lit.startswith("#"):
            HEX_TO_TOKENS.setdefault(_lit.upper(), []).append(_k[len("--color-"):])

ROLE_PREFERENCE = {
    "text":   ("-ink", "text-", "-fill"),
    "bg":     ("-surface", "-fill", "surface"),
    "border": ("-border", "border"),
}


def _pick(candidates, role):
    for hint in ROLE_PREFERENCE.get(role, ()):
        for c in candidates:
            if c.endswith(hint) or c.startswith(hint):
                return c
    return candidates[0]


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

SPACING_STEP = 0.25  # rem, from --spacing

# The old sheet's spacing tokens. --spacing is the same 0.25rem base, so
# --space-4 IS p-4. Leaving these as var() would compile fine today and break
# the moment phase 4 deletes tokens.css.
SPACE_TOKENS = {f"--space-{n}": str(n) for n in (1, 2, 3, 4, 5, 6, 8, 10, 12)}

# Where the stylesheet being converted lived, as a path under static/. A
# url() inside it was written relative to THAT directory; once the declaration
# moves into a class it is served from static/css/app.build.css instead, so
# every relative URL has to be rebased or it silently 404s.
URL_BASE: str = "css"

# Page-local custom properties, filled in by the caller from the sheet being
# converted. A sheet that declares --docs-measure and then reads it back would
# otherwise convert to a var() with nothing behind it once the sheet is gone.
LOCAL_VARS: dict[str, str] = {}


def known_token(name):
    """True if the old variable name has a utility of its own.

    These must never be resolved to a literal. --canvas resolves to #FCFBF9 on
    the dashboard and #FFF9F5 for students; baking either one in would turn a
    re-skinnable token into a fixed colour and break the surface it was not
    baked for.
    """
    return (name in THEME or name in COLOURS or name in SPACE_TOKENS
            or name in RADII or name in SHADOWS or name in FONT_SIZES
            or name in LEADINGS or name in TRACKINGS or name in FONTS
            or name in WEIGHTS)


def resolve_local(value):
    """Substitute custom properties that have no utility and no future.

    An alias like --card-bg: var(--surface) disappears with the stylesheet
    that declared it, so it has to be followed through to something that
    survives — here, back to --surface, which is a token.
    """
    def sub(m):
        name = m.group(1)
        if name in LOCAL_VARS and not known_token(name):
            return LOCAL_VARS[name]
        return m.group(0)
    prev = None
    while prev != value:
        prev, value = value, re.sub(r"var\((--[\w-]+)\)", sub, value)
    return value


def _length(value):
    """A spacing-scale step if the value lands on it exactly, else None."""
    value = value.strip()
    if value in ("0", "0px", "0rem"):
        return "0"
    m = re.fullmatch(r"(-?\d*\.?\d+)(rem|px)", value)
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2)
    rem = num if unit == "rem" else num / 16
    steps = rem / SPACING_STEP
    if abs(steps - round(steps)) < 1e-9 and 0 <= round(steps) <= 96:
        return str(round(steps))
    return None


def _rebase_urls(value):
    import posixpath

    def sub(m):
        quote, url = m.group(1), m.group(2)
        if url.startswith(("data:", "http:", "https:", "/", "#")):
            return m.group(0)
        resolved = posixpath.normpath(posixpath.join(URL_BASE, url))
        return f"url({quote}{posixpath.relpath(resolved, 'css')}{quote})"

    return re.sub(r"""url\((['"]?)([^)'"]+)\1\)""", sub, value)


def _rename_vars(value):
    """Rewrite an old variable name to the one the theme actually declares.

    An arbitrary value carries its text through verbatim, so a var(--surface)
    inside a gradient would still say --surface after the theme renamed it to
    --color-surface — compiling fine today and resolving to nothing the day
    tokens.css is deleted.
    """
    def sub(m):
        name = m.group(1)
        if name in COLOURS:
            return f"var(--color-{COLOURS[name]})"
        if name in SPACE_TOKENS:
            return f"calc(var(--spacing)*{SPACE_TOKENS[name]})"
        if name in SHADOW_RENAMES:
            return f"var({SHADOW_RENAMES[name]})"
        return m.group(0)
    return re.sub(r"var\((--[\w-]+)\)", sub, value)


def _arb(value):
    # ALL whitespace collapses, not just spaces. A multi-line box-shadow left
    # its newlines in place, and the caller splits utilities on whitespace, so
    # one shadow arrived as two broken class names.
    #
    # Double quotes become single ones. A class attribute is delimited by ",
    # so [&[aria-current="page"]]: ends the attribute early and the browser
    # reads the rest of the class list as stray attributes — the utility does
    # not merely fail, it corrupts the element.
    # Underscores are escaped BEFORE spaces are collapsed to underscores:
    # Tailwind reads _ as a space, so a literal one in a filename or a BEM
    # name has to say so.
    out = _rebase_urls(_rename_vars(value.strip())).replace("_", "\\_")
    out = re.sub(r"\s+", "_", out)
    return "[" + out.replace('"', "'") + "]"


def _colour(value, role="text"):
    """A colour value -> a utility suffix. Raises if it is not a colour."""
    value = value.strip()
    # var(--x, fallback): the variable is the value; the fallback only matters
    # where the variable is undefined, which is not the case after migration.
    fallback = re.fullmatch(r"var\(\s*(--[\w-]+)\s*,.*\)", value, re.S)
    if fallback:
        value = f"var({fallback.group(1)})"
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if m:
        if m.group(1) in COLOURS:
            return COLOURS[m.group(1)]
        return _arb(value)
    if value.upper() in HEX_TO_TOKENS:
        return _pick(HEX_TO_TOKENS[value.upper()], role)
    if re.fullmatch(r"#[0-9A-Fa-f]{3,8}", value):
        return _arb(value)
    # System colours, used under forced-colors: to hand control to the OS.
    if value in ("Highlight", "HighlightText", "Canvas", "CanvasText", "LinkText",
                 "ButtonText", "ButtonFace", "GrayText", "ActiveText", "Field",
                 "FieldText", "Mark", "MarkText", "SelectedItem", "SelectedItemText"):
        return _arb(value)
    if value in ("transparent", "currentColor", "inherit", "white", "black"):
        return {"currentColor": "current"}.get(value, value)
    if value.startswith(("rgb", "hsl", "oklch", "color-mix")):
        return _arb(value)
    raise Unconvertible(f"not a colour: {value!r}")


# Keywords a spacing property accepts that are not lengths. mx-[auto] is not a
# utility Tailwind will generate, so `margin: 0 auto` silently lost its
# centring and every page sat flush against the left edge.
SPACING_KEYWORDS = {"auto", "inherit", "initial", "revert", "unset"}


def _spacing(value):
    """A length -> a scale step, a keyword, or an exact arbitrary value."""
    if value.strip() in SPACING_KEYWORDS:
        return value.strip()
    m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    if m and m.group(1) in SPACE_TOKENS:
        return SPACE_TOKENS[m.group(1)]
    step = _length(value)
    return step if step is not None else _arb(value)


def _sized(value, table, prefix_arb=True):
    m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    if m and m.group(1) in table:
        return table[m.group(1)]
    return _arb(value) if prefix_arb else None


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

KEYWORDS = {
    "display": {"flex": "flex", "block": "block", "inline-block": "inline-block",
                "inline-flex": "inline-flex", "grid": "grid", "inline": "inline",
                "none": "hidden", "inline-grid": "inline-grid", "contents": "contents",
                "table": "table", "list-item": "list-item", "flow-root": "flow-root"},
    "align-items": {"center": "items-center", "flex-start": "items-start",
                    "flex-end": "items-end", "start": "items-start", "end": "items-end",
                    "baseline": "items-baseline", "stretch": "items-stretch"},
    "justify-content": {"center": "justify-center", "flex-start": "justify-start",
                        "flex-end": "justify-end", "space-between": "justify-between",
                        "space-around": "justify-around", "space-evenly": "justify-evenly",
                        "start": "justify-start", "end": "justify-end"},
    "text-align": {"left": "text-left", "center": "text-center", "right": "text-right",
                   "justify": "text-justify", "start": "text-start", "end": "text-end"},
    "position": {"relative": "relative", "absolute": "absolute", "fixed": "fixed",
                 "sticky": "sticky", "static": "static"},
    "overflow": {"hidden": "overflow-hidden", "auto": "overflow-auto",
                 "visible": "overflow-visible", "scroll": "overflow-scroll",
                 "clip": "overflow-clip"},
    "overflow-x": {"hidden": "overflow-x-hidden", "auto": "overflow-x-auto",
                   "visible": "overflow-x-visible", "scroll": "overflow-x-scroll"},
    "overflow-y": {"hidden": "overflow-y-hidden", "auto": "overflow-y-auto",
                   "visible": "overflow-y-visible", "scroll": "overflow-y-scroll"},
    "white-space": {"nowrap": "whitespace-nowrap", "normal": "whitespace-normal",
                    "pre": "whitespace-pre", "pre-wrap": "whitespace-pre-wrap",
                    "pre-line": "whitespace-pre-line"},
    "text-transform": {"uppercase": "uppercase", "lowercase": "lowercase",
                       "capitalize": "capitalize", "none": "normal-case"},
    "text-decoration": {"none": "no-underline", "underline": "underline",
                        "line-through": "line-through"},
    "flex-direction": {"row": "flex-row", "column": "flex-col",
                       "row-reverse": "flex-row-reverse", "column-reverse": "flex-col-reverse"},
    "flex-wrap": {"wrap": "flex-wrap", "nowrap": "flex-nowrap",
                  "wrap-reverse": "flex-wrap-reverse"},
    "cursor": {"pointer": "cursor-pointer", "default": "cursor-default",
               "not-allowed": "cursor-not-allowed", "text": "cursor-text",
               "move": "cursor-move", "grab": "cursor-grab", "wait": "cursor-wait"},
    "vertical-align": {"middle": "align-middle", "top": "align-top", "bottom": "align-bottom",
                       "baseline": "align-baseline"},
    "text-overflow": {"ellipsis": "truncate", "clip": "text-clip"},
    "place-items": {"center": "place-items-center", "start": "place-items-start",
                    "end": "place-items-end", "stretch": "place-items-stretch"},
    "box-sizing": {"border-box": "box-border", "content-box": "box-content"},
    "user-select": {"none": "select-none", "text": "select-text", "all": "select-all",
                    "auto": "select-auto"},
    "object-fit": {"cover": "object-cover", "contain": "object-contain", "fill": "object-fill",
                   "none": "object-none", "scale-down": "object-scale-down"},
    "pointer-events": {"none": "pointer-events-none", "auto": "pointer-events-auto"},
    "list-style": {"none": "list-none", "disc": "list-disc", "decimal": "list-decimal"},
    "list-style-type": {"none": "list-none", "disc": "list-disc", "decimal": "list-decimal"},
}

# Expressed verbatim through Tailwind's [prop:value] syntax. Every one of these
# is a declaration with no lossy step: what goes in comes out.
ARBITRARY_PROPERTIES = {
    "transform", "transform-origin", "background-image", "background-position",
    "background-size", "background-repeat", "background-clip", "backdrop-filter",
    "font-variant-numeric", "font-feature-settings", "-webkit-backdrop-filter",
    "border-block", "border-inline", "border-block-width", "outline-offset", "font",
    "stroke-width", "stroke-linecap", "stroke-linejoin", "clip", "grid-row", "grid-column",
    "grid-template-rows", "grid-auto-flow", "grid-auto-rows", "appearance",
    "break-inside", "break-after", "page-break-inside", "filter", "mix-blend-mode",
    "text-underline-offset", "text-decoration-thickness", "text-decoration-color",
    "scroll-behavior", "scroll-margin-top", "aspect-ratio", "isolation",
    "will-change", "touch-action", "overscroll-behavior", "caret-color",
    "-webkit-font-smoothing", "-webkit-text-size-adjust", "-webkit-line-clamp",
    "-webkit-box-orient", "-webkit-overflow-scrolling", "text-rendering",
    # counter-reset / counter-increment are deliberately absent: a counter is
    # only ever useful with a ::before whose content reads it, and a pseudo
    # element is a judgement call, not a mechanical translation.
    "writing-mode", "clip-path", "quotes",
    "resize", "table-layout", "border-collapse", "border-spacing", "order",
    "visibility", "float", "clear", "direction", "unicode-bidi", "word-break",
    "overflow-wrap", "hyphens", "tab-size", "color-scheme", "forced-color-adjust",
}

SPACING_PROPS = {
    "padding": "p", "padding-top": "pt", "padding-right": "pr",
    "padding-bottom": "pb", "padding-left": "pl", "padding-inline": "px",
    "padding-block": "py",
    "margin": "m", "margin-top": "mt", "margin-right": "mr",
    "margin-bottom": "mb", "margin-left": "ml", "margin-inline": "mx",
    "margin-block": "my",
    "gap": "gap", "column-gap": "gap-x", "row-gap": "gap-y",
    "top": "top", "right": "right", "bottom": "bottom", "left": "left", "inset": "inset",
}

SIZE_PROPS = {"width": "w", "height": "h", "min-width": "min-w", "min-height": "min-h",
              "max-width": "max-w", "max-height": "max-h", "flex-basis": "basis"}

SIZE_KEYWORDS = {"100%": "full", "auto": "auto", "100vh": "screen", "100dvh": "dvh",
                 "fit-content": "fit", "max-content": "max", "min-content": "min",
                 "0": "0", "none": "none"}


def _expand_shorthand(prefix, parts):
    """padding: a b c d -> pt/pr/pb/pl, honouring CSS's 1/2/3/4-value rules."""
    axis = {"p": ("pt", "pr", "pb", "pl", "px", "py"),
            "m": ("mt", "mr", "mb", "ml", "mx", "my")}[prefix]
    t, r, b, l, x, y = axis
    if len(parts) == 1:
        return [f"{prefix}-{_spacing(parts[0])}"]
    if len(parts) == 2:
        return [f"{y}-{_spacing(parts[0])}", f"{x}-{_spacing(parts[1])}"]
    if len(parts) == 3:
        return [f"{t}-{_spacing(parts[0])}", f"{x}-{_spacing(parts[1])}",
                f"{b}-{_spacing(parts[2])}"]
    return [f"{t}-{_spacing(parts[0])}", f"{r}-{_spacing(parts[1])}",
            f"{b}-{_spacing(parts[2])}", f"{l}-{_spacing(parts[3])}"]


def _split_values(value):
    """Split on whitespace, but not inside var(...) / rgb(...) / calc(...)."""
    out, depth, cur = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                out.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def _convert(prop, value):
    prop, value = prop.strip().lower(), value.strip()

    if prop in KEYWORDS:
        if value in KEYWORDS[prop]:
            return [KEYWORDS[prop][value]]
        # A keyword this table does not list is still a valid declaration —
        # vertical-align: -0.125em, say. An arbitrary property carries it
        # exactly, which beats refusing and dropping the whole rule.
        return [_arb(f"{prop}:{value}")]

    if prop in SPACING_PROPS:
        parts = _split_values(value)
        if prop in ("padding", "margin") and len(parts) > 1:
            return _expand_shorthand(SPACING_PROPS[prop], parts)
        return [f"{SPACING_PROPS[prop]}-{_spacing(parts[0])}"]

    if prop in SIZE_PROPS:
        p = SIZE_PROPS[prop]
        if value in SIZE_KEYWORDS:
            return [f"{p}-{SIZE_KEYWORDS[value]}"]
        return [f"{p}-{_spacing(value)}"]

    if prop == "color":
        return [f"text-{_colour(value, "text")}"]
    if prop in ("background-color", "background"):
        if value == "none":
            return ["bg-none"] if prop == "background" else ["bg-transparent"]
        if prop == "background" and (
            "gradient" in value or "url(" in value or len(_split_values(value)) > 1
        ):
            # A gradient or a layered background travels verbatim. Tailwind's
            # own gradient utilities cannot reproduce an arbitrary colour-mix
            # stop list, and approximating one would move a pixel.
            return [_arb(f"background:{value}")]
        return [f"bg-{_colour(value, "bg")}"]
    if prop == "border-color":
        return [f"border-{_colour(value, "border")}"]
    if prop in ("border-top-color", "border-right-color",
                "border-bottom-color", "border-left-color"):
        side = {"top": "t", "right": "r", "bottom": "b", "left": "l"}[prop.split("-")[1]]
        return [f"border-{side}-{_colour(value, "border")}"]
    if prop in ("fill", "stroke"):
        # `none` is a paint value, not a colour. .icon sets fill: none and
        # stroke: currentColor; refusing the first dropped the whole rule and
        # every sprite icon fell back to the SVG default of 150x150.
        if value == "none":
            return [f"{prop}-none"]
        return [f"{prop}-{_colour(value, 'bg' if prop == 'fill' else 'text')}"]
    if prop == "accent-color":
        return [f"accent-{_colour(value, "bg")}"]

    if prop == "font-size":
        return [f"text-{_sized(value, FONT_SIZES)}"]
    if prop == "font-weight":
        m = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if m and m.group(1) in WEIGHTS:
            return [f"font-{WEIGHTS[m.group(1)]}"]
        if value in RAW_WEIGHTS:
            return [f"font-{RAW_WEIGHTS[value]}"]
        raise Unconvertible(f"{prop}: {value}")
    if prop == "font-style":
        return {"italic": "italic", "normal": "not-italic",
                "oblique": "[font-style:oblique]"}.get(value) and [
            {"italic": "italic", "normal": "not-italic",
             "oblique": "[font-style:oblique]"}[value]] or [_arb(f"font-style:{value}")]
    if prop == "font-family":
        return [f"font-{_sized(value, FONTS)}"]
    if prop == "line-height":
        m = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if m and m.group(1) in LEADINGS:
            return [f"leading-{LEADINGS[m.group(1)]}"]
        return [f"leading-{_arb(value)}"]
    if prop == "letter-spacing":
        return [f"tracking-{_sized(value, TRACKINGS)}"]

    if prop == "border-radius":
        parts = _split_values(value)
        if len(parts) == 1:
            return [f"rounded-{_sized(value, RADII)}"]
        # Per-corner shorthand: top-left, top-right, bottom-right, bottom-left.
        corners = ["tl", "tr", "br", "bl"]
        if len(parts) == 2:
            vals = [parts[0], parts[1], parts[0], parts[1]]
        elif len(parts) == 3:
            vals = [parts[0], parts[1], parts[2], parts[1]]
        else:
            vals = parts[:4]
        return [f"rounded-{c}-{_sized(v, RADII)}" for c, v in zip(corners, vals)]
    if prop == "box-shadow":
        if value == "none":
            return ["shadow-none"]
        m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
        if m and m.group(1) in SHADOWS:
            ref = SHADOW_RENAMES.get(m.group(1), m.group(1))
            # shadow-sm would BAKE the literal at build time, and the student
            # skin re-declares --shadow-* under its scope with warmer, softer
            # values. Keeping the reference is what lets the scope reach it.
            # Colour and radius utilities need no such care: they already
            # compile to var().
            return [f"shadow-[var({ref})]"]
        return [f"shadow-{_arb(value)}"]
    if prop == "opacity":
        try:
            return [f"opacity-{round(float(value) * 100)}"]
        except ValueError:
            raise Unconvertible(f"{prop}: {value}")
    if prop == "z-index":
        return [f"z-{value}" if value.lstrip('-').isdigit() else f"z-{_arb(value)}"]

    if prop == "border":
        parts = _split_values(value)
        if value == "none" or value == "0":
            return ["border-0"]
        if len(parts) == 3:
            w, style, col = parts
            out = ["border" if w == "1px" else f"border-{_length(w) or _arb(w)}"]
            if style != "solid":
                out.append(f"border-{style}")
            out.append(f"border-{_colour(col, 'border')}")
            return out
        raise Unconvertible(f"{prop}: {value}")
    if prop in ("border-top", "border-right", "border-bottom", "border-left"):
        side = {"border-top": "t", "border-right": "r",
                "border-bottom": "b", "border-left": "l"}[prop]
        parts = _split_values(value)
        if value in ("none", "0"):
            return [f"border-{side}-0"]
        if len(parts) == 3:
            w, style, col = parts
            out = [f"border-{side}" if w == "1px" else f"border-{side}-{_length(w) or _arb(w)}"]
            if style != "solid":
                # NOT border-dashed: that sets the border-style SHORTHAND, so
                # all four sides become dashed. A side whose width was never
                # set then computes to `medium` — 3px of dashed border on
                # three sides that should have none. The chart gridlines grew
                # from 1px to 4px tall that way.
                full = {"t": "top", "r": "right", "b": "bottom", "l": "left"}[side]
                out.append(_arb(f"border-{full}-style:{style}"))
            # border-l-<colour>, not border-<colour>: the longhand colours one
            # edge. .doc-note sets a border all round and then a heavier left
            # edge in a different hue, and flattening the second would have
            # repainted all four.
            out.append(f"border-{side}-{_colour(col, 'border')}")
            return out
        raise Unconvertible(f"{prop}: {value}")
    if prop == "border-width":
        return ["border" if value == "1px" else f"border-{_length(value) or _arb(value)}"]

    if prop == "flex":
        return {"1": ["flex-1"], "auto": ["flex-auto"], "none": ["flex-none"],
                "1 1 auto": ["flex-auto"], "0 1 auto": ["flex-initial"]}.get(
            value, [f"flex-{_arb(value)}"])
    if prop == "flex-shrink":
        return ["shrink-0" if value == "0" else f"shrink-{value}"]
    if prop == "flex-grow":
        return ["grow" if value == "1" else f"grow-{value}"]
    if prop == "grid-template-columns":
        m = re.fullmatch(r"repeat\((\d+),\s*(?:minmax\(0,\s*)?1fr\)?\)", value)
        if m:
            return [f"grid-cols-{m.group(1)}"]
        return [f"grid-cols-{_arb(value)}"]

    if prop == "transition":
        if "," in value:
            # Several properties in one declaration. Splitting on whitespace
            # would strand a comma inside a duration and produce a utility
            # that silently does nothing.
            return [_arb(f"transition:{value}")]
        parts = _split_values(value)
        if value == "none":
            return ["transition-none"]
        base = {"all": "transition-all", "color": "transition-colors",
                "opacity": "transition-opacity", "transform": "transition-transform",
                "background-color": "transition-colors", "box-shadow": "transition-shadow"}
        out = [base.get(parts[0], "transition-all" if parts[0] == "all" else "transition")]
        for extra in parts[1:]:
            if re.fullmatch(r"[\d.]+m?s", extra) or "--dur" in extra:
                out.append(f"duration-{_arb(extra)}")
            elif "ease" in extra or "cubic-bezier" in extra or extra in ("linear",):
                out.append(f"ease-{_arb(extra)}")
        return out

    if prop == "align-self":
        return [{"center": "self-center", "flex-start": "self-start", "start": "self-start",
                 "flex-end": "self-end", "end": "self-end", "stretch": "self-stretch",
                 "baseline": "self-baseline", "auto": "self-auto"}[value]]

    if prop == "content":
        return [f"content-{_arb(value)}"]

    if prop == "outline":
        return ["outline-none"] if value in ("none", "0") else [f"outline-{_arb(value)}"]

    if prop == "animation":
        m = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if m and m.group(1).startswith("--animate-"):
            return [f"animate-{m.group(1)[len('--animate-'):]}"]
        return [f"animate-{_arb(value)}"]

    # Properties with no first-class utility. Tailwind's arbitrary-property
    # syntax expresses them verbatim, which is exactly what pixel fidelity
    # needs — the declaration survives unchanged, it just travels in the class
    # attribute. Anything NOT on this list still raises, so a property that
    # deserves a human decision gets one.
    if prop in ARBITRARY_PROPERTIES:
        return [_arb(f"{prop}:{value}")]

    raise Unconvertible(f"{prop}: {value}")


def decls_to_utilities(css: str) -> str:
    """Convert a declaration block to a space-separated utility string.

    Raises Unconvertible on the first declaration with no faithful form, so a
    caller always learns what needs doing by hand rather than losing it.
    """
    out = []
    for decl in css.split(";"):
        decl = decl.strip()
        if not decl:
            continue
        if ":" not in decl:
            raise Unconvertible(f"not a declaration: {decl!r}")
        prop, _, value = decl.partition(":")
        value = re.sub(r"\s*!important\s*$", "", value.strip())
        value = resolve_local(value)
        out.extend(_convert(prop, value))
    return " ".join(out)


if __name__ == "__main__":
    import sys
    print(decls_to_utilities(" ".join(sys.argv[1:])))
