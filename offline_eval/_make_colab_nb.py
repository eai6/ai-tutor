"""Generate offline_eval/colab_eval.ipynb — a ready-to-run Colab notebook for the
MULTI-TURN evaluation (Improved Eval 3) of the top-15 OSS Qwen models on an A100.

Tutor = OSS via Ollama (swapped per model); student-sim = Anthropic Haiku 4.5;
judge = Anthropic Sonnet 4.6 (multi-turn default, per the Haiku-vs-Sonnet A/B).
Results land in offline_eval/multi_turn_results/<SWEEP>/ (persisted to Drive).

Run: python offline_eval/_make_colab_nb.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'                       # the repo you collaborate on (origin)
BRANCH = 'pixeldesignlabs-dev-portuguese'
DRIVE_FOLDER = 'ai-tutor-eval-multi-turn'    # Drive folder that persists results
SWEEP = 'sweep2'                             # results subfolder — one per sweep
SUBSET = 'core15'                            # stratified 15-scenario subset (~half the wall-clock)
RESULTS = f'offline_eval/multi_turn_results/{SWEEP}'

# Read the subset straight off the dataset so the notebook can never drift from
# the YAML tags. Used by Cell 10b, which must filter sweep 1 by scenario id (its
# stored rows predate the tag).
_ROOT = Path(__file__).resolve().parents[1]
CORE15_IDS = sorted(
    p.stem for p in (_ROOT / 'evals' / 'dataset' / 'multi_turn').glob('*.yaml')
    if f'{SUBSET}]' in p.read_text() or f'{SUBSET},' in p.read_text()
)
assert len(CORE15_IDS) == 15, f'expected 15 {SUBSET} scenarios, found {len(CORE15_IDS)}'

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

**Run one tab per GROUP (Colab Pro+ allows concurrent A100/H100 sessions).** The 9
OSS Qwen models are split into 3 **disjoint** groups; pick this tab's group in the
**Cell 7b** dropdown. Every tab writes per-model JSONs into the **same** Drive
folder, so no two tabs score the same model and the board merges automatically:

- **group1** — `qwen3.5:4b`, `qwen3:4b`, `qwen3.5:9b` *(small)*
- **group2** — `qwen3:14b`, `qwen3:30b-a3b`, `qwen3.6:27b`
- **group3** — `qwen3.6:35b-a3b`, `qwen2.5:32b`, `qwen2.5:72b`

Each model auto-tunes to its family profile (Qwen → Markdown Block-0, temp 0.7 /
top_p 0.8 / top_k 20, num_ctx 24K so `<think>` + answer fit).

## What changed since sweep 1

**1. This sweep runs the `{SUBSET}` subset — 15 scenarios, not 30.** They are a
stratified sample of the full set: all 6 personas, both subjects, all 4 lessons,
and the 6/12/15/24-turn budget extremes. Re-scoring sweep 1's 14 models on just
these 15 reproduces the full-30 ranking (Spearman **0.97**, bootstrap 0.95±0.04)
for **~50% of the wall-clock**.

**2. The engine's tool protocol was repaired.** Sweep 1 was largely measuring
tool-protocol compliance rather than teaching: duplicate `pose_question` calls
were overwriting the graded question (gemini-3.1-pro emitted 139 in one turn),
`tool_choice` forcing was hard-gated to Gemini, and Ollama silently dropped it.
All fixed. A model that skips the protocol is now repaired on the turn's existing
second call, so a repaired turn costs no more than a correct one.

**3. Results are versioned per sweep.** Sweep 1 lives in
`multi_turn_results/sweep1/`; this run writes to **`multi_turn_results/{SWEEP}/`**.
Compare like with like: score sweep 1 on `{SUBSET}` before reading any delta.

**Output → `{RESULTS}/`** (symlinked to Drive, resume-safe).

> ⚠️ **Runtime.** Each model runs 15 scenarios × up to ~24 turns × (tutor +
> student + judge) calls. The **small + MoE** models (4b/9b/14b/30b-a3b/35b-a3b)
> are hours-scale; the **large dense** ones (27b/32b/**72b**) are slower — though
> note sweep 1 showed the 72b generates only ~161 tokens per call while the
> reasoning models emit thousands, so the 4b/35b models dominate wall-clock, not
> the 72b. The run is **resume-safe** (finished models are skipped, results live
> on Drive), so you can stop/restart freely.

**Before you start**
1. Runtime → **Change runtime type** → pick the GPU for THIS tab's group (High-RAM):
   **group1** T4/L4 ok · **group2** L4 or A100 · **group3** **A100 80 GB or H100**
   (72b q4 ~47 GB + 32b/35b need the big card — T4/L4 can't fit the 72b).
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
import subprocess, time, shutil, os
# Robust install: the official installer pulls the binary from github releases,
# which intermittently times out on Colab VMs. Retry it, then fall back to the
# standalone linux binary straight from ollama.com (no github).
def _has_ollama():
    return shutil.which('ollama') is not None
def _install_ollama():
    if _has_ollama():
        return True
    for i in range(1, 4):
        print(f'[ollama] install.sh attempt {i}', flush=True)
        subprocess.run('curl -fsSL https://ollama.com/install.sh | sh', shell=True)
        if _has_ollama():
            return True
        time.sleep(5)
    for i in range(1, 4):
        print(f'[ollama] direct-binary attempt {i} (ollama.com)', flush=True)
        subprocess.run('curl -fL --retry 5 --retry-all-errors --connect-timeout 30 '
                       '-o /tmp/ollama.tgz https://ollama.com/download/ollama-linux-amd64.tgz',
                       shell=True)
        if os.path.exists('/tmp/ollama.tgz') and os.path.getsize('/tmp/ollama.tgz') > 1_000_000:
            subprocess.run('tar -C /usr -xzf /tmp/ollama.tgz', shell=True)
            if _has_ollama():
                return True
        time.sleep(5)
    return False
assert _install_ollama(), ("ollama install failed — Colab couldn't reach ollama.com/github. "
                           "Try Runtime -> Disconnect and delete runtime, then start fresh.")
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
   f"Symlinks **only `{RESULTS}/`** to `{DRIVE_FOLDER}/{SWEEP}/` on Drive. The rest of "
   "`multi_turn_results/` stays as cloned, so sweep 1's per-scenario JSONs remain on "
   "disk for the Cell-10b baseline. Resume-safe: a reconnect re-symlinks the same Drive "
   f"folder and `run_matrix.sh` skips models that already have a JSON in `{SWEEP}/`.")
code(rf"""
!mkdir -p /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP}
!rm -rf {RESULTS} && ln -s /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP} {RESULTS}
import os, glob
print('this sweep writes to:', os.path.realpath('{RESULTS}'))
done = sorted(os.path.basename(p)[:-5] for p in glob.glob('{RESULTS}/*.json'))
print('already scored in {SWEEP}:', done or '(none yet)')
print('sweep-1 JSONs available for the Cell-10b baseline:',
      len(glob.glob('offline_eval/multi_turn_results/sweep1/*.json')))
