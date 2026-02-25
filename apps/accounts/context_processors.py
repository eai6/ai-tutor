"""
Theme context processor — injects platform branding into every template.

Branding is platform-wide (stored in PlatformConfig), so all users see the
same theme regardless of which school they belong to.
"""

from apps.accounts.models import PlatformConfig


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


def _build_theme_dict():
    """Build theme context dict from PlatformConfig singleton."""
    config = PlatformConfig.load()
    primary = config.primary_color or '#E8590C'
    return {
        'theme_primary': primary,
        'theme_secondary': config.secondary_color or '#4ECDC4',
        'theme_accent': config.accent_color or '#FFE66D',
        'theme_primary_dark': _darken_hex(primary),
        'theme_primary_light': _lighten_hex(primary),
        'theme_logo_url': config.logo.url if config.logo else '',
        'theme_institution_name': config.platform_name or 'AI Tutor',
    }


def institution_theme(request):
    """Return platform-wide theme variables for every template."""
    return _build_theme_dict()
