"""The world's countries, for the one form that has to offer all of them.

`Country` rows are the platform's own record: one exists once a country has
actually adopted the platform. The account-request form cannot be limited to
those — a ministry arrives before its country is on the platform, which is
the entire point of the form — so it offers this list instead and the row is
created from the chosen code.

WHY A LIST IN THE REPOSITORY. `pycountry` and `django-countries` both do this
better, and neither is installed; adding a dependency to render a <select>
is the wrong trade at this size. The codes are ISO 3166-1 alpha-2, which is
also what the flag emoji is derived from, so the list carries no flags of its
own — 249 pasted emoji would be 249 chances to paste the wrong one.
"""

# code:name, ISO 3166-1 alpha-2. Names are the short English forms a reader
# would pick their own country by, not the protocol forms ("Bolivia", not
# "Bolivia (Plurinational State of)").
_RAW = """
AF:Afghanistan AL:Albania DZ:Algeria AD:Andorra AO:Angola AG:Antigua and Barbuda
AR:Argentina AM:Armenia AU:Australia AT:Austria AZ:Azerbaijan BS:Bahamas
BH:Bahrain BD:Bangladesh BB:Barbados BY:Belarus BE:Belgium BZ:Belize BJ:Benin
BT:Bhutan BO:Bolivia BA:Bosnia and Herzegovina BW:Botswana BR:Brazil
BN:Brunei BG:Bulgaria BF:Burkina Faso BI:Burundi CV:Cabo Verde KH:Cambodia
CM:Cameroon CA:Canada CF:Central African Republic TD:Chad CL:Chile CN:China
CO:Colombia KM:Comoros CG:Congo CD:Congo (Democratic Republic) CR:Costa Rica
CI:Côte d'Ivoire HR:Croatia CU:Cuba CY:Cyprus CZ:Czechia DK:Denmark
DJ:Djibouti DM:Dominica DO:Dominican Republic EC:Ecuador EG:Egypt
SV:El Salvador GQ:Equatorial Guinea ER:Eritrea EE:Estonia SZ:Eswatini
ET:Ethiopia FJ:Fiji FI:Finland FR:France GA:Gabon GM:Gambia GE:Georgia
DE:Germany GH:Ghana GR:Greece GD:Grenada GT:Guatemala GN:Guinea
GW:Guinea-Bissau GY:Guyana HT:Haiti HN:Honduras HU:Hungary IS:Iceland
IN:India ID:Indonesia IR:Iran IQ:Iraq IE:Ireland IL:Israel IT:Italy
JM:Jamaica JP:Japan JO:Jordan KZ:Kazakhstan KE:Kenya KI:Kiribati
KW:Kuwait KG:Kyrgyzstan LA:Laos LV:Latvia LB:Lebanon LS:Lesotho LR:Liberia
LY:Libya LI:Liechtenstein LT:Lithuania LU:Luxembourg MG:Madagascar
MW:Malawi MY:Malaysia MV:Maldives ML:Mali MT:Malta MH:Marshall Islands
MR:Mauritania MU:Mauritius MX:Mexico FM:Micronesia MD:Moldova MC:Monaco
MN:Mongolia ME:Montenegro MA:Morocco MZ:Mozambique MM:Myanmar NA:Namibia
NR:Nauru NP:Nepal NL:Netherlands NZ:New Zealand NI:Nicaragua NE:Niger
NG:Nigeria KP:North Korea MK:North Macedonia NO:Norway OM:Oman PK:Pakistan
PW:Palau PS:Palestine PA:Panama PG:Papua New Guinea PY:Paraguay PE:Peru
PH:Philippines PL:Poland PT:Portugal QA:Qatar RO:Romania RU:Russia
RW:Rwanda KN:Saint Kitts and Nevis LC:Saint Lucia
VC:Saint Vincent and the Grenadines WS:Samoa SM:San Marino
ST:São Tomé and Príncipe SA:Saudi Arabia SN:Senegal RS:Serbia SC:Seychelles
SL:Sierra Leone SG:Singapore SK:Slovakia SI:Slovenia SB:Solomon Islands
SO:Somalia ZA:South Africa KR:South Korea SS:South Sudan ES:Spain
LK:Sri Lanka SD:Sudan SR:Suriname SE:Sweden CH:Switzerland SY:Syria
TJ:Tajikistan TZ:Tanzania TH:Thailand TL:Timor-Leste TG:Togo TO:Tonga
TT:Trinidad and Tobago TN:Tunisia TR:Türkiye TM:Turkmenistan TV:Tuvalu
UG:Uganda UA:Ukraine AE:United Arab Emirates GB:United Kingdom
US:United States UY:Uruguay UZ:Uzbekistan VU:Vanuatu VA:Vatican City
VE:Venezuela VN:Vietnam YE:Yemen ZM:Zambia ZW:Zimbabwe
"""


def _parse(raw):
    """`_RAW` is whitespace-separated `CODE:Name`, and a name may contain
    spaces — so the split is on the code, not on the whitespace."""
    out, code, words = [], None, []
    for token in raw.split():
        if len(token) > 2 and token[2] == ':' and token[:2].isupper():
            if code:
                out.append((code, ' '.join(words)))
            code, words = token[:2], [token[3:]]
        else:
            words.append(token)
    if code:
        out.append((code, ' '.join(words)))
    return out


def flag(code):
    """The regional-indicator pair a browser renders as a flag.

    'TZ' -> '\U0001F1F9\U0001F1FF'. Derived rather than stored: the emoji IS
    the code, so there is nothing to keep in step.
    """
    code = (code or '').strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ''
    return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code)


COUNTRIES = sorted(_parse(_RAW), key=lambda p: p[1])
BY_CODE = dict(COUNTRIES)


def choices():
    """(code, "🇹🇿 Tanzania") pairs, alphabetical by name.

    The flag goes in the label rather than a separate column because a
    <select> renders one string per option — the same reason the language
    picker in settings.py carries its flag inline.
    """
    return [(code, f'{flag(code)} {name}') for code, name in COUNTRIES]


def name_for(code):
    return BY_CODE.get((code or '').strip().upper())
