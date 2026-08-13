"""
Custom template tags for dashboard.
"""
import re
from django import template
from django.utils.html import escape
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


# Inline `code` (single backtick).
_INLINE_CODE_RE = re.compile(r'`([^`\n]+)`')
# **bold**
_BOLD_RE = re.compile(r'\*\*([^*\n]+)\*\*')
# *italic* (single-star, but only when not adjacent to another *).
_ITALIC_RE = re.compile(r'(?<![\*\w])\*([^*\n]+)\*(?![\*\w])')
# Markdown horizontal rule
_HR_RE = re.compile(r'^---+\s*$')


@register.filter
def render_markdown(text):
    """Render the limited Markdown subset used by PlatformTerms / FAQ.

    Supports: paragraphs, ###/##/#-style headings, **bold**, *italic*,
    inline `code`, --- horizontal rules, and `- ` bullet lists. Output
    is HTML-escaped first then re-marked safe.

    No external dependency (the `markdown` package isn't installed).
    Good enough for short admin-edited prose; not full CommonMark.
    """
    if text is None:
        return ''
    raw = str(text)

    blocks = []
    current_para = []
    current_list = []

    def _flush_para():
        if current_para:
            joined = ' '.join(current_para)
            blocks.append(('p', joined))
            current_para.clear()

    def _flush_list():
        if current_list:
            blocks.append(('ul', list(current_list)))
            current_list.clear()

    for raw_line in raw.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            _flush_para()
            _flush_list()
            continue

        if _HR_RE.match(stripped):
            _flush_para(); _flush_list()
            blocks.append(('hr', None))
            continue

        # Headings (### / ## / #).
        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            _flush_para(); _flush_list()
            level = len(m.group(1))
            blocks.append((f'h{level}', m.group(2)))
            continue

        # Bullet list — `- item` or `* item`.
        m = re.match(r'^[-*]\s+(.*)$', stripped)
        if m:
            _flush_para()
            current_list.append(m.group(1))
            continue

        _flush_list()
        current_para.append(stripped)

    _flush_para()
    _flush_list()

    def _inline(s: str) -> str:
        # Escape first to keep output safe; then convert known patterns
        # back to tags (the chevrons we insert won't be re-escaped).
        s = escape(s)
        s = _INLINE_CODE_RE.sub(r'<code>\1</code>', s)
        s = _BOLD_RE.sub(r'<strong>\1</strong>', s)
        s = _ITALIC_RE.sub(r'<em>\1</em>', s)
        return s

    out = []
    for kind, content in blocks:
        if kind == 'hr':
            out.append('<hr>')
        elif kind == 'p':
            out.append(f'<p>{_inline(content)}</p>')
        elif kind == 'ul':
            items = ''.join(f'<li>{_inline(item)}</li>' for item in content)
            out.append(f'<ul>{items}</ul>')
        elif kind in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            out.append(f'<{kind}>{_inline(content)}</{kind}>')

    return mark_safe(''.join(out))
