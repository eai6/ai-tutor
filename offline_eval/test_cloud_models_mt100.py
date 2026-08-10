"""The mt100 model list is data the sweep depends on; typos cost hours."""
import pathlib

FILE = pathlib.Path(__file__).resolve().parent / 'cloud_models_mt100.txt'
EXPECTED = {
    'anthropic/claude-opus-5': 'claude-opus-5',
    'anthropic/claude-sonnet-5': 'claude-sonnet-5',
    'anthropic/claude-haiku-4-5-20251001': 'claude-haiku-4-5',
    'anthropic/claude-opus-4-7': 'claude-opus-4-7',
    'anthropic/claude-sonnet-4-6': 'claude-sonnet-4-6',
    'openai/gpt-5.6-sol': 'gpt-5.6-sol',
    'openai/gpt-5.6-terra': 'gpt-5.6-terra',
    'openai/gpt-5.6-luna': 'gpt-5.6-luna',
    'openai/gpt-5.4-mini': 'gpt-5.4-mini',
    'openai/gpt-5.4-nano': 'gpt-5.4-nano',
    'google/gemini-3.5-flash': 'gemini-3.5-flash',
    'google/gemini-3.1-pro-preview': 'gemini-3.1-pro',
    'google/gemini-2.5-flash': 'gemini-2.5-flash',
    'google/gemini-2.5-pro': 'gemini-2.5-pro',
}


def _rows():
    out = {}
    for line in FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        out[parts[0]] = parts[1]
    return out


def test_has_exactly_the_fourteen_api_arms():
    assert _rows() == EXPECTED


def test_safe_names_are_unique_and_filesystem_safe():
    names = list(_rows().values())
    assert len(names) == len(set(names)), 'safe_name collides — results overwrite'
    for n in names:
        assert '/' not in n and ' ' not in n, n
