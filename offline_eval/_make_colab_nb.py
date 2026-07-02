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
# AI Tutor — OSS **Qwen3 / Qwen3.5 re-run** on Google Colab (free T4)  ·  *results3b*

Re-run of the small **Qwen3** and **Qwen3.5** reasoning models after the
**`num_ctx` fix**, using the **same harness** (tutor = OSS via Ollama; judge +
student-sim = Anthropic).

**Why re-run.** The first pass gave nonsense (qwen3.5:9b scored *below* the 2b).
Root cause: the Ollama request never set `num_ctx`, so it defaulted to **4096** —
the ~2K-token prompt left only ~2K for output, and these reasoning models were
**cut off mid-`<think>`** before the answer landed → empty completion → the engine's
"Let's keep going." placeholder. qwen3.5 lost **34–48 of 60** turns this way. The
fix (`apps/llm/client.py`) sizes `num_ctx` to fit the prompt + generation
(~24K here). Non-thinking models (qwen2.5) were unaffected.

**Models (Cell 8).** All 9 fit the **free-T4 tier** (<14B):
`qwen3.5:{{0.8,2,4,9}}b` + `qwen3:{{0.6,1.7,4,8,14}}b`. Each auto-tunes to the
**qwen** profile (temp 0.7 / top_p 0.8 / top_k 20, Markdown Block-0, num_ctx 24K).

**Output → `offline_eval/results3b/`** (a fresh folder), seeded from the committed
`results3/*.json` (the 20 prior valid models: cloud + qwen2.5 + qwen3.6 + …), so
Cell 10 shows the full combined board with the corrected Qwen3/3.5 rows.

> Requires the `{BRANCH}` branch to contain the `num_ctx` fix commit. Cell 2 clones
> that branch, so make sure it's pushed before running.

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

md("## Cell 7 — persist results to a FRESH results3b folder on Drive\n"
   "This is a clean re-run, so it writes into a **new** `results3b/` — seeded from "
   "the committed `results3/*.json` (the 20 prior valid models: cloud + qwen2.5 + "
   "qwen3.6 + …). The 9 re-run models aren't committed, so they run fresh with the "
   "`num_ctx` fix. Symlinked to Drive so it survives Colab disconnects.")
code(r"""
!mkdir -p /content/drive/MyDrive/ai-tutor-eval-results3b
# seed the NEW results3b folder from the committed PRIOR results (results3) so the
# combined board stays complete; the 9 qwen3/qwen3.5 re-run models aren't committed
# → not seeded → they run fresh with the num_ctx fix.
!cp -n offline_eval/results3/*.json /content/drive/MyDrive/ai-tutor-eval-results3b/ 2>/dev/null || true
!rm -rf offline_eval/results3b && ln -s /content/drive/MyDrive/ai-tutor-eval-results3b offline_eval/results3b
!ls offline_eval/results3b/
""")

md("## Cell 8 — Qwen3 / Qwen3.5 re-run matrix + seed configs\n"
   "**This run: the small Qwen3.5 + Qwen3 dense line** (all <14B → free-T4 tier), "
   "re-run with the `num_ctx` fix. All Qwen3-family models are tool-capable and now "
   "get a 24K context window, so the `<think>` trace + answer fit. "
   "~15–40 min each; **resume-safe** (done models are skipped).")
