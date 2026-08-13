"""Generate offline_eval/colab_eval.ipynb — a ready-to-run Colab notebook for the
MULTI-TURN evaluation of 10 OSS Qwen + Gemma models on Colab GPUs.

Tutor = OSS via Ollama (swapped per model); student-sim = Anthropic Haiku 4.5
persona player; judge = Anthropic Sonnet 4.6 @ temp 0 session-level rubric
(evals/runner.py MULTI_TURN_RUBRIC_JUDGE).
Results land in offline_eval/multi_turn_results/<SWEEP>/ (persisted to Drive).

Run: python offline_eval/_make_colab_nb.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'                       # the repo you collaborate on (origin)
BRANCH = 'pixeldesignlabs-dev-portuguese'
DRIVE_FOLDER = 'ai-tutor-eval-multiturn'     # Drive folder that persists results
SWEEP = 'mt50'                               # 50-scenario board; cloud legs (run_cloud.sh) write to the same-named local folder for one merged leaderboard

# Multi-turn: each scenario is a FULL tutoring session (10-30 turns) driven by
# the Anthropic-Haiku student simulator and scored by a Sonnet-4.6 session-level
# rubric plus deterministic assertions. SAMPLE=50/SEED=5 is the mt50 board — a
# NEW, larger seeded draw (a different set than the old 20-scenario fixcheck
# draw; n=50 halves the confidence interval, ~±14pp -> ~±10pp at p=0.5). The
# cloud legs (offline_eval/cloud_models_mt50.txt via run_cloud.sh) run the
# SAME draw, so OSS + cloud land on one leaderboard. SAMPLE=None = full 200.
SAMPLE = 50
SEED = 5
RESULTS = f'offline_eval/multi_turn_results/{SWEEP}'

_ROOT = Path(__file__).resolve().parents[1]
_MT = [p for p in (_ROOT / 'evals' / 'dataset' / 'multi_turn').rglob('*.yaml')
       if 'smoke' not in p.parts]
assert len(_MT) == 200, f'expected 200 multi-turn scenarios, found {len(_MT)}'

MODE = '--multi-turn' if SAMPLE is None else f'--multi-turn --sample {SAMPLE} --seed {SEED}'
N_SCENARIOS = len(_MT) if SAMPLE is None else SAMPLE

cells = []

def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})

def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(rf"""
# AI Tutor — **Multi-turn Eval** · OSS Qwen + Gemma · Colab GPU

**Full tutoring sessions**: each scenario drives a complete 10–30-turn session —
an Anthropic-Haiku student simulator plays one of five personas (capable, average,
struggler, error-prone, non-responder) against the OSS tutor, and the whole
transcript is scored by deterministic assertions (turn budgets, repetition,
tool-syntax leaks, expected end-state) plus a session-level rubric.

- **Tutor** = OSS Qwen / Gemma via Ollama (swapped per model with `TUTOR_MODEL_OVERRIDE`).
- **Student sim** = Anthropic **Haiku 4.5** persona player (constant across models).
- **Judge** = Anthropic **Sonnet 4.6** @ temp 0 session-level rubric
  (`evals/runner.py` `MULTI_TURN_RUBRIC_JUDGE` — identical to the engine
  fix-cycle sweeps, so scores are comparable).
- **Engine** = `simple_tutor` (the production engine; `run_eval` prints a banner
  confirming it and errors on the legacy engine).
- **Benchmark draw** = the mt50 board: {SAMPLE} scenarios, seed {SEED}. A NEW
  baseline — a different (larger) draw than the 20-scenario fixcheck board, so
  compare only against other mt50 runs. The cloud fleet (Anthropic, Gemini,
  Vertex Model Garden — `cloud_models_mt50.txt` via `run_cloud.sh`) runs the
  same draw for one merged leaderboard.

**Run one tab per GROUP (Colab Pro+ allows concurrent A100/H100 sessions).** The 10
OSS Qwen + Gemma models are split into 3 **disjoint size groups**; pick this tab's
group in the **Cell 7b** dropdown. Every tab writes per-model JSONs into the
**same** Drive folder, so no two tabs score the same model and the board merges
automatically:

- **group1 — small (≤4B)** — `qwen3:4b`, `qwen3.5:4b`
- **group2 — medium (9–14B)** — `qwen3.5:9b`, `qwen3:14b`
- **group3 — large (27B+)** — `qwen3:30b-a3b`, `qwen3.6:35b-a3b`

**This run is the qwen re-validation** of the OSS-sweep engine nets (dedupe,
verdict-polarity alignment, reveal filter, auto-grade fallback, extended
retries). The Gemma entries are commented out in Cell 8 pending the
`colab_eval_gemma.ipynb` smoke — uncomment them there once it's clean.

