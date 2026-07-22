"""Generate offline_eval/colab_eval_gemma.ipynb — a single-tab Colab smoke of
the tool-enabled Gemma repackagings on 5 multi-turn scenarios.

Purpose: the first oss13_mt sweep zeroed all 7 stock Gemmas on an Ollama
template limitation (no tool role → HTTP 400 on every tools request). The
okamototk/gemma3-tools repackagings carry a tool-enabled template; this notebook
answers "does Gemma actually run and tutor through the engine now?" for a few
dollars before committing to the full 20-scenario sweep.

Tutor = okamototk/gemma3-tools:{1b,4b,12b,27b} via Ollama; student-sim = Anthropic
Haiku 4.5; judge = Anthropic Sonnet 4.6 @ temp 0. Results land in
offline_eval/multi_turn_results/gemma_probe5/ (persisted to Drive).

Run: python offline_eval/_make_colab_nb_gemma.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'
BRANCH = 'pixeldesignlabs-dev-portuguese'
DRIVE_FOLDER = 'ai-tutor-eval-multiturn'
SWEEP = 'gemma_probe5_v3'   # v3: after mid-reply polarity + option-set repeat fixes (v2 = post-scrub, v1 = first smoke)

# 5-scenario seeded smoke (same convention as the first multi-turn fix-check
# probe). NOT comparable to the 20-scenario seed-5 fixcheck board — a different
# draw and far too small for score claims. It answers: do sessions RUN, POSE,
# GRADE, and END cleanly?
SAMPLE = 5
SEED = 0
RESULTS = f'offline_eval/multi_turn_results/{SWEEP}'
MODE = f'--multi-turn --sample {SAMPLE} --seed {SEED}'

# All four sizes run for a complete v1->v2 comparison. v1 smoke context:
# 1b went 0/5 (protocol collapse — 2 tool calls across 5 sessions) and 4b
# 1/5 (27 reveal-filter redactions in 5 sessions); 12b's v1 assert failures
# were an engine scrub gap (XML tool markup), fixed in bbde312 — expect its
# v2 to recover; 27b was mechanically clean (5/5 exit tickets, 3/5 pass).
MODELS = [
    ('okamototk/gemma3-tools:1b',  'big', 'gemma3 1B'),
    ('okamototk/gemma3-tools:4b',  'big', 'gemma3 4B'),
    ('okamototk/gemma3-tools:12b', 'big', 'gemma3 12B'),
    ('okamototk/gemma3-tools:27b', 'xl',  'gemma3 27B — largest; needs the big card'),
]

cells = []


def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})


def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


model_list_md = '\n'.join(
    f'- `{tag}` — {note}' for tag, _tier, note in MODELS)

md(rf"""
# AI Tutor — **Gemma multi-turn smoke** · 5 scenarios · single tab

