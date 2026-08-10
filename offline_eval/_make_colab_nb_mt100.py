"""Generate offline_eval/colab_mt100_qwen.ipynb — Colab notebook that runs the
MULTI-TURN eval for all five local Qwen tutor arms on the representative-100
multi-turn scenarios (tag `v2`), the OSS leg of the mt100 board.

Purpose: score every Jetson-deployable Qwen size the offline tutor could ship
on — 2b/4b/8b/27b/30b-a3b — against the same 100-scenario subset the cloud
arms (Task 5's API model list) are scored on, so the board is one apples-to-
apples leaderboard across both legs.

Three deliberate choices, so the numbers mean something:

  1. **All five arms, Modelfile-pinned tags only.** Every arm ships from
     `infra/ollama/Modelfile.<tag>` (num_ctx/template baked in per
     apps/llm/model_profiles.py — see the CRITICAL note below). Bare
     registry tags were deprecated repo-wide (b5b7a68) and are NOT used here:
     the Tier-2 verifier reaches Ollama through /v1/chat/completions where
     num_ctx cannot be pinned, so a bare tag's 4096-default runner evicts the
     tutor on every graded turn. run_matrix.sh builds each tag via
     `ollama create <tag> -f infra/ollama/Modelfile.<tag>` — it detects the
     Modelfile automatically and never falls back to `ollama pull <tag>` for
     these five.
  2. **The representative-100 multi-turn scenarios** (`--subset v2`): Task 1
     selected 100 scenarios as a representative draw over the full dataset;
     Task 2 tagged them `v2`. No `--sample` cap — the subset IS the 100.
  3. **CRITICAL — exact profile keys only.** `apps/llm/model_profiles.py::
     get_model_profile` falls through to a generic `r"qwen3"` regex when an
     exact `MODEL_PROFILES` key is missing. That fallback returns a CLOUD
     profile with `num_ctx=None`, which the client then sizes to 24192 — the
     exact value that OOMed an 8 GB box in an earlier eval arm. The five tags
     below MUST be spelled exactly as they appear in `MODEL_PROFILES`
     (apps/llm/model_profiles.py) and MUST each have a matching
     `infra/ollama/Modelfile.<tag>`; a single character off silently runs the
     arm with the wrong context size instead of failing loudly.

Tutor = the local qwen under test; student-sim + rubric judge = Anthropic.

Run: python offline_eval/_make_colab_nb_mt100.py
"""
import json
import os
from pathlib import Path

REPO = 'eai6/ai-tutor'
BRANCH = os.environ.get('MT100_BRANCH', 'offline-harness-copy')  # push before running
DRIVE_FOLDER = 'ai-tutor-eval-multiturn'
# Env overrides let one generator emit sibling notebooks that can run
# CONCURRENTLY in separate Colab tabs: each sweep gets its own Drive folder
# (same folder = clobbered logs + broken resume-skip) and its own .ipynb.
SWEEP = os.environ.get('MT100_SWEEP', 'mt100')
OUT_NAME = os.environ.get('MT100_OUT', 'colab_mt100_qwen.ipynb')
RESULTS = f'offline_eval/multi_turn_results/{SWEEP}'
# No --sample cap: the v2 subset IS the 100 — every arm sees all 100.
MODE = os.environ.get('MT100_MODE', '--multi-turn --subset v2')

# The five arms under test (tag tier, run_matrix.sh shape). Every tag is
# Modelfile-pinned — see infra/ollama/Modelfile.<tag> for each.
QWEN_MODELS = os.environ.get('MT100_MODELS') or """\
# mt100 OSS leg — five Qwen sizes, every one in INSTRUCT mode.
# 2b/8b/27b are hybrid templates with think suppressed via the profile's
# ollama_think=False; 4b and 30b are instruct CHECKPOINTS with nothing to
# suppress. Do not ""fix"" the asymmetry — see the Modelfiles.
local_ollama/qwen3.5-2b-jetson       qwen3.5-2b-jetson
local_ollama/qwen3-4b-jetson         qwen3-4b-jetson
local_ollama/qwen3-8b-jetson         qwen3-8b-jetson
local_ollama/qwen3.6-27b-instruct    qwen3.6-27b-instruct
local_ollama/qwen3-30b-a3b-jetson    qwen3-30b-a3b-jetson
"""

# Sanity at generation time: the v2 tag must select exactly 100 multi-turn
# scenarios, and every model's Modelfile must exist.
import re as _re

_ROOT = Path(__file__).resolve().parents[1]
_v2_mt = 0
for _f in (_ROOT / 'evals' / 'dataset').rglob('*.yaml'):
    _t = _f.read_text()
    if _re.search(r'(?m)^tags: \[.*\bv2\]', _t) and 'mode: multi_turn' in _t:
        _v2_mt += 1
