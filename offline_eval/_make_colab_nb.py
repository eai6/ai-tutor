"""Generate offline_eval/colab_eval.ipynb — a ready-to-run Colab notebook for the
MULTI-TURN evaluation (Improved Eval 3) of the top-15 OSS Qwen models on an A100.

Tutor = OSS via Ollama (swapped per model); student-sim = Anthropic Haiku 4.5;
judge = Anthropic Sonnet 4.6 (multi-turn default, per the Haiku-vs-Sonnet A/B).
Results land in offline_eval/multi_turn_results/ (persisted to Drive).

Run: python offline_eval/_make_colab_nb.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'                       # the repo you collaborate on (origin)
BRANCH = 'pixeldesignlabs-dev-portuguese'
DRIVE_FOLDER = 'ai-tutor-eval-multi-turn'    # Drive folder that persists results

cells = []

def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})

def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(rf"""
# AI Tutor — **Multi-turn Eval (Improved Eval 3)** · top-15 OSS Qwen · Colab **A100**

Full **multi-turn** sessions (opening → pose → teach → grade → advance → exit
ticket), not single graded turns. Same harness as the laptop cloud run:

- **Tutor** = OSS Qwen via Ollama (swapped per model with `TUTOR_MODEL_OVERRIDE`).
- **Student-sim** = Anthropic **Haiku 4.5** (fixed persona player).
- **Judge** = Anthropic **Sonnet 4.6** (multi-turn default — the Haiku-vs-Sonnet
  A/B found Haiku too lenient on whole-session judging).
- **Engine** = `simple_tutor` (the production engine; `run_eval` prints a banner
  confirming it and errors on the legacy engine).

**Models (Cell 8), ordered small → large** so cheap results land first:
`qwen3.5:4b`, `qwen3:4b`, `qwen3.5:9b`, `qwen3:14b`, `qwen3:30b-a3b`,
`qwen3.6:27b`, `qwen3.6:35b-a3b`, `qwen2.5:32b`, `qwen2.5:72b`. Each auto-tunes to
its family profile (Qwen → Markdown Block-0, temp 0.7 / top_p 0.8 / top_k 20,
num_ctx 24K so `<think>` + answer fit).

**Output → `offline_eval/multi_turn_results/`** (symlinked to Drive, resume-safe).

