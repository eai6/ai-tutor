"""Teacher instructions on the session report — written by an LLM, grounded.

What the report already knows, deterministically, is who is in which competency
band and which objectives they got wrong. That part stays exactly as it is: it
comes from exit-ticket ``concept_tag`` matching against the lesson's objectives,
and nothing here can change a band or invent a weakness.

What the LLM adds is the sentence a teacher reads. Before this, each band
carried a constant f-string — the same words for every AE group on every lesson
on the platform, with three objective names spliced in. It told a teacher
nothing the band label had not already told them, and its one real payload was
that list of names.

Design notes, because the failure modes here are specific:

* **One call for the whole report, not one per band.** Five sequential calls on
  a page load is seconds of latency and five ways to fail. One call also lets
  the model see the shape of the whole class — an AE group next to an empty ME
  group is a different situation from one next to a full ME group.
* **The objectives are quoted verbatim, and the prompt says so.** A teacher
  acting on a re-worded or invented sub-objective is the one harmful output
  this can produce, so the model is told to copy the names it is given and the
  result is checked (``_grounded``) before it is shown.
* **Fail-soft to the old templates.** No model configured, no API key, a
  timeout, a malformed response — the report renders the constant text it has
  always rendered. A teacher report that 500s because a model was slow would be
  a bad trade for better prose.
* **It uses the tutoring model.** No dedicated purpose, no seed step. The job
  is two sentences from numbers the platform has already worked out, and the
  model that teaches these students is a reasonable one to describe them.
* **Cached on the inputs, not the page.** The key is a hash of the bands, the
  counts and the objective lists, so the call happens once per distinct state
  of the report rather than once per page view.
"""
from __future__ import annotations

import hashlib
import json
import logging
from django.core.cache import cache
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 24h. The inputs are in the key, so a stale entry is impossible by
# construction — this bound only reclaims memory for reports nobody opens.
CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_PREFIX = 'report_instruction:v1:'

# What each band means, so the model writes advice that fits the situation
# rather than generic encouragement. Kept here rather than in the prompt string
# so the two lists cannot drift apart.
BAND_MEANING = {
    'UN': 'has not taken the exit ticket, so there is no competency data yet',
    'BE': 'scored below the passing threshold and needs intensive support',
    'AE': 'scored close to the threshold and needs a targeted nudge',
    # A student can pass on 8/10 and still have missed two objectives, so ME
    # and EE can arrive with a weak list. Those are residual gaps, not
    # blockers — say so, or the advice reads as contradicting the band.
    'ME': ('met the threshold; any objectives listed are residual gaps to tidy '
           'up, not blockers, and the group is ready for the next lesson'),
    'EE': ('exceeded the threshold and needs extension; any objectives listed '
           'are minor gaps, not blockers'),
}


class BandInstruction(BaseModel):
    code: str = Field(description="The band code this instruction is for: UN, BE, AE, ME or EE.")
    instruction: str = Field(
        description=(
            "Advice for the teacher about this group, in two sentences. "
            "Name the weak objectives given for the band, copied word for word. "
            "Say what to do about them in a way a teacher could act on tomorrow."
        )
    )


class ReportInstructions(BaseModel):
    bands: list[BandInstruction]


def _fingerprint(lesson, groups) -> str:
    """A key over exactly what the model is shown.

    Student identity is deliberately absent: two classes with the same band
    sizes and the same weak objectives get the same advice, and should.
    """
    payload = [
        {
            'code': g['code'],
            'n': len(g.get('students') or []),
            'weak': _weak_for(g),
        }
        for g in groups
    ]
    raw = json.dumps({'lesson': getattr(lesson, 'id', None),
                      'title': getattr(lesson, 'title', ''),
                      'groups': payload}, sort_keys=True)
    return CACHE_PREFIX + hashlib.sha256(raw.encode()).hexdigest()[:32]