assert _v2_mt == 100, f'expected 100 v2 multi-turn scenarios, found {_v2_mt}'
_ARM_TAGS = []
for _line in QWEN_MODELS.strip().splitlines():
    if not _line.strip() or _line.strip().startswith('#'):
        continue
    _spec, _tag = _line.split()[0], _line.split()[1]
    assert _spec.startswith('local_ollama/'), f'unexpected spec format: {_spec}'
    _mf = _ROOT / 'infra' / 'ollama' / f'Modelfile.{_tag}'
    assert _mf.exists(), f'missing {_mf}'
    _ARM_TAGS.append(_tag)
assert len(_ARM_TAGS) == 5, f'expected 5 qwen arms, found {len(_ARM_TAGS)}'

cells = []


def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})


def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(rf"""
# AI Tutor — **mt100** · Qwen leg · five local arms · representative 100 (`v2`)

Runs the five Jetson-deployable Qwen tutor sizes against the representative-100
multi-turn scenarios (tag `v2`, selected by Task 1, no sampling). This is the
OSS leg of the mt100 board — the cloud leg (Task 5's model list) is scored on
the same 100 scenarios so both legs sit on one leaderboard.

| tag | note |
| --- | --- |
| `qwen3.5-2b-jetson` | hybrid template, think suppressed (`ollama_think=False`) |
| `qwen3-4b-jetson` | instruct checkpoint, nothing to suppress |
| `qwen3-8b-jetson` | hybrid template, think suppressed |
| `qwen3.6-27b-instruct` | hybrid template, think suppressed, `num_ctx=32768` |
| `qwen3-30b-a3b-jetson` | instruct checkpoint, nothing to suppress |

- **Scenarios:** `{MODE}` — the representative 100 multi-turn scenarios
  (tag `v2`), every model sees all 100. No sampling — the subset IS the 100.
- **Tags are Modelfile-pinned, never bare.** run_matrix.sh builds each via
  `ollama create <tag> -f infra/ollama/Modelfile.<tag>` (never `ollama pull`
  a bare tag) — see the module docstring in `_make_colab_nb_mt100.py` for why
  a bare tag silently confounds the run (falls through `get_model_profile`'s
  generic `qwen3` regex to a `num_ctx=24192` CLOUD profile).
- **Tutor** = the local qwen under test; **student-sim + rubric judge** =
  Anthropic (needs `ANTHROPIC_API_KEY`).
- **Branch `{BRANCH}`** must be pushed and must carry the mt100 profiles/
  Modelfiles — Cell 2 prints HEAD; check it.

**Before you start**
1. Runtime → **Change runtime type** → **T4 GPU** (run_matrix frees weights between models; the 30b-a3b arm is the largest download).
2. Colab Secrets (🔑 sidebar), *Notebook access ON*:
   - `GH_TOKEN` — GitHub classic PAT, `repo` scope (collaborator on `{REPO}`).
   - `ANTHROPIC_API_KEY` — **required** (student-sim + rubric judge).
   - `GOOGLE_API_KEY`, `OPENAI_API_KEY` — grader/judge fallback cascade.
""")

md("## Cell 1 — GPU + mount Drive")
code(r"""
!nvidia-smi -L
from google.colab import drive
drive.mount('/content/drive')
""")

md(f"## Cell 2 — clone the repo (branch `{BRANCH}` — carries the mt100 profiles/Modelfiles)")
code(rf"""
from google.colab import userdata
import subprocess, os
tok = (userdata.get('GH_TOKEN') or '').strip()
assert tok and ' ' not in tok, "GH_TOKEN missing or contains a space — re-save the secret"
url = f"https://{{tok}}@github.com/{REPO}.git"
subprocess.run(['rm', '-rf', '/content/ai-tutor'], check=True)
subprocess.run(['git', 'clone', '--depth', '1', '-b', '{BRANCH}', url, '/content/ai-tutor'], check=True)
os.chdir('/content/ai-tutor')
print('cloned at', os.getcwd())
print('HEAD:', subprocess.run(['git','log','-1','--oneline'],capture_output=True,text=True).stdout.strip())
""")

md("## Cell 3 — fix hardcoded laptop paths")
code(r"""
!sed -i 's#/home/daniel/Documents/work/Nyansapo/web/ai-tutor#/content/ai-tutor#g; s#\$ROOT/venv/bin/python#python#g; s#venv/bin/python#python#g' offline_eval/*.py offline_eval/*.sh
""")

md("## Cell 4 — install deps + start Ollama\n"
   "Version-pinned: tool-call parsing is Ollama-version-sensitive "
   "(memory: ollama-sweep-gotchas).")
