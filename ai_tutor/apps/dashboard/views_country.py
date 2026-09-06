"""The country account's own pages.

A country account is a super admin bounded by one country, and everything it
does elsewhere in the dashboard it does through the existing views with
`staff_ctx['institution']` set. Only one thing has no home in those views:
the list of schools in the country, and the form that adds one. That is this
module.

Kept out of `dashboard/views.py` deliberately. That file is over 9,000 lines,
and a new surface with its own audience is exactly the kind of thing that
should not be appended to it.
"""

import logging

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from ai_tutor.apps.accounts.models import Country, Institution, Membership
from ai_tutor.apps.dashboard.views import staff_required

logger = logging.getLogger(__name__)


@staff_required
def country_schools(request):
    """Every school in this account's country, with what each one holds."""
    ctx = request.staff_ctx
    if not ctx['can_add_schools']:
        messages.error(request, _("You don't have access to that."))
        return redirect('dashboard:home')

    # `all_schools` is already the right set — the platform's for a super
    # admin, one country's for a country account — so the counts are annotated
    # onto that rather than onto a second query that could disagree with it.
    schools = Institution.objects.filter(
        pk__in=[s.pk for s in ctx['all_schools']]
    ).select_related('country').annotate(
        student_count=Count('memberships', distinct=True,
                            filter=Q(memberships__role=Membership.Role.STUDENT,
                                     memberships__is_active=True)),
        staff_count=Count('memberships', distinct=True,
                          filter=Q(memberships__role__in=(
                              Membership.Role.STAFF,
                              Membership.Role.SCHOOL_ADMIN),
                              memberships__is_active=True)),
    ).order_by('name')

    return render(request, 'dashboard/country/schools.html', {
        **ctx,
        'schools': schools,
        'country': ctx.get('country'),
    })


@staff_required
@require_POST
def country_school_create(request):
    ctx = request.staff_ctx
    if not ctx['can_add_schools']:
        messages.error(request, _("You don't have access to that."))
        return redirect('dashboard:home')

    name = (request.POST.get('name') or '').strip()
    slug = slugify(request.POST.get('slug') or name)
    if not name or not slug:
        messages.error(request, _("A school needs a name."))
        return redirect('dashboard:country_schools')
    if Institution.objects.filter(slug=slug).exists():
        messages.error(request, _("That short name is already taken."))
        return redirect('dashboard:country_schools')

    # The creator's country, never a form field: a country account may only
    # ever add schools to its own country, and a hidden input would be a
    # cross-tenant hole.
    country = ctx.get('country') or Country.get_platform()
    Institution.objects.create(
        name=name, slug=slug, country=country,
        default_locale=country.default_locale,
    )
    messages.success(request, _("%(name)s added.") % {'name': name})
    return redirect('dashboard:country_schools')
