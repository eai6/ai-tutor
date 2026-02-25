"""
Theme context processor — injects institution branding into every template.

For staff users, piggybacks on `request.staff_ctx` (zero extra queries).
For students, does one Membership lookup.
Anonymous users get an empty dict (all templates use |default filters).
"""

from apps.accounts.models import Membership


def _darken_hex(hex_color, factor=0.15):
    """Darken a hex color by a factor (0-1)."""
    hex_color = hex_color.lstrip('#')
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r = max(0, int(r * (1 - factor)))
    g = max(0, int(g * (1 - factor)))
    b = max(0, int(b * (1 - factor)))
    return f'#{r:02x}{g:02x}{b:02x}'


def _lighten_hex(hex_color, factor=0.85):
    """Lighten a hex color towards white by a factor (0-1)."""
    hex_color = hex_color.lstrip('#')
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r = min(255, int(r + (255 - r) * factor))
    g = min(255, int(g + (255 - g) * factor))
    b = min(255, int(b + (255 - b) * factor))
    return f'#{r:02x}{g:02x}{b:02x}'


def institution_theme(request):
    """Return theme variables for the current user's institution."""
    if not hasattr(request, 'user') or not request.user.is_authenticated:
        return {}

    institution = None

    # Staff path — reuse already-loaded context (zero queries)
    if hasattr(request, 'staff_ctx') and request.staff_ctx:
        institution = request.staff_ctx.get('institution')
    else:
        # Student path — one query
        membership = Membership.objects.filter(
            user=request.user,
            is_active=True,
        ).select_related('institution').first()
        if membership:
            institution = membership.institution

    if not institution:
        return {}

    primary = institution.primary_color or '#E8590C'

    return {
        'theme_primary': primary,
        'theme_secondary': institution.secondary_color or '#4ECDC4',
        'theme_accent': institution.accent_color or '#FFE66D',
        'theme_primary_dark': _darken_hex(primary),
        'theme_primary_light': _lighten_hex(primary),
        'theme_custom_css': institution.custom_css or '',
        'theme_logo_url': institution.logo.url if institution.logo else '',
        'theme_institution_name': institution.name,
    }
