"""The Country Adoption Playbook, as a navigable documentation site.

The prose lives in templates/docs/sections/, generated from the Word original
by scripts/build_playbook_docs.py. What lives here is everything the .docx
does not carry: the reading order, the one-line summaries, and the grouping
the index page presents.

The grouping is the editorial work. The document has 23 sections in two parts,
and a flat list of 23 links is a table of contents, not a way in. The six
cards are the six questions a country actually arrives with — "what is this",
"what shape do we run it in", "what will it cost us", and their technical
counterparts — with the document's own numbering kept visible so a reader can
tell they are reading section 6 of 12 rather than an isolated article.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ._index import HEADINGS, WORDS

# Front matter from the document control table. Shown on the index page so a
# reader knows what vintage of a moving argument they are reading.
VERSION = '1.0'
VERSION_DATE = '31 August 2026'
STATUS = 'For discussion — not a commercial offer or a quotation'
EVIDENCE_BASE = (
    'Seychelles national pilot (live, secondary geography and mathematics); '
    'a second-locale adaptation in preparation; platform source and '
    'evaluation harnesses as at August 2026.'
)
COSTS_NOTE = (
    'Observed pilot figures, given as planning inputs. They are not prices '
    'and must be re-derived for each country.'
)


@dataclass(frozen=True)
class Section:
    """One page. ``label`` is the document's own numbering, not an index."""

    slug: str
    label: str
    title: str
    summary: str
    part: str

    @property
    def headings(self) -> list[tuple[str, str]]:
        return HEADINGS.get(self.slug, [])

    @property
    def words(self) -> str:
        return WORDS.get(self.slug, '')

    @property
    def search_terms(self) -> str:
        """Everything the index page's filter should match on, lowercased.

        Rendered onto each link as a data attribute rather than shipped as a
        JSON blob: the subheadings are not otherwise on the page, and an
        inline <script> of any type is the thing the content security policy
        exists to refuse.
        """
        parts = [self.label, self.title, self.summary] + [h[1] for h in self.headings]
        return ' '.join(parts).lower()


PART_ONE = 'The decision'
PART_TWO = 'The implementation'
APPENDICES = 'Appendices'

# The document's own division. A reader who lands on section F from a search
# needs to know it is a technical appendix before they read a word of it.
PART_NUMERALS = {
    PART_ONE: 'Part I',
    PART_TWO: 'Part II',
    # The appendices name themselves; "Appendix · Appendices" says it twice.
    APPENDICES: '',
}

PART_BLURBS = {
    PART_ONE: 'For ministry officials, programme leads and advisors. '
              'No technical knowledge assumed.',
    PART_TWO: 'For the technical team that will stand up and run a '
              'national instance.',
    APPENDICES: 'Reference material for both audiences.',
}

# Reading order — the document's, unchanged. The index page reorders nothing;
# it only offers several ways in.
SECTIONS: list[Section] = [
    Section('executive-summary', '1', 'Executive summary',
            'What adoption involves, what a country adapts, and how long a '
            'first cohort takes.', PART_ONE),
    Section('what-is-adopted', '2', 'What is actually being adopted',
            'The product as a student, a teacher and a ministry each meet it '
            '— and what it is not.', PART_ONE),
    Section('five-layers', '3', 'The five layers of adaptation',
            'Language, curriculum, local context, national structures and '
            'infrastructure — five projects, not one.', PART_ONE),
    Section('adoption-models', '4', 'Four models of adoption',
            'Managed, country cloud, single server and offline: who operates '
            'it and where the data lives.', PART_ONE),
    Section('data-sovereignty', '5', 'Data sovereignty: the honest account',
            'What stays in country, what leaves and why no setting changes '
            'it, and the three ways to close the gap.', PART_ONE),
    Section('costs', '6', 'What it costs, and what drives the cost',
            'The fixed infrastructure line, the model spend that dominates '
            'it, and the levers worth pulling.', PART_ONE),
    Section('cost-sharing', '7', 'Cost sharing and sustainability',
            'Who pays for which line, four funding models, and whether it is '
            'still funded in year three.', PART_ONE),
    Section('evidence', '8', 'Evidence, evaluation and quality assurance',
            'Three layers of answer to "how do you know it teaches well", '
            'and the gate to apply before a cohort.', PART_ONE),
    Section('roadmap', '9', 'A phased adoption roadmap',
            'Six phases from decision to scale, each with an owner and an '
            'exit criterion.', PART_ONE),
    Section('teacher-training', '10', 'Teacher training and support',
            'The part that cannot be bought or deployed: champions, '
            'accompaniment, clinics and three lines of support.', PART_ONE),
    Section('governance', '11', 'Governance and what the country must provide',
            'The five roles that need a name against them, and what each '
            'side supplies.', PART_ONE),
    Section('risks', '12', 'Risks and how they are managed',
            'Ten risks, why each one happens, and the control already in '
            'place for it.', PART_ONE),

    Section('onboarding-checklist', 'A', 'Country onboarding checklist',
            'Nine steps in order, and where each one lives. Content is step '
            'five for a reason.', PART_TWO),
    Section('language-locale', 'B', 'Language and locale adaptation',
            'The five places a locale is registered, which language wins per '
            'request, and the register decisions to settle first.', PART_TWO),
    Section('curriculum-pipeline', 'C', 'Curriculum ingestion and the content pipeline',
            'Two routes in, the eight generation steps, and why review '
            'throughput is the schedule.', PART_TWO),
    Section('national-structures', 'D', 'National structures',
            'Grades, schools, terminology and competency thresholds — all '
            'configuration, no migration.', PART_TWO),
    Section('hosting', 'E', 'Hosting the platform',
            'Three server paths and two offline shapes, with the commands, '
            'the symptoms and the backup rules.', PART_TWO),
    Section('quality-gates', 'F', 'Quality gates before a cohort',
            'Three harnesses at three units of analysis, and why judges are '
            'a filter rather than a gate.', PART_TWO),
    Section('operations', 'G', 'Operating the platform',
            'Backups, upgrades, secrets, monitoring, flagged sessions and '
            'the login lockout rule.', PART_TWO),
    Section('integration', 'H', 'Integration surface',
            'A versioned REST API, a published schema, and a realistic '
            'integration scope for a pilot.', PART_TWO),

    Section('case-study', 'A1', 'Case study: what a second adaptation changed',
            'The real change list from taking the platform to a second '
            'country. No schema redesign appears in it.', APPENDICES),
    Section('before-phase-1', 'A2', 'Questions to answer before Phase 1',
            'Ten questions. A country that can answer them can start; one '
            'that cannot will stall on the unanswered one.', APPENDICES),
    Section('glossary', 'A3', 'Glossary',
            'The eleven terms this document uses in a particular way.',
            APPENDICES),
]

