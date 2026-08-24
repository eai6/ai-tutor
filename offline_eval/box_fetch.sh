#!/usr/bin/env bash
# Pull one arm's results (board + trace) back from the box.
#
#   bash offline_eval/box_fetch.sh <ip> <port> <out_dir>
#
# Results live only on a rented host until this runs, and a destroyed instance
# takes them with it. Fetch BEFORE teardown, always.
set -euo pipefail
IP="${1:?usage: box_fetch.sh <ip> <port> <out_dir>}"; PORT="${2:?}"; OUT="${3:?}"
KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_vast}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/offline_eval/multi_turn_results/$OUT"
mkdir -p "$DEST"
scp -i "$KEY" -o StrictHostKeyChecking=no -P "$PORT" -r \
  "root@$IP:/root/ai-tutor/offline_eval/multi_turn_results/$OUT/*" "$DEST/" 2>/dev/null || true
echo "fetched into $DEST:"
ls -1 "$DEST" | sed 's/^/  /'
[ -d "$DEST/trace" ] && echo "  trace/ ($(ls -1 "$DEST/trace" | wc -l | tr -d ' ') file(s))"