code(r"""
!pip install -q -r requirements.txt
# zstd is REQUIRED: Ollama's Linux artifact is now .tar.zst — install.sh pipes
# through zstd and the legacy .tgz URL 404s for pinned versions (verified
# 2026-08-03: ollama-linux-amd64.tgz?version=0.30.7 -> 404, .tar.zst -> 200).
!apt-get -qq install -y zstd || (apt-get -qq update && apt-get -qq install -y zstd)
import subprocess, time, shutil, os
OLLAMA_VERSION = '0.30.7'
def _has_ollama(): return shutil.which('ollama') is not None
def _install_ollama():
    if _has_ollama(): return True
    for i in range(1, 4):
        print(f'[ollama] pinned install attempt {i}', flush=True)
        subprocess.run(f'curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION={OLLAMA_VERSION} sh', shell=True)
        if _has_ollama(): return True
        time.sleep(5)
    for i in range(1, 4):
        print(f'[ollama] direct-binary attempt {i}', flush=True)
        r = subprocess.run(f'curl -fL --retry 5 --retry-all-errors --connect-timeout 30 '
                           f'-o /tmp/ollama.tar.zst "https://ollama.com/download/ollama-linux-amd64.tar.zst?version={OLLAMA_VERSION}"',
                           shell=True)
        if r.returncode != 0:
            print(f'[ollama] curl exited {r.returncode}', flush=True)
        if os.path.exists('/tmp/ollama.tar.zst') and os.path.getsize('/tmp/ollama.tar.zst') > 1_000_000:
            subprocess.run('tar --zstd -C /usr -xf /tmp/ollama.tar.zst', shell=True)
            if _has_ollama(): return True
        time.sleep(5)
    return False
assert _install_ollama(), "ollama install failed — Runtime -> Disconnect and delete runtime, then retry."
print(subprocess.run(['ollama','--version'],capture_output=True,text=True).stdout.strip())
subprocess.Popen(['ollama', 'serve'], stdout=open('/content/ollama.log', 'w'), stderr=subprocess.STDOUT)
for _ in range(30):
    if subprocess.run(['bash','-c','ollama list'], capture_output=True).returncode == 0:
        print('ollama ready'); break
    time.sleep(2)
else:
    print('ollama NOT ready — check /content/ollama.log')
""")

md("## Cell 5 — write `.env` from Colab Secrets")
code(r"""
from google.colab import userdata
open('.env', 'w').write(
    "SECRET_KEY=colab-eval\nDEBUG=True\nEMBEDDING_BACKEND=sqlite\n"
    f"ANTHROPIC_API_KEY={userdata.get('ANTHROPIC_API_KEY')}\n"
    f"GOOGLE_API_KEY={userdata.get('GOOGLE_API_KEY')}\n"
    f"OPENAI_API_KEY={userdata.get('OPENAI_API_KEY')}\n")
print('.env written')
""")

md("## Cell 6 — fresh DB + eval fixtures")
code(r"""
!python manage.py migrate
!python manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json
""")

md("## Cell 7 — persist results to Drive (symlink → survives disconnects)\n"
   f"Symlinks `{RESULTS}/` to `{DRIVE_FOLDER}/{SWEEP}/` on Drive. Resume-safe: "
   "run_matrix.sh skips any model that already has a JSON.")
code(rf"""
!mkdir -p /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP}
!rm -rf {RESULTS} && mkdir -p offline_eval/multi_turn_results && ln -s /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP} {RESULTS}
import os, glob
print('this run writes to:', os.path.realpath('{RESULTS}'))
done = sorted(os.path.basename(p)[:-5] for p in glob.glob('{RESULTS}/*.json'))
print('already scored:', done or '(none yet)')
""")

md("## Cell 8 — write the model list (all five Qwen arms, Modelfile-pinned tags)\n"
   "run_matrix.sh detects `infra/ollama/Modelfile.<tag>` and builds the tag "
   "via `ollama create` from its registry base instead of pulling it — never "
   "a bare `ollama pull <tag>` for these five.")
code(rf"""
open('offline_eval/models.txt', 'w').write('''# Qwen mt100 — Modelfile-pinned local tags
{QWEN_MODELS}''')
print(open('offline_eval/models.txt').read())
""")

