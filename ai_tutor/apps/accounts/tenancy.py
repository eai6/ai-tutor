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


def visible_to(queryset, institution, field='institution', country_field='country'):
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
    if institution is None:
        return queryset
    own = {field: institution}
    shared = {f'{field}__isnull': True, country_field: institution.country_id}
    return queryset.filter(Q(**own) | Q(**shared))
