"""Generate offline_eval/colab_eval.ipynb — a ready-to-run Colab notebook for
evaluating bigger OSS models on a free T4 GPU.

Workflow: git-clone the repo using a GitHub classic PAT (works when you are a
collaborator on the repo, not just the owner). No zip upload needed.

Run: python offline_eval/_make_colab_nb.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'                       # the repo you collaborate on (origin)
BRANCH = 'pixeldesignlabs-dev-portuguese'

cells = []

def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})

def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(rf"""
# AI Tutor — OSS **Qwen** eval on Google Colab (free T4)  ·  *Improved Evaluation (results3)*

Evaluate the open-source **Qwen** tutor models that an 8 GB laptop can't run, using
the **same harness** (tutor = Qwen via Ollama; judge + student-sim = Anthropic).

**This run is Qwen-only.** Cell 8 lists only `qwen2.5:7b` and `qwen2.5:14b` (the
free-T4 tier); the larger `qwen2.5:32b/72b` are commented out for an A100/Colab-Pro
runtime. Each model is sampled with the **per-family Qwen settings** from
`apps/llm/model_profiles.py` (**temp 0.7 / top_p 0.8 / top_k 20**) and uses the
**Qwen-specific Block-0 prompt** (Markdown + targeted rules + few-shot), selected
automatically by the engine. Results land in **`offline_eval/results3/`** so they
join the Gemini + Qwen-MaaS cloud re-run on the new board.

> Requires the `{BRANCH}` branch to contain the bottleneck-fix commit (B1/B2 engine
> fixes, per-family prompts, rubric n/a, dataset reference fixes). Cell 2 clones that
> branch, so just make sure it's pushed before running.

**Before you start**
1. Runtime → **Change runtime type → T4 GPU**.
2. Add these **Colab Secrets** (🔑 icon in the left sidebar), each toggled
   *Notebook access ON*:
   - `GH_TOKEN` — a GitHub **classic** Personal Access Token with the **`repo`**
     scope. Make it at github.com/settings/tokens → *Generate new token
     (classic)* → check **repo**. A classic token works on `{REPO}` because you
     are a **collaborator** (a fine-grained token would only work if you *owned*
     the repo).
   - `ANTHROPIC_API_KEY` — required (judge + student-simulator).
   - `GOOGLE_API_KEY` and `OPENAI_API_KEY` — keep these too so the judge/grader
     cross-vendor cascade matches the laptop runs (comparable scores).

**T4 fits models up to ~14B q4.** For the bigger A100/Colab-Pro tier (commented
out in Cell 8), use a Colab Pro A100 runtime — nothing else changes.
""")

md("## Cell 1 — confirm GPU + mount Drive (Drive persists results across disconnects)")
code(r"""
!nvidia-smi -L
from google.colab import drive
drive.mount('/content/drive')
""")

md(f"## Cell 2 — clone the repo (branch `{BRANCH}`) using the GH_TOKEN classic PAT")
code(rf"""
from google.colab import userdata
import subprocess, os
tok = (userdata.get('GH_TOKEN') or '').strip()   # strip stray spaces/newlines
assert tok and ' ' not in tok, "GH_TOKEN missing or contains a space — re-save the secret with no whitespace"
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

md("## Cell 4 — install deps + start Ollama (a few min; ignore pip resolver warnings)")
code(r"""
!pip install -q -r requirements.txt
# The Ollama installer is now zstd-compressed; the Colab VM lacks zstd, so install
# it first (otherwise the installer aborts and `ollama` is never created).
!apt-get -qq install -y zstd || (apt-get -qq update && apt-get -qq install -y zstd)
!curl -fsSL https://ollama.com/install.sh | sh
import subprocess, time, shutil
assert shutil.which('ollama'), "ollama did not install — check the install output above (zstd?)"
subprocess.Popen(['ollama', 'serve'],
                 stdout=open('/content/ollama.log', 'w'),
                 stderr=subprocess.STDOUT)
for _ in range(30):
    if subprocess.run(['bash', '-c', 'ollama list'], capture_output=True).returncode == 0:
        print('ollama ready'); break
    time.sleep(2)
else:
    print('ollama NOT ready — check /content/ollama.log')
""")

md("## Cell 5 — **required** — write .env from Colab Secrets\n"
   "`.env` isn't in the repo (gitignored). Keep **all three** keys so the "
   "judge/grader cascade matches the laptop runs (comparable scores).")
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

md("## Cell 7 — persist results to Drive (seed with the committed cloud results3, then symlink)\n"
   "Writes into **`results3/`** so the OSS Qwen models join the Gemini + Qwen-MaaS "
   "cloud re-run. Seeds from any committed cloud `results3/*.json` so the combined "
   "leaderboard shows cloud + OSS together, and survives Colab disconnects.")
