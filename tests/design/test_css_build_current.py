"""The committed stylesheet must be what the source compiles to.

app.build.css is committed so that the Docker image, the Python wheel and the
frozen desktop build stay Python-only — none of them needs Node. The price of
that is drift, and this is what pays it: if someone edits app.css and forgets
to rebuild, the tree is inconsistent and this says so.
"""

import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "ai_tutor" / "static_src" / "app.css"
BUILT = REPO / "ai_tutor" / "static" / "css" / "app.build.css"


@pytest.mark.skipif(shutil.which("npx") is None, reason="Node absent; a fresh checkout still runs")
@pytest.mark.skipif(not (REPO / "node_modules").exists(), reason="npm install has not been run")
def test_the_committed_stylesheet_matches_its_source(tmp_path):
    out = tmp_path / "rebuilt.css"
    subprocess.run(
        ["npx", "tailwindcss", "-i", str(SRC), "-o", str(out), "--minify"],
        cwd=REPO, check=True, capture_output=True,
    )
    assert out.read_bytes() == BUILT.read_bytes(), (
        "app.build.css is stale — run `npm run css` and commit the result"
    )
