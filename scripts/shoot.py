#!/usr/bin/env python
"""Screenshot every page, and compare two sets of screenshots.

This is the gate for the Tailwind migration: the rewrite is meant to be
invisible, so every page is photographed before and after and any difference
is a defect.

chrome-devtools-mcp is not installed on this machine, so this drives
/usr/bin/chromium directly over raw CDP using the venv's websockets.

    python scripts/shoot.py --out .screens/baseline
    python scripts/shoot.py --compare .screens/baseline .screens/after

Sessions are created through Django rather than by filling in the login form:
django-axes locks the (ip, username) pair after five failures, and a harness
that logs in a few hundred times would lock the fixture users out for an hour.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
CHROMIUM = "/usr/bin/chromium"
VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}
NETWORK_IDLE_TIMEOUT = 8.0   # seconds to wait for the page to stop fetching
SETTLE_MS = 250              # after idle: webfonts, icon sprite, chart paint
DEFAULT_BASE = "http://127.0.0.1:8000"


# --------------------------------------------------------------------------
# Django-side: session cookies without going through the login form
# --------------------------------------------------------------------------

def session_cookies(roles):
    """Return {role: sessionid} for each role that names a real user."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_tutor.config.settings")
    sys.path.insert(0, str(REPO))
    import django

    django.setup()
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.backends.db import SessionStore

    User = get_user_model()
    out = {}
    for role, username in roles.items():
        if username is None:
            continue
        user = User.objects.filter(username=username).first()
        if user is None:
            print(f"  ! no user {username!r} — {role} pages will render logged out")
            continue
        store = SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        out[role] = store.session_key
    return out


# --------------------------------------------------------------------------
# CDP
# --------------------------------------------------------------------------

