"""Generate offline_eval/colab_eval.ipynb — a ready-to-run Colab notebook for the
SINGLE-TURN evaluation (sweep 3) of 13 OSS Qwen + Gemma models on an A100.

Tutor = OSS via Ollama (swapped per model); student-sim = Anthropic Haiku 4.5;
judge = Anthropic Haiku 4.5 @ temp 0 (evals/scorers/llm_rubric.py default).
Results land in offline_eval/single_turn_results/<SWEEP>/ (persisted to Drive).

Run: python offline_eval/_make_colab_nb.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'                       # the repo you collaborate on (origin)
BRANCH = 'pixeldesignlabs-dev-portuguese'
DRIVE_FOLDER = 'ai-tutor-eval-single-turn'   # Drive folder that persists results
SWEEP = 'sweep3'                             # results subfolder — one per sweep

# The single-turn set is 200 scenarios, balanced across 6 personas x 16 lessons x
# 26 archetypes. This sweep runs the FULL 200 -- not a sample -- so the OSS models
# are scored on exactly the same scenarios as the Gemini / Vertex Model Garden
# models in the laptop sweep (single_turn_results/sweep3/). Same denominator, same
# scenarios, one combined leaderboard.
#
# Set SAMPLE to an int (with SEED) to cut wall-clock; the draw is seeded so every
# model still sees the same scenarios. But a sampled OSS run is NOT comparable to
# the full-200 cloud run, so only do that for a quick directional read.
SAMPLE = None
SEED = 0
RESULTS = f'offline_eval/single_turn_results/{SWEEP}'

_ROOT = Path(__file__).resolve().parents[1]
_ST = [p for p in (_ROOT / 'evals' / 'dataset').rglob('*.yaml')
       if 'smoke' not in p.parts and 'multi_turn' not in p.parts]
assert len(_ST) == 200, f'expected 200 single-turn scenarios, found {len(_ST)}'

MODE = '--single-turn' if SAMPLE is None else f'--single-turn --sample {SAMPLE} --seed {SEED}'
N_SCENARIOS = len(_ST) if SAMPLE is None else SAMPLE

cells = []

def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})

def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(rf"""
# AI Tutor — **Single-turn Eval (sweep 3)** · OSS Qwen + Gemma · Colab **A100**

**Single graded turns**: each scenario seeds a conversation and one student
utterance; the model produces the next tutor turn, which is then scored. Same
harness, same 200 scenarios, and the same judge as the laptop cloud sweep — so the
OSS models here land on **one leaderboard** with the Gemini / Vertex Model Garden
models.

- **Tutor** = OSS Qwen / Gemma via Ollama (swapped per model with `TUTOR_MODEL_OVERRIDE`).
- **Judge** = Anthropic **Haiku 4.5** @ temp 0 (`evals/scorers/llm_rubric.py`
  default — identical to the cloud sweep, so scores are comparable).
- **Engine** = `simple_tutor` (the production engine; `run_eval` prints a banner
  confirming it and errors on the legacy engine).

**Run one tab per GROUP (Colab Pro+ allows concurrent A100/H100 sessions).** The 13
OSS Qwen + Gemma models are split into 3 **disjoint size groups**; pick this tab's
group in the **Cell 7b** dropdown. Every tab writes per-model JSONs into the
**same** Drive folder, so no two tabs score the same model and the board merges
automatically:

- **group1 — small (≤4B)** — `gemma3:1b`, `gemma2:2b`, `qwen3:4b`, `qwen3.5:4b`, `gemma3:4b`
- **group2 — medium (9–14B)** — `gemma2:9b`, `qwen3.5:9b`, `gemma3:12b`, `qwen3:14b`
- **group3 — large (27B+)** — `gemma2:27b`, `gemma3:27b`, `qwen3:30b-a3b`, `qwen3.6:35b-a3b`

