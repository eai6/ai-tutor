"""
Custom template tags for dashboard.
"""
import json

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Get item from dictionary by key."""
    if dictionary is None:
        return None
    return dictionary.get(key)


@register.filter
def percentage(value, total):
    """Calculate percentage."""
    if total == 0:
        return 0
    return int((value / total) * 100)


@register.filter
def safe_json(value):
    """Dump ``value`` as JSON, HTML-escape, return as safe string.

    Use inside an attribute, e.g. ``data-x="{{ obj|safe_json }}"``. Escapes
    the unsafe trio <, >, & plus quotes so the attribute stays well-formed.
    """
    payload = json.dumps(value, default=str)
    return mark_safe(
        payload.replace('&', '&amp;')
               .replace('<', '&lt;')
               .replace('>', '&gt;')
               .replace('"', '&quot;')
               .replace("'", '&#x27;')
    )


@register.filter
def safe_json_pretty(value):
    """Like ``safe_json`` but indented. Use inside <textarea>."""
    payload = json.dumps(value, default=str, indent=2, ensure_ascii=False)
    return mark_safe(
        payload.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    )