The first OSS sweep zeroed all seven stock Gemmas before the models saw a
single prompt: Ollama's gemma templates have **no tool role**, so every
`tools` request returned HTTP 400 and every session deadlocked at turn 1.
This notebook smokes the **tool-enabled repackagings**
([okamototk/gemma3-tools](https://ollama.com/okamototk/gemma3-tools) — official
gemma3 weights, tool-enabled chat template) on **{SAMPLE} multi-turn
scenarios** per model, to answer *does Gemma run, pose, grade, and end
sessions cleanly through the engine?* before spending a full sweep on it.

{model_list_md}

- **Student sim** = Anthropic **Haiku 4.5** persona player.
- **Judge** = Anthropic **Sonnet 4.6** @ temp 0 session rubric.
- **Engine** = `simple_tutor` with the full eval guard stack (the gemma tags
  resolve to the `gemma` family profile, so forcing/salvage/nets are active).
- **Cell 8c probes tool support first** — an incapable model is dropped in
  seconds, not 20 sessions.

> ⚠️ This is a smoke, not a board: n={SAMPLE} on a different draw than the
> 20-scenario fixcheck benchmark. Read END-REASONS and the logs, not the pass
> rate. Gemma is evaluated HERE, separately from the qwen sweep, by design.

**Runtime**: one tab covers all four models sequentially (weights are removed
after each model). **Pick an A100 (40 GB)** so the 27b fits.

**Before you start** — Colab Secrets (🔑 sidebar), each *Notebook access ON*:
- `GH_TOKEN` — GitHub classic PAT with `repo` scope (collaborator on `{REPO}`).
- `ANTHROPIC_API_KEY` — **required** (student-sim + judge).
- `GOOGLE_API_KEY` + `OPENAI_API_KEY` — keep both so the cross-vendor grader
  cascade matches the other sweeps.
""")

md("## Cell 1 — confirm GPU + mount Drive")
code(r"""
!nvidia-smi -L
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

md("## Cell 4 — install deps + start Ollama (a few min; ignore pip resolver warnings)")
code(r"""
!pip install -q -r requirements.txt
# The Ollama installer is zstd-compressed; the Colab VM lacks zstd.
!apt-get -qq install -y zstd || (apt-get -qq update && apt-get -qq install -y zstd)
import subprocess, time, shutil, os
def _has_ollama():
    return shutil.which('ollama') is not None
def _install_ollama():
    if _has_ollama():
        return True
    # PINNED to 0.30.7 — the version the gemma3-tools tool templates were
    # verified against locally (5/5 probe passes). Colab's un-pinned latest
    # silently stopped parsing the repackaged template's tool calls: the same
    # model emitted zero tool_calls on every probe.
    for i in range(1, 4):
        print(f'[ollama] install.sh attempt {i} (pinned 0.30.7)', flush=True)
        subprocess.run('curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=0.30.7 sh', shell=True)
        if _has_ollama():
            return True
        time.sleep(5)
    for i in range(1, 4):
        print(f'[ollama] direct-binary attempt {i} (ollama.com, pinned)', flush=True)
        subprocess.run('curl -fL --retry 5 --retry-all-errors --connect-timeout 30 '
                       '-o /tmp/ollama.tgz "https://ollama.com/download/ollama-linux-amd64.tgz?version=0.30.7"',
                       shell=True)
        if os.path.exists('/tmp/ollama.tgz') and os.path.getsize('/tmp/ollama.tgz') > 1_000_000:
            subprocess.run('tar -C /usr -xzf /tmp/ollama.tgz', shell=True)
            if _has_ollama():
                return True
        time.sleep(5)
    return False
assert _install_ollama(), "ollama install failed — restart the runtime and retry"
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

md("## Cell 5 — **required** — write .env from Colab Secrets")
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

md(f"## Cell 7 — persist results to Drive (symlink → survives disconnects)\n"
   f"Writes to `{DRIVE_FOLDER}/{SWEEP}/`. Resume-safe: already-scored models "
   "are skipped on a rerun.")
code(rf"""
!mkdir -p /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP}
!rm -rf {RESULTS} && mkdir -p offline_eval/multi_turn_results && ln -s /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP} {RESULTS}
import os, glob
print('this run writes to:', os.path.realpath('{RESULTS}'))
done = sorted(os.path.basename(p)[:-5] for p in glob.glob('{RESULTS}/*.json'))
print('already scored:', done or '(none yet)')
""")

models_txt = '\\n'.join(
    f'{tag:<26} {tier}     # {note}' for tag, tier, note in MODELS)
md("## Cell 8 — write the model matrix + seed configs")
code(rf"""
open('offline_eval/models.txt', 'w').write(
    "# Gemma multi-turn smoke — tool-enabled repackagings\n"
    "{models_txt}\n")
print(open('offline_eval/models.txt').read())
!python offline_eval/seed_ollama_configs.py
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from apps.llm.model_profiles import get_model_profile
for line in open('offline_eval/models.txt'):
    tag = line.split('#')[0].split()[0] if line.split('#')[0].split() else ''
    if not tag:
        continue
    p = get_model_profile(f'local_ollama/{{tag}}')
    assert p and p.family == 'gemma', f'{{tag}}: expected gemma family profile, got {{p}}'
    print(f'{{tag:<26}} family={{p.family}} mode={{p.mode}} sampling={{p.sampling_dict()}}')