> ⚠️ **Runtime: multi-turn is ~15-25× heavier than single-turn.** Each model runs
> ~30 scenarios × up to ~24 turns × (tutor + student + judge) calls. The **small +
> MoE** models (4b/9b/14b/30b-a3b/35b-a3b) are hours-scale; the **large dense**
> ones (27b/32b/**72b**) can take **many hours each** — reasoning models also emit
> long `<think>` traces. Plan **several A100 sessions**; the run is **resume-safe**
> (finished models are skipped, results live on Drive), so you can stop/restart and
> even skip the 72b if it's not worth the wall-clock.

**Before you start**
1. Runtime → **Change runtime type → A100 GPU** (High-RAM). 72b q4 (~47 GB) needs the 80 GB A100.
2. Add these **Colab Secrets** (🔑 sidebar), each *Notebook access ON*:
   - `GH_TOKEN` — GitHub **classic** PAT with **`repo`** scope (you're a collaborator on `{REPO}`).
   - `ANTHROPIC_API_KEY` — **required** (student-sim Haiku + judge Sonnet).
   - `GOOGLE_API_KEY` + `OPENAI_API_KEY` — keep both so the cross-vendor **grader**
     cascade (Gemini→OpenAI→Haiku, self-excluding) matches the laptop runs.
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
   "`.env` isn't in the repo (gitignored). Keep **all three** keys: ANTHROPIC drives "
   "the student-sim (Haiku) **and** the judge (Sonnet); GOOGLE/OPENAI feed the "
   "cross-vendor grader cascade so scores match the laptop runs.")
code(r"""
from google.colab import userdata
open('.env', 'w').write(
    "SECRET_KEY=colab-eval\nDEBUG=True\nEMBEDDING_BACKEND=sqlite\n"
    f"ANTHROPIC_API_KEY={userdata.get('ANTHROPIC_API_KEY')}\n"
    f"GOOGLE_API_KEY={userdata.get('GOOGLE_API_KEY')}\n"
    f"OPENAI_API_KEY={userdata.get('OPENAI_API_KEY')}\n")
print('.env written')
""")

md("## Cell 6 — fresh DB + eval fixtures\n"
   "`lessons.json` carries the 4 eval lessons with their **LessonSteps + exit "
   "tickets** (1137/1138 = 10 steps, 1463/1464 = 5) — multi-turn traverses every "
   "step, so this fixture is required. `institution.json` seeds the active "
   "`student_sim` ModelConfig (Haiku).")
code(r"""
!python manage.py migrate
!python manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json
""")

md("## Cell 7 — persist results to Drive (symlink → survives disconnects)\n"
   f"Writes into a **new** `{DRIVE_FOLDER}/` on Drive, symlinked to "
   "`offline_eval/multi_turn_results/`. No seeding from the single-turn board — "
   "this is a fresh multi-turn run. Resume-safe: a reconnect re-symlinks the same "
   "Drive folder and `run_matrix.sh` skips already-scored models.")
code(rf"""
!mkdir -p /content/drive/MyDrive/{DRIVE_FOLDER}
!rm -rf offline_eval/multi_turn_results && ln -s /content/drive/MyDrive/{DRIVE_FOLDER} offline_eval/multi_turn_results
!ls -la offline_eval/multi_turn_results/
""")

md("## Cell 8 — multi-turn model matrix (top-15 OSS Qwen) + seed configs\n"
   "Ordered **small → large** so cheap results land first. The big dense models "
   "(27b/32b/72b) are the slow tail — you can stop before them and still have a "
   "full small/MoE board. `~15-40 min` each for the small ones; **hours** for 72b.")
code(r"""
open('offline_eval/models.txt', 'w').write('''\
# ============ Multi-turn Eval 3 — top-15 OSS Qwen (A100), small -> large =========
qwen3.5:4b           big
qwen3:4b             big
qwen3.5:9b           big
qwen3:14b            big
qwen3:30b-a3b        xl     # Qwen3 30B MoE (3B active) — fast for its size
qwen3.6:27b          xl     # Qwen3.6 dense 27B
qwen3.6:35b-a3b      xl     # Qwen3.6 35B MoE (3B active)
qwen2.5:32b          xl
qwen2.5:72b          xl     # q4 ~47GB — needs the 80GB A100; SLOWEST, run last
''')
!python offline_eval/seed_ollama_configs.py
# Show the per-family sampling each model will use (from apps/llm/model_profiles).
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
   "still on disk. This clears every previously-pulled Ollama model and the package "
   "caches **before** the first download, then prints free space. On a fresh VM it's "
   "a harmless no-op.")
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

md("## Cell 9 — run the MULTI-TURN sweep (pulls + scores each model; resume-safe)\n"
   "`MODE=--multi-turn` runs full sessions. `RESULTS_DIR=…/multi_turn_results` writes "
   "to Drive. `CLEANUP_MODELS=1` deletes each model's weights right after it's scored "
   "so peak disk ≈ one model at a time. **Expect hours** — the run tolerates "
   "disconnects (done models are skipped on restart). `run_eval` prints a "
   "`>> Tutor engine: simple_tutor` banner at the top of each model — confirm it.")
code(r"""
!RESULTS_DIR=$PWD/offline_eval/multi_turn_results SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 \
  MODE="--multi-turn" bash offline_eval/run_matrix.sh
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

md("## Cell 10 — multi-turn leaderboard (run anytime; scores whatever is on Drive)")
code(r"""
!RESULTS_DIR=$PWD/offline_eval/multi_turn_results python offline_eval/aggregate.py
""")

md(rf"""
## After a Colab disconnect (A100 Pro: long sessions, but not infinite)
Re-run **Cells 1–8b**, then **Cell 9** again. Because results live on Drive (Cell 7),
`run_matrix.sh` **skips already-scored models** and continues. To pick up new commits
on the branch, re-run **Cell 2** (it re-clones).

**Strategy for the slow tail.** The small + MoE models finish first (Cell 8 order).
If the 27b/32b/**72b** dense models are too slow for your session budget, just stop —
you'll still have a complete small/MoE board, and you can resume the big ones in a
later session (or skip 72b entirely by commenting it out in Cell 8).

To pull results back to your laptop: copy the JSONs from
`MyDrive/{DRIVE_FOLDER}/` into the repo's `offline_eval/multi_turn_results/` and run
`RESULTS_DIR=offline_eval/multi_turn_results python offline_eval/aggregate.py`.

**Sanity checks per model:**
- The `run_eval` banner reads `>> Tutor engine: simple_tutor` (NOT conversational_tutor).
- `multi_turn_results/<model>.log` shows sessions reaching `exit_ticket` / `completed`
  (not all `deadlock`); each scored session's `rubric_result.model` is
  `claude-sonnet-4-6`.
- Reasoning models: `[OllamaTools] response: ... blocks=['tool_use', ...]` (or `['text']`),
  **not** `blocks=[]` (empty = num_ctx still truncating).
""")

nb = {
    "cells": cells,
    "metadata": {
        "accelerator": "GPU",
        "colab": {"provenance": [], "gpuType": "A100"},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}

out = Path(__file__).resolve().parent / 'colab_eval.ipynb'
out.write_text(json.dumps(nb, indent=1))
print(f"wrote {out} ({len(cells)} cells)")