BY_SLUG = {s.slug: s for s in SECTIONS}


@dataclass(frozen=True)
class Card:
    """One tile on the index page: a question, and the sections that answer it."""

    title: str
    lede: str
    part: str
    slugs: list[str]
    more_slug: str = ''
    more_label: str = ''
    more_url_name: str = ''
    sections: list[Section] = field(default_factory=list, init=False)

    def __post_init__(self):
        object.__setattr__(self, 'sections', [BY_SLUG[s] for s in self.slugs])

    @property
    def more_section(self) -> Section | None:
        return BY_SLUG.get(self.more_slug) if self.more_slug else None


# Six cards, two rows of three — Part I above, Part II below. Each card's
# footer link leads out of the card rather than deeper into it: the section a
# reader of these three most often needs next.
CARDS: list[Card] = [
    Card(
        title='Start here',
        lede='What the thing is, before deciding anything about it.',
        part=PART_ONE,
        slugs=['executive-summary', 'what-is-adopted', 'five-layers', 'glossary'],
        more_slug='before-phase-1',
        more_label='Questions to answer before Phase 1',
    ),
    Card(
        title='Decide the shape',
        lede='Where it runs, where the data lives, and who owns each decision.',
        part=PART_ONE,
        slugs=['adoption-models', 'data-sovereignty', 'governance', 'risks'],
        more_slug='case-study',
        more_label='What a second adaptation changed',
    ),
    Card(
        title='Plan and fund it',
        lede='What it costs, who carries which line, and how a first cohort is sequenced.',
        part=PART_ONE,
        slugs=['costs', 'cost-sharing', 'roadmap', 'teacher-training'],
        more_slug='evidence',
        more_label='Evidence, evaluation and quality assurance',
    ),
    Card(
        title='Stand it up',
        lede='Provision the deployment, then run it without anyone needing to be called.',
        part=PART_TWO,
        slugs=['onboarding-checklist', 'hosting', 'operations'],
        more_label='Run it yourself — the short version',
        more_url_name='self_hosting',
    ),
    Card(
        title='Make it yours',
        lede='Register the locale, ingest the syllabus, configure the country.',
        part=PART_TWO,
        slugs=['language-locale', 'curriculum-pipeline', 'national-structures'],
        more_slug='five-layers',
        more_label='The five layers of adaptation',
    ),
    Card(
        title='Prove it, then connect it',
        lede='The gate to pass before students arrive, and the surface other systems talk to.',
        part=PART_TWO,
        slugs=['quality-gates', 'evidence', 'integration'],
        more_slug='risks',
        more_label='Risks and how they are managed',
    ),
]


def neighbours(section: Section) -> tuple[Section | None, Section | None]:
    """The previous and next section in reading order.

    A documentation site people arrive at through search still has to read
    front to back for the reader who wants the whole argument.
    """
    i = SECTIONS.index(section)
    return (SECTIONS[i - 1] if i else None,
            SECTIONS[i + 1] if i + 1 < len(SECTIONS) else None)
