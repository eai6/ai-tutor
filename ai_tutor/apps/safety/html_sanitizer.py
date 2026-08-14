"""Allow-list sanitiser for the figure markup that questions carry.

Three fields on ``ExitTicketQuestion.answer_data`` hold raw markup and are
rendered with ``|safe``: ``figure_svg`` (legacy inline SVG charts),
``data_description`` (the table or chart a data-interpretation question asks
about) and ``source`` (source material shown above an MCQ).

None of it is written by us. ``content_generator.py`` asks the model for HTML in
so many words — "For DATA_INTERPRETATION questions, use these figures in
data_description as HTML" — and the model's output is shaped by whatever
textbook or worksheet a teacher uploaded. That is the whole chain: a document
with instructions embedded in it reaches the generator, the generator emits
markup containing them, and the markup is rendered into a page a student is
looking at. There is no enforced CSP to contain the result (assessment finding
F-02), and finding F-07 asked for exactly this review.

**Allow-list, never deny-list.** The set below is what a figure legitimately
needs: text, tables, images and SVG drawing primitives. Everything else goes,
including every element that can execute or navigate — ``script``,
``foreignObject`` (it switches back to the HTML namespace mid-SVG), ``use`` and
``image`` (they dereference a URL), ``a``, ``iframe``, ``object``, ``embed``,
``form``, and the SVG animation elements, which can retarget an ``href``
attribute after the fact. Event-handler attributes are absent from the
attribute list, so they are dropped without needing to be named.

Three settings do work that is easy to miss:

* ``filter_style_properties`` — ``style`` has to stay, because the generator
  emits ``style='max-width:100%'`` on nearly every figure and the templates are
  full of inline styles besides. Allowing the attribute wholesale would allow
  ``background: url(https://attacker/?c=…)``, which is not script execution but
  is a beacon. Filtering per CSS property keeps the layout declarations and
  drops anything that can name a URL. Position properties are left out too, so
  a figure cannot be styled into an overlay covering the page.

* ``url_relative='pass_through'`` — figure images are served from our own domain
  at ``/media/…``. Denying relative URLs would blank every existing figure.

* ``clean_content_tags`` — for ``script`` and ``style``, the element's *content*
  is removed as well. Dropping only the tag would leave the JavaScript source
  sitting in the page as visible text.

**Fails closed.** If nh3 is somehow unavailable the sanitiser escapes the input
rather than passing it through. A broken-looking figure is a bug report; the
other failure mode is not.
"""
from __future__ import annotations

import logging

from django.utils.html import escape

logger = logging.getLogger(__name__)

try:
    import nh3
except ImportError:  # pragma: no cover - nh3 is a pinned dependency
    nh3 = None


# ── Elements ────────────────────────────────────────────────────────────────
_TEXT_TAGS = {
    'p', 'br', 'hr', 'span', 'div', 'b', 'strong', 'i', 'em', 'u', 's',
    'sub', 'sup', 'small', 'mark', 'code', 'pre', 'blockquote',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'figure', 'figcaption',
}

_TABLE_TAGS = {
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td',
    'caption', 'colgroup', 'col',
}

# Drawing primitives only. No `use`, `image`, `a`, `foreignObject`, `script`,
# `style`, `animate`, `animateTransform`, `animateMotion`, `set`, `switch`,
# `filter` or `metadata`.
_SVG_TAGS = {
    'svg', 'g', 'defs', 'symbol', 'title', 'desc',
    'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
    'text', 'tspan', 'textPath',
    'marker', 'linearGradient', 'radialGradient', 'stop',
    'clipPath', 'mask', 'pattern',
}

ALLOWED_TAGS = _TEXT_TAGS | _TABLE_TAGS | _SVG_TAGS | {'img'}

# Removed along with whatever they contain, so the source text does not survive
# as visible content.
CONTENT_STRIPPED_TAGS = {'script', 'style', 'iframe', 'object', 'embed', 'template'}


# ── Attributes ──────────────────────────────────────────────────────────────
# Deliberately absent everywhere: id and name (DOM clobbering), href/xlink:href,
# and anything beginning with `on`.
_GENERIC_ATTRS = {'class', 'style', 'title', 'lang', 'dir', 'role'}

