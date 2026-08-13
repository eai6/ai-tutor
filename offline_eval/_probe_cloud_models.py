"""Ping candidate cloud model IDs via the project client to see which resolve.
A 1-token call per id — cheap. Prints which are reachable so the benchmark
sweep only runs valid IDs (a wrong id would waste a full 60-scenario run).

    venv/bin/python offline_eval/_probe_cloud_models.py
"""
import os, sys, django
ROOT = os.environ.get('AI_TUTOR_ROOT') or '/home/daniel/Documents/work/Nyansapo/web/ai-tutor'
os.chdir(ROOT); sys.path.insert(0, ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()

from ai_tutor.apps.llm.models import ModelConfig
from ai_tutor.apps.llm.client import get_llm_client

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
    # Vertex Model Garden MaaS — (provider, model, region). Regions differ.
    ('vertex_model_garden', 'deepseek-ai/deepseek-v3.2-maas', 'global'),
    ('vertex_model_garden', 'deepseek-ai/deepseek-v3.1-maas', 'us-west2'),
    ('vertex_model_garden', 'moonshotai/kimi-k2-thinking-maas', 'global'),
    ('vertex_model_garden', 'deepseek-ai/deepseek-r1-0528-maas', 'us-central1'),
    # Batch 2 — Qwen3 / Grok / GLM (all global)
    ('vertex_model_garden', 'qwen/qwen3-next-80b-a3b-instruct-maas', 'global'),
    ('vertex_model_garden', 'qwen/qwen3-next-80b-a3b-thinking-maas', 'global'),
    ('vertex_model_garden', 'qwen/qwen3-coder-480b-a35b-instruct-maas', 'global'),
    ('vertex_model_garden', 'qwen/qwen3-235b-a22b-instruct-2507-maas', 'global'),
    ('vertex_model_garden', 'xai/grok-4.1-fast-reasoning', 'global'),
    ('vertex_model_garden', 'xai/grok-4.1-fast-non-reasoning', 'global'),
    ('vertex_model_garden', 'xai/grok-4.20-reasoning', 'global'),
    ('vertex_model_garden', 'xai/grok-4.20-non-reasoning', 'global'),
    ('vertex_model_garden', 'zai-org/glm-5-maas', 'global'),
    ('vertex_model_garden', 'zai-org/glm-4.7-maas', 'global'),
]


def probe(provider, model):
    cfg = ModelConfig.resolve_runtime(provider, model)
    if cfg is None:
        return False, 'resolve_runtime returned None (key/env missing?)'
    try:
        client = get_llm_client(cfg)
        resp = client.generate(messages=[{'role': 'user', 'content': 'hi'}],
                               system_prompt='Reply with one word.', max_tokens=32)
        txt = (getattr(resp, 'content', '') or getattr(resp, 'text', '') or '')[:30]
        return True, repr(txt)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:140]}"


def main():
    ok = []
    for cand in CANDIDATES:
        provider, model = cand[0], cand[1]
        region = cand[2] if len(cand) > 2 else None
        if region:
            os.environ['GOOGLE_CLOUD_LOCATION'] = region
        else:
            # Clear any region left by a prior candidate so a regionless
            # provider never inherits a stale GOOGLE_CLOUD_LOCATION.
            os.environ.pop('GOOGLE_CLOUD_LOCATION', None)
        good, detail = probe(provider, model)
        label = f"{provider}/{model}" + (f" @{region}" if region else "")
        print(f"  [{'OK ' if good else 'XX '}] {label:48} {detail}")
        if good:
            ok.append(f"{provider}/{model}")
    print("\nReachable:")
    for s in ok:
        print(f"  {s}")


if __name__ == '__main__':
    main()
