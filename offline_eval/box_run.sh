#!/usr/bin/env bash
# Run one eval arm ON the box, detached, and pull the results back.
#
#   bash offline_eval/box_run.sh <ip> <port> <models_file> <subset> <out_dir>
#   bash offline_eval/box_run.sh 1.2.3.4 22279 \
#        offline_eval/models_27b_only.txt "math --sample 34 --seed 0" math_27b_v3
#
# Preconditions are CHECKED, not assumed. Every one of them has silently
# invalidated a run at least once:
#   * the model tag exists                — a missing tag makes run_matrix.sh
#                                           print "pull failed — skipping" and
#                                           produce an empty board
#   * the subset selects what you expect  — a mis-ported tag once meant a run
#                                           completed over the wrong scenarios
#   * the warm-up is reachable            — it depends on seeded mastery in the
#                                           shipped DB, and was silently absent
#                                           for every board before 2026-08-24
set -euo pipefail

IP="${1:?usage: box_run.sh <ip> <port> <models_file> <subset> <out_dir>}"
PORT="${2:?}"; MODELS="${3:?}"; SUBSET="${4:?}"; OUT="${5:?}"
KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_vast}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=20 -p "$PORT" "root@$IP")

MODELS_BASE="$(basename "$MODELS")"

echo "===== preconditions ====="
"${SSH[@]}" "cd /root/ai-tutor && source /opt/conda/etc/profile.d/conda.sh && conda activate eval312 && \
  export DEBUG=True OLLAMA_HOST=127.0.0.1:11434 TUTORING_QUESTION_TYPES=mcq && \
  python - <<'PY'
import os, pathlib, re, types
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.tutoring.management.commands.run_eval import filter_by_subset
from ai_tutor.apps.curriculum.models import Lesson, LessonStep
from ai_tutor.apps.tutoring.models import TutorSession
from ai_tutor.apps.tutoring.student_sim.driver import _get_or_create_sim_user
from ai_tutor.apps.tutoring.simple_tutor.warm_up import select_warm_up_question

rows=[]
for f in pathlib.Path('evals/dataset/multi_turn').rglob('*.yaml'):
    t=f.read_text(); m=re.search(r'(?m)^tags: \[(.*)\]', t)
    rows.append(types.SimpleNamespace(id=f.stem, tags=[x.strip() for x in m.group(1).split(',')] if m else []))
tag='${SUBSET}'.split()[0]
sel=filter_by_subset(rows, tag)
print(f'  subset {tag!r} selects {len(sel)} scenario(s)')
assert sel, 'subset selects NOTHING — the tag did not travel with the fixtures'

n=LessonStep.objects.filter(step_type=LessonStep.StepType.WARM_UP).count()
print(f'  warm-up steps in the DB: {n}')
les=Lesson.objects.filter(id__in=[1463,1137]).first()
if les:
    inst=getattr(getattr(les.unit,'course',None),'institution',None)
    s=TutorSession(institution=inst, student=_get_or_create_sim_user(inst), lesson=les, engine='simple')
    s.pk=les.id
    print(f'  warm-up selectable: {select_warm_up_question(s) is not None}')
PY" || { echo "!! preconditions failed — not starting the run"; exit 1; }

echo "===== model tag present? ====="
"${SSH[@]}" "OLLAMA_HOST=127.0.0.1:11434 ollama list | tail -n +2 | awk '{print \"  \"\$1}'"

echo "===== launching (detached) ====="
"${SSH[@]}" "cat > /root/_run.sh" <<REMOTE
set -uo pipefail
cd /root/ai-tutor
source /opt/conda/etc/profile.d/conda.sh && conda activate eval312
export AI_TUTOR_ROOT="\$PWD" PY=python DEBUG=True
export TUTOR_CALL_MODE=two EVAL_SKIP_RUBRIC=1 TUTORING_QUESTION_TYPES=mcq
export OLLAMA_HOST=http://127.0.0.1:11434
export MODELS_FILE="\$PWD/offline_eval/${MODELS_BASE}"
export MODE="--multi-turn --subset ${SUBSET}"
export RESULTS_DIR="\$PWD/offline_eval/multi_turn_results/${OUT}"
mkdir -p "\$RESULTS_DIR"
date -u +"start %FT%TZ"
bash offline_eval/run_matrix.sh
echo "exit=\$?"
date -u +"end %FT%TZ"
REMOTE
"${SSH[@]}" 'setsid nohup bash /root/_run.sh > /root/run.log 2>&1 < /dev/null & sleep 2; echo "  detached pid $(pgrep -f _run.sh | head -1)"'

echo
echo "Running on the box. It survives disconnection."
echo "  watch:  ssh -i $KEY -p $PORT root@$IP 'tail -f /root/run.log'"
echo "  fetch:  bash offline_eval/box_fetch.sh $IP $PORT $OUT"
