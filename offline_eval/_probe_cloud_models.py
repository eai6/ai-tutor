"""Ping candidate cloud model IDs via the project client to see which resolve.
A 1-token call per id — cheap. Prints which are reachable so the benchmark
sweep only runs valid IDs (a wrong id would waste a full 60-scenario run).

    venv/bin/python offline_eval/_probe_cloud_models.py
"""
import os, sys, django
ROOT = '/home/daniel/Documents/work/Nyansapo/web/ai-tutor'
os.chdir(ROOT); sys.path.insert(0, ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.llm.models import ModelConfig
from apps.llm.client import get_llm_client

CANDIDATES = [
    # Anthropic
    ('anthropic', 'claude-opus-4-7'),
    ('anthropic', 'claude-opus-4-8'),
    ('anthropic', 'claude-sonnet-4-6'),
    ('anthropic', 'claude-haiku-4-5-20251001'),
    # Google / Gemini
    ('google', 'gemini-2.5-pro'),
    ('google', 'gemini-2.5-flash'),
    ('google', 'gemini-3-pro-preview'),
    ('google', 'gemini-3-pro'),
    ('google', 'gemini-3.1-pro'),
    ('google', 'gemini-3-flash-preview'),
    ('google', 'gemini-3.5-flash'),
    ('google', 'gemini-3-flash'),
]


def probe(provider, model):
    cfg = ModelConfig.resolve_runtime(provider, model)
    if cfg is None:
        return False, 'resolve_runtime returned None (key/env missing?)'
    try:
        client = get_llm_client(cfg)
        resp = client.generate(messages=[{'role': 'user', 'content': 'hi'}],
                               system_prompt='Reply with one word.', max_tokens=5)
        txt = (getattr(resp, 'content', '') or getattr(resp, 'text', '') or '')[:30]
        return True, repr(txt)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:140]}"


def main():
    ok = []
    for provider, model in CANDIDATES:
        good, detail = probe(provider, model)
        print(f"  [{'OK ' if good else 'XX '}] {provider}/{model:32} {detail}")
        if good:
            ok.append(f"{provider}/{model}")
    print("\nReachable:")
    for s in ok:
        print(f"  {s}")


if __name__ == '__main__':
    main()
