#!/usr/bin/env bash
# Offline-model eval sweep: run the eval harness once per model in models.txt,
# swapping ONLY the tutor model (TUTOR_MODEL_OVERRIDE) while judge + student-sim
# stay on Anthropic. Saves one run JSON per model under results/.
#
# Usage:
#   bash offline_eval/run_matrix.sh                 # single-turn sweep (default, fastest)
#   MODE="" bash offline_eval/run_matrix.sh         # full suite (single + multi turn)
#   MODE="--multi-turn" bash offline_eval/run_matrix.sh
#   SIMPLE_TUTOR_ENGINE=0 bash offline_eval/run_matrix.sh   # use legacy ConversationalTutor
#   MODELS_FILE=offline_eval/finalists.txt bash offline_eval/run_matrix.sh
#
# Prereqs: Ollama installed + `ollama serve` running. Judge/sim call the
# Anthropic API (needs ANTHROPIC_API_KEY in .env), so each run incurs a small
# Anthropic cost. CPU-only inference is slow — expect minutes per model.
set -uo pipefail

# AI_TUTOR_ROOT / PY let this run on a checkout other than the author's box
# (e.g. the Jetson, where the live interpreter is .venv not venv). Defaults are
# unchanged, so an existing workflow that sets neither behaves exactly as before.
ROOT="${AI_TUTOR_ROOT:-/home/daniel/Documents/work/Nyansapo/web/ai-tutor}"
cd "$ROOT"
PY="${PY:-$ROOT/venv/bin/python}"
RESULTS="${RESULTS_DIR:-$ROOT/offline_eval/single_turn_results/results}"
MODELS_FILE="${MODELS_FILE:-$ROOT/offline_eval/models.txt}"
MODE="${MODE:---single-turn}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$ROOT/offline_eval/ollama_models}"
# simple_tutor (the production engine) is now provider-agnostic: its _call_llm
# routes non-Anthropic providers through get_llm_client().generate_with_tools(),
# so it drives OSS Ollama models that support tool-calling (e.g. llama3.2:3b).
# Default ON to evaluate the real production engine. Set =0 for the legacy
# ConversationalTutor (no tool-use; works with non-tool models too).
# NOTE: models that can't tool-call will hit the engine's fallback on simple_tutor.
export SIMPLE_TUTOR_ENGINE="${SIMPLE_TUTOR_ENGINE:-1}"

# Remote Ollama (a rented GPU box). Two different consumers need the address:
#   * the `ollama` CLI below (pull/create/list/stop/rm) reads OLLAMA_HOST;
#   * the Django client reads api_base off the ModelConfig row, which
#     seed_ollama_configs.py writes from OLLAMA_API_BASE.
# Derive one from the other so callers only have to export the obvious one, and
# hand the result to the identity probe, which would otherwise hardcode
# localhost and report "probe failed" against a remote box — silently losing
# the ONE check that catches a thinking model masquerading as instruct.
if [[ -z "${OLLAMA_API_BASE:-}" && -n "${OLLAMA_HOST:-}" ]]; then
  case "$OLLAMA_HOST" in
    http://*|https://*) OLLAMA_API_BASE="$OLLAMA_HOST" ;;
    *)                  OLLAMA_API_BASE="http://$OLLAMA_HOST" ;;
  esac
fi
export OLLAMA_API_BASE="${OLLAMA_API_BASE:-}"
PROBE_BASE="${OLLAMA_API_BASE:-http://localhost:11434}"
mkdir -p "$RESULTS" "$OLLAMA_MODELS"
# Per-scenario checkpoints go STRAIGHT to the (Drive-backed) results dir —
# a dead runtime keeps them (2026-08-05: VM-disk checkpoints died with the VM).
export EVAL_CHECKPOINT_DIR="$RESULTS"

command -v ollama >/dev/null 2>&1 || { echo "ERROR: ollama not installed."; exit 1; }
ollama list >/dev/null 2>&1 || { echo "ERROR: Ollama server not reachable. Start it: 'ollama serve &'"; exit 1; }

echo ">> Seeding local_ollama ModelConfig rows..."
"$PY" offline_eval/seed_ollama_configs.py || { echo "seed failed"; exit 1; }

echo ">> Engine: SIMPLE_TUTOR_ENGINE=$SIMPLE_TUTOR_ENGINE   Mode: ${MODE:-(full suite)}"
echo ">> Model weights: $OLLAMA_MODELS"
echo

