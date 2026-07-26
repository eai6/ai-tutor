"""Generate offline_eval/colab_eval_cloud.ipynb — Colab runner for CLOUD-API
mt50 legs (no Ollama, no GPU): the four grok MaaS models and the
gemini-2.5-pro re-run, split into two groups so two tabs run in parallel
while the local sweep finishes the glms.

Tutor = Vertex Model Garden (grok) / Google API (gemini-2.5-pro);
student-sim = Anthropic Haiku 4.5; judge = Anthropic Sonnet 4.6 @ temp 0.
Results land in Drive ai-tutor-eval-multiturn/mt50 — download into the
repo's offline_eval/multi_turn_results/mt50/ to merge with the local legs.

Run: python offline_eval/_make_colab_nb_cloud.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'
BRANCH = 'pixeldesignlabs-dev-portuguese'
DRIVE_FOLDER = 'ai-tutor-eval-multiturn'
SWEEP = 'mt50'
GCP_PROJECT = 'ai-tutor-499714'
SAMPLE = 50
SEED = 5
RESULTS = f'offline_eval/multi_turn_results/{SWEEP}'
MODE = f'--multi-turn --sample {SAMPLE} --seed {SEED}'

GROUPS = {
    'groks': [
        ('vertex_model_garden/xai/grok-4.1-fast-reasoning',     'grok-4.1-fast-reasoning',     'global'),
        ('vertex_model_garden/xai/grok-4.1-fast-non-reasoning', 'grok-4.1-fast-non-reasoning', 'global'),
        ('vertex_model_garden/xai/grok-4.20-reasoning',         'grok-4.20-reasoning',         'global'),
        ('vertex_model_garden/xai/grok-4.20-non-reasoning',     'grok-4.20-non-reasoning',     'global'),
    ],
    'gemini': [
        ('google/gemini-2.5-pro', 'gemini-2.5-pro', ''),
    ],
}

cells = []


def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})


def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(rf"""
# AI Tutor — **mt50 cloud-API legs on Colab** · groks + gemini-2.5-pro re-run

Runs the remaining CLOUD-API rows of the mt50 board (--multi-turn --sample
{SAMPLE} --seed {SEED}) on Colab so they proceed **in parallel** with the local
sweep (which finishes the glms). **No GPU and no Ollama** — the tutor is an API
(Vertex Model Garden for grok, Google API for gemini-2.5-pro); a plain **CPU
runtime** is correct here.

**Two tabs, one group each (Cell 7b dropdown):**
- **groks** — grok-4.1-fast-reasoning / -non-reasoning, grok-4.20-reasoning /
  -non-reasoning (~2-4h each, sequential; ~10-14h for the tab)
- **gemini** — the gemini-2.5-pro re-run (its local first attempt hung at ~75%
  and was killed; ~5-8h)

Results write to Drive `{DRIVE_FOLDER}/{SWEEP}/` — the same folder the OSS legs
used. **Make sure only ONE `{SWEEP}` folder exists in Drive** (two tabs once
raced and created duplicates); both tabs here share it safely because they
score different models. Download into the repo's `{RESULTS}/` to merge with the
local legs.

**Before you start** — Colab Secrets (🔑, *Notebook access ON*):
- `GH_TOKEN` — GitHub classic PAT, `repo` scope.
- `ANTHROPIC_API_KEY` — required (student-sim + judge).
- `GOOGLE_API_KEY` + `OPENAI_API_KEY` — gemini tutor calls + grader cascade.