# Bands whose weak-objective list means nothing.
#
# UN is "has not taken the exit ticket". A student with no attempt fails every
# objective by default, so common_weak for UN is the whole lesson — and handing
# that to the model produced "Re-teach the foundational concepts: …" for
# students who have not been assessed at all. There is nothing to re-teach yet;
# the only useful advice is to get them to finish and submit.
_BANDS_WITHOUT_MEANINGFUL_WEAKNESS = frozenset({'UN'})


def _weak_for(group) -> list[str]:
    if group.get('code') in _BANDS_WITHOUT_MEANINGFUL_WEAKNESS:
        return []
    return list(group.get('common_weak') or [])


def _build_prompt(lesson, groups) -> str:
    """Data first, task last — the ordering Anthropic measures as better, and
    the one that keeps the instruction closest to the tokens being generated.
    """
    parts = [
        '<lesson>',
        f'  <title>{getattr(lesson, "title", "")}</title>',
        f'  <objective>{getattr(lesson, "objective", "")}</objective>',
        '</lesson>',
        '<groups>',
    ]
    for g in groups:
        code = g['code']
        parts.append(f'  <group code="{code}" students="{len(g.get("students") or [])}">')
        parts.append(f'    <meaning>This group {BAND_MEANING.get(code, "")}.</meaning>')
        weak = _weak_for(g)
        if weak:
            parts.append('    <weak_objectives>')
            for w in weak:
                parts.append(f'      <objective>{w}</objective>')
            parts.append('    </weak_objectives>')
        else:
            parts.append('    <weak_objectives/>')
        parts.append('  </group>')
    parts.append('</groups>')
    parts.append('')
    parts.append(
        '<task>\n'
        'Write one instruction for each group above, addressed to the teacher '
        'of this class.\n'
        '\n'
        'For each group:\n'
        '- Write two sentences. The first names what to re-teach, the second '
        'says how.\n'
        '- Name at most three of that group\'s weak objectives, copied word for '
        'word from <weak_objectives>. Use the objectives given for that group '
        'and no others.\n'
        '- Say how to re-teach them: name a concrete move a teacher can make in '
        'their next lesson — a worked example on the board, a paired drill, a '
        'quick diagnostic question — chosen to fit what the objective actually '
        'asks students to do.\n'
        '- Where a group has no weak objectives listed, write about what the '
        'group needs next instead, and name no objectives at all.\n'
        '\n'
        'Write plainly, as one teacher to another. No preamble, no praise for '
        'the class, no restating the band name.\n'
        '</task>'
    )
    return '\n'.join(parts)


# Words that carry no topic. Overlap on these means nothing.
_STOPWORDS = frozenset("""
a an and are as at be by for from how in into is it its of on or that the their
them then there these this to use used using with within your you students
student able will can should each other more most such than
""".split())

# How much of an objective the instruction has to cover before we accept that
# it is talking about that objective.
#
# 0.5 tolerates the model dropping a tail the teacher does not need — "Determine
# the horizontal easting value for a four-figure grid reference" written as
# "determining the horizontal easting value" is correct advice and covers 4 of
# 7 words. 0.6 rejected exactly that. Off-topic and filler advice overlap zero,
# so the bar is nowhere near them either way; the room between is where the
# false negatives live, and a false negative only costs the old template.
_COVERAGE = 0.5


def _content_words(text: str) -> list[str]:
    cleaned = ''.join(c if (c.isalnum() or c in "-'") else ' ' for c in (text or '').lower())
    return [w for w in cleaned.split() if len(w) > 2 and w not in _STOPWORDS]


def _stem(word: str) -> str:
    """Crude prefix stem, because the model inflects verbs and should.

    The objective reads "Locate a grid square"; good advice reads "locating a
    grid square". Matching those is the whole job — an exact-substring check
    rejects the correct output and accepts nothing useful.
    """
    return word[:5] if len(word) >= 5 else word


