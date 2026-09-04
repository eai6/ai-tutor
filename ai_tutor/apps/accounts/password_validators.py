"""Password validators beyond the four Django ships.

Django's set covers length, the common-password list, all-numeric passwords
and similarity to the user's own name — but says nothing about the mix of
characters. This adds the rule the sign-up form now states out loud, so the
checklist beside the field and the server that accepts the form are testing
the same thing. A green tick that the server then refuses is worse than no
tick at all.

The character classes are deliberately ASCII:

    letter  [A-Za-z]
    number  [0-9]
    symbol  anything else, space included

They are duplicated character-for-character in static/js/password-field.js,
which is the only way the checklist can promise what this enforces. A wider,
Unicode-aware definition would have to be expressed twice in two regex
dialects that disagree at the edges — `\\w` is Unicode-aware in Python and
ASCII-only in JavaScript — and a student whose password ticks green here and
red there is exactly the failure this file exists to prevent. The cost is
that "é" counts as a symbol rather than a letter; a password still needs one
plain letter alongside it.
"""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

LETTER = re.compile(r'[A-Za-z]')
NUMBER = re.compile(r'[0-9]')
SYMBOL = re.compile(r'[^A-Za-z0-9]')


class CharacterVarietyValidator:
    """Require a letter, a number and a symbol.

    Each class is separately switchable so a deployment can relax one from
    settings without a code change — a primary-school pilot on shared tablets
    is the case we expect to want it.
    """

    def __init__(self, require_letter=True, require_number=True, require_symbol=True):
        self.require_letter = require_letter
        self.require_number = require_number
        self.require_symbol = require_symbol

    def _missing(self, password):
        missing = []
        if self.require_letter and not LETTER.search(password):
            missing.append(_('a letter'))
        if self.require_number and not NUMBER.search(password):
            missing.append(_('a number'))
        if self.require_symbol and not SYMBOL.search(password):
            missing.append(_('a symbol, such as ! ? # or @'))
        return missing

    def validate(self, password, user=None):
        missing = self._missing(password or '')
        if not missing:
            return
        # One message listing everything that is missing, rather than one
        # error per class: a form that reports a single fault at a time makes
        # someone resubmit three times to learn all three rules.
        raise ValidationError(
            _('Your password is missing %(missing)s.'),
            code='password_missing_character_classes',
            params={'missing': ', '.join(missing)},
        )

    def get_help_text(self):
        wanted = []
        if self.require_letter:
            wanted.append(_('a letter'))
        if self.require_number:
            wanted.append(_('a number'))
        if self.require_symbol:
            wanted.append(_('a symbol'))
        return _('Your password must contain %(wanted)s.') % {'wanted': ', '.join(wanted)}
