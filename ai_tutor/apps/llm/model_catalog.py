"""Which models an admin may pick, as selectable options rather than free text.

`ModelConfig.model_name` is a CharField, so choosing a model has meant typing an
exact tag. On the offline Jetson that is a trap with no feedback: a typo, or a
tag that is not pulled, or a tag with no entry in ``apps/llm/model_profiles.py``
all fail the same way — at the next student turn, on a box with no monitor.

The last of those is the subtle one. A local tag without an exact profile entry
falls through to a CLOUD family profile and gets sized at num_ctx=24192, which
does not fit this box. So "is it pulled" is not sufficient; "is it pulled AND
profiled" is the real question, and it is what this module answers.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OLLAMA_HOST = 'http://127.0.0.1:11434'

# What infra/systemd/ollama.service actually launches with. See _fits().
DEPLOY_KV_TYPE = 'q8_0'

# Headroom left for the OS, the desktop if present, gunicorn workers and the
# page cache. Measured on this box: ~1.3 GB in use with the desktop up and the
# tutor serving, before any model. 2 GB is that plus margin — deliberately
# generous, because a model that is offered and then fails to load on a headless
# kiosk is worse than one that was never offered.
SYSTEM_RESERVE_BYTES = 2 * 1024 ** 3

# Cloud models offered even when no ModelConfig row exists yet, so an admin can
# pick one without first hand-typing the tag. Grouped by provider because
# ModelConfig stores provider and model_name separately and the picker only
# edits the latter — selecting a Gemini model on an `anthropic` row would build
# a spec that resolves to nothing.
#
# IDs are exact and complete: current Claude IDs carry NO date suffix
# (`claude-opus-5`, never `claude-opus-5-2026…`), and the Gemini entries are the
# ones already used elsewhere in this codebase rather than guesses.
CURATED_CLOUD_MODELS = {
    'anthropic': [
        ('claude-opus-5', 'Claude Opus 5 — most capable'),
        ('claude-opus-4-8', 'Claude Opus 4.8'),
        ('claude-sonnet-5', 'Claude Sonnet 5 — balanced'),
        ('claude-sonnet-4-6', 'Claude Sonnet 4.6'),
        ('claude-haiku-4-5', 'Claude Haiku 4.5 — fastest, cheapest'),
    ],
    'google': [
        ('gemini-3.5-flash', 'Gemini 3.5 Flash'),
        ('gemini-2.5-pro', 'Gemini 2.5 Pro'),
        ('gemini-2.5-flash', 'Gemini 2.5 Flash'),
    ],
}


def local_tags(api_base: str = OLLAMA_HOST, timeout: int = 5) -> list[str]:
    """Ollama tags pulled on this machine. Empty when Ollama is not running."""
    import requests

    try:
        resp = requests.get(f"{api_base}/api/tags", timeout=timeout)
        resp.raise_for_status()
        names = []
        for m in resp.json().get('models') or []:
            name = m.get('name') or m.get('model') or ''
            if name:
                names.append(name)
        return sorted(names)
    except Exception as e:
        logger.debug("[ModelCatalog] could not list local tags: %s", e)
        return []


def _fits(spec: str, api_base: str = OLLAMA_HOST) -> bool:
    """Whether this model can actually be loaded on this machine right now.

    Delegates to the real preflight rather than re-deriving a size rule, so the
    picker and the runtime cannot disagree. Being pulled and profiled is not
    enough: qwen3.5:9b is both, and its 6.1 GB of weights do not fit a 7.4 GB
    unified pool. Offering it would let an admin break a headless kiosk with a
    dropdown.
    """
    from ai_tutor.apps.llm.client import (
        _OLLAMA_RUNTIME_OVERHEAD_BYTES, _ollama_kv_bytes, _ollama_model_footprint,
    )
    from ai_tutor.apps.llm.model_profiles import MODEL_PROFILES

    name = spec.split('/', 1)[1] if '/' in spec else spec
    try:
        footprint = _ollama_model_footprint(api_base, name)
        if footprint is None:
            return True                       # unreadable — do not hide it
        weights, info, file_ctx = footprint
        profile = MODEL_PROFILES.get(spec)
        num_ctx = getattr(profile, 'num_ctx', None) or file_ctx or 4096
        # KV_CACHE_TYPE passed explicitly. It is a SERVER setting, and this
        # process is not the server — left to the ambient environment it reads
        # nothing, assumes f16, and doubles the estimate. infra/systemd/
        # ollama.service runs q8_0, so that is the deployment's real value.
        needed = weights + (_ollama_kv_bytes(info, num_ctx, DEPLOY_KV_TYPE) or 0) \
            + _OLLAMA_RUNTIME_OVERHEAD_BYTES

        # Judged against TOTAL memory less a system reserve, not MemAvailable.
        # The picker answers "can this device run this model", a property of the
        # hardware. MemAvailable is whatever the desktop, the browser and today's
        # resident model happen to be holding, so using it would hide the working
        # default half the time and change its answer between page loads.
        with open('/proc/meminfo') as fh:
            total = next(int(l.split()[1]) for l in fh
                         if l.startswith('MemTotal:')) * 1024
        return needed <= total - SYSTEM_RESERVE_BYTES
    except Exception as e:
        logger.debug("[ModelCatalog] fit check failed for %s: %s", spec, e)
        return True


def _pins_own_context(tag: str, api_base: str = OLLAMA_HOST) -> bool:
    """True when the tag bakes num_ctx into its own Modelfile.

    This is the property that makes a local model safe to run here, and it is
    NOT the same as having a profile: a Modelfile PARAMETER applies on every
    endpoint, including the OpenAI-compatible one our profile cannot reach.

    Fail-soft returns True — an unreadable tag is not evidence of a defect, and
    hiding a working model is worse than listing a questionable one.
    """
    from ai_tutor.apps.llm.client import _ollama_model_footprint

    footprint = _ollama_model_footprint(api_base, tag)
    if footprint is None:
        return True
    return footprint[2] is not None          # (weights, info, modelfile num_ctx)


def _has_exact_profile(spec: str) -> bool:
    """True when MODEL_PROFILES has an EXACT entry for this spec.

    Checked against the raw dict rather than get_model_profile(), which falls
    back to family patterns — the fallback is precisely what we are trying to
    detect and exclude.
    """
    from ai_tutor.apps.llm.model_profiles import MODEL_PROFILES

    return spec in MODEL_PROFILES


def available_choices(include: str = '') -> list[tuple[str, list[tuple[str, str]]]]:
    """Grouped ``(group_label, [(value, label), ...])`` for a select widget.

    Value is the bare model name, matching what ``ModelConfig.model_name``
    stores. ``include`` is the row's current value, always kept selectable so
    opening an existing record cannot silently invalidate it.
    """
    from ai_tutor.apps.llm.models import ModelConfig

    runnable, unusable = [], []
    for tag in local_tags():
        spec = f'local_ollama/{tag}'
        bare = tag.rsplit(':latest', 1)[0] if tag.endswith(':latest') else tag
        profiled = _has_exact_profile(spec) or _has_exact_profile(f'local_ollama/{bare}')
        if not profiled:
            unusable.append((bare, f'{bare}  (no profile — would be sized for cloud)'))
        elif not _pins_own_context(bare):
            # Offering this is a trap. A profile pins num_ctx only for requests
            # THIS codebase builds; the grader's Tier-2 verifier reaches Ollama
            # through instructor's OpenAI-compatible endpoint, which has no
            # num_ctx field and therefore loads a separate 4096-context runner.
            # Under OLLAMA_MAX_LOADED_MODELS=1 the two evict each other every
            # graded turn — measured at 4 reloads across 2 turns on the bare
            # qwen3.5:2b, and 0 once the same weights were given a pinned tag.
            unusable.append((
                bare,
                f'{bare}  (no num_ctx in its Modelfile — would reload every turn; '
                f'build a pinned tag)',
            ))
        elif not _fits(f'local_ollama/{bare}'):
            unusable.append((bare, f'{bare}  (too large for this device)'))
        else:
            runnable.append((bare, f'{bare}  (local, fits)'))

    known = (
        ModelConfig.objects
        .exclude(model_name='')
        .values_list('provider', 'model_name')
        .distinct()
    )
    seen = {v for v, _ in runnable + unusable}
    # Curated first (in the order declared — most capable first), then anything
    # else already present in the database. The provider is in every label
    # because this widget edits model_name only: the admin has to keep the
    # provider field in step, and a Gemini id on an `anthropic` row resolves to
    # nothing at runtime.
    cloud: list[tuple[str, str]] = []
    for provider, entries in CURATED_CLOUD_MODELS.items():
        for name, label in entries:
            if name not in seen:
                seen.add(name)
                cloud.append((name, f'{label}  [{provider}]'))
    extra = sorted(
        {(m, p) for p, m in known if p != 'local_ollama' and m not in seen}
    )
    cloud += [(m, f'{m}  [{p}]') for m, p in extra]

    groups: list[tuple[str, list[tuple[str, str]]]] = []
    if runnable:
        groups.append(('Local — ready to run offline', runnable))
    if cloud:
        groups.append(('Cloud — needs internet', cloud))
    if unusable:
        groups.append(('Local — pulled but NOT usable here', unusable))

    flat = {v for _, opts in groups for v, _ in opts}
    if include and include not in flat:
        groups.insert(0, ('Current value', [(include, f'{include}  (current)')]))
    return groups
