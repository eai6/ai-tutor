"""The eight-dimension pedagogical evaluation taxonomy.

Verbatim from Maurya, Srivatsa, Petukhova and Kochmar, *Unifying AI Tutor
Evaluation: An Evaluation Taxonomy for Pedagogical Ability Assessment of
LLM-Powered AI Tutors*, NAACL 2025 (arXiv:2412.09416), Table 2.

Single source of truth. The model field choices, the annotation form, the
scoring rules and any future judge prompt all read from here, so the taxonomy
cannot drift between what an annotator is asked and what the scorer counts.
Same role ``labels.py`` plays for the turn-level rubric.

TWO DELIBERATE DEPARTURES from the paper, both recorded so a reader of the
results knows what they are looking at:

1. **Unit of analysis.** The paper annotates a SINGLE tutor response, shown
   with the dialogue history up to the student's mistake. We annotate the WHOLE
   session. Several dimensions are only meaningful across turns — a tutor can
   be locally coherent on every turn while contradicting itself across the
   session — and a session is also the unit a teacher cares about. Definitions
   below are the paper's; the session-scope reading is in ``SESSION_GUIDANCE``.

2. **N/A is available on every dimension.** The paper's scale has no "not
   applicable", because a response is only sampled once a student has erred.
   Over a whole session any dimension can genuinely fail to arise: no mistake
   was ever made, so there was nothing to identify or locate; the student
   needed no guidance; nothing was left unsolved to reveal.

   It was first offered on the two mistake dimensions only, on the reasoning
   that coherence and tone apply to any session and an opt-out would let an
   annotator dodge the dimension this whole design exists to measure. That was
   the wrong trade. Withholding N/A does not make an annotator judge a
   dimension that did not arise — it makes them record something false, and a
   false "Yes" INFLATES the pass rate. N/A is excluded from scoring, so it
   costs a smaller denominator and nothing else. A wrong number is worse than a
   missing one.

   Excluded, not counted as failure — the same correction already made for the
   rubric scorer in ``evals/scorers/llm_rubric.py``. Counting it as failure
   would penalise the tutor for the student's competence.

Everything else follows the paper: the three-way scale, the per-dimension value
sets, and the desiderata.
"""
from __future__ import annotations

from typing import NamedTuple

# The three-way scale used by six of the eight dimensions.
YES = 'yes'
TO_SOME_EXTENT = 'to_some_extent'
NO = 'no'

# Revealing of the answer splits "yes" by whether what was revealed was right.
YES_CORRECT = 'yes_correct'
YES_INCORRECT = 'yes_incorrect'

# Tutor tone has its own three values.
ENCOURAGING = 'encouraging'
NEUTRAL = 'neutral'
OFFENSIVE = 'offensive'

# Not in the paper. See departure (2) above.
NOT_APPLICABLE = 'n/a'


class Dimension(NamedTuple):
    key: str
    label: str
    definition: str          # the paper's wording, verbatim
    session_guidance: str    # how to read it across a whole session
    values: tuple            # (value, human label) pairs, in the paper's order
    desideratum: str         # the single value that passes
    allows_na: bool


