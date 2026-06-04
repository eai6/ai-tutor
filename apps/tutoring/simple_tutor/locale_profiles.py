"""Per-locale tutor identity profiles — the static, country-specific parts of
the tutor's role that vary by deployment/course locale.

Kept as code (not DB) because they are static, must be cache-deterministic
(the audience clause lives in the cached Block 0 of the system prompt), and
mirror the locale-branch pattern of ``_build_locale_rule`` in prompts.py. The
admin-editable factual local-context *library* is a separate concern — see
``SeychellesContext`` and ``memory/tutor_country_context_plan.md``.

Adding a country = one entry here (plus a ``_build_locale_rule`` branch for
language). No DB change.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocaleProfile:
    locale: str
    # The audience clause dropped into the tutor's <role> line. MUST stay
    # byte-for-byte stable per locale — it sits in the cached Block 0 prefix,
    # so any drift for an existing locale invalidates that locale's cache.
    role_audience: str


# en-us is the EXACT historical string, so the Seychelles cache prefix and
# tutor behaviour are unchanged by this refactor.
_DEFAULT = LocaleProfile(
    locale='en-us',
    role_audience='Seychelles secondary-school students (grades S3-S5)',
)

LOCALE_PROFILES = {
    'en-us': _DEFAULT,
    'pt-mz': LocaleProfile(
        locale='pt-mz',
        role_audience='Mozambican secondary-school students (8ª–12ª Classe)',
    ),
}


def get_profile(locale: str | None) -> LocaleProfile:
    """Return the LocaleProfile for ``locale`` (case-insensitive), falling back
    to the en-us/Seychelles default for unknown or missing locales — matching
    the pre-refactor behaviour where every student got the Seychelles role."""
    if not locale:
        return _DEFAULT
    return LOCALE_PROFILES.get(locale.lower(), _DEFAULT)