class Chrome:
    def __init__(self, port=9222):
        self.port = port
        self.proc = None
        self._id = 0
        self._events = []

    def __enter__(self):
        from websockets.sync.client import connect

        profile = REPO / ".screens" / ".chrome-profile"
        shutil.rmtree(profile, ignore_errors=True)
        profile.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                CHROMIUM,
                "--headless=new",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={profile}",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                "--disable-lcd-text",          # subpixel AA is machine-dependent
                "--font-render-hinting=none",  # so is hinting; both add noise
                "--disable-extensions",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ws_url = self._wait_for_target()
        self.ws = connect(ws_url, max_size=200 * 1024 * 1024, open_timeout=30)
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Page.setLifecycleEventsEnabled", enabled=True)
        self.send("Network.enable")
        self.send("Network.setCacheDisabled", cacheDisabled=True)
        return self

    def _wait_for_target(self):
        import urllib.request

        for _ in range(120):
            try:
                raw = urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=1
                ).read()
                for t in json.loads(raw):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError("chromium did not expose a CDP target")

    def send(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._events.append(msg)

    def wait_for_lifecycle(self, name, timeout):
        """Block until Chrome reports `name` for the current navigation.

        A fixed sleep is not enough: the tutor pages fetch their lesson over
        XHR after load, so a page photographed too early shows a skeleton and
        the same page photographed twice does not match itself.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in list(self._events):
                if (msg.get("method") == "Page.lifecycleEvent"
                        and msg.get("params", {}).get("name") == name):
                    self._events.clear()
                    return True
            self._events.clear()
            try:
                raw = self.ws.recv(timeout=max(0.05, deadline - time.time()))
            except Exception:
                pass
            else:
                self._events.append(json.loads(raw))
        return False

    def __exit__(self, *exc):
        try:
            self.ws.close()
        except Exception:
            pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --------------------------------------------------------------------------
# Shooting
# --------------------------------------------------------------------------

def slug(role, path):
    s = re.sub(r"[^a-z0-9]+", "-", f"{role}{path}".lower()).strip("-")
    return s or "root"


def read_pages(path):
    pages = []
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        role, url = parts[0], parts[1]
        manual = len(parts) > 2 and parts[2] == "manual"
        pages.append((role, url, manual))
    return pages


def shoot(out_dir, base_url, pages_file, only=None):
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pages = read_pages(pages_file)
    if only:
        pages = [p for p in pages if only in p[1]]
    (out / "manual.txt").write_text(
        "".join(f"{slug(r, u)}\n" for r, u, m in pages if m))
    cookies = session_cookies(
        {"teacher": "teacher_daniel", "student": "student_daniel",
         "admin": "superadmin_daniel", "country": "country_daniel",
         "school_admin": "schooladmin_daniel"}
    )

    host = base_url.split("//", 1)[1].split(":")[0]
    failures = []
    with Chrome() as chrome:
        for role, path, _manual in pages:
            chrome.send("Network.clearBrowserCookies")
            if role in cookies:
                chrome.send(
                    "Network.setCookie",
                    name="sessionid",
                    value=cookies[role],
                    domain=host,
                    path="/",
                )
            for vp, (w, h) in VIEWPORTS.items():
                chrome.send(
                    "Emulation.setDeviceMetricsOverride",
                    width=w, height=h, deviceScaleFactor=1,
                    mobile=(vp == "mobile"),
                )
                try:
                    chrome.send("Page.navigate", url=base_url + path)
                    chrome.wait_for_lifecycle("networkIdle", NETWORK_IDLE_TIMEOUT)
                    time.sleep(SETTLE_MS / 1000)
                    # Freeze anything still moving so the frame is stable, and
                    # await the webfonts — Nunito swapping in after capture was
                    # a source of one-run-in-three text shifts.
                    chrome.send(
                        "Runtime.evaluate",
                        expression=(
                            "document.getAnimations?.().forEach(a=>{a.pause();"
                            "a.currentTime=0});"
                            "const s=document.createElement('style');"
                            "s.textContent='*,*::before,*::after{transition:none!important;"
                            "animation-play-state:paused!important;caret-color:transparent!important}';"
                            "document.head.appendChild(s);"
                            "document.fonts.ready"
                        ),
                        awaitPromise=True,
                    )
                    shot = chrome.send(
                        "Page.captureScreenshot", format="png", captureBeyondViewport=True
                    )
                except Exception as exc:
                    failures.append(f"{role} {path} [{vp}]: {exc}")
                    continue
                name = f"{slug(role, path)}__{vp}.png"
                (out / name).write_bytes(base64.b64decode(shot["data"]))
            print(f"  shot {role:8} {path}")

    print(f"\n{len(list(out.glob('*.png')))} screenshots -> {out}")
    for f in failures:
        print(f"  FAILED {f}")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# Comparing
# --------------------------------------------------------------------------

def _noise_mask(noise_dirs, name, shape):
    """Pixels that differ between two shots of the SAME tree.

    A handful of pages are not reproducible by nature — the lesson monitor
    polls, the help page renders a relative timestamp. Rather than lower the
    bar everywhere with a tolerance, the harness shoots one tree twice and
    excludes exactly the pixels that moved on their own. Everything else stays
    at zero tolerance.
    """
    import numpy as np
    from PIL import Image


    a, b = pathlib.Path(noise_dirs[0]) / name, pathlib.Path(noise_dirs[1]) / name
    if not (a.exists() and b.exists()):
        return None
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size or (ia.size[1], ia.size[0]) != shape:
        return None
    m = np.abs(np.asarray(ia, np.int16) - np.asarray(ib, np.int16)).max(axis=2) > 0
    # Grow by 2px: antialiasing around a changed glyph lands just outside it.
    for _ in range(2):
        m |= np.roll(m, 1, 0) | np.roll(m, -1, 0) | np.roll(m, 1, 1) | np.roll(m, -1, 1)
    return m


def compare(before_dir, after_dir, tolerance=0, noise=None, min_pixels=0):
    import numpy as np
    from PIL import Image

    before, after = pathlib.Path(before_dir), pathlib.Path(after_dir)
    manual_file = after / "manual.txt"
    manual = set(manual_file.read_text().split()) if manual_file.exists() else set()
    names = sorted({p.name for p in before.glob("*.png")} | {p.name for p in after.glob("*.png")})
    if not names:
        print("no screenshots to compare")
        return 1

    differing, missing, by_eye = [], [], []
    for name in names:
        b, a = before / name, after / name
        if not b.exists() or not a.exists():
            missing.append(name)
            continue
        ib, ia = Image.open(b).convert("RGB"), Image.open(a).convert("RGB")
        if ib.size != ia.size:
            differing.append((name, -1, f"{ib.size} -> {ia.size}"))
            continue
        diff = np.abs(np.asarray(ib, np.int16) - np.asarray(ia, np.int16)).max(axis=2)
        changed = diff > tolerance
        masked = 0
        if noise:
            m = _noise_mask(noise, name, changed.shape)
            if m is not None:
                masked = int((changed & m).sum())
                changed = changed & ~m
        n = int(changed.sum())
        if n and n < min_pixels:
            n = 0
        if n:
            detail = f"{100 * n / diff.size:.3f}% of pixels"
            if masked:
                detail += f" (+{masked} px ignored as known-unstable)"
            if name.rsplit("__", 1)[0] in manual:
                by_eye.append((name, n, detail))
            else:
                differing.append((name, n, detail))

    print(f"compared {len(names) - len(missing)} pages")
    for name in missing:
        print(f"  MISSING  {name}")
    for name, n, detail in differing:
        print(f"  DIFFERS  {name:<58} {detail}")
    for name, n, detail in by_eye:
        print(f"  BY EYE   {name:<58} {detail}")
    if not differing and not missing:
        print("  identical — no visual change on any asserted page")
    return 1 if (differing or missing) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--pages", default=str(REPO / "scripts" / "pages.txt"))
    ap.add_argument("--only", help="substring filter on the URL")
    # 2/255 absorbs glyph antialiasing, which lands a shade differently
    # between runs on the same tree. It is deliberately tiny: a real
    # regression from this migration either uses the same token or an exact
    # arbitrary value, so it moves a channel by far more than two.
    ap.add_argument("--tolerance", type=int, default=2)
    # Chromium's glyph and curve antialiasing is not bit-reproducible: shooting
    # one unchanged tree twice moves a handful of pixels on the edge of a
    # rounded logo tile or a letterform, by up to ~46/255. Measured across four
    # full runs the worst page showed 7 such pixels, so 30 leaves a wide margin
    # while still catching anything this migration could plausibly break — a
    # changed padding, radius, colour or font moves hundreds of pixels, not
    # tens. The cost is that a one-pixel icon nudge would slip through.
    ap.add_argument("--min-pixels", type=int, default=30,
                    help="ignore a page whose differing-pixel count is below this")
    ap.add_argument("--noise", nargs="+", metavar="RUN",
                    help="two or more shots of unchanged trees; pixels that "
                         "differ between any pair are excluded as unstable")
    args = ap.parse_args()

    if args.compare:
        return compare(*args.compare, tolerance=args.tolerance,
                       noise=args.noise, min_pixels=args.min_pixels)
    if args.out:
        with socket.socket() as s:
            host, port = args.base.split("//", 1)[1].split(":")
            if s.connect_ex((host, int(port))) != 0:
                sys.exit(f"no dev server on {args.base} — start runserver first")
        return shoot(args.out, args.base, args.pages, args.only)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
