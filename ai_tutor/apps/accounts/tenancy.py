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


def visible_q(institution, field='institution', country_field=None, model=None):
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

    *model* is what the Q will be filtered against, and it is only consulted to
    answer one question: does this model carry a country at all? A
    TeachingMaterialUpload does not. Its shared half would ask for
    ``country__in`` on a table with no such column, which is a FieldError
    rather than a leak — /dashboard/curriculum/ raised one for every teacher.
    `in_country_q` has guarded this since it was written; this is the same
    guard on the other half of the pair.

    Without a country there is no way to say which country a row with no
    institution belongs to, so it belongs to none of them: the shared half is
    dropped, which is the answer `shared=False` gives for the same reason.
    Callers that do not pass *model* keep the old behaviour, so a site that
    works today cannot break by omission.
    """
    if institution is None:
        return Q()
    if country_field is None:
        country_field = field.rsplit('__', 1)[0] + '__country' if '__' in field else 'country'

    own, countries = _resolve(institution, field)
    if model is not None and not _has_path(model, country_field):
        return Q(**own)
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


def in_country_q(country, field='institution', model=None):
    """Everything one country may see, across every school in it.

    The aggregated view's counterpart to `visible_q`. `visible_q` answers
    "what may this one school see"; this answers "what may this whole country
    see", which is the question a ministry's dashboard asks when no school is
    selected.

    Before countries, that question had no answer and no need of one: an
    unselected school meant the platform super admin looking at everything, so
    `dashboard/views.py::filter_by_institution` simply returned the queryset
    unfiltered. A country account reaches the same views with the same
    unselected school and a very different entitlement.

    The shared half — rows with no institution — is included only when the
    model carries a country of its own. A Course does. A TutorSession does
    not, and does not need to: it always has a school.
    """
    if country is None:
        return Q()
    country_id = getattr(country, 'pk', country)
    q = Q(**{f'{field}__country': country_id})

    shared_field = (
        field.rsplit('__', 1)[0] + '__country' if '__' in field else 'country')
    if model is not None and _has_path(model, shared_field):
        q |= Q(**{f'{field}__isnull': True, shared_field: country_id})
    return q


def _has_path(model, path):
    """Whether *path* ('country', 'course__country') resolves on *model*."""
    for part in path.split('__'):
        try:
            field = model._meta.get_field(part)
        except Exception:
            return False
        model = field.related_model
        if model is None:
            return True
    return True


def scope_q(institution, country, field='institution', model=None, shared=True):
    """What a dashboard view's scope may see — all three cases in one Q.

    The dashboard wrote this branch out by hand at 26 sites::

        if institution is not None:
            course = get_object_or_404(Course, visible_q(institution), id=n)
        else:
            course = get_object_or_404(Course, id=n)

    and the `else` is the bug. It reads as "the super admin sees everything",
    which was true when the only account with no school selected WAS the super
    admin. A country account has no school selected either, and that branch
    handed it every other country's curriculum.

    One school selected -> `visible_q`. No school but a country -> that
    country. Neither -> everything, which is still the super admin.

    *shared* is whether rows with no institution count. They do for content a
    school reads: a national curriculum exists to be visible. They do not for
    rows a school owns — a student group, a roster, an invitation — where
    `institution=None` is not "shared with everyone" but "belongs to nobody".
    Those sites were strict before countries existed, and widening them is a
    different change from the one this makes, so they pass `shared=False` and
    get the country without the shared half.
    """
    if institution is not None:
        return (visible_q(institution, field, model=model) if shared
                else Q(**{field: institution}))
    if country is None:
        return Q()
    if not shared:
        return Q(**{f'{field}__country': getattr(country, 'pk', country)})
    return in_country_q(country, field, model=model)
