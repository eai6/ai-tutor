"""Generate offline_eval/colab_qwen_mt30.ipynb — Colab notebook that runs the
MULTI-TURN eval for qwen3-4b-jetson (the settled offline tutor) on the
ORIGINAL 30 multi-turn scenarios (tag `v1`), in TWO-CALL mode.

Purpose: measure the 2026-08-03 qwen3-4b bottleneck fixes (grader percent/
decimal equivalence, ack-prepend, reveal-filter broadening, per-stem pose cap,
same-turn hint guard, one-call escalation — see
offline_eval/multi_turn_results/qwen3_4b_bottlenecks_2026-08-03.md) against
the pre-expansion benchmark the earlier boards were judged on.

Three deliberate choices, so the numbers mean something:

  1. **Qwen only, Modelfile-pinned tags.** The offline tutor ships on
     `qwen3-4b-jetson` (built from qwen3:4b-instruct with num_ctx/template
     baked in — bare tags were deprecated in b5b7a68). The sweep scores the
     tags the Jetson actually runs; run_matrix.sh builds them via
     `ollama create` from infra/ollama/Modelfile.<tag>.
  2. **The original 30 multi-turn scenarios** (`--subset v1`): the 90-scenario
     pre-expansion dataset (60 single + 30 multi) is tagged `v1`; this run
     filters to its 30 multi-turn.
  3. **TUTOR_CALL_MODE=two**: mt50 ran two-call; forcing two-call here keeps
     the comparison to that board clean (the current engine default for local
     families is one-call, which would change two variables at once).

Tutor = the local qwen under test; student-sim + rubric judge = Anthropic.

Run: python offline_eval/_make_colab_nb_qwen_mt30.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'
BRANCH = 'offline-optimization'      # MUST carry the 2026-08-03 fixes — push first
DRIVE_FOLDER = 'ai-tutor-eval-multiturn'
SWEEP = 'qwen_mt30'
RESULTS = f'offline_eval/multi_turn_results/{SWEEP}'
MODE = '--multi-turn --subset v1'    # the original 30 multi-turn scenarios

# The model under test — Modelfile-pinned tag (tag tier), run_matrix.sh shape.
# qwen3-4b-jetson is the settled offline tutor; this is deliberately a
# single-model run (user direction 2026-08-03). Add qwen3.5-4b-jetson /
# qwen3.5-2b-jetson lines here to widen to the fallback candidates.
QWEN_MODELS = """\
qwen3-4b-jetson      jetson
"""

# Sanity at generation time: the v1 tag must select exactly 30 multi-turn
# scenarios, and every model's Modelfile must exist.
import re as _re

_ROOT = Path(__file__).resolve().parents[1]
_v1_mt = 0
for _f in (_ROOT / 'evals' / 'dataset').rglob('*.yaml'):
    _t = _f.read_text()
    if _re.search(r'(?m)^tags: \[.*\bv1\]', _t) and 'mode: multi_turn' in _t:
        _v1_mt += 1
assert _v1_mt == 30, f'expected 30 v1 multi-turn scenarios, found {_v1_mt}'
for _line in QWEN_MODELS.strip().splitlines():
    _tag = _line.split()[0]
    _mf = _ROOT / 'infra' / 'ollama' / f'Modelfile.{_tag}'
    assert _mf.exists(), f'missing {_mf}'

cells = []


def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})


def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(rf"""
# AI Tutor — **qwen3-4b multi-turn** · original 30 scenarios (`v1`) · two-call mode

Measures the **2026-08-03 qwen3-4b bottleneck fixes** on the pre-expansion
multi-turn benchmark. Single model — the tag the offline (Jetson) tutor
actually ships:

| tag | note |
| --- | --- |
| `qwen3-4b-jetson` | the settled offline tutor (qwen3:4b-instruct + pinned num_ctx) |

- **Scenarios:** `{MODE}` — the ORIGINAL 30 multi-turn scenarios (tag `v1`),
  every model sees all 30. No sampling.
- **Call mode:** `TUTOR_CALL_MODE=two` — same as the mt50 board, so score
  deltas are attributable to the fixes, not the call-mode change.
- **Tutor** = the local qwen under test; **student-sim + rubric judge** =
  Anthropic (needs `ANTHROPIC_API_KEY`).
- **Branch `{BRANCH}`** must be pushed and must carry the fixes — Cell 2
  prints HEAD; check it.

**Before you start**
1. Runtime → **Change runtime type** → **T4 GPU** (the model is ~2.5 GB).
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

