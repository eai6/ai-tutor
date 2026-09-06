#!/usr/bin/env python
"""The handful of rules no class map could express, applied to the markup.

Each one is here because a CSS selector reached across elements in a way a
variant on a single class cannot reproduce, or because a stylesheet the
migration deletes was the only thing linking a page to its styles.
"""

import pathlib
import re

TEMPLATES = pathlib.Path("ai_tutor/templates")
LINK = "    <link rel=\"stylesheet\" href=\"{% static 'css/app.build.css' %}\">\n"

# tr:hover .row-actions .btn — the ancestor is a <tr>, not a class, so the
# condition has to be written out in full on .row-actions itself.
ROW_ACTIONS = ("[tr:hover_&_.btn]:text-text [tr:hover_&_.btn]:border-border-strong "
               "[tr:hover_&_.btn]:bg-surface")


def edit(rel, fn):
    p = TEMPLATES / rel
    s = p.read_text()
    new = fn(s)
    if new != s:
        p.write_text(new)
        return True
    return False


def add_class(src, marker, extra):
    def fix(m):
        cls = m.group(1)
        if marker not in cls.split() or extra.split()[0] in cls:
            return m.group(0)
        return f'class="{cls} {extra}"'
    return re.sub(r'class="([^"]*)"', fix, src)


# The per-institution theme block sets the token names the stylesheets read.
# The theme renamed them, so the block has to follow: a school's brand colour
# written to --primary reaches nothing once the utilities read
# --color-primary. This is the one thing the "v4 not v3" decision existed to
# protect, so it is not allowed to rot.
THEME_RENAMES = {
    "dashboard/base.html": [
        ("--primary:", "--color-primary:"),
        ("--primary-fill:", "--color-primary-fill:"),
        ("--primary-dark:", "--color-primary-dark:"),
        ("--primary-light:", "--color-primary-light:"),
        ("--primary-ink:", "--color-primary-ink:"),
    ],
}


def theme_block(src, pairs):
    for old, new in pairs:
        src = src.replace(f"            {old}", f"            {new}")
    return src


# Every shell that injects the institution's brand colour. Three of them write
# --coral*, one writes --primary*, and all four were writing names the theme
# no longer uses. Renaming in place is enough: nothing else reads the old
# spelling once the stylesheets are gone.
CORAL_SHELLS = ["base.html", "docs/base.html", "accounts/landing.html"]


def coral_theme_block(src):
    """Rename --coral* to --color-coral* inside the DB-driven theme block."""
    if "--color-coral" in src:
        return src
    return re.sub(r"(\n\s+)--coral(-(?:fill|ink|tint))?:", r"\1--color-coral\2:", src)


# Every shell that used to load css/student/brand.css. The skin now lives in
# app.css scoped to [data-surface="student"], so the scope has to be declared
# where the stylesheet used to be linked — including the marketing and
# documentation shells, which have always worn the student skin.
STUDENT_SURFACES = [
    "base.html", "docs/base.html", "accounts/landing.html",
    "downloads/index.html", "downloads/self_hosting.html",
    "desktop/setup.html", "desktop/server.html",
]


def mark_student_surface(src):
    if 'data-surface="student"' in src:
        return src
    # On <html>, not <body>: the scope has to cover the DB-driven theme block
    # in <head> as well as the page.
    return re.sub(r"<html(?![^>]*data-surface)", '<html data-surface="student"',
                  src, count=1)


# css/student/shell.css styled `body` directly — an element selector, which no
# class map can carry. It is the only rule that made the student app 16px
# rather than the dashboard's 14px, and losing it shrank every student page.
STUDENT_BODY = ("font-body text-md leading-normal text-text bg-canvas "
                "min-h-screen [min-height:100dvh] "
                "[-webkit-font-smoothing:antialiased]")


def student_body(src):
    if STUDENT_BODY.split()[0] in src.split("<body", 1)[-1][:400]:
        return src
    return re.sub(r"<body\b(?![^>]*\bclass=)", f'<body class="{STUDENT_BODY}"', src, count=1)


# The auth shell picks its photograph by building a class name at request
# time — auth-art--{% block auth_art %}student{% endblock %}. Tailwind's
# scanner never sees the result, so both photographs went missing and the
# panel rendered as an empty gradient. The block now carries the whole
# utility rather than the half of a class name.
AUTH_ART = {
    "student": "[background-image:url('../img/marketing/auth-students.webp')]",
    "teacher": "[background-image:url('../img/marketing/auth-teacher.webp')]",
}


def auth_shell(src):
    return src.replace(
        "auth-art auth-art--{% block auth_art %}student{% endblock %}",
        "auth-art {% block auth_art %}" + AUTH_ART["student"] + "{% endblock %}")


def auth_page(src):
    for who, util in AUTH_ART.items():
        src = src.replace("{% block auth_art %}" + who + "{% endblock %}",
                          "{% block auth_art %}" + util + "{% endblock %}")
    return src


