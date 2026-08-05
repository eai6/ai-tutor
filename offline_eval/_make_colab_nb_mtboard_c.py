"""Generate offline_eval/colab_mtboard_c.ipynb — group C of the 2026-08-05
mt30 board suite: sllm/glm-z1-9b (local Ollama, community GLM-Z1-9B tag) +
qwen3-next-80b-instruct (Vertex MaaS), full 30 v1 multi-turn scenarios,
TUTOR_CALL_MODE=two. Cloned from _make_colab_nb_multiturn.py to reuse its
Vertex auth + run_cloud cells.

Purpose: this is a FIX-CHECK, not a ranking. Sweep 2 showed the multi-turn
bottleneck is protocol/state discipline (sessions time out without the lesson
advancing). After sweep 2 we shipped engine + prompt fixes (adaptive tool-forcing,
reasoning-channel recovery, server-side bare-answer grading, empty-slot handling,
a positively-framed Gemini prompt). This notebook runs a small, fast multi-turn
sample to see whether those fixes let sessions COMPLETE. 5 scenarios × 6 models,
so it finishes quickly and is meant for iteration, not a leaderboard.

Top-6 by single-turn sweep 3 — one local OSS + five cloud (both tiers, as asked):
  1. qwen3.5:4b            (local, Ollama)         89%
  2. glm-4.7               (Vertex MaaS)           88%
  3. gemini-2.5-flash      (Gemini API)            81%
  4. kimi-k2-thinking      (Vertex MaaS)           80.5%   <- exercises B4 (thinking)
  5. qwen3-next-80b-instruct (Vertex MaaS)         80.5%
  6. deepseek-v3.1         (Vertex MaaS)           79.5%

Tutor = the model under test; student-sim + rubric judge = Anthropic Haiku 4.5.

Run: python offline_eval/_make_colab_nb_multiturn.py
"""
import json
from pathlib import Path

REPO = 'eai6/ai-tutor'
BRANCH = 'offline-optimization'   # MUST carry the arbiter + checkpoint fixes
DRIVE_FOLDER = 'ai-tutor-eval-multiturn'
SWEEP = 'mtboard_c'
GCP_PROJECT = 'ai-tutor-499714'             # Vertex Model Garden MaaS project
RESULTS = f'offline_eval/multi_turn_results/{SWEEP}'
SAMPLE = 30   # full v1 board (kept for the prose f-strings)
SEED = 0
MODE = '--multi-turn --subset v1'   # the original 30 multi-turn scenarios

# The one local OSS model (Ollama). run_matrix.sh reads this shape (tag tier).
OSS_MODEL = 'sllm/glm-z1-9b'

# The five cloud models: "provider/model  safe_name  [region]" — run_cloud.sh
# reads this shape. gemini-2.5-flash is the Gemini Developer API (GOOGLE_API_KEY,
# no region); the other four are Vertex Model Garden MaaS (ADC + region).
CLOUD_MODELS = """\
vertex_model_garden/qwen/qwen3-next-80b-a3b-instruct-maas     qwen3-next-80b-instruct    global
"""

# Sanity: confirm the multi-turn dataset is present at generation time.
_ROOT = Path(__file__).resolve().parents[1]
_MT = list((_ROOT / 'evals' / 'dataset' / 'multi_turn').rglob('*.yaml'))
assert len(_MT) == 200, f'expected 200 multi-turn scenarios, found {len(_MT)}'

cells = []


def md(s: str):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": s.strip("\n").splitlines(keepends=True)})


def code(s: str):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": s.strip("\n").splitlines(keepends=True)})