code(r"""
open('offline_eval/models.txt', 'w').write('''\
# ============ THIS RUN — Qwen3.5 + Qwen3 small dense (<14B, free-T4 tier) ======
qwen3.5:0.8b         big
qwen3.5:2b           big
qwen3.5:4b           big
qwen3.5:9b           big
qwen3:0.6b           big
qwen3:1.7b           big
qwen3:4b             big
qwen3:8b             big
qwen3:14b            big

# ============ Already evaluated — commented out (results kept on the board) =====
# --- T4 tier ---
# qwen2.5:0.5b        big
# qwen2.5:1.5b        big
# qwen2.5:3b          big
# qwen2.5:7b          big
# qwen2.5:14b         big
# gemma3:1b           big    # BROKEN: gemma3 has no tool support in Ollama -> 400s
# gemma3:4b           big
# gemma3:12b          big
# --- XL tier (A100 / Colab-Pro) ---
# qwen2.5:32b         xl
# qwen2.5:72b         xl     # q4 ~47GB — needs the 80GB A100
# gemma3:27b          xl     # BROKEN (see above)
# qwen3:30b-a3b       xl     # Qwen3 30B MoE (3B active)
# qwen3.6:27b         xl     # Qwen3.6 dense 27B
# qwen3.6:35b-a3b     xl     # Qwen3.6 35B MoE (3B active)
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

md("## Cell 8b — reclaim disk BEFORE the sweep (important on a resumed session)\n"
   "A reconnected / re-cloned Colab can start with old pulled model weights + caches "
   "still on disk (the last sweep left it ~half full). This clears every "
   "previously-pulled Ollama model and the package caches **before** the first "
   "download, then prints free space. On a fresh VM it's a harmless no-op.")
code(r"""
import subprocess
def _df(tag):
    print(f"--- disk {tag} ---\n" + subprocess.run(['df','-h','/'],capture_output=True,text=True).stdout)
_df('BEFORE cleanup')
# remove every model the Ollama server currently holds (server-mediated → frees blobs)
!ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | xargs -r -n1 ollama rm 2>/dev/null || true
# backstop: wipe any stray model store + pip/apt/HF caches (server re-creates on next pull)
!rm -rf /root/.ollama/models/blobs/* /root/.ollama/models/manifests/* offline_eval/ollama_models/* 2>/dev/null || true
!pip cache purge 2>/dev/null || true
!apt-get clean 2>/dev/null || true
!rm -rf /root/.cache/huggingface /root/.cache/pip 2>/dev/null || true
_df('AFTER cleanup')
""")

md("## Cell 9 — run the sweep (pulls + scores each model; resume-safe; ~15–25 min/model on T4)\n"
   "`RESULTS_DIR=…/results3b` writes to the fresh folder. `CLEANUP_MODELS=1` deletes "
   "each model's weights from disk **right after** it's scored, so peak disk ≈ one "
   "model at a time (results are already on Drive, so a re-run skips done models).")
code(r"""
!RESULTS_DIR=$PWD/offline_eval/results3b SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 bash offline_eval/run_matrix.sh
""")

md("## Cell 9b — reclaim disk AFTER the sweep (final backstop)\n"
   "`CLEANUP_MODELS=1` already removes each model as it finishes; this drops anything "
   "left and prints free space. Results are safe on Drive (Cell 7).")
code(r"""
!ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | xargs -r -n1 ollama rm 2>/dev/null || true
!rm -rf /root/.ollama/models/blobs/* /root/.ollama/models/manifests/* offline_eval/ollama_models/* 2>/dev/null || true
!pip cache purge 2>/dev/null || true
!apt-get clean 2>/dev/null || true
import subprocess
print(subprocess.run(['df','-h','/'],capture_output=True,text=True).stdout)
""")

md("## Cell 10 — combined results3b leaderboard (20 prior + 9 re-run Qwen3/3.5; run anytime)")
code(r"""
!RESULTS_DIR=$PWD/offline_eval/results3b python offline_eval/aggregate.py
""")

md(rf"""
## After a Colab disconnect (free tier: ~90 min idle / ~12 h max)
Re-run **Cells 1–8b**, then **Cell 9** again. Because results live on Drive (Cell 7),
`run_matrix.sh` **skips already-scored models** and continues.

To pick up new commits on the branch, just re-run **Cell 2** (it re-clones).

**Tips:** keep the tab active (free Colab kills idle sessions); a model interrupted
mid-run restarts (resume only skips *completed* models); each model is also bound on
the Anthropic judge calls, so plan a few models per session.

To pull these results back to your laptop: copy the JSONs from
`MyDrive/ai-tutor-eval-results3b/` into the repo's `offline_eval/results3b/` and run
`RESULTS_DIR=offline_eval/results3b python offline_eval/aggregate.py`.

**Sanity check the fix worked:** after a model runs, its `results3b/<model>.log` should
show `[OllamaTools] response: ... blocks=['tool_use', ...]` (or `['text']`) — **not**
`blocks=[]`. Empty blocks = still truncating (raise `num_ctx` further).
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