""")

md("## Cell 7b — **pick this tab's model GROUP** (run one tab per group)\n"
   "Set `GROUP` in the dropdown (right side in Colab) to `group1`, `group2`, or "
   "`group3`. Each group is a **disjoint** set of 3 models; every tab writes to the "
   "same Drive folder so the board merges. Re-run this cell whenever you change it.\n"
   "\n"
   "- **group1** — `qwen3.5:4b`, `qwen3:4b`, `qwen3.5:9b`  *(== the original "
   "full-list tab; skip if that's already running)*\n"
   "- **group2** — `qwen3:14b`, `qwen3:30b-a3b`, `qwen3.6:27b`\n"
   "- **group3** — `qwen3.6:35b-a3b`, `qwen2.5:32b`, `qwen2.5:72b`")
code(r"""
GROUP = "group2"  #@param ["group1", "group2", "group3"]
print("This tab will evaluate group:", GROUP)
""")

md("## Cell 8 — write THIS tab's matrix (from GROUP) + seed configs\n"
   "Writes only the 3 models for the group picked in **Cell 7b** into `models.txt`, "
   "then prints the per-family sampling each will use. `run_matrix.sh` (Cell 9) reads "
   "this file. Resume-safe: any model already scored on Drive is skipped.")
code(r"""
# Master registry (tag -> tier, note) + the 3 disjoint groups. Cell 7b's GROUP
# picks which 3 models THIS tab writes into models.txt. Groups partition the 9
# OSS Qwen models so parallel Colab tabs never score the same model.
MODELS = {
    'qwen3.5:4b':      ('big', ''),
    'qwen3:4b':        ('big', ''),
    'qwen3.5:9b':      ('big', ''),
    'qwen3:14b':       ('big', ''),
    'qwen3:30b-a3b':   ('xl',  'Qwen3 30B MoE (3B active) — fast for its size'),
    'qwen3.6:27b':     ('xl',  'Qwen3.6 dense 27B'),
    'qwen3.6:35b-a3b': ('xl',  'Qwen3.6 35B MoE (3B active)'),
    'qwen2.5:32b':     ('xl',  ''),
    'qwen2.5:72b':     ('xl',  'q4 ~47GB — needs an 80GB A100/H100; SLOWEST'),
}
GROUPS = {
    'group1': ['qwen3.5:4b', 'qwen3:4b', 'qwen3.5:9b'],
    'group2': ['qwen3:14b', 'qwen3:30b-a3b', 'qwen3.6:27b'],
    'group3': ['qwen3.6:35b-a3b', 'qwen2.5:32b', 'qwen2.5:72b'],
}
GROUP = globals().get('GROUP')
if GROUP is None:
    raise SystemExit("Run Cell 7b first to pick GROUP (group1/group2/group3).")
assert GROUP in GROUPS, f"GROUP must be one of {list(GROUPS)}, got {GROUP!r}"