Each model auto-tunes to its family profile (Qwen → Markdown Block-0, temp 0.7 /
top_p 0.8 / top_k 20, num_ctx 24K so `<think>` + answer fit; Gemma runs the
profile registered in `apps/llm/model_profiles`, or engine defaults if none).

## What changed since sweep 2

**1. The dataset was rebuilt. Sweep 3 is a NEW BASELINE — the old 60-scenario
single-turn numbers are not comparable to it.** The single-turn set went 60 →
**200 scenarios**, re-grounded on **16 lessons** (up from 4), and balanced by
construction across 6 personas × 16 lessons × 26 situation archetypes. Both the
denominator and the content changed, so there is no honest delta against the
earlier board. Start the trend line here.

**2. Why the dataset grew.** The old set was badly skewed — 8 of 24 persona×lesson
cells were empty, and `error_prone` had exactly ONE single-turn scenario, so any
per-persona claim drawn from it was unsupported. Two of four lessons carried 85% of
the content. On top of that, at n=60 two models had to differ by ~18pp before the
benchmark could tell them apart; at n=200 that drops to ~10pp.

**3. This run scores the FULL 200 — no sampling.** That is deliberate: the
Gemini / Vertex Model Garden models are being scored on the same 200 scenarios in
the laptop sweep, so the OSS results merge into **one leaderboard** with the same
denominator. (`SAMPLE` in `_make_colab_nb.py` can cut wall-clock, but a sampled OSS
run is not comparable to the full-200 cloud run.)

**4. Results are versioned per sweep.** This run writes to
**`single_turn_results/{SWEEP}/`** — the SAME folder the cloud sweep writes to, so
`aggregate.py` boards them together.

**Output → `{RESULTS}/`** (symlinked to Drive, resume-safe).

> ⚠️ **Runtime.** Each model runs 200 scenarios × (2 tutor calls + judge). Single
> turns are far cheaper than full sessions — no student-sim, no 24-turn
> trajectories — but 200 scenarios is 3.3x the old 60, so budget accordingly. The
> reasoning models (which emit thousands of `<think>` tokens per call) dominate
> wall-clock, not the big dense ones. The run is **resume-safe** (finished models
> are skipped, results live on Drive), so you can stop/restart freely.

**Before you start**
1. Runtime → **Change runtime type** → pick the GPU for THIS tab's group (High-RAM):
   **group1** (≤4B) T4/L4 ok · **group2** (9–14B) L4 or A100 · **group3** (27B+)
   **A100 40 GB** — the 27b dense and 35b-a3b MoE q4 weights (~17–22 GB) plus
   KV cache need more than a T4/L4 comfortably provides.
2. Add these **Colab Secrets** (🔑 sidebar), each *Notebook access ON*:
   - `GH_TOKEN` — GitHub **classic** PAT with **`repo`** scope (you're a collaborator on `{REPO}`).
   - `ANTHROPIC_API_KEY` — **required** (rubric judge, Haiku 4.5 @ temp 0).
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
   "the rubric judge (Haiku 4.5); GOOGLE/OPENAI feed the "
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
   "tickets** (16 lessons: 8 math x 10 steps, 8 geography x 5) — the scenarios ground "
   "step, so this fixture is required. `institution.json` seeds the active "
   "`student_sim` ModelConfig (Haiku).")
code(r"""
!python manage.py migrate
!python manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json
""")

md("## Cell 7 — persist results to Drive (symlink → survives disconnects)\n"
   f"Symlinks **only `{RESULTS}/`** to `{DRIVE_FOLDER}/{SWEEP}/` on Drive. The rest of "
   "`single_turn_results/` stays as cloned, so the old 60-scenario board remains on "
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
      len(glob.glob('offline_eval/single_turn_results/results/*.json')))