code(r"""
!mkdir -p /content/drive/MyDrive/ai-tutor-eval-results3
# seed the Drive folder with the cloud results committed in the repo (no-clobber)
!cp -n offline_eval/results3/*.json /content/drive/MyDrive/ai-tutor-eval-results3/ 2>/dev/null || true
!rm -rf offline_eval/results3 && ln -s /content/drive/MyDrive/ai-tutor-eval-results3 offline_eval/results3
!ls offline_eval/results3/
""")

md("## Cell 8 — OSS Qwen matrix + seed configs\n"
   "**Qwen-only run.** Both models are tool-calling capable (the engine requires it) "
   "and fit the **free-T4 tier** (≤~14B q4). The larger `qwen2.5:32b/72b` are "
   "commented out (needs an A100 / Colab-Pro runtime; uncomment there). Each runs "
   "~20–40 min.")
code(r"""
open('offline_eval/models.txt', 'w').write('''\
# ============ T4 (free Colab, 16GB) tier — fits ~14B q4 ============
qwen2.5:7b            big
qwen2.5:14b           big

# ============ A100 / Colab-Pro tier — needs >16GB VRAM; UNCOMMENT on A100 ===
# qwen2.5:32b         xl
# qwen2.5:72b         xl
''')
!python offline_eval/seed_ollama_configs.py
# Show the per-family sampling each model will use (from apps/llm/model_profiles).
# This is the "improved" tuning — confirm it resolves before spending GPU time.
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from apps.llm.model_profiles import get_model_profile
print(f"{'MODEL':<22} {'FAMILY':<9} {'MODE':<11} {'MAXTOK':>7}  SAMPLING")
print('-' * 78)
for line in open('offline_eval/models.txt'):
    tag = line.split('#')[0].split()[0] if line.split('#')[0].split() else ''
    if not tag:
        continue
    p = get_model_profile(f'local_ollama/{tag}')
    if p:
        print(f"{tag:<22} {p.family:<9} {p.mode:<11} {p.max_tokens:>7}  {p.sampling_dict()}")
    else:
        print(f"{tag:<22} (no profile — runs at engine default)")
""")

md("## Cell 9 — run the sweep (pulls + scores each model; resume-safe; ~20–40 min/model on T4)\n"
   "`RESULTS_DIR=…/results3` puts these on the new board (Gemini + Qwen-MaaS + OSS Qwen). "
   "`CLEANUP_MODELS=1` deletes each model's weights from disk right after it's "
   "scored, so Colab's ~112 GB disk never fills up (results are already saved to "
   "Drive, so a re-run still skips done models).")
code(r"""
!RESULTS_DIR=$PWD/offline_eval/results3 SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 bash offline_eval/run_matrix.sh
""")

md("## Cell 10 — combined results3 leaderboard (cloud + OSS Qwen; run anytime)")
code(r"""
!RESULTS_DIR=$PWD/offline_eval/results3 python offline_eval/aggregate.py
""")

md(rf"""
## After a Colab disconnect (free tier: ~90 min idle / ~12 h max)
Re-run **Cells 1–8**, then **Cell 9** again. Because results live on Drive (Cell 7),
`run_matrix.sh` **skips already-scored models** and continues.

To pick up new commits on the branch, just re-run **Cell 2** (it re-clones).

**Tips:** keep the tab active (free Colab kills idle sessions); a model interrupted
mid-run restarts (resume only skips *completed* models); each model is also bound on
the Anthropic judge calls, so plan 1–3 models per session.

To pull these results back to your laptop: copy the JSONs from
`MyDrive/ai-tutor-eval-results3/` into the repo's `offline_eval/results3/` and run
`RESULTS_DIR=offline_eval/results3 python offline_eval/aggregate.py`.

**If a Qwen model leaks tool calls as text** (the engine's text-tool-call recovery
should now catch `record_answer(...)`): set `OLLAMA_DEBUG_RAW=1` before Cell 9, then
check `offline_eval/results3/qwen2.5_14b.log` for an `[OllamaToolLeak]` line and
share it — the parser can then be extended to match that exact format.
""")

nb = {
    "cells": cells,
    "metadata": {
        "accelerator": "GPU",
        "colab": {"provenance": [], "gpuType": "T4"},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}

out = Path(__file__).resolve().parent / 'colab_eval.ipynb'
out.write_text(json.dumps(nb, indent=1))
print(f"wrote {out} ({len(cells)} cells)")