md(rf"""
# AI Tutor — **Multi-turn FIX-CHECK** · top-6 sweep-3 models · {SAMPLE} scenarios

This is a **fast validation run, not a leaderboard.** Sweep 2 found the multi-turn
bottleneck is protocol/state discipline: non-Anthropic sessions run out of turns
without the lesson ever advancing, and pass rate tracks lesson-completion almost
exactly. After sweep 2 we shipped engine + prompt fixes. This notebook runs a
**{SAMPLE}-scenario** multi-turn sample across the **top-6 single-turn models** —
one local OSS model and five cloud models — to see whether sessions now **complete**.

**Models (top-6 by single-turn sweep 3 — both tiers):**

| # | model | tier | single-turn | fix it exercises |
|---|---|---|---|---|
| 1 | `qwen3.5:4b` | local (Ollama) | 89% | B1 auto-grade, B2 forcing |
| 2 | `glm-4.7` | Vertex MaaS | 88% | B2 forcing |
| 3 | `gemini-2.5-flash` | Gemini API | 81% | **B6 Gemini prompt**, B2 |
| 4 | `kimi-k2-thinking` | Vertex MaaS | 80.5% | **B4 reasoning-channel** |
| 5 | `qwen3-next-80b-instruct` | Vertex MaaS | 80.5% | B2 forcing, B1 |
| 6 | `deepseek-v3.1` | Vertex MaaS | 79.5% | B1, B3 |

- **Tutor** = the model under test (swapped per model).
- **Student-sim + rubric judge** = Anthropic **Haiku 4.5** (needs `ANTHROPIC_API_KEY`).
- **Engine** = `simple_tutor` on branch **`{BRANCH}`** — which MUST carry the fixes.
  Cell 11 verifies the fix log-lines actually fired.
- **Sample:** `{MODE}` — the draw is seeded, so all 6 models see the SAME 5 scenarios.

> ⚠️ This is deliberately tiny (5 scenarios, ±~22pp at n=5) — read it as "did the
> pipeline run and did sessions complete?", NOT as a ranking. Scale up only once
> the fixes look healthy.

**Before you start**
1. Runtime → **Change runtime type** → **T4** (High-RAM) is plenty — only the 3 GB
   `qwen3.5:4b` runs locally; the other five are API calls.
2. Add these **Colab Secrets** (🔑 sidebar), each *Notebook access ON*:
   - `GH_TOKEN` — GitHub **classic** PAT, `repo` scope (collaborator on `{REPO}`).
   - `ANTHROPIC_API_KEY` — **required** (student-sim + rubric judge).
   - `GOOGLE_API_KEY` — gemini-2.5-flash tutor + grader cascade.
   - `OPENAI_API_KEY` — grader cascade.
   - **For the 4 Vertex MaaS models** (glm-4.7, kimi, qwen-next, deepseek): GCP auth
     — Cell 5b uses your Colab Google account by default; if it lacks Vertex access
     on project `{GCP_PROJECT}`, add a service-account key JSON as secret `GCP_SA_KEY`.
""")

md("## Cell 1 — GPU + mount Drive")
code(r"""
!nvidia-smi -L
from google.colab import drive
drive.mount('/content/drive')
""")

md(f"## Cell 2 — clone the repo (branch `{BRANCH}` — carries the fixes)")
code(rf"""
from google.colab import userdata
import subprocess, os
tok = (userdata.get('GH_TOKEN') or '').strip()
assert tok and ' ' not in tok, "GH_TOKEN missing or contains a space — re-save the secret"
url = f"https://{{tok}}@github.com/{REPO}.git"
subprocess.run(['rm', '-rf', '/content/ai-tutor'], check=True)
subprocess.run(['git', 'clone', '--depth', '1', '-b', '{BRANCH}', url, '/content/ai-tutor'], check=True)
os.chdir('/content/ai-tutor')
print('cloned at', os.getcwd())
print('HEAD:', subprocess.run(['git','log','-1','--oneline'],capture_output=True,text=True).stdout.strip())
""")

md("## Cell 3 — fix hardcoded laptop paths")
code(r"""
!sed -i 's#/home/daniel/Documents/work/Nyansapo/web/ai-tutor#/content/ai-tutor#g; s#\$ROOT/venv/bin/python#python#g; s#venv/bin/python#python#g' offline_eval/*.py offline_eval/*.sh
""")