""")

md("## Cell 7b — **pick this tab's model GROUP** (run one tab per group)\n"
   "Set `GROUP` in the dropdown (right side in Colab) to `group1`, `group2`, or "
   "`group3`. The groups are **disjoint, size-based** sets; every tab writes to the "
   "same Drive folder so the board merges. Re-run this cell whenever you change it.\n"
   "\n"
   "- **group1 — small (≤4B)** — `gemma3:1b`, `gemma2:2b`, `qwen3:4b`, "
   "`qwen3.5:4b`, `gemma3:4b`\n"
   "- **group2 — medium (9–14B)** — `gemma2:9b`, `qwen3.5:9b`, `gemma3:12b`, "
   "`qwen3:14b`\n"
   "- **group3 — large (27B+)** — `gemma2:27b`, `gemma3:27b`, `qwen3:30b-a3b`, "
   "`qwen3.6:35b-a3b`")
code(r"""
GROUP = "group2"  #@param ["group1", "group2", "group3"]
print("This tab will evaluate group:", GROUP)
""")

md("## Cell 8 — write THIS tab's matrix (from GROUP) + seed configs\n"
   "Writes only the models for the group picked in **Cell 7b** into `models.txt`, "
   "then prints the per-family sampling each will use. `run_matrix.sh` (Cell 9) reads "
   "this file. Resume-safe: any model already scored on Drive is skipped.")
code(r"""
# Master registry (tag -> tier, note) + the 3 disjoint size groups. Cell 7b's
# GROUP picks which models THIS tab writes into models.txt. Groups partition
# the 13 OSS Qwen + Gemma models so parallel Colab tabs never score the same
# model. group1 = small (<=4B), group2 = medium (9-14B), group3 = large (27B+).
MODELS = {
    # ── small (≤4B) ──
    'gemma3:1b':       ('big', ''),
    'gemma2:2b':       ('big', ''),
    'qwen3:4b':        ('big', ''),
    'qwen3.5:4b':      ('big', ''),
    'gemma3:4b':       ('big', ''),
    # ── medium (9–14B) ──
    'gemma2:9b':       ('big', ''),
    'qwen3.5:9b':      ('big', ''),
    'gemma3:12b':      ('big', ''),
    'qwen3:14b':       ('big', ''),
    # ── large (27B+; MoE tags sized by total params) ──
    'gemma2:27b':      ('xl',  'Gemma 2 dense 27B'),
    'gemma3:27b':      ('xl',  'Gemma 3 dense 27B'),
    'qwen3:30b-a3b':   ('xl',  'Qwen3 30B MoE (3B active) — fast for its size'),
    'qwen3.6:35b-a3b': ('xl',  'Qwen3.6 35B MoE (3B active)'),
}
GROUPS = {
    'group1': ['gemma3:1b', 'gemma2:2b', 'qwen3:4b', 'qwen3.5:4b',
               'gemma3:4b'],
    'group2': ['gemma2:9b', 'qwen3.5:9b', 'gemma3:12b', 'qwen3:14b'],
    'group3': ['gemma2:27b', 'gemma3:27b', 'qwen3:30b-a3b',
               'qwen3.6:35b-a3b'],
}
GROUP = globals().get('GROUP')
if GROUP is None:
    raise SystemExit("Run Cell 7b first to pick GROUP (group1/group2/group3).")
assert GROUP in GROUPS, f"GROUP must be one of {list(GROUPS)}, got {GROUP!r}"

lines = [f"# Single-turn Eval (sweep 3) — {GROUP} (this tab)"]
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

md("## Cell 9 — run the SINGLE-TURN sweep (pulls + scores each model; resume-safe)\n"
   f"`MODE=\"{MODE}\"` scores all {N_SCENARIOS} single-turn scenarios. "
   f"`RESULTS_DIR=…/{SWEEP}` writes to Drive. `CLEANUP_MODELS=1` "
   "deletes each model's weights right after it's scored so peak disk ≈ one model at a "
   "time. The run tolerates disconnects (done models are skipped on restart).\n\n"
   "**Sanity-check the first model's output before walking away:**\n"
   "- `>> Tutor engine: simple_tutor` — not the legacy engine.\n"
   f"- `>> SAMPLED RUN: {N_SCENARIOS} of 200 scenarios (seed={SEED})` — the draw took "
   "effect. If it says 200, `SAMPLE` is None and you are doing a full publication run "
   "(expect ~3x the wall-clock).\n"
   f"- `=== Running {N_SCENARIOS} scenario(s) ===`. If the count is wrong, the branch is "
   "stale: re-run Cell 2.\n"
   "- No `dropped duplicate pose_question` storms, and few `record_answer without "
   "in-flight question` lines.")
