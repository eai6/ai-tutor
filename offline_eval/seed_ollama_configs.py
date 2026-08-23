"""Seed an inactive local_ollama ModelConfig row per model in models.txt.

Why: ModelConfig.resolve_runtime() (invoked when TUTOR_MODEL_OVERRIDE is set)
resolves a (provider, model_name) pair by finding an existing row first. For
keyless local providers it returns None unless such a row exists. We create
the rows as is_active=False so they're invisible to normal get_for() resolution
(which filters is_active=True) but findable by the override's exact-pair lookup.

Idempotent. Run once before the matrix sweep:
    venv/bin/python offline_eval/seed_ollama_configs.py
"""
import os, sys, django
from pathlib import Path
# Derive the repo root from this file's location, as the sibling offline_eval
# scripts do. The previous hardcoded default pointed at one developer's machine
# and made this script unrunnable anywhere else, including the Jetson.
ROOT = os.environ.get('AI_TUTOR_ROOT') or str(Path(__file__).resolve().parent.parent)
os.chdir(ROOT); sys.path.insert(0, ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.llm.models import ModelConfig
from apps.accounts.models import Institution


OLLAMA_API_BASE = os.environ.get('OLLAMA_API_BASE', '')


def load_tags(path):
    tags = []
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        tags.append(line.split()[0])
    return tags


def main():
    inst = Institution.objects.filter(pk=999001).first() or Institution.get_global()
    # run_matrix.sh already honours MODELS_FILE and calls this script, so without
    # reading it too a scoped sweep (e.g. a Jetson-only tag list) seeds the wrong
    # rows — the full models.txt — and the scoped run silently resolves against them.
    models_file = os.environ.get('MODELS_FILE') or os.path.join(
        ROOT, 'offline_eval', 'models.txt')
    tags = load_tags(models_file)
    created = updated = 0
    for tag in tags:
        obj, was_created = ModelConfig.objects.update_or_create(
            provider='local_ollama', model_name=tag,
            defaults=dict(
                institution=inst,
                name=f'offline-eval: {tag}',
                purpose='tutoring',          # harmless: is_active=False won't shadow
                api_key_env_var='',          # Ollama needs no key
                # Empty => OllamaClient falls back to http://localhost:11434.
                # Set OLLAMA_API_BASE to point the Django client at a REMOTE
                # Ollama (a rented GPU box): the eval then runs on this machine
                # while inference happens there, with the Modelfile, num_ctx and
                # ollama_think mechanics unchanged. Export the matching
                # OLLAMA_HOST so run_matrix.sh's `ollama pull/create/list/stop`
                # CLI calls target the same server.
                api_base=OLLAMA_API_BASE,
                temperature=0.2,             # within the tutoring [0.1, 0.3] clamp
                max_tokens=1024,
                is_active=False,
            ),
        )
        created += was_created
        updated += (not was_created)
        print(f"  {'created' if was_created else 'updated'}: local_ollama/{tag}")
    print(f"\nSeeded {len(tags)} ollama configs ({created} created, {updated} updated).")


if __name__ == '__main__':
    main()
