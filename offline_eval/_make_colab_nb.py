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
# AI Tutor — offline-model eval on Google Colab (free T4)

Evaluate bigger open-source tutor models than an 8 GB laptop can run, using the
**same harness** (tutor = OSS via Ollama; judge + student-sim = Anthropic).

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
tok = userdata.get('GH_TOKEN')
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
!curl -fsSL https://ollama.com/install.sh | sh
import subprocess, time
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

md("## Cell 7 — persist results to Drive (seed with the committed laptop results, then symlink)\n"
   "This makes resume survive disconnects AND gives a **combined** leaderboard "
   "(laptop small models + the big models you run here).")
code(r"""
!mkdir -p /content/drive/MyDrive/ai-tutor-eval-results
# seed the Drive folder with the laptop results committed in the repo (no-clobber)
!cp -n offline_eval/results/*.json /content/drive/MyDrive/ai-tutor-eval-results/ 2>/dev/null || true
!rm -rf offline_eval/results && ln -s /content/drive/MyDrive/ai-tutor-eval-results offline_eval/results
!ls offline_eval/results/
""")

md("## Cell 8 — choose the big-model matrix + seed configs\n"
   "All models below are **tool-calling capable** (the engine requires it). The "
   "**T4 tier** (≤~14B q4) runs on free Colab. The **A100 / Colab-Pro tier** "
   "(>16GB VRAM) is commented out so it won't OOM a T4 — uncomment those lines "
   "only on an A100 runtime. Trim the list to control runtime (~20–40 min each).")
code(r"""
open('offline_eval/models.txt', 'w').write('''\
# ============ T4 (free Colab, 16GB) tier — fits ~14B q4 ============
qwen2.5:7b            big
llama3.1:8b           big
mistral:7b            big
glm4:9b               big
qwen2.5:14b           big
phi4                  big
mistral-nemo:12b      big
granite3.1-dense:8b   big
hermes3:8b            big
aya-expanse:8b        big    # Cohere — multilingual, relevant for MZ/TZ
falcon3:10b           big
command-r7b           big    # Cohere 7B — multilingual + tools

# ============ A100 / Colab-Pro tier — needs >16GB VRAM; UNCOMMENT on A100 ===
# mistral-small:24b   xl
# qwen2.5:32b         xl
# command-r:35b       xl     # strong tool-use + multilingual
# mixtral:8x7b        xl     # 47B MoE
# llama3.3:70b        xl
# qwen2.5:72b         xl
# athene-v2:72b       xl
# command-r-plus:104b xl     # 104B — needs an 80GB A100
''')
!python offline_eval/seed_ollama_configs.py
""")

md("## Cell 9 — run the sweep (pulls + scores each model; resume-safe; ~20–40 min/model on T4)")
code(r"""
!SIMPLE_TUTOR_ENGINE=1 bash offline_eval/run_matrix.sh
""")

md("## Cell 10 — combined leaderboard (run anytime)")
code(r"""
!python offline_eval/aggregate.py
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
`MyDrive/ai-tutor-eval-results/` into the repo's `offline_eval/results/` and run
`python offline_eval/aggregate.py`.

**If GLM under-scores:** it leaks tool calls as text under the full prompt. Set
`OLLAMA_DEBUG_RAW=1` before Cell 9, then check `offline_eval/results/glm4_9b.log`
for an `[OllamaToolLeak]` line and share it — the parser can then be extended to
match glm4's exact format.
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
