#!/usr/bin/env python
"""Convert every template, using the stylesheets its own shell loaded.

    python scripts/convert.py

Replaces the single global pass. `.card` is defined in
dashboard/components/surfaces.css AND in student/components.css with
different rules, and each surface only ever loaded one of them; a merged map
hands dashboard pages the student definition. Forty classes collide that way.

Order: restore the pre-migration templates and stylesheets, convert each
surface against its own sheets, delete every sheet, then run the passes that
have to see the result (dead-variable renames, the handful of rules no map
can express, stale <link> removal), and build.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scripts import apply_map  # noqa: E402
from scripts.surfaces import BASE_COMMIT, groups  # noqa: E402

CSS = pathlib.Path("ai_tutor/static/css")
TEMPLATES = pathlib.Path("ai_tutor/templates")


def sh(*args, **kw):
    return subprocess.run(args, check=kw.pop("check", True), **kw)


def restore():
    sh("git", "checkout", BASE_COMMIT, "--", "ai_tutor/templates", "ai_tutor/static/css")
    sh("git", "checkout", "HEAD", "--", "ai_tutor/static/css/app.build.css")


def main():
    restore()
    by_sheets = groups()

    for sheets, tpls in sorted(by_sheets.items(), key=lambda kv: -len(kv[1])):
        present = [str(CSS.parent / s) for s in sheets if (CSS.parent / s).exists()]
        if not present or not tpls:
            continue
        print(f"\n=== {len(tpls)} templates <- "
              f"{', '.join(s.split('/')[-1] for s in sheets)}")
        apply_map.run(present, [str(TEMPLATES / t) for t in tpls], apply=True)

    # Every sheet goes; app.build.css is the only one left.
    for p in sorted(CSS.rglob("*.css")):
        if p.name != "app.build.css" and "vendor" not in p.as_posix():
            p.unlink()
    for d in sorted(CSS.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    sh(sys.executable, "scripts/inline_styles.py", "--apply")
    sh(sys.executable, "scripts/fixups.py")
    sh("npm", "run", "css", stdout=subprocess.DEVNULL)
    sh(sys.executable, "manage.py", "collectstatic", "--noinput",
       stdout=subprocess.DEVNULL,
       env={**__import__("os").environ, "DJANGO_SETTINGS_MODULE": "ai_tutor.config.settings"})
    print("\nconverted per surface")


if __name__ == "__main__":
    main()
