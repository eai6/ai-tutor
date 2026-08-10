"""One tool-driven turn per API provider, before committing to the sweep.

The mt100 roster crosses three vendors, and each has its own way of refusing a
tool call — gpt-5.6 rejects function tools outright unless reasoning is off.
Catching that here costs pennies; catching it 20 hours into the sweep does not.

Run: ./venv/bin/python offline_eval/preflight_mt100.py
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for line in (ROOT / '.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        key, _, val = line.partition('=')
        os.environ.setdefault(key.strip(), val.strip().strip('"\''))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django  # noqa: E402
django.setup()

from apps.llm.client import get_llm_client  # noqa: E402
from apps.llm.models import ModelConfig  # noqa: E402

TOOLS = [{
    'name': 'pose_question',
    'description': 'Pose a question to the student',
    'input_schema': {
        'type': 'object',
        'properties': {'question_text': {'type': 'string'}},
        'required': ['question_text'],
    },
}]
# One representative per provider path — the failure modes are per-vendor.
ARMS = [
    ('anthropic', 'claude-haiku-4-5-20251001'),
    ('openai', 'gpt-5.6-sol'),
    ('openai', 'gpt-5.4-nano'),
    ('google', 'gemini-2.5-flash'),
]


def main() -> int:
    failures = []
    for provider, model in ARMS:
        # ModelConfig.resolve_runtime (not a bare ModelConfig(...)) — it infers
        # api_key_env_var from the provider (ANTHROPIC_API_KEY / OPENAI_API_KEY
        # / GOOGLE_API_KEY). A bare constructor leaves api_key_env_var at the
        # model field's default ('ANTHROPIC_API_KEY') for every arm, so the
        # openai/google arms would silently read the wrong key. Same helper
        # offline_eval/_probe_cloud_models.py already uses for ad-hoc runs.
        cfg = ModelConfig.resolve_runtime(provider, model)
        if cfg is None:
            failures.append(
                f'{provider}/{model}: resolve_runtime returned None '
                '(unknown provider or missing API key env var)')
            continue
        try:
            resp = get_llm_client(cfg).generate_with_tools(
                messages=[{'role': 'user',
                           'content': 'Ask me one question about map scale.'}],
                system_prompt='You are a tutor. Always call pose_question.',
                tools=TOOLS, max_tokens=2048)
            # All three providers' generate_with_tools return an
            # Anthropic-Message-shaped object: raw anthropic.types.Message
            # for the Anthropic client, AdaptedMessage (apps/llm/client.py)
            # for OpenAI/Gemini. Both expose `.content` — a list of blocks
            # with `.type` — not `.content_blocks`, which doesn't exist on
            # either shape.
            blocks = getattr(resp, 'content', None) or []
            calls = [b for b in blocks if getattr(b, 'type', None) == 'tool_use']
            if not calls:
                failures.append(f'{provider}/{model}: no tool call returned')
            else:
                print(f'  OK  {provider}/{model}')
        except Exception as exc:
            failures.append(f'{provider}/{model}: {type(exc).__name__}: {exc}')
    for f in failures:
        print(f'  FAIL {f}')
    print('PREFLIGHT OK' if not failures else 'PREFLIGHT FAILED')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
