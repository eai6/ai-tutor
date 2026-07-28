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
    from apps.llm.client import (
        _OLLAMA_RUNTIME_OVERHEAD_BYTES, _ollama_kv_bytes, _ollama_model_footprint,
    )
    from apps.llm.model_profiles import MODEL_PROFILES

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


def _has_exact_profile(spec: str) -> bool:
    """True when MODEL_PROFILES has an EXACT entry for this spec.

    Checked against the raw dict rather than get_model_profile(), which falls
    back to family patterns — the fallback is precisely what we are trying to
    detect and exclude.
    """
    from apps.llm.model_profiles import MODEL_PROFILES

    return spec in MODEL_PROFILES


def available_choices(include: str = '') -> list[tuple[str, list[tuple[str, str]]]]:
    """Grouped ``(group_label, [(value, label), ...])`` for a select widget.

    Value is the bare model name, matching what ``ModelConfig.model_name``
    stores. ``include`` is the row's current value, always kept selectable so
    opening an existing record cannot silently invalidate it.
    """
    from apps.llm.models import ModelConfig

    runnable, unusable = [], []
    for tag in local_tags():
        spec = f'local_ollama/{tag}'
        bare = tag.rsplit(':latest', 1)[0] if tag.endswith(':latest') else tag
        profiled = _has_exact_profile(spec) or _has_exact_profile(f'local_ollama/{bare}')
        if not profiled:
            unusable.append((bare, f'{bare}  (no profile — would be sized for cloud)'))
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
    cloud = sorted(
        {(m, f'{m}  ({p})') for p, m in known if p != 'local_ollama' and m not in seen}
    )

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
