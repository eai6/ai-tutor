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
only when `Course.subject_code == 'mathematics'`.

Subject keys mirror `Course.SubjectCode` values exactly:
  - 'mathematics' (canonical) — math-specific rules
  - 'geography', 'physics', 'chemistry', 'biology', 'english',
    'french', 'history', 'computer_science', 'other' — currently
    map to the empty 'general' default; add bespoke injections as
    sibling modules when curriculum demands.

Aliases:
  - 'math'    → 'mathematics' (legacy / convenience)
  - 'general' → empty default (used when subject_code is blank
    or unrecognised)

Provider keys (match the prompts dispatcher):
  - 'anthropic'           — XML-tagged block
  - 'google' / 'gemini'   — markdown section
  - 'openai'              — markdown section (same as Gemini)

How to use:
    from apps.tutoring.prompts.injections import get_subject_injection

    injection = get_subject_injection('mathematics', provider='google')
    # → returns the Gemini-formatted math rules block, or '' if none
"""

from __future__ import annotations

from . import general, math


# Canonical subject-code → module mapping. Add new subject modules
# (e.g. science.py with unit/sig-fig rules) here as sibling files
# and register them below.
_REGISTRY = {
    # Default + alias
    'general': general,
    # Mathematics canonical
    'mathematics': math,
    # Legacy / convenience aliases for 'mathematics'
    'math': math,
    # Other Course.SubjectCode values — currently no bespoke rules
    'geography': general,
    'physics': general,
    'chemistry': general,
    'biology': general,
    'english': general,
    'french': general,
    'history': general,
    'computer_science': general,
    'other': general,
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
    pack = _REGISTRY.get(
        (subject_pack or 'general').strip().lower(),
        general,
    )
    prov_key = _PROVIDER_ALIASES.get(
        (provider or 'anthropic').strip().lower(),
        'anthropic',
    )
    attr = f'INJECTION_{prov_key.upper()}'
    return getattr(pack, attr, '') or ''


__all__ = [
    'get_subject_injection',
]
