"""
Custom template tags for dashboard.
"""
from django import template

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
