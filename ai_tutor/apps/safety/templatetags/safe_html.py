"""Template filter for the figure markup questions carry.

    {% load safe_html %}
    {{ question.answer_data.data_description|sanitized }}

Replaces ``|safe`` at every site that renders ``figure_svg``,
``data_description`` or ``source`` — see apps/safety/html_sanitizer.py for what
survives and why (assessment finding F-07).

Sanitising at render rather than at write is deliberate: rows generated before
this existed are already in the database, and a filter covers them without a
backfill. It also means a future writer cannot reintroduce the hole by
forgetting to sanitise on the way in.
"""
from django import template
from django.utils.safestring import mark_safe

from ai_tutor.apps.safety.html_sanitizer import sanitize_figure_html

register = template.Library()


@register.filter(is_safe=True)
def sanitized(value):
    """Reduce ``value`` to presentational markup, then mark it safe."""
    return mark_safe(sanitize_figure_html(value))