lines = [f"# Multi-turn Eval 3 — {GROUP} (this tab)"]
for tag in GROUPS[GROUP]:
    tier, note = MODELS[tag]
    lines.append(f"{tag:<20} {tier}" + (f"     # {note}" if note else ""))
open('offline_eval/models.txt', 'w').write("\n".join(lines) + "\n")
print(f">> {GROUP}: this tab will evaluate")
for tag in GROUPS[GROUP]:
    print("   -", tag)
print()

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
   f"`MODE=\"--multi-turn --subset {SUBSET}\"` runs full sessions on the 15-scenario "
   f"stratified subset. `RESULTS_DIR=…/{SWEEP}` writes to Drive. `CLEANUP_MODELS=1` "
   "deletes each model's weights right after it's scored so peak disk ≈ one model at a "
   "time. The run tolerates disconnects (done models are skipped on restart).\n\n"
   "**Sanity-check the first model's output before walking away:**\n"
   "- `>> Tutor engine: simple_tutor` — not the legacy engine.\n"
   f"- `=== Running 15 scenario(s) ===` — the `{SUBSET}` filter took effect. If it says "
   "30, the branch is stale: re-run Cell 2.\n"
   "- No `dropped duplicate pose_question` storms, and few `record_answer without "
   "in-flight question` lines — those are the sweep-1 defects the fixes target.")
code(rf"""
!RESULTS_DIR=$PWD/{RESULTS} SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 \
  MODE="--multi-turn --subset {SUBSET}" bash offline_eval/run_matrix.sh
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

md(f"## Cell 10 — multi-turn leaderboard (run anytime; scores whatever is in `{SWEEP}/`)\n"
   "Pass rates here are out of **15**, so they are NOT comparable to sweep 1's out-of-30 "
   "numbers. Cell 10b prints the apples-to-apples baseline.")
code(rf"""
!RESULTS_DIR=$PWD/{RESULTS} python offline_eval/aggregate.py
""")

md(f"## Cell 10b — sweep-1 baseline on the same 15 scenarios (apples-to-apples)\n"
   f"Re-scores sweep 1's stored per-scenario results restricted to the `{SUBSET}` ids, so "
   "you can read the delta the engine fixes actually produced. Filters by **scenario id**, "
   f"not by tag: sweep 1 ran before the `{SUBSET}` tag existed, so its stored rows do not "
   "carry it. Needs `sweep1/` on Drive; otherwise skip — this cell is diagnostic only.")
code(rf"""
import json, glob, os
CORE15 = {CORE15_IDS!r}
SUB = 'offline_eval/multi_turn_results/sweep1'
paths = [p for p in sorted(glob.glob(f'{{SUB}}/*.json'))
         if not os.path.basename(p).startswith('_')]
if not paths:
    print('sweep1/ not present on this VM — skip (diagnostic only)')
else:
    rows = []
    for p in paths:
        m = os.path.basename(p)[:-5]
        R = json.load(open(p))['results']
        sub = [r for r in R if r['scenario_id'] in CORE15]
        if len(sub) != 15:
            print(f'  !! {{m}}: only {{len(sub)}}/15 core15 scenarios present — skipping')
            continue
        rows.append((m, 100*sum(r['passed'] for r in sub)/15))
    print(f'{{"MODEL":<26}} {{"sweep1 @ core15":>16}}')
    print('-'*44)
    for m, rate in sorted(rows, key=lambda r: -r[1]):
        print(f'{{m:<26}} {{rate:>14.0f}}%  ({{round(rate*15/100)}}/15)')
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

To pull results back to your laptop: copy the JSONs + logs from
`MyDrive/{DRIVE_FOLDER}/{SWEEP}/` into the repo's `{RESULTS}/` and run
`RESULTS_DIR={RESULTS} python offline_eval/aggregate.py`.

**Sanity checks per model:**
- The `run_eval` banner reads `>> Tutor engine: simple_tutor` (NOT conversational_tutor).
- It reports `=== Running 15 scenario(s) ===`. If it says 30, the clone is stale —
  the `{SUBSET}` tags are missing. Re-run **Cell 2**.
- `{SWEEP}/<model>.log` shows sessions reaching `exit_ticket` / `completed`
  (not all `deadlock`); each scored session's `rubric_result.model` is
  `claude-sonnet-4-6`.
- Reasoning models: `[OllamaTools] response: ... blocks=['tool_use', ...]` (or `['text']`),
  **not** `blocks=[]` (empty = num_ctx still truncating).

**New in sweep 2 — the fixes should show up in the logs:**
- `dropped duplicate pose_question` — the cap firing. A few is fine; a storm means a
  model is spamming parallel calls (that was gemini-3.1-pro's 139-per-turn bug).
- `record_answer without in-flight question` — should be **rare**. In sweep 1 this hit
  90% of qwen3.5:4b's grading attempts.
- `call2_repair: Call 1 skipped …` — the repair riding on the second call. Expected on
  Ollama models, which cannot honour forced tool choice.
- `grading a STALE slot` — new diagnostic. Grep it afterwards; it tells us whether
  stale-question grading is a real problem or a rare one.
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