_THREE_WAY = (
    (YES, 'Yes'),
    (TO_SOME_EXTENT, 'To some extent'),
    (NO, 'No'),
)


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        key='mistake_identification',
        label='Mistake identification',
        definition="Has the tutor identified/recognized a mistake in a student's response?",
        session_guidance=(
            'Across the whole session: whenever the student made a mistake, did '
            'the tutor recognise it rather than affirm a wrong answer? One missed '
            'mistake in an otherwise good session is "To some extent".'
        ),
        values=_THREE_WAY,
        desideratum=YES,
        allows_na=True,
    ),
    Dimension(
        key='mistake_location',
        label='Mistake location',
        definition="Does the tutor's response accurately point to a genuine mistake and its location?",
        session_guidance=(
            'Did the tutor point at the specific step, misconception or slip — '
            'not a generic "not quite"? Judge only the mistakes it did identify; '
            'inventing a mistake the student did not make is "No".'
        ),
        values=_THREE_WAY,
        desideratum=YES,
        allows_na=True,
    ),
    Dimension(
        key='revealing_answer',
        label='Revealing of the answer',
        definition='Does the tutor reveal the final answer (whether correct or not)?',
        session_guidance=(
            'Did the tutor hand over the answer to any question the student had '
            'not yet solved? Affirming an answer the student has just got right '
            'is NOT revealing. Hints and partial scaffolding are not revealing.'
        ),
        values=(
            (YES_CORRECT, 'Yes — revealed the correct answer'),
            (YES_INCORRECT, 'Yes — revealed an incorrect answer'),
            (NO, 'No'),
        ),
        # The one dimension where the desired value is "No".
        desideratum=NO,
        allows_na=True,
    ),
    Dimension(
        key='providing_guidance',
        label='Providing guidance',
        definition=(
            'Does the tutor offer correct and relevant guidance, such as an '
            'explanation, elaboration, hint, examples, and so on?'
        ),
        session_guidance=(
            'Was the guidance correct AND relevant, and pitched at this '
            "student's level? Guidance that is correct but unusable — a wall of "
            'text, or a hint above the student\'s level — is "To some extent".'
        ),
        values=_THREE_WAY,
        desideratum=YES,
        allows_na=True,
    ),
    Dimension(
        key='actionability',
        label='Actionability',
        definition="Is it clear from the tutor's feedback what the student should do next?",
        session_guidance=(
            'Did the tutor consistently hand the floor back with something '
            'concrete to do? A session that repeatedly trails off ("take your '
            'time", "let me know") fails even if each explanation was sound.'
        ),
        values=_THREE_WAY,
        desideratum=YES,
        allows_na=True,
    ),
    Dimension(
        key='coherence',
        label='Coherence',
        definition="Is the tutor's response logically consistent with the student's previous responses?",
        session_guidance=(
            'THE dimension that needs a whole session. Did the tutor contradict '
            'its own earlier turns, re-teach something already settled, assume '
            'facts not yet established, or ignore what the student just said?'
        ),
        values=_THREE_WAY,
        desideratum=YES,
        allows_na=True,
    ),
    Dimension(
        key='tutor_tone',
        label='Tutor tone',
        definition="Is the tutor's response encouraging, neutral, or offensive?",
        session_guidance=(
            'The dominant tone across the session. Note the desideratum is '
            'ENCOURAGING — a merely neutral tone does not pass. This is stricter '
            'than our older binary judge, which accepted neutral.'
        ),
        values=(
            (ENCOURAGING, 'Encouraging'),
            (NEUTRAL, 'Neutral'),
            (OFFENSIVE, 'Offensive'),
        ),
        desideratum=ENCOURAGING,
        allows_na=True,
    ),
    Dimension(
        key='human_likeness',
        label='Human-likeness',
        definition="Does the tutor's response sound natural rather than robotic or artificial?",
        session_guidance=(
            'Did it read as a person teaching? Templated openers repeated every '
            'turn ("Great question!", "Let me think about this carefully") are '
            'the usual failure.'
        ),
        values=_THREE_WAY,
        desideratum=YES,
        allows_na=True,
    ),
)

DIMENSION_KEYS: tuple[str, ...] = tuple(d.key for d in DIMENSIONS)

_BY_KEY = {d.key: d for d in DIMENSIONS}


def get_dimension(key: str) -> Dimension:
    return _BY_KEY[key]


def choices_for(key: str) -> list[tuple[str, str]]:
    """Django field choices for one dimension, including N/A where allowed."""
    dim = _BY_KEY[key]
    out = list(dim.values)
    if dim.allows_na:
        out.append((NOT_APPLICABLE, 'Not applicable — never arose'))
    return out


def dimension_passes(key: str, value: str) -> bool | None:
    """True / False / None, where None means 'excluded from scoring'.

    None is returned for N/A and for an unset value. Callers must treat it as
    "does not count", not as a failure — see departure (2) in the module
    docstring.
    """
    if not value or value == NOT_APPLICABLE:
        return None
    return value == _BY_KEY[key].desideratum


def session_passes(values: dict) -> bool:
    """A session passes iff EVERY applicable dimension is at its desideratum.

    All-or-nothing, per the study design: one dimension at "To some extent" is
    a fail. That is deliberately demanding — the interesting statistic is the
    per-dimension pass rate, which ``session_scoring`` reports alongside this.

    A session with no scorable dimensions at all returns False rather than a
    vacuous True: nothing was assessed, so nothing was demonstrated.
    """
    verdicts = [dimension_passes(k, values.get(k) or '') for k in DIMENSION_KEYS]
    scorable = [v for v in verdicts if v is not None]
    return bool(scorable) and all(scorable)