while read -r tag tier _rest; do
  [[ -z "${tag:-}" || "$tag" == \#* ]] && continue
  safe="${tag//[:\/]/_}"
  # Resume-safety: skip models already scored (set FORCE=1 to redo).
  if [[ -f "$RESULTS/${safe}.json" && "${FORCE:-0}" != "1" ]]; then
    echo "==================== $tag ($tier) — already done, skipping ===================="
    echo; continue
  fi
  echo "==================== $tag ($tier) ===================="
  base=""
  # Modelfile-pinned tags (qwen3-4b-jetson, qwen3.6-27b-instruct, …) are not
  # registry tags — they are built locally from infra/ollama/Modelfile.<tag>,
  # which bakes in num_ctx and the tested sampling. Bare tags were deprecated
  # repo-wide (b5b7a68): the Tier-2 verifier reaches Ollama through
  # /v1/chat/completions where num_ctx cannot be pinned, so the 4096-default
  # runner evicts the tutor's on every graded turn.
  MODELFILE="$ROOT/infra/ollama/Modelfile.$tag"
  if [[ -f "$MODELFILE" ]]; then
    base=$(awk '/^FROM /{print $2; exit}' "$MODELFILE")
    echo ">> building $tag from $base via Modelfile ..."
    if ! ollama pull "$base"; then
      echo "!! pull failed for base $base — skipping"; echo; continue
    fi
    if ! ollama create "$tag" -f "$MODELFILE"; then
      echo "!! ollama create failed for $tag — skipping"; echo; continue
    fi
  else
    echo ">> pulling $tag ..."
    if ! ollama pull "$tag"; then
      echo "!! pull failed for $tag — skipping"; echo; continue
    fi
  fi
  # Model-identity probe: checkpoint families (Instruct vs Thinking) hide
  # behind lookalike tags and hybrids think by DEFAULT — the mt50-vs-mt30
  # confound was exactly this. Capability flags and template markers are not
  # discriminating, so the probe is EMPIRICAL: one tiny generation, honouring
  # the profile's think setting via the engine's own client, then report
  # whether the model thought.
  digest=$(ollama list | awk -v t="$tag" '$1==t || $1==t":latest" {print $2; exit}')
  identity=$(AI_TUTOR_TAG="$tag" AI_TUTOR_OLLAMA_BASE="$PROBE_BASE" \
    "$PY" - <<'PROBE' 2>/dev/null
import json, os, sys, urllib.request
sys.path.insert(0, os.environ.get('AI_TUTOR_ROOT') or os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
tag = os.environ['AI_TUTOR_TAG']
think = None
try:
    import django; django.setup()
    from ai_tutor.apps.llm.model_profiles import get_model_profile
    p = get_model_profile(f'local_ollama/{tag}')
    think = getattr(p, 'ollama_think', None) if p else None
except Exception:
    pass
body = {'model': tag, 'stream': False,
        'messages': [{'role': 'user', 'content': 'What is 2+2? Answer with just the number.'}],
        'options': {'num_predict': 120}}
if think is not None:
    body['think'] = think
try:
    base = os.environ.get('AI_TUTOR_OLLAMA_BASE') or 'http://localhost:11434'
    req = urllib.request.Request(f'{base}/api/chat',
                                 data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read())
    m = d.get('message') or {}
    t, c = len(m.get('thinking') or ''), len(m.get('content') or '')
    print(f"profile_think={think} thinking_chars={t} content_chars={c} -> "
          + ('THINKS' if t else 'answers directly (instruct-style)'))
except Exception as e:
    print(f'probe failed: {type(e).__name__}: {str(e)[:80]}')
PROBE
)
  identity_line=">> identity: $tag digest=$digest $identity"
  echo "$identity_line"
  # Into the results dir too — the notebook cell output dies with the browser
  # tab, but the results folder is what gets zipped and analyzed.
  echo "$(date -u +%FT%TZ) $identity_line" >> "$RESULTS/identity.log"
  start=$(date +%s)
  log="$RESULTS/${safe}.log"
  TUTOR_MODEL_OVERRIDE="local_ollama/$tag" "$PY" manage.py run_eval $MODE >"$log" 2>&1
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  out=$(grep -oE "Output: .*\.json" "$log" | tail -1 | sed 's/^Output: //')
  if [[ -n "$out" && -f "$out" ]]; then
    cp "$out" "$RESULTS/${safe}.json"
    summary=$(grep -E "^Result:" "$log" | tail -1)
    echo ">> saved results/${safe}.json   ${elapsed}s   $summary"
  else
    echo "!! no run JSON (rc=$rc, ${elapsed}s) — inspect $log"
    # Salvage the per-scenario checkpoint to the (Drive-backed) results dir:
    # evals/runs/ lives on the VM disk and dies with the runtime, and the
    # 2026-08-04 OOM kill cost 22 completed scenarios exactly this way.
    latest_partial=$(ls -1t "$ROOT"/evals/runs/partial_*.json 2>/dev/null | head -1)
    if [[ -n "$latest_partial" ]]; then
      cp "$latest_partial" "$RESULTS/${safe}.partial.json"
      echo ">> salvaged checkpoint -> results/${safe}.partial.json"
      echo ">> resume with: MODE=\"<same flags> --resume $RESULTS/${safe}.partial.json\" bash offline_eval/run_matrix.sh"
    fi
  fi
  ollama stop "$tag" >/dev/null 2>&1 || true   # unload to free RAM
  # CLEANUP_MODELS=1 also deletes the weights from disk after scoring — used on
  # Colab (small disk; results already saved so a re-run skips this model
  # without re-pulling). Off by default so the laptop keeps its cached models.
  if [[ "${CLEANUP_MODELS:-0}" == "1" && -f "$RESULTS/${safe}.json" ]]; then
    ollama rm "$tag" >/dev/null 2>&1 || true
    # A Modelfile-built tag leaves its base weights behind — drop those too.
    [[ -n "$base" ]] && ollama rm "$base" >/dev/null 2>&1 || true
    echo ">> removed $tag weights from disk (CLEANUP_MODELS=1)"
  fi
  echo
done < "$MODELS_FILE"

echo ">> Sweep complete. Leaderboard:"
"$PY" offline_eval/aggregate.py