md("## Cell 4 — install deps + start Ollama (for `qwen3.5:4b`)")
code(r"""
!pip install -q -r requirements.txt
!apt-get -qq install -y zstd || (apt-get -qq update && apt-get -qq install -y zstd)
import subprocess, time, shutil, os
def _has_ollama(): return shutil.which('ollama') is not None
def _install_ollama():
    if _has_ollama(): return True
    for i in range(1, 4):
        print(f'[ollama] install.sh attempt {i}', flush=True)
        subprocess.run('curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=0.30.7 sh', shell=True)
        if _has_ollama(): return True
        time.sleep(5)
    for i in range(1, 4):
        print(f'[ollama] direct-binary attempt {i}', flush=True)
        subprocess.run('curl -fL --retry 5 --retry-all-errors --connect-timeout 30 '
                       '-o /tmp/ollama.tar.zst "https://ollama.com/download/ollama-linux-amd64.tar.zst?version=0.30.7"', shell=True)
        if os.path.exists('/tmp/ollama.tar.zst') and os.path.getsize('/tmp/ollama.tar.zst') > 1_000_000:
            subprocess.run('tar --zstd -C /usr -xf /tmp/ollama.tar.zst', shell=True)
            if _has_ollama(): return True
        time.sleep(5)
    return False
assert _install_ollama(), "ollama install failed — Runtime -> Disconnect and delete runtime, then retry."
subprocess.Popen(['ollama', 'serve'], stdout=open('/content/ollama.log', 'w'), stderr=subprocess.STDOUT)
for _ in range(30):
    if subprocess.run(['bash','-c','ollama list'], capture_output=True).returncode == 0:
        print('ollama ready'); break
    time.sleep(2)
else:
    print('ollama NOT ready — check /content/ollama.log')
""")

md("## Cell 5 — write `.env` from Colab Secrets\n"
   "ANTHROPIC drives the student-sim + rubric judge; GOOGLE feeds the gemini tutor "
   "and the grader cascade; OPENAI feeds the grader cascade. `GOOGLE_CLOUD_PROJECT` "
   "targets the Vertex Model Garden project for the four MaaS models.")
code(rf"""
from google.colab import userdata
open('.env', 'w').write(
    "SECRET_KEY=colab-eval\nDEBUG=True\nEMBEDDING_BACKEND=sqlite\n"
    f"ANTHROPIC_API_KEY={{userdata.get('ANTHROPIC_API_KEY')}}\n"
    f"GOOGLE_API_KEY={{userdata.get('GOOGLE_API_KEY')}}\n"
    f"OPENAI_API_KEY={{userdata.get('OPENAI_API_KEY')}}\n"
    "GOOGLE_CLOUD_PROJECT={GCP_PROJECT}\n")
import os
os.environ['GOOGLE_CLOUD_PROJECT'] = '{GCP_PROJECT}'
print('.env written · GOOGLE_CLOUD_PROJECT={GCP_PROJECT}')
""")

md("## Cell 5b — **GCP auth for the 4 Vertex MaaS models**\n"
   "glm-4.7 / kimi-k2-thinking / qwen3-next-80b-instruct / deepseek-v3.1 are served "
   "on Vertex Model Garden and need Google Cloud credentials (ADC). Two ways — the "
   "cell tries the service-account key first, then falls back to your Colab account:\n"
   f"- **Service account (most reliable):** add secret `GCP_SA_KEY` = the full JSON "
   "key for a service account with Vertex AI User on project "
   f"`{GCP_PROJECT}`.\n"
   "- **Your Google account:** if `GCP_SA_KEY` is absent, the cell runs "
   "`auth.authenticate_user()` — works only if the account you run Colab under has "
   "Vertex access on that project.")
