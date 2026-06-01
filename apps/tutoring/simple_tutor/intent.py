"""Deterministic student-message intent classifier.

Added 2026-05-27 per user direction:

    "I think my understanding here is that we need to make the tutor
    more responsive to students. Not all messages from the student
    is an answer. Perhaps we should update the system to first
    identify if a students message is an answer or request or some
    inquiry for explanation."

The simple_tutor used to assume every student message was an answer
attempt whenever ``InFlightQuestion`` was populated — the slot's mere
existence implied GRADE mode. That assumption broke on three real
patterns observed in the phase-5 eval:

  - **Pushback / correction**: capable student says "actually you're
    wrong because…" — slot grader marks 'incorrect', tutor pivots
    away from the substantive correction.
  - **Clarification**: student says "wait, what does X mean?" mid-
    question — same pivot.
  - **Emotional / off-topic**: "i hate this i'm so stupid" — grader
    fails on the placeholder ref, tutor composes a wrong-answer
    response instead of engaging with the distress.

The classifier runs BEFORE the LLM call. Its output:
  - flows into the system prompt as ``<message_intent>X</message_intent>``
  - drives a hint to the LLM about which path to take (GRADE vs
    CONVERSATIONAL)

The LLM still has final say (it can call ``record_answer`` even when
the classifier said 'clarification' — useful for borderline cases).
The classifier just biases the model toward the right path.

Design: regex + heuristics. No LLM call. ~30 µs per classification.
For ambiguous cases the classifier returns ``'answer_or_other'`` and
the LLM decides — matching today's behavior on those cases.
"""
from __future__ import annotations

import re
from typing import Literal

IntentLabel = Literal[
    'answer',
    'clarification',
    'pushback',
    'off_topic',
    'non_engagement',
    'answer_or_other',
]


# Single letter A-D, optionally followed by ")" or "." or trivia.
_LETTER_ONLY = re.compile(r'^\s*[(\[]?\s*([A-Da-d])\s*[)\].:]?\s*$')

# Pure numeric answer (optionally signed / decimal / with simple unit).
_NUMERIC_ONLY = re.compile(
    r'^\s*[(\[]?'
    r'-?\d+(?:[.,]\d+)?'                          # integer or decimal
    r'\s*'
    r'(?:°|degrees?|cm|km|m|%|mm|kg|g|ml|l)?'      # optional unit
    r'\s*[)\].]?\s*$',
    re.IGNORECASE,
)

# Math-shaped answers with explicit work: "60+75+80=215", "x = 360-215 = 145"
_NUMERIC_WITH_WORK = re.compile(
    r'(?i)(=\s*-?\d|^\s*-?\d+\s*[+\-*x/×÷·]\s*-?\d)'
)

# "A) Map 1 and Map 4" — MCQ letter + the option label text.
_LETTERED_WITH_TAIL = re.compile(r'^\s*[(\[]?([A-Da-d])\s*[)\].:]\s+\S+', re.MULTILINE)

# ---------------------------------------------------------------------------
# Locale-keyed pattern tables. The numeric / single-letter primitives
# (_LETTER_ONLY, _NUMERIC_ONLY, _NUMERIC_WITH_WORK, _LETTERED_WITH_TAIL)
# above are language-agnostic — they detect shape, not vocabulary —
# so they stay shared.
#
# Adding a new locale: add a key to LANG_PATTERNS with the four lists
# (clarification, pushback, non_engagement, off_topic) and the
# classifier picks it up. Patterns should mirror the English shapes
# (start anchors, word boundaries, optional punctuation) so the
# classifier behaves consistently across locales.
# ---------------------------------------------------------------------------

_EN_CLARIFICATION = [
    re.compile(r"(?i)\bwhat\s+(does|is)\s+\w+\s+mean\b"),
    re.compile(r"(?i)\bcan\s+you\s+(explain|repeat|clarify|rephrase)\b"),
    re.compile(r"(?i)\bi\s+(don'?t|do not)\s+(understand|get\s+it|follow)\b"),
    re.compile(r"(?i)\b(what|how)\s+(do you mean|are you asking)\b"),
    re.compile(r"(?i)\bi'?m\s+confused\b"),
    re.compile(r"(?i)\b(help|hint|show\s+me)\b\s*[!?.]?\s*$"),
    re.compile(r"(?i)^\s*(hold on|wait\b|hang on|just a sec)"),
]

_EN_PUSHBACK = [
    re.compile(r"(?i)\b(but|however|actually|wait,?)\b.{0,80}\?"),
    re.compile(r"(?i)\b(that'?s|that is)\s+not\s+(right|correct|true|quite)\b"),
    re.compile(r"(?i)\b(no|nope),?\s+(it'?s|it is|you|that)\b"),
    re.compile(r"(?i)\bi\s+(think|believe)\s+(you|that|it)\s+\w+\s+(wrong|mistake|incorrect)\b"),
    re.compile(r"(?i)\b(i\s+(think|believe)\s+)?you\s+mean\b"),
    re.compile(r"(?i)\bwhat\s+if\b.{0,100}\?"),
    re.compile(r"(?i)\bwell\b,?\s+(actually|technically)\b"),
]