Gemma runs through **`okamototk/gemma3-tools`** (community repackaging of the official
gemma3 weights with a tool-enabled chat template) — stock Ollama gemma3/gemma2
templates have no tool role, so every tools request 400s before the model sees a
prompt; that zeroed all 7 Gemmas in the first sweep. gemma2 has no tool-enabled
repackaging and is out until one exists. **Cell 8c probes every model** and drops
any that cannot tool-call, so an incapable model costs seconds, not 20 sessions.

Each model auto-tunes to its family profile (Qwen → Markdown Block-0, temp 0.7 /
top_p 0.8 / top_k 20, num_ctx 24K so `<think>` + answer fit; Gemma runs the
profile registered in `apps/llm/model_profiles`, or engine defaults if none).

## Read this before budgeting a session

**Multi-turn is an order of magnitude heavier than single-turn.** Each scenario
is a full session: every turn costs 1–2 tutor calls (Ollama, GPU-bound) plus a
student-sim call and, at session end, one long-context Sonnet rubric call. On the
cloud sweeps a 20-scenario leg took ≈40 min for a fast API model and 2.5–4 h for
a slow thinking model; local Ollama inference speed will dominate here — expect
the small models to clear a leg in well under an hour on a T4/L4 and the 27B+
group to need a few hours on an A100.

**Scores read against the engine fix-cycle board.** Same 20 scenarios, same
seed, same student sim, same judge — the only variable is the tutor model. The
engine's guard stack (forced tool calls, pose salvage, letter coherence,
vocabulary scrub) is family-gated and active for Ollama models exactly as it was
for the cloud OSS models.

**Output → `{RESULTS}/`** (symlinked to Drive, resume-safe).

> ⚠️ **Runtime.** Each model runs {N_SCENARIOS} full sessions of 10–30 turns; every
> turn is 1–2 Ollama tutor calls plus a Haiku student-sim call, and each session
> ends with one long-context Sonnet rubric call. Thinking models (which emit
> thousands of `<think>` tokens per turn) dominate wall-clock. The run is
> **resume-safe** (finished models are skipped, results live on Drive), so you
> can stop/restart freely.

**Before you start**
1. Runtime → **Change runtime type** → pick the GPU for THIS tab's group (High-RAM):
   **group1** (≤4B) T4/L4 ok · **group2** (9–14B) L4 or A100 · **group3** (27B+)
   **A100 40 GB** — the 27b dense and 35b-a3b MoE q4 weights (~17–22 GB) plus
   KV cache need more than a T4/L4 comfortably provides.
2. Add these **Colab Secrets** (🔑 sidebar), each *Notebook access ON*:
   - `GH_TOKEN` — GitHub **classic** PAT with **`repo`** scope (you're a collaborator on `{REPO}`).
   - `ANTHROPIC_API_KEY` — **required** (student-sim Haiku 4.5 + Sonnet 4.6 session rubric judge).
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
os.chdir('/content')   # a re-run's cwd may be the about-to-be-deleted clone
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
# PINNED to 0.30.7 — the version the gemma3-tools tool templates were verified
# against (5/5 local probe passes); newer Ollama stopped parsing the repackaged
# template's tool calls. The check is on the VERSION, not mere presence: a
# reused Colab VM keeps its previously-installed binary across kernel
# restarts, which silently skipped the pin and re-broke the probe.
PIN = '0.30.7'
def _pinned_ollama():
    if shutil.which('ollama') is None:
        return False
    v = subprocess.run(['ollama', '--version'], capture_output=True, text=True)
    return PIN in (v.stdout + v.stderr)
def _install_ollama():
    if _pinned_ollama():
        return True
    if shutil.which('ollama') is not None:
        print('[ollama] wrong version present — removing before pinned install', flush=True)
        subprocess.run('pkill -f "ollama serve" || true', shell=True)
        subprocess.run('rm -f /usr/local/bin/ollama /usr/bin/ollama; '
                       'rm -rf /usr/local/lib/ollama /usr/lib/ollama', shell=True)
    for i in range(1, 4):
        print(f'[ollama] install.sh attempt {i} (pinned {PIN})', flush=True)
        subprocess.run(f'curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION={PIN} sh', shell=True)
        if _pinned_ollama():
            return True
        time.sleep(5)
    for i in range(1, 4):
        print(f'[ollama] github release attempt {i} (v{PIN})', flush=True)
        subprocess.run('curl -fL --retry 5 --retry-all-errors --connect-timeout 30 '
                       f'-o /tmp/ollama.tgz https://github.com/ollama/ollama/releases/download/v{PIN}/ollama-linux-amd64.tgz',
                       shell=True)
        if os.path.exists('/tmp/ollama.tgz') and os.path.getsize('/tmp/ollama.tgz') > 1_000_000:
            subprocess.run('tar -C /usr -xzf /tmp/ollama.tgz', shell=True)
            if _pinned_ollama():
                return True
        time.sleep(5)
    return False
assert _install_ollama(), (f"pinned ollama {PIN} install failed — "
                           "Disconnect and delete runtime, then retry")
subprocess.Popen(['ollama', 'serve'],
                 stdout=open('/content/ollama.log', 'w'),
                 stderr=subprocess.STDOUT)
for _ in range(30):
    if subprocess.run(['bash', '-c', 'ollama list'], capture_output=True).returncode == 0:
        subprocess.run(['ollama', '--version']); print('ollama ready'); break
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
   f"Symlinks **only `{RESULTS}/`** to `{DRIVE_FOLDER}/{SWEEP}/` on Drive. "
   "Resume-safe: a reconnect re-symlinks the same Drive "
   f"folder and `run_matrix.sh` skips models that already have a JSON in `{SWEEP}/`.")
