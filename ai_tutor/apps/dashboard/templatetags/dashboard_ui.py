"""Presentation-only template tags for the teacher dashboard.

These render UI primitives — icon, badge, metric tile, progress bar, empty
state, attention item. They hold no business logic and touch no models: every
one takes plain values and returns markup. Views keep computing; templates
keep composing.

Each tag renders a partial under ``templates/dashboard/_components/`` rather
than building HTML strings in Python, so the markup stays where a designer
can find it and the tag is just the calling convention. The icon tag is the
exception: its partial and sprite live in ``templates/_includes/`` because the
student app and the public pages use them too.

Variants (``tone``, ``variant``, ``size``) are validated against an allow-list
here rather than interpolated blindly. A typo then falls back to the neutral
skin instead of emitting a class that silently matches no rule.
"""

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


# Status vocabulary, shared by badge / tile / attention item / alert. The
# dashboard has exactly these five roles; anything else is a bug in the caller.
TONES = ('neutral', 'success', 'warning', 'danger', 'info', 'accent')

ICON_SIZES = ('sm', 'md', 'lg', 'xl')


def _tone(value):
    """Normalise a caller-supplied tone to a known one."""
    value = (value or 'neutral').strip().lower()
    return value if value in TONES else 'neutral'


@register.inclusion_tag('_includes/icon.html')
def icon(name, size='', css_class='', label=''):
    """Render a sprite icon.

        {% icon "flag" %}                       decorative
        {% icon "flag" label="Flagged" %}       meaningful — gets a title
        {% icon "flag" size="lg" %}

    Decorative by default: an icon that sits next to its own label is noise
    to a screen reader, so it is aria-hidden unless *label* is given.
    """
    size = size if size in ICON_SIZES else ''
    return {
        'name': name,
        'size_class': f'icon--{size}' if size else '',
        'css_class': css_class,
        'label': label,
    }


@register.inclusion_tag('dashboard/_components/badge.html')
def badge(label, tone='neutral', icon_name='', title=''):
    """A status pill.

        {% badge "Unreviewed" tone="danger" %}
        {% badge stu.grade tone="info" %}
    """
    return {
        'label': label,
        'tone': _tone(tone),
        'icon_name': icon_name,
        'title': title,
    }


@register.inclusion_tag('dashboard/_components/stat_tile.html')
def stat_tile(label, value, note='', tone='neutral', note_tone='', hint=''):
    """One metric: eyebrow label, display figure, one line of context.

        {% stat_tile _("Sessions started") total_sessions note=reach_note %}

    *hint* renders as a disclosure under the note — used for the "how is this
    computed" text that used to hide in a title attribute.
    """
    return {
        'label': label,
        'value': value,
        'note': note,
        'tone': _tone(tone),
        'note_tone': _tone(note_tone) if note_tone else '',
        'hint': hint,
    }


@register.inclusion_tag('dashboard/_components/progress.html')
def progress(value, tone='accent', label='', show_value=False, count=None, total=None):
    """A progress track. *value* is a percentage 0-100.

    Renders role="progressbar" with the aria-value* triple, so the figure is
    available to a screen reader even when the only visual is a coloured bar.

    Pass *count* and *total* to print "3/8" beside the bar instead of the
    percentage — a fraction is the more useful number when the denominator
    is small, and it saves the template building the string by hand.
    """
    try:
        pct = max(0, min(100, round(float(value or 0))))
    except (TypeError, ValueError):
        pct = 0
    return {
        'pct': pct,
        'tone': _tone(tone),
        'label': label,
        'show_value': show_value or count is not None,
        'count': count,
        'total': total,
    }


@register.inclusion_tag('dashboard/_components/empty_state.html')
def empty_state(title, body='', icon_name='empty-box', action_url='', action_label=''):
    """An empty region. Never just "No data" — say what would put something
    here, and offer the action that does it when there is one."""
    return {
        'title': title,
        'body': body,
        'icon_name': icon_name,
        'action_url': action_url,
        'action_label': action_label,
    }


@register.inclusion_tag('dashboard/_components/attention_item.html')
def attention_item(item):
    """One row of the triage rail. Takes a dict built by
    ``ai_tutor.apps.dashboard.attention.build_attention_items``."""
    return {'item': item}


@register.simple_tag
def tone_class(prefix, tone):
    """Build a validated modifier class: {% tone_class "badge" row.tone %}."""
    return mark_safe(f'{prefix}--{_tone(tone)}')


@register.filter
def pct_of(value, total):
    """Percentage of *total*, rounded, for progress widths.

    Distinct from the existing ``percentage`` filter in dashboard_extras,
    which floors and raises on a None total.
    """
    try:
        total = float(total)
        if total <= 0:
            return 0
        return round((float(value) / total) * 100)
    except (TypeError, ValueError):
        return 0


@register.filter
def language_label(code):
    """Language-only name for a locale code, for the public switcher.

    Falls back to ``settings.LANGUAGES`` (country-forward) and then to the raw
    code, so a locale added to LANGUAGES without a short label still renders
    something a person can read rather than disappearing.
    """
    from django.conf import settings

    short = getattr(settings, 'LANGUAGE_SHORT_LABELS', {})
    if code in short:
        return short[code]
    return dict(settings.LANGUAGES).get(code, code)