def _grounded(instruction: str, allowed: list[str]) -> bool:
    """Is the instruction actually about one of the objectives it was given?

    A teacher acting on an invented sub-objective is the one genuinely harmful
    output this can produce. There is no way to prove a negative here — the
    check that IS available is whether the advice overlaps an objective it was
    handed, which catches the real failure (the model writing about something
    else entirely) while tolerating grammar.

    A band with no weak objectives is supposed to get general advice, so an
    empty list passes by definition.
    """
    if not allowed:
        return True

    said = {_stem(w) for w in _content_words(instruction)}
    if not said:
        return False

    for obj in allowed:
        words = {_stem(w) for w in _content_words(obj)}
        if not words:
            continue
        if len(words & said) / len(words) >= _COVERAGE:
            return True
    return False


def generate(lesson, groups, *, timeout: float = 12.0) -> dict[str, str]:
    """Return ``{band_code: instruction}``, or ``{}`` to use the templates.

    Never raises. Every failure path — no model, no key, a timeout, a bad
    shape — returns an empty dict and the caller keeps what it had.
    """
    groups = [g for g in groups if g.get('code')]
    if not groups:
        return {}

    key = _fingerprint(lesson, groups)
    try:
        cached = cache.get(key)
        if cached is not None:
            return cached
    except Exception:                                # noqa: BLE001
        pass

    try:
        result = _call_model(lesson, groups, timeout=timeout)
    except Exception as exc:                         # noqa: BLE001
        logger.warning("[ReportInstructions] generation failed: %s", exc)
        return {}

    if not result:
        return {}

    try:
        cache.set(key, result, CACHE_TTL_SECONDS)
    except Exception:                                # noqa: BLE001
        pass
    return result


def _call_model(lesson, groups, *, timeout: float) -> dict[str, str]:
    from ai_tutor.apps.llm.models import ModelConfig

    # Whatever the platform is already tutoring with.
    #
    # A dedicated purpose meant a seed command on every deploy, and without
    # that row get_for falls back to `filter(is_active=True).first()` — any
    # active config at all, which could be the image-generation model or a
    # judge. Pinning to tutoring removes the setup step and makes the choice
    # explicit rather than incidental.
    #
    # It also follows the deployment: the offline build tutors with a local
    # model and writes these with the same one, which is correct there — a
    # report that needs the cloud on a device that has no internet is not a
    # report.
    config = ModelConfig.get_for(ModelConfig.Purpose.TUTORING)
    if config is None:
        logger.info("[ReportInstructions] no tutoring model — using templates")
        return {}

    from ai_tutor.apps.curriculum.content_judges._providers import (
        _get_instructor_client_for,
    )
    client = _get_instructor_client_for(config)
    if client is None:
        return {}

    system = (
        "You write short instructions for secondary-school teachers, from the "
        "results of one lesson's exit ticket. The teacher has thirty seconds "
        "and one class in front of them. Say what to re-teach and how."
    )

    response = client.chat.completions.create(
        model=config.model_name,
        response_model=ReportInstructions,
        max_tokens=900,
        # No temperature. Opus 4.7 rejects the parameter outright — "
        # `temperature` is deprecated for this model" — and since this now
        # follows whatever the platform tutors with, the set of models it has
        # to satisfy is not fixed. The provider default is right for two
        # sentences of description anyway.
        timeout=timeout,
        messages=[
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': _build_prompt(lesson, groups)},
        ],
    )

    allowed_by_code = {g['code']: _weak_for(g) for g in groups}
    out: dict[str, str] = {}
    for band in (response.bands or []):
        code = (band.code or '').strip().upper()
        text = ' '.join((band.instruction or '').split())
        if code not in allowed_by_code or not text:
            continue
        if not _grounded(text, allowed_by_code[code]):
            logger.warning(
                "[ReportInstructions] dropped ungrounded instruction for %s", code)
            continue
        out[code] = text
    return out
