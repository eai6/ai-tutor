#!/usr/bin/env bash
# Local qwen3-4b-jetson mt30 iteration runner (CPU box).
#
#   ITER=it1 bash offline_eval/run_local_mt30.sh
#
# Runs the original 30 multi-turn scenarios (tag v1) against the locally
# installed qwen3-4b-jetson tag in two-call mode — the same configuration as
# the Colab qwen_mt30 board, so scores are comparable across iterations.
# Writes results to offline_eval/multi_turn_results/local_<ITER>/.
set -uo pipefail

ROOT="${AI_TUTOR_ROOT:-/home/daniel/Documents/work/Nyansapo/web/ai-tutor}"
cd "$ROOT"
PY="${PY:-$ROOT/venv/bin/python}"
ITER="${ITER:?set ITER=itN}"
RESULTS="$ROOT/offline_eval/multi_turn_results/local_${ITER}"
mkdir -p "$RESULTS"

export TUTOR_MODEL_OVERRIDE=local_ollama/qwen3-4b-jetson
export TUTOR_CALL_MODE=two
export SIMPLE_TUTOR_ENGINE=1
# This box's Ollama is the untuned systemd service (f16 KV), so the client's
# fit preflight projects 5.1 GB against ~4 GB available and refuses — but the
# same model demonstrably runs here (kiosk session 74, 2026-08-03, 20-37s
# turns): the weights are mmap'd/evictable and swap absorbs the rest. Trust
# the measurement over the projection for local sweeps. Remove once the
# service carries OLLAMA_KV_CACHE_TYPE=q8_0 + OLLAMA_FLASH_ATTENTION=1.
export OLLAMA_FIT_GUARD=off

command -v ollama >/dev/null || { echo "ollama missing"; exit 1; }
ollama list | grep -q qwen3-4b-jetson || { echo "qwen3-4b-jetson not installed"; exit 1; }

"$PY" offline_eval/seed_ollama_configs.py >/dev/null

log="$RESULTS/qwen3-4b-jetson.log"
start=$(date +%s)
"$PY" manage.py run_eval --multi-turn --subset v1 >"$log" 2>&1
rc=$?
elapsed=$(( $(date +%s) - start ))
out=$(grep -oE "Output: .*\.json" "$log" | tail -1 | sed 's/^Output: //')
if [[ -n "$out" && -f "$out" ]]; then
  cp "$out" "$RESULTS/qwen3-4b-jetson.json"
  echo "DONE rc=$rc ${elapsed}s  $(grep -E '^Result:' "$log" | tail -1)"
  echo "saved $RESULTS/qwen3-4b-jetson.json"
else
  echo "FAILED rc=$rc ${elapsed}s — no run JSON; inspect $log"
fi
