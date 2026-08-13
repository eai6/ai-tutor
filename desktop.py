#!/usr/bin/env python3
"""AI Tutor desktop app — Linux, Windows, macOS.

    ./desktop.py                    # launch
    ./desktop.py --no-ollama        # assume Ollama is already running
    ./desktop.py --debug            # keep server logs on stderr, open devtools

A native window over the local Django app, with Ollama running the tutor
model. Nothing here talks to the internet.

Shape:

    desktop.py (pywebview, system webview)
      ├─ ollama serve            (subprocess, unless already running)
      └─ desktop_server.py       (subprocess, waitress on 127.0.0.1:<free>)

pywebview rather than Electron or Tauri: the Django app has to be frozen with
PyInstaller either way, so this adds no second toolchain, no Rust, and no Node.
The window is the platform's own webview — WKWebView, WebView2, WebKitGTK.

The splash is not decoration. Measured cold start is ~30 s before the first
token (model load) on top of migrations and any pack import, and a bare
spinner for that long reads as a hang — the instinct is to force quit
mid-write. So every step names itself and says whether it worked.

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

APP_TITLE = 'AI Tutor'
OLLAMA_HOST = os.environ.get('OLLAMA_HOST') or 'http://localhost:11434'

# Binary resolution and the tuned server environment live in one module shared
# with the provisioner — if the shell started one Ollama and the provisioner
# shelled out to another, a model would be installed into a store the running
# server cannot see. Imported without Django, which has not started yet.
sys.path.insert(0, str(ROOT))
from ai_tutor.apps.desktop.ollama_runtime import (  # noqa: E402
    resolve_binary as resolve_ollama, server_env as ollama_server_env,
)

SPLASH_HTML = """
<!doctype html><meta charset="utf-8">
<style>
  :root { color-scheme: light dark; }
  body { margin:0; height:100vh; display:flex; align-items:center;
         justify-content:center; font:15px/1.5 -apple-system,Segoe UI,Inter,
         system-ui,sans-serif; background:#faf9f7; color:#1c1b19; }
  @media (prefers-color-scheme: dark){ body{background:#17161a;color:#ece9e6} }
  .card { width:340px; }
  h1 { font-size:19px; margin:0 0 2px; font-weight:600; letter-spacing:-.01em; }
  .sub { opacity:.6; font-size:13px; margin-bottom:22px; }
  li { list-style:none; display:flex; gap:10px; padding:6px 0; font-size:13.5px;
       align-items:baseline; }
  ul { padding:0; margin:0; }
  .dot { width:8px; height:8px; border-radius:50%; background:#c9c6c1;
         flex:0 0 8px; transform:translateY(-1px); }
  .ok .dot { background:#2f9e5f; }
  .bad .dot { background:#d1493f; }
  .label { flex:0 0 118px; }
  .detail { opacity:.55; font-size:12.5px; }
  .bad .detail { opacity:.9; color:#d1493f; }
  .err { margin-top:18px; font-size:12.5px; opacity:.75; }
</style>
<div class="card">
  <h1>AI Tutor</h1>
  <div class="sub" id="sub">Starting…</div>
  <ul id="checks"></ul>
  <div class="err" id="err"></div>
</div>
<script>
  window.renderStatus = function (data) {
    document.getElementById('sub').textContent =
      data.ready ? 'Ready' : 'Getting things ready…';
    document.getElementById('checks').innerHTML = (data.checks || []).map(function (c) {
      return '<li class="' + (c.ok ? 'ok' : 'bad') + '"><span class="dot"></span>' +
             '<span class="label">' + c.label + '</span>' +
             '<span class="detail">' + (c.detail || '') + '</span></li>';
    }).join('');
    document.getElementById('err').textContent = data.message || '';
  };
</script>
"""


def log(message: str) -> None:
    print(f'[desktop] {message}', file=sys.stderr, flush=True)


class JsApi:
    """Functions the setup page can call from JavaScript.

    Only one so far, and it earns its place: picking the model means picking a
    ~2.5 GB file. An <input type="file"> would make the webview read the whole
    thing and POST it to a server on the same machine — gigabytes of pointless
    copying, and a memory spike, to tell a local process about a local path.
    The native dialog returns the path; the server opens it in place.
    """

    def __init__(self):
        self._window = None

    def bind(self, window):
        self._window = window

    def pick_path(self, kind: str = 'model'):
        import webview
        if self._window is None:
            return None
        # FOLDER, not OPEN: on a setup drive the user is looking at a folder
        # (`models/`, or the drive itself), and picking the folder lets the
        # server find the .gguf or the content pack inside it. Typing a path
        # to a specific file still works — the server accepts either.
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result


def ollama_running() -> bool:
    try:
        with urllib.request.urlopen(f'{OLLAMA_HOST}/api/version', timeout=3):
            return True
    except Exception:
        return False


class Runtime:
    """Owns the two child processes and shuts them down exactly once."""

    def __init__(self, start_ollama: bool, debug: bool):
        self.start_ollama = start_ollama
        self.debug = debug
        self.ollama: subprocess.Popen | None = None
        self.server: subprocess.Popen | None = None
        self.base_url: str | None = None
        self._stopped = False

    # ── startup ──────────────────────────────────────────────────────
    def launch_ollama(self) -> None:
        if not self.start_ollama or ollama_running():
            return
        binary = resolve_ollama()
        if binary is None:
            raise RuntimeError(
                'No Ollama binary found. A packaged build ships one in '
                'vendor/ollama/<platform>/; from source, run '
                '`python manage.py stage_ollama` or install Ollama.')
        log(f'starting ollama serve ({binary})')
        self.ollama = subprocess.Popen(
            [binary, 'serve'], env=ollama_server_env(),
            stdout=subprocess.DEVNULL,
            stderr=None if self.debug else subprocess.DEVNULL,
        )
        for _ in range(60):                     # up to ~30 s
            if ollama_running():
                return
            time.sleep(0.5)
        raise RuntimeError('Ollama did not become reachable within 30s')

    def launch_server(self) -> str:
        """Start the Django server and block until it reports its port.

        Reads the port from the child's stdout rather than assigning one here,
        so there is no window where we think it is listening and it is not.
        """
        # Frozen builds have no interpreter to hand a script to: sys.executable
        # IS this application, so spawning it with a .py path would just start a
        # second GUI. The frozen binary therefore re-invokes itself with
        # --serve, which main() dispatches to the server before any window is
        # created. From source, run the script directly.
        if getattr(sys, 'frozen', False):
            argv = [sys.executable, '--serve']
        else:
            argv = [sys.executable, str(ROOT / 'desktop_server.py')]
        log(f'starting desktop_server ({"frozen" if getattr(sys, "frozen", False) else "source"})')
        self.server = subprocess.Popen(
            argv,
            cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=None if self.debug else subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            line = self.server.stdout.readline()
            if not line:
                if self.server.poll() is not None:
                    raise RuntimeError(
                        f'server exited with code {self.server.returncode} '
                        f'before listening (run with --debug to see why)'
                    )
                continue
            line = line.strip()
            if not line.startswith('{'):
                continue
            payload = json.loads(line)
            if payload.get('event') == 'listening':
                self.base_url = f"http://127.0.0.1:{payload['port']}"
                log(f'server listening on {self.base_url}')
                return payload['url']
        raise RuntimeError('server did not report a port within 90s')

    def wait_until_serving(self, timeout: float = 30.0) -> None:
        """Block until the server accepts a TCP connection.

        Belt to the server's braces. The server now binds before announcing
        its port, so this should return on the first attempt — but a first
        launch also runs migrations and collectstatic, and a shell that dies on
        one refused connection turns a slow start into "startup failed".
        """
        import socket
        deadline = time.time() + timeout
        host, port = '127.0.0.1', int(self.base_url.rsplit(':', 1)[1])
        last = None
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=2):
                    return
            except OSError as exc:
                last = exc
                if self.server is not None and self.server.poll() is not None:
                    raise RuntimeError(
                        f'server exited with code {self.server.returncode} '
                        f'while starting up') from exc
                time.sleep(0.25)
        raise RuntimeError(f'server never accepted a connection: {last}')

    def poll_status(self) -> dict:
        assert self.base_url
        with urllib.request.urlopen(f'{self.base_url}/desktop/health/', timeout=10) as resp:
            return json.loads(resp.read())

    # ── shutdown ─────────────────────────────────────────────────────
    def stop(self) -> None:
        # Idempotent: pywebview can fire the closing handler and the atexit
        # path both, and terminating a reaped process raises on Windows.
        if self._stopped:
            return
        self._stopped = True
        for name, proc in (('server', self.server), ('ollama', self.ollama)):
            if proc is None or proc.poll() is not None:
                continue
            log(f'stopping {name}')
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception as exc:                     # noqa: BLE001
                log(f'could not stop {name}: {exc}')


def main() -> int:
    # Server dispatch, before argparse and before any GUI import. A frozen
    # build is one binary playing two roles; this is the fork in the road.
    # Checked by hand rather than through argparse so the server's own flags
    # (--port, --probe) pass straight through untouched.
    if '--serve' in sys.argv:
        sys.argv.remove('--serve')
        import desktop_server
        return desktop_server.main()

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--no-ollama', action='store_true',
                        help='Do not start Ollama; assume it is already running.')
    parser.add_argument('--debug', action='store_true',
                        help='Show child process logs and open devtools.')
    parser.add_argument('--url-only', action='store_true',
                        help='Start the runtime, print the URL, and wait. No '
                             'window — for testing the stack headlessly.')
    args = parser.parse_args()

    runtime = Runtime(start_ollama=not args.no_ollama, debug=args.debug)

    if args.url_only:
        try:
            runtime.launch_ollama()
            app_url = runtime.launch_server()
            runtime.wait_until_serving()
            print(json.dumps({'url': app_url, 'status': runtime.poll_status()}),
                  flush=True)
            # threading.Event().wait(), not signal.pause(): the latter does not
            # exist on Windows and raises AttributeError, so --url-only would
            # die immediately there while working fine on macOS and Linux.
            # Interruptible by Ctrl-C on every platform.
            threading.Event().wait()
        finally:
            runtime.stop()
        return 0

    import webview

    api = JsApi()
    window = webview.create_window(
        APP_TITLE, html=SPLASH_HTML, width=1100, height=780,
        min_size=(820, 600), background_color='#faf9f7', js_api=api,
    )
    api.bind(window)
    window.events.closed += runtime.stop

    def boot():
        """Bring the stack up, then swap the splash for the app."""
        try:
            runtime.launch_ollama()
            app_url = runtime.launch_server()
            runtime.wait_until_serving()

            # Poll until every blocking check passes. Unblocking failures
            # (empty KB, wrong embedding backend) are shown but do not stop
            # the app — the tutor still works, just degraded, and saying so
            # beats refusing to start.
            deadline = time.time() + 120
            status = {}
            while time.time() < deadline:
                status = runtime.poll_status()
                window.evaluate_js(
                    'window.renderStatus(' + json.dumps(status) + ')')
                if status.get('ready'):
                    break
                time.sleep(1.0)

            if not status.get('ready'):
                # An unprovisioned install is the normal first launch, not an
                # error: no model, no lessons. Send the user somewhere they can
                # fix it rather than showing a dead splash with red dots.
                log(f'not ready ({status.get("blocking_failures")}) — opening setup')
                window.load_url(f'{runtime.base_url}/desktop/setup/')
                return

            window.load_url(app_url)
        except Exception as exc:                          # noqa: BLE001
            log(f'startup failed: {exc}')
            payload = {'ready': False, 'checks': [],
                       'message': f'Could not start: {exc}'}
            try:
                window.evaluate_js(
                    'window.renderStatus(' + json.dumps(payload) + ')')
            except Exception:
                pass

    threading.Thread(target=boot, daemon=True).start()
    try:
        webview.start(debug=args.debug)
    finally:
        runtime.stop()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