_modelfile_rows = "\n".join(f"| `{tag}` | `infra/ollama/Modelfile.{tag}` |" for tag in _ARM_TAGS)
md(rf"""## Cell 8.5 — sanity: every arm resolves an EXACT profile + has a Modelfile

Guards the single easiest way to silently ruin this run: a misspelled profile
key falls through `get_model_profile`'s generic `qwen3` regex to a CLOUD
profile with `num_ctx=None` (client sizes that to 24192 — the value that
OOMed an 8 GB box in an earlier arm). Each tag below is built with
`ollama create <tag> -f infra/ollama/Modelfile.<tag>` — never a bare
`ollama pull`:

| tag | Modelfile |
| --- | --- |
{_modelfile_rows}
""")
code(r"""
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django; django.setup()
from apps.llm.model_profiles import MODEL_PROFILES

ARMS = [
    ("local_ollama/qwen3.5-2b-jetson", "qwen3.5-2b-jetson"),
    ("local_ollama/qwen3-4b-jetson", "qwen3-4b-jetson"),
    ("local_ollama/qwen3-8b-jetson", "qwen3-8b-jetson"),
    ("local_ollama/qwen3.6-27b-instruct", "qwen3.6-27b-instruct"),
    ("local_ollama/qwen3-30b-a3b-jetson", "qwen3-30b-a3b-jetson"),
]
for spec, tag in ARMS:
    assert spec in MODEL_PROFILES, f"{spec} has NO exact profile entry — would silently fall through to the qwen3 regex fallback (num_ctx=None -> 24192)"
    mf = f"infra/ollama/Modelfile.{tag}"
    assert os.path.exists(mf), f"missing {mf} — this arm would need a bare `ollama pull`, which is forbidden"
    p = MODEL_PROFILES[spec]
    print(f"OK  {spec:<38} num_ctx={p.num_ctx!s:<8} ollama_think={p.ollama_think}   built from {mf}")
print("all five arms: exact profile + Modelfile present")
""")

md(f"## Cell 9 — run the eval (100 v2 multi-turn scenarios, all five arms)\n"
   "Builds each tag from its Modelfile, runs 100 sessions per arm, saves "
   "JSON+log to Drive.")
code(rf"""
!RESULTS_DIR=$PWD/{RESULTS} SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 \
  MODE="{MODE}" bash offline_eval/run_matrix.sh
""")

md("## Cell 10 — results: pass rate + session end-reasons + mean rubric")
code(rf"""
import json, glob, os
from collections import Counter
def rub(r):
    items = (r.get('rubric_result') or {{}}).get('items') or []
    app = [i for i in items if i.get('applicable')]
    return sum(i['score'] for i in app)/len(app) if app else None
rows = []
for f in sorted(glob.glob('{RESULTS}/*.json')):
    d = json.load(open(f)); res = d.get('results') or []
    n = len(res); k = sum(bool(r.get('passed')) for r in res)
    reasons = Counter((r.get('sim_reason') or '?') for r in res)
    scores = [s for s in (rub(r) for r in res) if s is not None]
    mean_rub = sum(scores)/len(scores) if scores else 0.0
    rows.append((os.path.basename(f)[:-5], k, n, mean_rub, dict(reasons)))
print(f"{{'MODEL':<22}}{{'PASS':>8}}  {{'RUBRIC':>7}}   SESSION END-REASONS")
print('-'*76)
for m, k, n, mr, reasons in sorted(rows, key=lambda r: -(r[1]/r[2] if r[2] else 0)):
    print(f"{{m:<22}}{{k:>4}}/{{n:<3}}  {{mr:>6.3f}}   {{reasons}}")
if not rows:
    print("(no results yet — run Cell 9)")
""")

md("## Cell 11 — identity check: did any arm silently think or mis-size its context?\n"
   "run_matrix.sh's per-model probe writes to `identity.log` in the results "
   "dir — grep it here rather than scrolling Cell 9's raw output.")
code(rf"""
import os
log = '{RESULTS}/identity.log'
if os.path.exists(log):
    print(open(log).read())
else:
    print("(no identity.log yet — run Cell 9)")
""")

md(rf"""
## Reading this run

**What "clean" looks like:** all five arms complete 100/100 (or close —
run_matrix.sh logs a skip+reason for anything that fails outright), Cell 8.5
printed `OK` for all five with the expected `num_ctx` per arm (not `None`),
and Cell 11's identity log shows each hybrid arm (`qwen3.5-2b-jetson`,
`qwen3-8b-jetson`, `qwen3.6-27b-instruct`) answering directly rather than
`THINKS` — thinking was suppressed via `ollama_think=False` and should stay
suppressed.

**If an arm shows `num_ctx=None` or a suspiciously fast/garbled run:** stop —
that is the exact confound this board exists to avoid. Check the tag spelling
in `offline_eval/models.txt` against `MODEL_PROFILES` in
`apps/llm/model_profiles.py` character-for-character.

This is the OSS leg only. The cloud leg (14 arms across three vendors, Task 5)
is scored on the same `v2` 100 scenarios via `run_cloud.sh` / a separate
notebook — Task 8 aggregates both legs onto one leaderboard.
""")

nb = {
    "cells": cells,
    "metadata": {
        "colab": {"provenance": []},
        "kernelspec": {"display_name": "Python 3", "name": "python3"},
        "language_info": {"name": "python"},
        "accelerator": "GPU",
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}

out = Path(__file__).parent / OUT_NAME
out.write_text(json.dumps(nb, indent=1))
print(f'wrote {out} ({len(cells)} cells)')
