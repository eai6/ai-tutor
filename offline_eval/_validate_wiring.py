"""Validate the model-swap wiring (no Ollama server needed — stops before the HTTP call)."""
import os, sys, django
ROOT = os.environ.get('AI_TUTOR_ROOT') or '/home/daniel/Documents/work/Nyansapo/web/ai-tutor'
os.chdir(ROOT); sys.path.insert(0, ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
# Simulate what run_matrix.sh sets for one model:
os.environ['TUTOR_MODEL_OVERRIDE'] = 'local_ollama/qwen2.5:0.5b'
django.setup()

from apps.llm.models import ModelConfig
from apps.llm.client import get_llm_client

tut = ModelConfig.get_for('tutoring')
judge = ModelConfig.get_for('judge')
sim = ModelConfig.get_for('student_sim')

print("With TUTOR_MODEL_OVERRIDE=local_ollama/qwen2.5:0.5b:")
print(f"  tutoring -> {tut.provider}/{tut.model_name}  (is_active={tut.is_active})")
client = get_llm_client(tut)
print(f"  tutor client class -> {type(client).__name__}")
print(f"  judge       -> {judge.provider}/{judge.model_name}   (UNCHANGED = good)")
print(f"  student_sim -> {sim.provider}/{sim.model_name}   (UNCHANGED = good)")

ok = (
    tut.provider == 'local_ollama'
    and tut.model_name == 'qwen2.5:0.5b'
    and type(client).__name__ == 'OllamaClient'
    and judge.provider == 'anthropic'
    and sim.provider == 'anthropic'
)
print("\nRESULT:", "WIRING OK ✓" if ok else "WIRING BROKEN ✗")
sys.exit(0 if ok else 1)
