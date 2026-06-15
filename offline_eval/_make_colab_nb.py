"""Generate offline_eval/colab_eval.ipynb — a ready-to-run Colab notebook for
evaluating bigger OSS models on a free T4 GPU.

Workflow: upload a ZIP of the repo to Google Drive (no GitHub needed). When the
repo owner shares a GH_TOKEN later, switch Cell 2 back to a git clone — see the
commented CLONE_CELL below for the drop-in replacement.

Run: python offline_eval/_make_colab_nb.py
"""
import json
from pathlib import Path

cells = []

def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})

def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(r"""
# AI Tutor — offline-model eval on Google Colab (free T4)

Evaluate bigger open-source tutor models than an 8 GB laptop can run, using the
**same harness** (tutor = OSS via Ollama; judge + student-sim = Anthropic).

**Before you start**
1. Runtime → **Change runtime type → T4 GPU**.
2. On your **laptop**, make a clean zip of the repo with `git archive` (tracked
   files only — small, no `venv`/`.git`/weights; keeps the committed laptop result
   JSONs so you get a **combined** leaderboard):
   ```bash
   cd /path/to/ai-tutor
   git archive --format=zip -o ~/ai-tutor-src.zip HEAD
   ```
   Upload `~/ai-tutor-src.zip` to `MyDrive/ai-tutor-src.zip`.
   (`git archive` zips the **committed** state — commit any local tweaks first.)
3. Add your API keys as **Colab Secrets** (🔑 icon, *Notebook access ON*):
   `ANTHROPIC_API_KEY` (required — judge + student-sim), plus `GOOGLE_API_KEY` and
   `OPENAI_API_KEY` (keep all three so the judge cascade matches the laptop runs).
   `.env` is **not** in the zip (it's gitignored), so **Cell 5 is required**.

**T4 fits models up to ~14B q4.** For 32B/70B (and the big GLM-4.6/4.7/5 tier)
use Colab Pro (A100) — same notebook, just a bigger `models.txt` in Cell 8.

> **Later (clone workflow):** once the repo owner shares a `GH_TOKEN`, replace
> Cell 2 with a `git clone` of the private repo — the drop-in cell is at the
> bottom of this notebook. Then you skip the zip-and-upload step entirely.
""")

md("## Cell 1 — confirm GPU + mount Drive")
code(r"""
!nvidia-smi -L
from google.colab import drive
drive.mount('/content/drive')
""")

md("## Cell 2 — unzip the source from Drive")
code(r"""
!mkdir -p /content/ai-tutor && unzip -oq /content/drive/MyDrive/ai-tutor-src.zip -d /content/ai-tutor
%cd /content/ai-tutor
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
   "`.env` isn't in the `git archive` zip (gitignored). Keep **all three** keys so "
   "the judge/grader cascade matches the laptop runs (comparable scores).")
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

md("## Cell 7 — persist results to Drive (seed with the laptop results, then symlink)\n"
   "This makes resume survive disconnects AND gives a **combined** leaderboard "
   "(laptop small models + the big models you run here).")
code(r"""
!mkdir -p /content/drive/MyDrive/ai-tutor-eval-results
# seed the Drive folder with the laptop results shipped in the zip (no-clobber)
!cp -n offline_eval/results/*.json /content/drive/MyDrive/ai-tutor-eval-results/ 2>/dev/null || true
!rm -rf offline_eval/results && ln -s /content/drive/MyDrive/ai-tutor-eval-results offline_eval/results
!ls offline_eval/results/
""")

md("## Cell 8 — choose the big-model matrix (T4 fits ≤14B) + seed configs")
code(r"""
open('offline_eval/models.txt', 'w').write('''\
# Bigger than the 8GB laptop could run — T4 (16GB) tier
qwen2.5:7b      laptop
llama3.1:8b     laptop
mistral:7b      laptop
glm4:9b         laptop
qwen2.5:14b     big
phi4            big
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

md(r"""
## After a Colab disconnect (free tier: ~90 min idle / ~12 h max)
Re-run **Cells 1–8**, then **Cell 9** again. Because results live on Drive (Cell 7),
`run_matrix.sh` **skips already-scored models** and continues.

**Tips:** keep the tab active (free Colab kills idle sessions); a model interrupted
mid-run restarts (resume only skips *completed* models); each model is also bound on
the Anthropic judge calls, so plan 1–3 models per session.

To pull these results back to your laptop: copy the JSONs from
`MyDrive/ai-tutor-eval-results/` into the repo's `offline_eval/results/` and run
`python offline_eval/aggregate.py`.

---
### Later: clone instead of zip (when the owner shares a GH_TOKEN)
Add `GH_TOKEN` (repo **Contents: Read** scope) to Colab Secrets, then **replace
Cell 2** with this and skip the zip-and-upload step:
```python
from google.colab import userdata
import subprocess, os
tok = userdata.get('GH_TOKEN')
url = f"https://x-access-token:{tok}@github.com/<owner>/ai-tutor.git"
subprocess.run(['rm','-rf','/content/ai-tutor'], check=True)
subprocess.run(['git','clone','--depth','1','-b','pixeldesignlabs-dev-portuguese',url,'/content/ai-tutor'], check=True)
os.chdir('/content/ai-tutor')
```
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