_EN_NON_ENGAGEMENT = [
    re.compile(r"(?i)^\s*(idk|i don'?t know|dunno|i'?m lost|no idea)\b"),
    re.compile(r"(?i)^\s*(thanks|thank you|cheers|ok|okay|sure|yes|no|yeah|yep|nope|nah)\s*[.!]?\s*$"),
    re.compile(r"(?i)^\s*(skip|next|pass)\s*[.!]?\s*$"),
    re.compile(r"(?i)^\s*(thanks|thank\s+you|cheers)\b"),
    re.compile(r"(?i)\bi\s+(hate|love)\s+(this|maths?|geography)\b"),
    re.compile(r"(?i)\bi'?m\s+(so\s+)?(stupid|dumb|terrible|bad)\b"),
    re.compile(r"(?i)\bi\s+can'?t\s+(do\s+this|figure\s+(this|it)\s+out|get\s+it)\b"),
    re.compile(r"(?i)\b(give\s+up|gave\s+up|done\s+with\s+this)\b"),
    re.compile(r"(?i)\bthis\s+is\s+(too\s+)?(hard|difficult|boring|stupid|impossible)\b"),
]

_EN_OFF_TOPIC = [
    re.compile(r"(?i)\b(football|soccer|basketball|tiktok|instagram|youtube|netflix|game|gaming)\b"),
    re.compile(r"(?i)\b(my (friend|brother|sister|mom|dad))\b"),
    re.compile(r"(?i)\bwhat\s+time\s+(is|does)\b"),
]

# Mozambique Portuguese (pt-mz). Mirrors the English shapes but with
# Portuguese vocabulary. The (?i) flag is intentional — these run on
# user-typed messages, which may not honour capitalisation. Brand
# names + sports terms in off-topic share the English set because
# they're loanwords (futebol is the only language-specific add).
_PT_MZ_CLARIFICATION = [
    re.compile(r"(?i)\bo\s+que\s+(significa|quer\s+dizer)\b"),
    re.compile(r"(?i)\bpodes\s+(explicar|repetir|esclarecer|reformular)\b"),
    re.compile(r"(?i)\b(n[ãa]o\s+(percebi|entendi|compreendi|sei\s+o\s+que))\b"),
    re.compile(r"(?i)\b(n[ãa]o|nao)\s+(percebo|entendo|compreendo)\b"),
    re.compile(r"(?i)\b(como|o\s+que)\s+([ée]\s+que\s+)?(queres|quer|est[ãa]s\s+a)\b"),
    re.compile(r"(?i)\b(estou|to[uo]?)\s+confus[oa]\b"),
    re.compile(r"(?i)\b(ajuda|dica|mostra(?:-me)?)\b\s*[!?.]?\s*$"),
    re.compile(r"(?i)^\s*(espera|aguarda|um\s+momento)\b"),
]

_PT_MZ_PUSHBACK = [
    re.compile(r"(?i)\b(mas|por[ée]m|na verdade|espera,?)\b.{0,80}\?"),
    re.compile(r"(?i)\b(isso|isto)\s+n[ãa]o\s+(est[áa]|[ée])\s+(certo|correto|verdade)\b"),
    re.compile(r"(?i)\bn[ãa]o,?\s+(tu|voc[ê]|isso|isto)\b"),
    re.compile(r"(?i)\b(acho|creio|penso)\s+que\s+(tu\s+)?(queres|quer)\s+dizer\b"),
    re.compile(r"(?i)\b(e\s+se|mas\s+e\s+se)\b.{0,100}\?"),
    re.compile(r"(?i)\bbem,?\s+(na verdade|tecnicamente)\b"),
    re.compile(r"(?i)\b(acho|creio|penso)\s+que\s+(est[áa]s|estais)\s+errad[oa]\b"),
]

_PT_MZ_NON_ENGAGEMENT = [
    re.compile(r"(?i)^\s*(n[ãa]o\s+sei|nsei|n[ãa]o\s+fa[çc]o\s+ideia|sem\s+ideia)\b"),
    re.compile(r"(?i)^\s*(obrigad[oa]|valeu|ok|okay|sim|n[ãa]o|claro|certo|tá|ta)\s*[.!]?\s*$"),
    re.compile(r"(?i)^\s*(saltar|pr[óo]xim[oa]|passar|deixa)\s*[.!]?\s*$"),
    re.compile(r"(?i)^\s*(obrigad[oa]|muito\s+obrigad[oa]|valeu)\b"),
    re.compile(r"(?i)\b(odeio|adoro|detesto)\s+(isto|isso|matem[áa]tica|geografia|ci[êe]ncia|biologia)\b"),
    re.compile(r"(?i)\b(sou|estou)\s+(t[ãa]o\s+)?(burro[a]?|est[úu]pido[a]?|mau|m[áa])\b"),
    re.compile(r"(?i)\bn[ãa]o\s+(consigo|sou\s+capaz)\b"),
    re.compile(r"(?i)\b(desisto|vou\s+desistir|j[áa]\s+chega)\b"),
    re.compile(r"(?i)\b(isto|isso)\s+[ée]\s+(muito\s+)?(dif[íi]cil|chato|aborrecido|imposs[íi]vel)\b"),
]