code(rf"""
!mkdir -p /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP}
!rm -rf {RESULTS} && ln -s /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP} {RESULTS}
import os, glob
print('this sweep writes to:', os.path.realpath('{RESULTS}'))
done = sorted(os.path.basename(p)[:-5] for p in glob.glob('{RESULTS}/*.json'))
print('already scored in {SWEEP}:', done or '(none yet)')
""")

md("## Cell 7b — **pick this tab's model GROUP** (run one tab per group)\n"
   "Set `GROUP` in the dropdown (right side in Colab) to `group1`, `group2`, or "
   "`group3`. The groups are **disjoint, size-based** sets; every tab writes to the "
   "same Drive folder so the board merges. Re-run this cell whenever you change it.\n"
   "\n"
   "- **group1 — small (≤4B)** — `qwen3:4b`, `qwen3.5:4b`\n"
   "- **group2 — medium (9–14B)** — `qwen3.5:9b`, `qwen3:14b`\n"
   "- **group3 — large (27B+)** — `qwen3:30b-a3b`, `qwen3.6:35b-a3b`\n"
   "\n"
   "(Gemma lines are commented out in Cell 8 pending the gemma smoke.)")
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
# the 10 OSS Qwen + Gemma models so parallel Colab tabs never score the same
# model. group1 = small (<=4B), group2 = medium (9-14B), group3 = large (27B+).
# Gemma runs via okamototk/gemma3-tools — community repackagings of the official
# gemma3 QAT weights with a tool-enabled chat template (the stock ollama
# gemma3/gemma2 templates have NO tool role: every tools request 400s before
# the model sees a prompt, which zeroed all 7 Gemmas in the first oss13_mt
# sweep). gemma2 has no tool-enabled repackaging and is dropped until one
# exists. Cell 8c probes every model and drops any that cannot tool-call.
# Gemma entries are COMMENTED OUT pending the colab_eval_gemma.ipynb smoke
# (5-scenario go/no-go on the tool-enabled repackagings). Uncomment the
# gemma lines in MODELS and GROUPS together once the smoke is clean.
MODELS = {
    # ── small (≤4B) ──
    # 'okamototk/gemma3-tools:1b':  ('big', 'gemma3 1B, tool-enabled template'),
    'qwen3:4b':               ('big', ''),
    'qwen3.5:4b':             ('big', ''),
    # 'okamototk/gemma3-tools:4b':  ('big', 'gemma3 4B, tool-enabled template'),
    # ── medium (9–14B) ──
    'qwen3.5:9b':             ('big', ''),
    # 'okamototk/gemma3-tools:12b': ('big', 'gemma3 12B, tool-enabled template'),
    'qwen3:14b':              ('big', ''),
    # ── large (27B+; MoE tags sized by total params) ──
    # 'okamototk/gemma3-tools:27b': ('xl',  'gemma3 27B, tool-enabled template'),
    'qwen3:30b-a3b':          ('xl',  'Qwen3 30B MoE (3B active) — fast for its size'),
    'qwen3.6:35b-a3b':        ('xl',  'Qwen3.6 35B MoE (3B active)'),
}
GROUPS = {
    'group1': [  # 'okamototk/gemma3-tools:1b', 'okamototk/gemma3-tools:4b',
               'qwen3:4b', 'qwen3.5:4b'],
    'group2': [  # 'okamototk/gemma3-tools:12b',
               'qwen3.5:9b', 'qwen3:14b'],
    'group3': [  # 'okamototk/gemma3-tools:27b',
               'qwen3:30b-a3b', 'qwen3.6:35b-a3b'],
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
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.llm.model_profiles import get_model_profile
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

md("## Cell 8c — **tool-probe gate** (drops models that cannot tool-call)\n"
   "The engine's protocol needs native tool calling; a model whose Ollama "
   "template lacks a tool role 400s on every request and burns 20 dead "
   "sessions (that zeroed all 7 stock Gemmas in the first sweep). This cell "
   "pulls each of this tab's models, fires one probe tool-call, and REMOVES "
   "any model that fails from `models.txt` — a failure costs seconds. "
   "Weights stay cached for the sweep.")
code(r"""
import subprocess, sys

kept, dropped = [], []
lines = open('offline_eval/models.txt').read().splitlines()
for line in lines:
    bare = line.split('#')[0].split()
    if not bare:
        continue
    tag = bare[0]
    print(f'>> probing {tag} ...', flush=True)
    pull = subprocess.run(['ollama', 'pull', tag], capture_output=True, text=True)
    if pull.returncode != 0:
        print(f'   PULL FAILED — dropped. ({pull.stderr.strip()[:120]})')
        dropped.append((tag, 'pull failed'))
        continue
    probe = subprocess.run(
        [sys.executable, 'offline_eval/_probe_ollama_tools.py', tag],
        capture_output=True, text=True)
    out = probe.stdout + probe.stderr
    if 'TOOL-USE SUPPORTED' in out:
        print('   tool-call OK')
        kept.append(line)
    else:
        reason = 'HTTP 400 (no tool role in template)' if '400' in out else 'no tool_calls emitted'
        print(f'   NO TOOL SUPPORT — dropped. ({reason})')
        dropped.append((tag, reason))
        subprocess.run(['ollama', 'rm', tag], capture_output=True)
    subprocess.run(['ollama', 'stop', tag], capture_output=True)

header = [l for l in lines if l.strip().startswith('#')]
open('offline_eval/models.txt', 'w').write('\n'.join(header + kept) + '\n')
print()
print('will sweep:', [l.split()[0] for l in kept] or 'NOTHING')
if dropped:
    print('dropped   :', dropped)
assert kept, 'Every model in this group failed the tool probe — nothing to sweep.'
""")

md("## Cell 9 — run the MULTI-TURN sweep (pulls + scores each model; resume-safe)\n"
   f"`MODE=\"{MODE}\"` runs {N_SCENARIOS} full sessions per model. "
   f"`RESULTS_DIR=…/{SWEEP}` writes to Drive. `CLEANUP_MODELS=1` "
   "deletes each model's weights right after it's scored so peak disk ≈ one model at a "
   "time. The run tolerates disconnects (done models are skipped on restart).\n\n"
   "**Sanity-check the first model's output before walking away:**\n"
   "- `>> Tutor engine: simple_tutor` — not the legacy engine.\n"
   f"- The runner reports {N_SCENARIOS} scenarios; if it reports 200, `SAMPLE` is None "
   "and you are doing the full publication run (~10x wall-clock).\n"
   "- Sessions end with `exit_ticket` / `completed` — a wall of `deadlock` on the "
   "first model means something is wrong; stop and check the log.\n"
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

md(f"## Cell 10 — multi-turn leaderboard (run anytime; scores whatever is in `{SWEEP}/`)\n"
   f"Pass rates are out of **{N_SCENARIOS}** sessions. Every model ran the same seeded "
   "draw as the engine fix-cycle board, so these numbers read directly against "
   "gemini-2.5-flash 18/20, kimi-k2-thinking 18/20, and qwen3-next-80b 17/20 "
   "(cycle 11). An `errored` scenario is judge/sim infrastructure, not model "
   "failure — re-run those before comparing.")
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

md("## Cell 10c — session end-reasons (the multi-turn failure signature)\n"
   "Pass rate alone hides HOW sessions end. `exit_ticket`/`completed` are clean; "
   "`max_turns` = pacing; `deadlock` = the tutor repeated itself verbatim — the "
   "classic weak-model failure the guard stack exists to prevent. `errored` is "
   "infrastructure (judge/sim overload): re-run those scenarios before comparing.")
code(rf"""
import json, glob, os
from collections import Counter
print(f"{{'MODEL':<26}}{{'PASS':>8}}   SESSION END-REASONS")
print('-' * 70)
for f in sorted(glob.glob('{RESULTS}/*.json')):
    name = os.path.basename(f)[:-5]
    if name.startswith('_'):
        continue
    d = json.load(open(f))
    ends = Counter(r.get('sim_reason') or ('errored' if r.get('error') else '?')
                   for r in d['results'])
    reasons = '  '.join(f"{{k}}:{{v}}" for k, v in ends.most_common())
    print(f"{{name:<26}}{{d['passed']:>4}}/{{d['total_scenarios']:<3}}  {{reasons}}")
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

**Log greps worth running per model (the guard stack should show up):**
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