**Vertex auth (grok tab)**: Cell 5b pops the Google login — use the account
with access to GCP project `{GCP_PROJECT}` (pixeldesignlabs). The gemini tab
needs only the API key, but running 5b is harmless there.
""")

md("## Cell 1 — mount Drive (CPU runtime is fine — no GPU needed)")
code(r"""
from google.colab import drive
drive.mount('/content/drive')
""")

md(f"## Cell 2 — clone the repo (branch `{BRANCH}`)")
code(rf"""
from google.colab import userdata
import subprocess, os
os.chdir('/content')   # a re-run's cwd may be the about-to-be-deleted clone
tok = (userdata.get('GH_TOKEN') or '').strip()
assert tok and ' ' not in tok, "GH_TOKEN missing or contains a space — re-save the secret"
url = f"https://{{tok}}@github.com/{REPO}.git"
subprocess.run(['rm', '-rf', '/content/ai-tutor'], check=True)
subprocess.run(['git', 'clone', '--depth', '1', '-b', '{BRANCH}', url, '/content/ai-tutor'], check=True)
os.chdir('/content/ai-tutor')
print('cloned at', os.getcwd())
""")

md("## Cell 3 — fix hardcoded laptop paths (essential)")
code(r"""
!sed -i 's#/home/daniel/Documents/work/Nyansapo/web/ai-tutor#/content/ai-tutor#g; s#\$ROOT/venv/bin/python#python#g; s#venv/bin/python#python#g' offline_eval/*.py offline_eval/*.sh
""")

md("## Cell 4 — install deps (a few min; ignore pip resolver warnings)")
code(r"""
!pip install -q -r requirements.txt
""")

md(f"## Cell 5 — write .env from Colab Secrets (+ GCP project for Vertex)")
code(rf"""
from google.colab import userdata
open('.env', 'w').write(
    "SECRET_KEY=colab-eval\nDEBUG=True\nEMBEDDING_BACKEND=sqlite\n"
    f"ANTHROPIC_API_KEY={{userdata.get('ANTHROPIC_API_KEY')}}\n"
    f"GOOGLE_API_KEY={{userdata.get('GOOGLE_API_KEY')}}\n"
    f"OPENAI_API_KEY={{userdata.get('OPENAI_API_KEY')}}\n"
    "GOOGLE_CLOUD_PROJECT={GCP_PROJECT}\n"
    "GOOGLE_CLOUD_LOCATION=global\n")
print('.env written')
""")

md(f"## Cell 5b — Vertex auth (needed for the **groks** tab)\n"
   f"Pops the Google sign-in; pick the account with access to `{GCP_PROJECT}`. "
   "Sets up ADC that google-auth (VertexModelGardenClient) picks up.")
code(rf"""
from google.colab import auth
auth.authenticate_user(project_id='{GCP_PROJECT}')
print('ADC ready for', '{GCP_PROJECT}')
""")

md("## Cell 6 — fresh DB + eval fixtures")
code(r"""
!python manage.py migrate
!python manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json
""")

md(f"## Cell 7 — persist results to Drive (symlink → survives disconnects)")
code(rf"""
!mkdir -p /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP}
!rm -rf {RESULTS} && mkdir -p offline_eval/multi_turn_results && ln -s /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP} {RESULTS}
import os, glob
print('this run writes to:', os.path.realpath('{RESULTS}'))
done = sorted(os.path.basename(p)[:-5] for p in glob.glob('{RESULTS}/*.json'))
print('already scored on Drive:', done or '(none yet)')
""")

md("## Cell 7b — **pick this tab's GROUP**\n"
   "- **groks** — the four grok MaaS rows (needs Cell 5b auth)\n"
   "- **gemini** — the gemini-2.5-pro re-run (API key only)")
code(r"""
GROUP = "groks"  #@param ["groks", "gemini"]
print("This tab will evaluate group:", GROUP)
""")

groups_py = json.dumps(GROUPS, indent=4)
md("## Cell 8 — write THIS tab's model list")
code(rf"""
GROUPS = {groups_py}
GROUP = globals().get('GROUP')
assert GROUP in GROUPS, "Run Cell 7b first."
lines = ["# mt50 cloud-API legs — " + GROUP + " (this tab)"]
for spec, safe, region in GROUPS[GROUP]:
    lines.append(f"{{spec:<60}} {{safe:<28}} {{region}}".rstrip())
open('offline_eval/cloud_models_colab.txt', 'w').write("\n".join(lines) + "\n")
print(open('offline_eval/cloud_models_colab.txt').read())
""")

md(f"## Cell 9 — run the sweep (resume-safe; ~2-4h per grok, ~5-8h for 2.5-pro)\n"
   "Console stays quiet per model until it saves — progress streams to the "
   f"per-model log in `{RESULTS}/`. Check liveness anytime with:\n"
   "```\n!tail -3 " + RESULTS + "/<model>.log\n```")
code(rf"""
!RESULTS_DIR=$PWD/{RESULTS} SIMPLE_TUTOR_ENGINE=1 \
  CLOUD_MODELS_FILE=$PWD/offline_eval/cloud_models_colab.txt \
  MODE="{MODE}" bash offline_eval/run_cloud.sh
""")

md("## Cell 10 — board + end-reasons for whatever is on Drive")
code(rf"""
import json, glob, os
from collections import Counter
print(f"{{'MODEL':<30}}{{'PASS':>8}}   END-REASONS")
print('-' * 75)
for f in sorted(glob.glob('{RESULTS}/*.json')):
    name = os.path.basename(f)[:-5]
    if name.startswith('_'):
        continue
    d = json.load(open(f))
    ends = Counter(r.get('sim_reason') or ('errored' if r.get('error') else '?')
                   for r in d['results'])
    reasons = '  '.join(f"{{k}}:{{v}}" for k, v in ends.most_common(3))
    print(f"{{name:<30}}{{d['passed']:>4}}/{{d['total_scenarios']:<3}} {{reasons}}")
""")

nb = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "name": "python3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}
out = Path(__file__).resolve().parent / 'colab_eval_cloud.ipynb'
out.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + '\n',
               encoding='utf-8')
print(f'wrote {out} ({len(cells)} cells)')