# Off-topic stays close to English — brand names + sports translate
# directly (loanwords on the platform side) and the relationship terms
# are localised.
_PT_MZ_OFF_TOPIC = [
    re.compile(r"(?i)\b(futebol|basquete|basquetebol|tiktok|instagram|youtube|netflix|jogo|jogar)\b"),
    re.compile(r"(?i)\b(meu|minha)\s+(amigo[a]?|irm[ãa]o|m[ãa]e|pai)\b"),
    re.compile(r"(?i)\bque\s+horas\s+(s[ãa]o|[ée])\b"),
]


LANG_PATTERNS: dict[str, dict[str, list[re.Pattern]]] = {
    'en-us': {
        'clarification': _EN_CLARIFICATION,
        'pushback': _EN_PUSHBACK,
        'non_engagement': _EN_NON_ENGAGEMENT,
        'off_topic': _EN_OFF_TOPIC,
    },
    'pt-mz': {
        'clarification': _PT_MZ_CLARIFICATION,
        'pushback': _PT_MZ_PUSHBACK,
        'non_engagement': _PT_MZ_NON_ENGAGEMENT,
        'off_topic': _PT_MZ_OFF_TOPIC,
    },
}


def _patterns_for(locale: str) -> dict[str, list[re.Pattern]]:
    """Return the pattern bundle for a locale, with English fallback
    for unknown codes. We log the fallback so a misconfigured course
    surfaces in the engine log (`[simple_tutor] intent=...`) rather
    than silently degrading.
    """
    code = (locale or 'en-us').lower()
    if code in LANG_PATTERNS:
        return LANG_PATTERNS[code]
    return LANG_PATTERNS['en-us']


def classify_student_message(
    text: str,
    *,
    has_inflight_question: bool,
    locale: str = 'en-us',
) -> IntentLabel:
    """Classify the student's last message.

    Order of checks matters — most-specific patterns first. A clean
    numeric/letter answer beats a clarification hit; a clarification
    beats a pushback (the "wait, what if…?" case); pushback beats
    non-engagement.

    Args:
        text: the student's message text.
        has_inflight_question: True when an InFlightQuestion row
            exists for the session. When False, the classifier
            collapses 'answer' to 'answer_or_other' since there's
            nothing to answer.
        locale: the course's locale (e.g. 'en-us', 'pt-mz'). Selects
            the vocabulary pattern bundle. Unknown codes fall back to
            English. Letter / numeric shape detection is locale-
            agnostic.

    Returns:
        IntentLabel — one of 'answer', 'clarification', 'pushback',
        'off_topic', 'non_engagement', 'answer_or_other'.
    """
    s = (text or '').strip()
    if not s:
        return 'non_engagement'

    patterns = _patterns_for(locale)

    # 1. Clear answer shape — single letter / pure number / numeric+work.
    if has_inflight_question:
        if _LETTER_ONLY.match(s) or _LETTERED_WITH_TAIL.match(s):
            return 'answer'
        if _NUMERIC_ONLY.match(s) or _NUMERIC_WITH_WORK.search(s):
            return 'answer'
        # Short non-question textual fragment without pushback / clarification
        # markers — likely a short_answer attempt.
        if len(s) < 80 and not s.rstrip().endswith('?'):
            # Drop down to the more specific checks before deciding.
            pass

    # 2. Clarification — explicit ask for explanation.
    for p in patterns['clarification']:
        if p.search(s):
            return 'clarification'

    # 3. Pushback — explicit correction or counter-question. Order
    # matters: "what if all six are equal..." is pushback even though
    # it ends with "?" (would otherwise trip the "?" check).
    for p in patterns['pushback']:
        if p.search(s):
            return 'pushback'

    # 4. Off-topic.
    for p in patterns['off_topic']:
        if p.search(s):
            return 'off_topic'

    # 5. Non-engagement / emotional / short filler.
    for p in patterns['non_engagement']:
        if p.search(s):
            return 'non_engagement'

    # 6. Long student message ending with "?" — clarification by default
    # unless we have an in-flight question and it looks short-answer-ish.
    if s.rstrip().endswith('?'):
        if has_inflight_question and len(s) < 80:
            return 'answer_or_other'
        return 'clarification'

    # 7. Fallback — defer to the LLM.
    return 'answer_or_other'