md(f"## Cell 2 — clone the repo (branch `{BRANCH}` — carries the fixes)")
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
        subprocess.run(f'curl -fL --retry 5 --retry-all-errors --connect-timeout 30 '
                       f'-o /tmp/ollama.tgz https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/ollama-linux-amd64.tgz', shell=True)
        if os.path.exists('/tmp/ollama.tgz') and os.path.getsize('/tmp/ollama.tgz') > 1_000_000:
            subprocess.run('tar -C /usr -xzf /tmp/ollama.tgz', shell=True)
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

md("## Cell 8 — write the model list (local qwen, Modelfile-pinned tags)\n"
   "run_matrix.sh detects `infra/ollama/Modelfile.<tag>` and builds the tag "
   "via `ollama create` from its registry base instead of pulling it.")
code(rf"""
open('offline_eval/models.txt', 'w').write('''# Qwen mt30 — Modelfile-pinned local tags
{QWEN_MODELS}''')
print(open('offline_eval/models.txt').read())
""")

md(f"## Cell 9 — run the eval (30 v1 multi-turn scenarios, qwen3-4b-jetson)\n"
   "`TUTOR_CALL_MODE=two` pins two-call (mt50-comparable). Builds the tag from "
   "its Modelfile, runs 30 sessions, saves JSON+log to Drive.")
code(rf"""
!TUTOR_CALL_MODE=two RESULTS_DIR=$PWD/{RESULTS} SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 \
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

md("## Cell 11 — **did the 2026-08-03 fixes fire?** (grep the logs)\n"
   "Each line is a fix leaving a trace. Expectations vs the mt50 baseline:\n"
   "- `percent/decimal scale equivalence` — grader crediting decimal-form "
   "answers (mt50 had 5 false negatives).\n"
   "- `prepended missing … ack` — silent-pose turns getting an acknowledgement "
   "(62 in mt50).\n"
   "- `ack_rotate` — 'Exactly —' chains broken.\n"
   "- `reveal_filter` — should be MORE frequent than mt50's 3 (now covers "
   "no-verdict turns + option paraphrases).\n"
   "- `repeat_pose rejected … posed 2x` — the per-stem cap.\n"
   "- `premature_pose blocked … graded incorrect this turn` — same-turn hint "
   "guard.\n"
   "- `auto_pivot` — stuck slots pivoted (attempts OR age).")
code(rf"""
import glob, os, re
PATTERNS = {{
  'grader scale-equiv': r'percent/decimal scale equivalence',
  'ack prepended': r'prepended missing \w+ ack',
  'ack rotated': r'ack_rotate: repeated opener',
  'reveal redacted': r'reveal_filter: redacted',
  'stem cap': r'already posed \d+x this session',
  'same-turn hint guard': r'graded incorrect this turn',
  'auto pivot': r'auto_pivot: replaced stuck slot',
  'call2 repair': r'call2_repair: Call 1 skipped',
}}
logs = sorted(glob.glob('{RESULTS}/*.log'))
if not logs:
    print("(no logs yet — run Cell 9)")
else:
    hdr = f"{{'MODEL':<22}}" + ''.join(f"{{k[:15]:>17}}" for k in PATTERNS)
    print(hdr); print('-'*len(hdr))
    for lg in logs:
        txt = open(lg, errors='ignore').read()
        counts = [len(re.findall(p, txt)) for p in PATTERNS.values()]
        print(f"{{os.path.basename(lg)[:-4]:<22}}" + ''.join(f"{{c:>17}}" for c in counts))
""")

md(rf"""
## Reading this run

**Baseline:** mt50 scored `qwen3:4b` 44/50 (88%) on a different (50-scenario,
seed-5) draw — the closest apples-to-apples reference for THIS scenario set is
a fresh run, so treat this board as the new baseline for the original-30.

**What "the fixes worked" looks like:** completions stay at/near 30/30, mean
rubric rises (mt50's tail was 0.42–0.76 driven by false corrections, silent
poses, reveals, and re-asks), and Cell 11 shows the nets firing in sensible
volumes — a handful each, not storms. A net firing hundreds of times means the
model is fighting the harness; investigate before trusting the score.

At n=30 the binomial SE at p≈0.9 is ~5.5pp — a pass-rate change under ~11pp
vs a prior run of this board is noise; lean on mean rubric + end-reasons.
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

out = Path(__file__).parent / 'colab_qwen_mt30.ipynb'
out.write_text(json.dumps(nb, indent=1))
print(f'wrote {out} ({len(cells)} cells)')
