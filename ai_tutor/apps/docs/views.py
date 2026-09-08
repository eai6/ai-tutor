"""Public documentation pages for the Enterprise Adoption Playbook.

Public by design, and for the same reason /self-hosting/ is: a ministry
evaluating the platform reads this before anyone has an account to log into,
and a document that can only be read behind a login cannot do the job of
persuading someone to ask for one.

No models, no state. The prose is a set of template partials generated from
the Word original; the reading order and grouping are in playbook.py.
"""
from __future__ import annotations

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.template import TemplateDoesNotExist, loader
from django.utils import translation
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import cache_control

from . import playbook


def _chrome() -> dict:
    return {
        'version': playbook.VERSION,
        'version_date': playbook.VERSION_DATE,
    }


def _body_template(slug: str) -> tuple[str, bool]:
    """The section partial for the active language, and whether it is one.

    The prose is generated from the .docx, so it cannot be wrapped in
    {% trans %} — the next build would overwrite the wrapping, and a 500-word
    passage is the wrong size for a msgid anyway: one English comma edit
    invalidates the whole translated paragraph. Translations are whole files
    instead, under a language directory:

        templates/docs/sections/costs.html          <- English, generated
        templates/docs/sections/pt_mz/costs.html    <- translated
        templates/docs/sections/fr/costs.html

    Falls back to English rather than 404ing or rendering blank, and reports
    which it found so the page can say so. A ministry reading a half-Portuguese
    playbook should be told that is what it is, not left to infer it.

    pt-mz is tried as both `pt_mz` and `pt`, so one Portuguese translation can
    serve every Portuguese locale without being copied per country.
    """
    english = f'docs/sections/{slug}.html'
    lang = (translation.get_language() or '').lower()
    candidates = []
    if lang:
        specific = lang.replace('-', '_')
        candidates.append(f'docs/sections/{specific}/{slug}.html')
        base = specific.split('_')[0]
        if base != specific:
            candidates.append(f'docs/sections/{base}/{slug}.html')

    for candidate in candidates:
        try:
            loader.get_template(candidate)
        except TemplateDoesNotExist:
            continue
        return candidate, True
    return english, False


# GET and HEAD: link previewers and uptime checks probe with HEAD, and
# require_GET answers those with 405. Cached for five minutes — the content
# only changes when a deploy ships a new .docx conversion.
@require_http_methods(['GET', 'HEAD'])
@cache_control(max_age=300, public=True)
def index(request):
    """The documentation front door: six ways in, plus a full contents list."""
    return render(request, 'docs/index.html', {
        **_chrome(),
        'cards': playbook.CARDS,
        'sections': playbook.SECTIONS,
        'part_one': playbook.PART_ONE,
        'part_two': playbook.PART_TWO,
        'part_one_blurb': playbook.PART_BLURBS[playbook.PART_ONE],
        'part_two_blurb': playbook.PART_BLURBS[playbook.PART_TWO],
        'part_one_label': playbook.PART_LABELS[playbook.PART_ONE],
        'part_two_label': playbook.PART_LABELS[playbook.PART_TWO],
        'status': playbook.STATUS,
        'evidence_base': playbook.EVIDENCE_BASE,
        'costs_note': playbook.COSTS_NOTE,
    })


@require_http_methods(['GET', 'HEAD'])
@cache_control(max_age=300, public=True)
def section(request, slug: str):
    """One section of the playbook."""
    entry = playbook.BY_SLUG.get(slug)
    if entry is None:
        raise Http404('unknown documentation section')

    # A section in the index with no generated partial is a build that half
    # ran. Fail loudly at the request rather than rendering an empty page that
    # looks like the section is genuinely blank.
    template, body_translated = _body_template(slug)
    try:
        loader.get_template(template)
    except TemplateDoesNotExist as exc:
        raise Http404('documentation section has no content') from exc

    previous, following = playbook.neighbours(entry)
    return render(request, 'docs/section.html', {
        **_chrome(),
        'section': entry,
        'body_template': template,
        # False only when the reader asked for a language this section has no
        # translation for; the template shows a note rather than pretending.
        'body_translated': body_translated,
        'is_english': translation.get_language() in (None, '') or
                      (translation.get_language() or '').lower().startswith('en'),
        'previous': previous,
        'following': following,
        'part_numeral': playbook.PART_NUMERALS[entry.part],
    })


# An hour, not five minutes: the index only changes when a deploy ships a new
# conversion of the .docx, and it is the largest thing this app serves.
@require_http_methods(['GET', 'HEAD'])
@cache_control(max_age=3600, public=True)
def search_index(request):
    """The word index behind the documentation search.

    Fetched by docs.js on the first keystroke rather than rendered into the
    page. Two reasons: it is 45 KB that most visitors never search, and an
    inline <script> — of any type, data included — is exactly what the content
    security policy is there to refuse.

    Until it arrives the search still works against titles, summaries and
    subheadings, which the page carries as data attributes.
    """
    return JsonResponse({s.slug: s.words for s in playbook.SECTIONS})