_SVG_PRESENTATION_ATTRS = {
    'd', 'points', 'transform', 'viewBox', 'xmlns', 'preserveAspectRatio',
    'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry',
    'dx', 'dy', 'width', 'height', 'offset',
    'fill', 'fill-opacity', 'fill-rule',
    'stroke', 'stroke-width', 'stroke-opacity', 'stroke-linecap',
    'stroke-linejoin', 'stroke-dasharray', 'stroke-dashoffset', 'stroke-miterlimit',
    'opacity', 'color', 'visibility', 'display', 'overflow',
    'font-family', 'font-size', 'font-weight', 'font-style',
    'text-anchor', 'dominant-baseline', 'alignment-baseline',
    'letter-spacing', 'word-spacing', 'text-decoration', 'writing-mode',
    'stop-color', 'stop-opacity', 'gradientUnits', 'gradientTransform',
    'spreadMethod', 'patternUnits', 'patternContentUnits', 'patternTransform',
    'clipPathUnits', 'maskUnits', 'maskContentUnits', 'clip-path', 'clip-rule',
    'mask', 'marker-start', 'marker-mid', 'marker-end',
    'markerWidth', 'markerHeight', 'refX', 'refY', 'orient', 'markerUnits',
    'vector-effect', 'shape-rendering', 'paint-order', 'pathLength',
}
# html5ever normalises SVG attribute case for the names it knows and leaves the
# rest alone, so both spellings are listed rather than guessing which arrives.
_SVG_PRESENTATION_ATTRS |= {name.lower() for name in _SVG_PRESENTATION_ATTRS}

_CELL_ATTRS = {'colspan', 'rowspan', 'scope', 'headers', 'align', 'valign', 'abbr'}

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    '*': _GENERIC_ATTRS,
    'img': {'src', 'alt', 'width', 'height', 'loading', 'decoding'},
    'td': _CELL_ATTRS,
    'th': _CELL_ATTRS,
    'col': {'span'},
    'colgroup': {'span'},
    'table': {'summary'},
    'ol': {'start', 'reversed', 'type'},
}
for _tag in _SVG_TAGS:
    ALLOWED_ATTRIBUTES[_tag] = _SVG_PRESENTATION_ATTRS

# ``background`` and ``background-image`` are absent on purpose — the shorthand
# accepts url(), which turns a figure into a tracking beacon. So are position,
# top/left/right/bottom and z-index, which would let a figure be styled into an
# overlay on top of the rest of the page.
ALLOWED_STYLE_PROPERTIES = {
    'width', 'min-width', 'max-width', 'height', 'min-height', 'max-height',
    'margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
    'padding', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
    'border', 'border-top', 'border-right', 'border-bottom', 'border-left',
    'border-color', 'border-style', 'border-width', 'border-radius',
    'border-collapse', 'border-spacing',
    'color', 'background-color',
    'font', 'font-size', 'font-weight', 'font-family', 'font-style',
    'line-height', 'letter-spacing', 'text-align', 'text-decoration',
    'text-transform', 'vertical-align', 'white-space', 'word-break',
    'display', 'float', 'clear', 'box-sizing', 'opacity', 'overflow',
    'overflow-x', 'overflow-y', 'table-layout', 'caption-side',
    'flex', 'flex-direction', 'flex-wrap', 'gap', 'align-items',
    'justify-content', 'grid-template-columns',
}

# data: for base64 thumbnails; relative for our own /media/ paths, which are
# handled by url_relative rather than by a scheme.
ALLOWED_URL_SCHEMES = {'http', 'https', 'data'}


# The three keys on ExitTicketQuestion.answer_data that hold raw markup.
MARKUP_FIELDS = ('figure_svg', 'data_description', 'source')


def sanitize_answer_data(answer_data):
    """Copy of ``answer_data`` with its markup fields sanitised.

    For the JSON that reaches the exit-ticket modal. The templates get the same
    treatment through the ``|sanitized`` filter, but that payload never passes
    through a template — it is assembled in the view and handed to JavaScript,
    which builds the question card with ``innerHTML``. Sanitising here means the
    browser is not the only thing standing between generated markup and the
    page.

    Returns a shallow copy; the original (and the row it came from) is left
    alone, so nothing is rewritten in the database as a side effect of being
    displayed.
    """
    if not isinstance(answer_data, dict):
        return {}
    cleaned = dict(answer_data)
    for field in MARKUP_FIELDS:
        if isinstance(cleaned.get(field), str):
            cleaned[field] = sanitize_figure_html(cleaned[field])
    return cleaned


def sanitize_figure_html(value) -> str:
    """Return ``value`` reduced to presentational markup.

    Non-string input returns an empty string — the field is optional and
    sometimes holds ``None`` or, in older rows, a dict.
    """
    if not value or not isinstance(value, str):
        return ''

    if nh3 is None:
        logger.error(
            'nh3 is not installed; refusing to render unsanitised figure markup. '
            'Install it (it is pinned in requirements.txt) to restore figures.'
        )
        return escape(value)

    return nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        clean_content_tags=CONTENT_STRIPPED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        filter_style_properties=ALLOWED_STYLE_PROPERTIES,
        url_schemes=ALLOWED_URL_SCHEMES,
        url_relative='pass_through',
        strip_comments=True,
    )
