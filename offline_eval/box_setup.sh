#!/usr/bin/env bash
# One-command setup for running the eval ON a rented GPU box.
#
#   bash offline_eval/box_setup.sh <ip> <ssh_port>
#
# Idempotent and resumable: every step checks before doing. Re-run it after a
# dropped connection and it picks up where it stopped.
#
# WHY ON THE BOX AND NOT THROUGH A TUNNEL. An SSH forward carrying inference
# for a 90-minute run has now damaged three arms: the first qwen3.8-27b
# geography arm (24 of 34 sessions deadlocked) and math-27b twice (123
# connection failures, 30 fatal). Three mitigations — a non-colliding local
# port, a health-checking supervisor, a longer connection retry ladder — each
# reduced it and none removed it. Putting the app, the DB and the model on one
# host removes the failure class instead of mitigating it a fourth time.
#
# THE FOUR THINGS THAT COST HOURS ON 2026-08-24, each now handled below:
#
#   1. `vastai create instance` can return success:False and still hand back a
#      contract id. The instance sits at intended_status=stopped, billing, and
#      looks exactly like "still pulling the image". ALWAYS check actual_status
#      and run `vastai start instance <id>` if it is not running.
#   2. A long remote script started over a foreground ssh dies with the ssh —
#      SIGHUP kills the remote bash. Anything slow must be `setsid nohup`'d ON
#      the box, not merely backgrounded locally.
#   3. The repo is PRIVATE, so an anonymous `git clone https://...` hangs
#      asking for a username. Ship a tarball instead; that also keeps a GitHub
#      token off a rented third-party host.
#   4. requirements.txt pins Django 6.0.2, which needs Python 3.12+. Common
#      GPU images ship 3.11. Do NOT downgrade Django to fit — a different
#      Django is a different app, and the run stops being comparable to the
#      boards it is meant to join. Build a 3.12 env instead.
set -euo pipefail

IP="${1:?usage: box_setup.sh <ip> <ssh_port>}"
PORT="${2:?usage: box_setup.sh <ip> <ssh_port>}"
KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_vast}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=20 -p "$PORT" "root@$IP")

echo "===== 0. reachable? ====="
"${SSH[@]}" 'echo "  ok: $(hostname)"; python --version 2>&1 | sed "s/^/  base /"'

echo "===== 1. ship code + data ====="
# Excludes results, weights and caches — the payload is ~12 MB, not ~800 MB.
tar czf /tmp/aitutor_box.tgz -C "$ROOT" \
  --exclude='*/multi_turn_results/*' --exclude='*/single_turn_results/*' \
  --exclude='*/ollama_models/*' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*/viewer_deploy/index.html' --exclude='*/prompt_snapshots/*' \
  ai_tutor evals manage.py requirements.txt infra/ollama \
  offline_eval/run_matrix.sh offline_eval/seed_ollama_configs.py \
  offline_eval/aggregate.py offline_eval/models_27b_only.txt \
  offline_eval/models_4b_only.txt
echo "  payload $(du -h /tmp/aitutor_box.tgz | cut -f1)"
# The DB is SHIPPED, not rebuilt. It carries the warm-up steps, 450 mastery
# rows and 427 prerequisite edges that make the warm-up reachable; rebuilding
# risks silent divergence in exactly the fixtures the run depends on.
scp -i "$KEY" -o StrictHostKeyChecking=no -P "$PORT" \
  /tmp/aitutor_box.tgz "$ROOT/db.sqlite3" "$ROOT/.env" "root@$IP:/root/" >/dev/null
echo "  code + db.sqlite3 + .env delivered"

echo "===== 2. remote setup (detached) ====="
"${SSH[@]}" 'cat > /root/_setup.sh' <<'REMOTE'
set -euo pipefail
export PATH=/opt/conda/bin:$PATH
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0
export OLLAMA_KEEP_ALIVE=1h OLLAMA_MAX_LOADED_MODELS=1

command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
pgrep -f "ollama serve" >/dev/null || {
  setsid nohup ollama serve > /root/ollama.log 2>&1 < /dev/null &
  sleep 8
}
curl -s -m 5 http://127.0.0.1:11434/api/version && echo " <- ollama up"

mkdir -p /root/ai-tutor && cd /root/ai-tutor
tar xzf /root/aitutor_box.tgz
cp -f /root/db.sqlite3 /root/ai-tutor/db.sqlite3
cp -f /root/.env /root/ai-tutor/.env

# Python 3.12 — see note 4 in the header.
if [ ! -d /opt/conda/envs/eval312 ]; then
  conda create -y -q -n eval312 python=3.12 2>&1 | tail -1
fi
source /opt/conda/etc/profile.d/conda.sh && conda activate eval312
python --version
pip install -q --no-input -r requirements.txt 2>&1 | tail -2
python -c "import django; print('  django', django.get_version())"

echo "SETUP DONE"
REMOTE
"${SSH[@]}" 'setsid nohup bash /root/_setup.sh > /root/setup.log 2>&1 < /dev/null & sleep 2; echo "  detached pid $(pgrep -f _setup.sh | head -1)"'

echo
echo "Setup is running ON the box and survives disconnection."
echo "  watch:  ssh -i $KEY -p $PORT root@$IP 'tail -f /root/setup.log'"
echo "  then:   bash offline_eval/box_run.sh $IP $PORT <models_file> <subset> <out>"