""")

md("## Cell 8b — reclaim disk BEFORE the run (harmless no-op on a fresh VM)")
code(r"""
!ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | xargs -r -n1 ollama rm 2>/dev/null || true
!rm -rf /root/.ollama/models/blobs/* /root/.ollama/models/manifests/* offline_eval/ollama_models/* 2>/dev/null || true
!pip cache purge 2>/dev/null || true
import subprocess
print(subprocess.run(['df','-h','/'],capture_output=True,text=True).stdout)
""")

md("## Cell 8c — **tool-probe gate** (drops models that cannot tool-call)\n"
   "Pulls each model, fires one probe tool-call, and REMOVES any model that "
   "fails from `models.txt` — a failure costs seconds, not sessions. This is "
   "the cell that would have caught the stock-Gemma 400s instantly.")
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
assert kept, 'Every model failed the tool probe — nothing to smoke.'
""")

md(f"## Cell 9 — run the smoke ({SAMPLE} sessions per model; resume-safe)\n"
   "**Sanity-check the first model before walking away:**\n"
   "- `>> Tutor engine: simple_tutor` — not the legacy engine.\n"
   f"- The runner reports {SAMPLE} scenarios.\n"
   "- Sessions end `exit_ticket`/`completed`, not a wall of `deadlock`.\n"
   "- Expect `pose salvaged` / `auto_grade_fallback` / `polarity_align` lines "
   "in the logs — that's the guard stack carrying a new family; a few is "
   "healthy, a storm is diagnostic gold either way.")
code(rf"""
!RESULTS_DIR=$PWD/{RESULTS} SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 \
  MODE="{MODE}" bash offline_eval/run_matrix.sh
""")

md("## Cell 10 — smoke board: pass + session end-reasons\n"
   f"n={SAMPLE} per model — read the END-REASONS, not the pass rate. Clean = "
   "sessions reach `exit_ticket`/`completed`. `deadlock` at turn 1-2 = the "
   "template still isn't carrying tools; `max_turns` everywhere = pacing; "
   "`errored` = judge/sim infrastructure, re-run those.")
code(rf"""
import json, glob, os
from collections import Counter
print(f"{{'MODEL':<28}}{{'PASS':>7}}   SESSION END-REASONS")
print('-' * 72)
for f in sorted(glob.glob('{RESULTS}/*.json')):
    name = os.path.basename(f)[:-5]
    if name.startswith('_'):
        continue
    d = json.load(open(f))
    ends = Counter(r.get('sim_reason') or ('errored' if r.get('error') else '?')
                   for r in d['results'])
    reasons = '  '.join(f"{{k}}:{{v}}" for k, v in ends.most_common())
    print(f"{{name:<28}}{{d['passed']:>3}}/{{d['total_scenarios']:<3}} {{reasons}}")
print()
print('Guard-stack activity per model (healthy = present, storm = look closer):')
for f in sorted(glob.glob('{RESULTS}/*.log')):
    name = os.path.basename(f)[:-4]
    log = open(f, errors='replace').read()
    counts = {{k: log.count(k) for k in
              ('pose salvaged', 'auto_grade_fallback', 'polarity_align',
               'reveal_filter', 'dedupe_reply', 'call2_repair')}}
    print(f"  {{name:<26}} " + '  '.join(f'{{k.split()[0]}}:{{v}}' for k, v in counts.items()))
""")

md(rf"""
## Reading the result

- **Clean smoke** (sessions end at exit_ticket, few guard interventions):
  promote Gemma into the full 20-scenario sweep — it's already wired into
  `colab_eval.ipynb`; just run that notebook's groups as usual.
- **Runs but messy** (heavy `polarity_align` / `auto_grade_fallback`, budget
  overruns): pull the transcripts from `MyDrive/{DRIVE_FOLDER}/{SWEEP}/` into
  the repo's `{RESULTS}/` and do a bottleneck pass before the full sweep.
- **Still deadlocking at turn 1**: the repackaged template didn't hold —
  Cell 8c output says exactly why; fall back to option 2 (vLLM serving) from
  the Gemma analysis.

To pull results to the laptop: copy JSONs + logs from
`MyDrive/{DRIVE_FOLDER}/{SWEEP}/` into `{RESULTS}/`.
""")

nb = {
    "cells": cells,
    "metadata": {
        "accelerator": "GPU",
        "colab": {"gpuType": "A100", "provenance": []},
        "kernelspec": {"display_name": "Python 3", "name": "python3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}

out = Path(__file__).resolve().parent / 'colab_eval_gemma.ipynb'
out.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + '\n',
               encoding='utf-8')
print(f'wrote {out} ({len(cells)} cells)')