code(rf"""
import os, json
os.environ['GOOGLE_CLOUD_PROJECT'] = '{GCP_PROJECT}'
from google.colab import userdata
sa = None
try:
    sa = userdata.get('GCP_SA_KEY')
except Exception:
    sa = None
if sa and sa.strip().startswith('{{'):
    open('/content/gcp_sa.json', 'w').write(sa)
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/content/gcp_sa.json'
    print('GCP auth: using service-account key (GCP_SA_KEY)')
else:
    from google.colab import auth
    auth.authenticate_user()
    print('GCP auth: using your Colab Google account (no GCP_SA_KEY secret found)')
# Smoke-test ADC + Vertex reachability against ONE MaaS model before the sweep.
import google.auth, google.auth.transport.requests, openai
try:
    creds, proj = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    creds.refresh(google.auth.transport.requests.Request())
    base = f"https://aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}/locations/global/endpoints/openapi"
    c = openai.OpenAI(base_url=base, api_key=creds.token)
    r = c.chat.completions.with_raw_response.create(
        model='zai-org/glm-4.7-maas', messages=[{{'role':'user','content':'hi'}}], max_tokens=1)
    print('Vertex MaaS reachable — glm-4.7 responded OK. HTTP', r.status_code)
except Exception as e:
    print('!! Vertex MaaS check FAILED:', type(e).__name__, str(e)[:300])
    print('   The 4 MaaS models will error. Fix GCP access (add GCP_SA_KEY) before Cell 9b.')
""")

md("## Cell 6 — fresh DB + eval fixtures")
code(r"""
!python manage.py migrate
!python manage.py loaddata evals/fixtures/institution.json evals/fixtures/lessons.json
""")

md("## Cell 7 — persist results to Drive (symlink → survives disconnects)\n"
   f"Symlinks `{RESULTS}/` to `{DRIVE_FOLDER}/{SWEEP}/` on Drive. Resume-safe: both "
   "runner scripts skip any model that already has a JSON.")
code(rf"""
!mkdir -p /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP}
!rm -rf {RESULTS} && mkdir -p offline_eval/multi_turn_results && ln -s /content/drive/MyDrive/{DRIVE_FOLDER}/{SWEEP} {RESULTS}
import os, glob
print('this run writes to:', os.path.realpath('{RESULTS}'))
done = sorted(os.path.basename(p)[:-5] for p in glob.glob('{RESULTS}/*.json'))
print('already scored:', done or '(none yet)')
""")

md("## Cell 8 — write the two model lists (1 OSS + 5 cloud)\n"
   "`models.txt` holds the single Ollama model (run_matrix.sh); "
   "`cloud_models_fixcheck.txt` holds the five cloud models (run_cloud.sh).")
code(rf"""
open('offline_eval/models.txt', 'w').write("# Multi-turn fix-check — OSS tier\n{OSS_MODEL} small\n")
open('offline_eval/cloud_models_fixcheck.txt', 'w').write('''{CLOUD_MODELS}''')
print("OSS  :", open('offline_eval/models.txt').read().strip().splitlines()[-1])
print("CLOUD:")
print(open('offline_eval/cloud_models_fixcheck.txt').read())
""")

md(f"## Cell 9a — run the **1 OSS** model multi-turn ({SAMPLE} scenarios)\n"
   "`run_matrix.sh` pulls `qwen3.5:4b`, runs the sample, writes to the Drive folder.")
code(rf"""
!TUTOR_CALL_MODE=two RESULTS_DIR=$PWD/{RESULTS} SIMPLE_TUTOR_ENGINE=1 CLEANUP_MODELS=1 \
  MODE="{MODE}" bash offline_eval/run_matrix.sh
""")

md(f"## Cell 9b — run the **5 cloud** models multi-turn ({SAMPLE} scenarios)\n"
   "`run_cloud.sh` swaps each cloud tutor while judge + sim stay Anthropic. Skips any "
   "model already scored, so you can re-run freely.")
code(rf"""
!TUTOR_CALL_MODE=two CLOUD_MODELS_FILE=$PWD/offline_eval/cloud_models_fixcheck.txt \
  RESULTS_DIR=$PWD/{RESULTS} SIMPLE_TUTOR_ENGINE=1 \
  MODE="{MODE}" bash offline_eval/run_cloud.sh
""")

md(f"## Cell 10 — results: pass rate + **session outcome** (the real signal)\n"
   "For multi-turn, pass rate tracks lesson-COMPLETION. This cell prints, per model, "
   f"passes out of {SAMPLE} and the split of session end-reasons (reached exit ticket "
   "vs. ran out of turns vs. deadlock). Completions rising is the fixes working.")
