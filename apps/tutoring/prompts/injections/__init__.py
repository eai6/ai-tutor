"""Subject-specific tutor prompt injections.

Phase 2c of task #229. The base tutor prompt (per-provider in
sibling files anthropic.py / gemini.py / openai.py) covers everything
shared across subjects: role, state machine, pedagogy, safety, tools,
output format. Subject-specific rules (math probing rules, science
unit conventions, etc.) live here.

Why: previously the Anthropic base template carried ~50 lines of
math-only rules (PARTIAL_CORRECT signals, banned probes for math,
estimation prompts, word problem extraction). Every non-math
session — geography, history, language arts — paid the token cost
AND got math-specific instructions that made the LLM overzealous
about working / probing on subjects where it doesn't apply.

Now the base prompt is subject-agnostic and the math rules attach
only when `Course.is_math` is True.

How to use:
    from apps.tutoring.prompts.injections import get_subject_injection

    injection = get_subject_injection('math', provider='google')
    # → returns the Gemini-formatted math rules block, or '' if none

Subject packs:
  - 'general' — empty default. Returns ''.
  - 'math'    — math-specific rules (probing, working, estimation,
                word problems).
  - (future)  — 'science', 'language_arts', 'social_studies', etc.

Provider keys (match the prompts dispatcher):
  - 'anthropic'           — XML-tagged block
  - 'google' / 'gemini'   — markdown section
  - 'openai'              — markdown section (same as Gemini)
"""

from __future__ import annotations

from . import general, math


_REGISTRY = {
    'general': general,
    'math': math,
}

_PROVIDER_ALIASES = {
    'anthropic': 'anthropic',
    'google': 'gemini',
    'gemini': 'gemini',
    'openai': 'openai',
}


def get_subject_injection(subject_pack: str, provider: str) -> str:
    """Return the subject-specific injection text for the given
    provider, or '' when no pack matches.

    Unknown subject_pack → 'general' (empty). Unknown provider →
    Anthropic format (Phase 1 fallback convention).
    """
    pack = _REGISTRY.get((subject_pack or 'general').strip().lower(), general)
    prov_key = _PROVIDER_ALIASES.get(
        (provider or 'anthropic').strip().lower(),
        'anthropic',
    )
    attr = f'INJECTION_{prov_key.upper()}'
    return getattr(pack, attr, '') or ''


__all__ = [
    'get_subject_injection',
]