# {% block page_class %} builds a modifier on <main> at request time, so the
# scanner never sees page--auth or page--wide and both pages lost their
# override: the auth split was squeezed into the 62rem reading column instead
# of running full-bleed. The `!` is deliberate — the base utility it overrides
# is also an arbitrary max-width, and which of two equal-specificity utilities
# wins would otherwise depend on the order Tailwind happens to emit them in.
PAGE_CLASS = {
    "accounts/_auth_shell.html": ("page--auth", "max-w-none! p-0!"),
    "accounts/terms.html": ("page--wide", "max-w-[76rem]!"),
}


def page_class(src, old, new):
    return src.replace("{% block page_class %}" + old + "{% endblock %}",
                       "{% block page_class %}" + new + "{% endblock %}")


def main():
    done = []

    for rel, (old, new) in PAGE_CLASS.items():
        if edit(rel, lambda s, o=old, n=new: page_class(s, o, n)):
            done.append(f"page modifier on {rel}")

    if edit("accounts/_auth_shell.html", auth_shell):
        done.append("auth photograph on _auth_shell.html")
    for rel in ["accounts/staff_login.html", "accounts/student_register.html",
                "accounts/register.html", "accounts/staff_self_register.html",
                "accounts/staff_register.html", "accounts/login.html",
                "accounts/student_login.html"]:
        if edit(rel, auth_page):
            done.append(f"auth photograph on {rel}")

    if edit("base.html", student_body):
        done.append("student body defaults on base.html")

    for rel in STUDENT_SURFACES:
        if edit(rel, mark_student_surface):
            done.append(f"student skin scope on {rel}")

    for rel, pairs in THEME_RENAMES.items():
        if edit(rel, lambda s, p=pairs: theme_block(s, p)):
            done.append(f"institution theme names in {rel}")
    for rel in CORAL_SHELLS:
        if edit(rel, coral_theme_block):
            done.append(f"institution theme names in {rel}")

    # .lp-section.lp-band--deploy sits later in the sheet than either class
    # alone, so its background-position and padding win over both.
    def deploy(src):
        def fix(m):
            cls = m.group(1)
            if "lp-band--deploy" not in cls:
                return m.group(0)
            cls = cls.replace("[background-position:center]", "[background-position:center_top]")
            cls = re.sub(r"(?<![\w-])py-20(?![\w-])", "py-20 max-xl:py-12", cls)
            return f'class="{cls}"'
        return re.sub(r'class="([^"]*)"', fix, src)
    if edit("accounts/landing.html", deploy):
        done.append("lp-band--deploy")

    for rel in sorted(p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.html")):
        if edit(rel, lambda s: add_class(s, "row-actions", ROW_ACTIONS)):
            done.append(f"row-actions in {rel}")

    # Standalone marketing documents never had the stylesheet link: they do not
    # extend a base, so Task 3 never reached them.
    for rel in ["accounts/landing.html", "downloads/index.html", "downloads/self_hosting.html"]:
        def link(s):
            if "app.build.css" in s:
                return s
            links = list(re.finditer(r'^[ \t]*<link rel="stylesheet"[^>]*>\n', s, re.M))
            at = links[-1].end() if links else re.search(
                r'^[ \t]*<title>.*?</title>\n', s, re.M | re.S).end()
            return s[:at] + LINK + s[at:]
        if edit(rel, link):
            done.append(f"stylesheet link in {rel}")

    # Every document links the built stylesheet. This is not inherited from
    # the restore point — that commit predates the link — so it is asserted
    # here: any template with a <head> of its own, except the HTML email.
    def ensure_link(src):
        if "app.build.css" in src or "<head" not in src:
            return src
        links = list(re.finditer(r'^[ \t]*<link rel="stylesheet"[^>]*>\n', src, re.M))
        if links:
            at = links[-1].end()
        else:
            m = re.search(r"^[ \t]*<title>.*?</title>\n", src, re.M | re.S)
            if not m:
                return src
            at = m.end()
        return src[:at] + LINK + src[at:]

    for rel in sorted(p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.html")):
        if rel.startswith("email/"):
            continue
        if edit(rel, ensure_link):
            done.append(f"stylesheet link in {rel}")

    # Any <link> whose stylesheet no longer exists. Removing them by name per
    # phase missed dashboard/home.html, which carries its own page sheet in a
    # block rather than in the shell — and a stale link is not cosmetic: the
    # manifest storage raises on it and every page 500s.
    static = pathlib.Path("ai_tutor/static")
    dead = re.compile(r"^[ \t]*<link[^>]*\{% static '(css/[^']+)' %\}[^>]*>\n", re.M)

    def drop_dead(src):
        return dead.sub(lambda m: "" if not (static / m.group(1)).exists() else m.group(0), src)

    for rel in sorted(p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.html")):
        if edit(rel, drop_dead):
            done.append(f"stale stylesheet link in {rel}")

    for d in done:
        print(f"  fixup: {d}")


if __name__ == "__main__":
    main()
