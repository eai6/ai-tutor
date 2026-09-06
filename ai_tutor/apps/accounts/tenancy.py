"""What one school may see.

The single definition of the multi-tenancy rule CLAUDE.md calls critical.

Before countries existed the rule was::

    Q(institution=inst) | Q(institution__isnull=True)

and a row with no institution was visible to every school on the platform.
Countries narrowed that: a row with no institution is now visible to every
school in the SAME COUNTRY. That is what stops a national curriculum crossing
a border.

WHY A FUNCTION AND NOT A MANAGER METHOD. Fifteen models across six apps carry
an ``institution`` FK — accounts, curriculum, dashboard, llm, media_library,
tutoring. A manager method would mean surgery on every one of them, and half
of the call sites reach the FK through a relation anyway, where a manager on
the far model is no help.

WHY IT IS NOT ``filter_by_institution``. That helper lives in
``dashboard/views.py`` and has 36 callers. It answers a different question:
when institution is None it returns EVERYTHING, which is the super admin's
aggregated view. This one answers "what may this one school see". The names
are close enough to invite the mistake, and making it would hand a school
every other school's content, so they are kept apart deliberately.
"""

from django.db.models import Q


def visible_q(institution, field='institution', country_field=None):
    """The Q for what *institution* may see, for callers that build a filter.

    Most call sites pass this rule as one positional argument alongside other
    filters::

        .filter(visible_q(institution), is_active=True)

    replacing the hand-written ``Q(institution=inst) |
    Q(institution__isnull=True)``. Keeping the call shape identical is the
    point: fifty sites change by one expression each, rather than being
    restructured into something a reviewer has to re-read.

    *country_field* defaults to *field* with its last segment swapped for
    ``country`` — ``unit__course__institution`` becomes
    ``unit__course__country`` — because the two must walk the same relation.

    ``institution=None`` yields an empty Q, which matches everything: the
    caller is aggregating.
    """
    if institution is None:
        return Q()
    if country_field is None:
        country_field = field.rsplit('__', 1)[0] + '__country' if '__' in field else 'country'

    own, countries = _resolve(institution, field)
    return Q(**own) | Q(**{f'{field}__isnull': True, f'{country_field}__in': countries})


def shared_in(country, field='institution', country_field='country'):
    """The Q for rows shared across one country: no institution, that country.

    Distinct from `visible_q`, which also includes a school's own rows. The
    material-inheritance lookups ask this narrower question — "which shared
    courses could this course inherit from" — and before countries existed
    they asked it of the whole platform.
    """
    country_id = getattr(country, 'pk', country)
    return Q(**{f'{field}__isnull': True, country_field: country_id})


def _resolve(institution, field):
    """Accept an Institution, an id, or a list of ids.

    Call sites hold all three. The ones that hold an id or a list of them are
    the ones a user belongs to more than one school — and the rule there is
    "any of my schools, plus anything shared with any of my schools'
    countries", so the country lookup has to be a query rather than an
    attribute read.
    """
    from ai_tutor.apps.accounts.models import Institution

    if isinstance(institution, Institution):
        return {field: institution}, [institution.country_id]
    if isinstance(institution, (list, tuple, set, frozenset)):
        ids = list(institution)
        countries = list(Institution.objects.filter(
            pk__in=ids).values_list('country_id', flat=True).distinct())
        return {f'{field}__in': ids}, countries
    country_id = Institution.objects.filter(
        pk=institution).values_list('country_id', flat=True).first()
    return {field: institution}, [country_id]


def visible_to(queryset, institution, field='institution', country_field=None):
    """Rows *institution* may see: its own, plus its country's shared rows.

    A shared row is one with no institution — a national curriculum, a
    platform-wide prompt pack. It is visible to every school whose country
    matches the row's.

    ``institution=None`` means the caller is already aggregating (a super
    admin looking across schools), so nothing is filtered.

    *field* and *country_field* are lookup paths for callers that reach the
    FKs through a relation::

        visible_to(Unit.objects.all(), inst,
                   field='course__institution', country_field='course__country')

    Passing *field* without *country_field* is almost always a bug: the two
    have to walk the same relation, or the query asks "this school's units,
    or any unit shared with this school's country" of a model that has no
    country of its own.
    """
    return queryset.filter(visible_q(institution, field, country_field))