code(rf"""
!RESULTS_DIR=$PWD/{RESULTS} SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 \
  MODE="{MODE}" bash offline_eval/run_matrix.sh
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

md(f"## Cell 10 — single-turn leaderboard (run anytime; scores whatever is in `{SWEEP}/`)\n"
   f"Pass rates are out of **{N_SCENARIOS}**. Every model was scored on the same seeded "
   "draw, so the models are comparable to EACH OTHER. They are **not** comparable to "
   "sweep 1 or sweep 2 — those ran a different dataset on different lessons.")
code(rf"""
!RESULTS_DIR=$PWD/{RESULTS} python offline_eval/aggregate.py
""")

md("## Cell 10b — error bars\n"
   "A pass rate without an interval invites over-reading. At "
   f"n={N_SCENARIOS} the standard error at p≈0.5 is ±{50/(N_SCENARIOS**0.5):.1f}pp, so two models "
   f"must differ by roughly {1.96*(2**0.5)*50/(N_SCENARIOS**0.5):.0f}pp before the gap means anything. "
   "This cell prints a Wilson 95% interval per model — read the intervals, not the "
   "point estimates.")
code(rf"""
import json, glob, os, math

def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = k / n
    d = 1 + z*z/n
    c = p + z*z/(2*n)
    s = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))
    return (100*(c - s)/d, 100*(c + s)/d)

paths = [p for p in sorted(glob.glob('{RESULTS}/*.json'))
         if not os.path.basename(p).startswith('_')]
rows = []
for p in paths:
    m = os.path.basename(p)[:-5]
    R = json.load(open(p))['results']
    k, n = sum(bool(r['passed']) for r in R), len(R)
    lo, hi = wilson(k, n)
    rows.append((m, k, n, 100*k/n, lo, hi))

print(f'{{"MODEL":<28}} {{"PASS":>9}}  {{"RATE":>6}}   95% CI')
print('-'*66)
for m, k, n, rate, lo, hi in sorted(rows, key=lambda r: -r[3]):
    print(f'{{m:<28}} {{k:>4}}/{{n:<4}} {{rate:>5.0f}}%   [{{lo:>4.0f}}, {{hi:>4.0f}}]')
""")

md(rf"""
## After a Colab disconnect (A100 Pro: long sessions, but not infinite)
Re-run **Cells 1–8b**, then **Cell 9** again. Because results live on Drive (Cell 7),
`run_matrix.sh` **skips already-scored models** and continues. To pick up new commits
on the branch, re-run **Cell 2** (it re-clones).

**Strategy for the slow tail.** The small + MoE models finish first (Cell 8 order).
If the 27b dense models are too slow for your session budget, just stop —
you'll still have a complete small/MoE board, and you can resume the big ones in a
later session (or trim group3 by commenting models out in Cell 8).

To pull results back to your laptop: copy the JSONs + logs from
`MyDrive/{DRIVE_FOLDER}/{SWEEP}/` into the repo's `{RESULTS}/` and run
`RESULTS_DIR={RESULTS} python offline_eval/aggregate.py`.

**Sanity checks per model:**
- The `run_eval` banner reads `>> Tutor engine: simple_tutor` (NOT conversational_tutor).
- It reports `=== Running {N_SCENARIOS} scenario(s) ===`. If the count is wrong, the clone
  is stale — re-run **Cell 2**.
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