code(rf"""
import json, glob, os
from collections import Counter
rows = []
for f in sorted(glob.glob('{RESULTS}/*.json')):
    d = json.load(open(f)); res = d.get('results') or []
    n = len(res); k = sum(bool(r.get('passed')) for r in res)
    reasons = Counter((r.get('sim_reason') or '?') for r in res)
    rows.append((os.path.basename(f)[:-5], k, n, dict(reasons)))
print(f"{{'MODEL':<26}}{{'PASS':>8}}   SESSION END-REASONS")
print('-'*72)
for m, k, n, reasons in sorted(rows, key=lambda r: -(r[1]/r[2] if r[2] else 0)):
    print(f"{{m:<26}}{{k:>4}}/{{n:<3}}   {{reasons}}")
if not rows:
    print("(no results yet — run 9a/9b)")
""")

md("## Cell 11 — **did the fixes fire?** (grep the logs)\n"
   "The point of this run. Each line below is a fix leaving a trace; counts per model. "
   "You want completions up AND these behaving as described:\n"
   "- `recovered … from the reasoning channel` — **B4** salvaging a thinking model's "
   "tool call (expect on kimi-k2-thinking).\n"
   "- `autograded a clearly-correct bare answer` — **B1** advancing a lesson the model "
   "forgot to grade.\n"
   "- `record_answer without in-flight question` — **B3** empty-slot; should be RARE "
   "(and each one now trips the adaptive gate).\n"
   "- `call2_repair: Call 1 skipped` — **B2** repair riding on Call 2.\n"
   "- `dropped duplicate pose_question` — the per-turn cap; a few is fine, a storm is "
   "the old gemini spray.")
code(rf"""
import glob, os, re
PATTERNS = {{
  'B4 reasoning-recovery': r'recovered .* from the reasoning channel',
  'B1 bare-answer autograde': r'autograded a clearly-correct bare answer',
  'B3 empty-slot grade': r'record_answer without in-flight question',
  'B2 call2 repair': r'call2_repair: Call 1 skipped',
  'dup pose_question (cap)': r'dropped duplicate pose_question',
}}
logs = sorted(glob.glob('{RESULTS}/*.log'))
if not logs:
    print("(no logs yet — run 9a/9b)")
else:
    hdr = f"{{'MODEL':<26}}" + ''.join(f"{{k[:14]:>16}}" for k in PATTERNS)
    print(hdr); print('-'*len(hdr))
    for lg in logs:
        txt = open(lg, errors='ignore').read()
        counts = [len(re.findall(p, txt)) for p in PATTERNS.values()]
        print(f"{{os.path.basename(lg)[:-4]:<26}}" + ''.join(f"{{c:>16}}" for c in counts))
""")

md(rf"""
## Reading this run

**Primary question:** did sessions **complete**? Cell 10's end-reason split is the
answer. Sweep 2's failing sessions almost all ended in `max_turns` or `deadlock`
with the lesson never advancing. If the same models now reach the exit ticket, the
protocol fixes are working.

**{SAMPLE} scenarios is tiny on purpose.** At n={SAMPLE} the noise is enormous
(±~22pp), so do NOT read Cell 10 as a ranking. It answers "does the pipeline run
end-to-end and do sessions complete on the branch with the fixes?" — nothing more.
Once that looks healthy, raise `SAMPLE` in `_make_colab_nb_multiturn.py` (or drop it
for the full 200) and re-generate.

**If the 4 Vertex MaaS models error** in Cell 9b with `Publisher model … 404` or a
`403`, GCP auth (Cell 5b) didn't land — the Colab account lacks Vertex access on
project `{GCP_PROJECT}`. Add a `GCP_SA_KEY` service-account secret and re-run 5b.

**To pull results to the laptop:** copy the JSONs + logs from
`MyDrive/{DRIVE_FOLDER}/{SWEEP}/` into `{RESULTS}/`.
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

out = Path(__file__).resolve().parent / 'colab_mtboard_c.ipynb'
out.write_text(json.dumps(nb, indent=1))
print(f"wrote {out} ({len(cells)} cells)")
