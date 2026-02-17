"""
Safety views for consent management and data privacy.
"""

import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from .models import ConsentRecord, SafetyAuditLog
from . import DataPrivacy


@login_required
def privacy_dashboard(request):
    """User's privacy dashboard - view and manage their data."""
    consents = ConsentRecord.objects.filter(user=request.user)
    
    # Create missing consent records
    consent_types = [c[0] for c in ConsentRecord.ConsentType.choices]
    existing_types = set(consents.values_list('consent_type', flat=True))
    
    for ct in consent_types:
        if ct not in existing_types:
            ConsentRecord.objects.create(user=request.user, consent_type=ct)
    
    consents = ConsentRecord.objects.filter(user=request.user)
    
    # Get data summary
    from apps.tutoring.models import TutorSession, SessionTurn
    
    data_summary = {
        'sessions': TutorSession.objects.filter(student=request.user).count(),
        'messages': SessionTurn.objects.filter(session__student=request.user).count(),
        'account_created': request.user.date_joined,
    }
    
    return render(request, 'safety/privacy_dashboard.html', {
        'consents': consents,
        'data_summary': data_summary,
    })


@login_required
@require_http_methods(["POST"])
def update_consent(request, consent_type):
    """Update a consent record."""
    consent = get_object_or_404(
        ConsentRecord,
        user=request.user,
        consent_type=consent_type
    )
    
    try:
        data = json.loads(request.body)
        give_consent = data.get('consent', False)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    if give_consent:
        consent.given = True
        consent.given_at = timezone.now()
        consent.withdrawn_at = None
        event_type = 'consent_given'
    else:
        consent.given = False
        consent.withdrawn_at = timezone.now()
        event_type = 'consent_withdrawn'
    
    consent.ip_address = get_client_ip(request)
    consent.save()
    
    # Log the consent change
    SafetyAuditLog.log(
        event_type,
        user=request.user,
        details={
            'consent_type': consent_type,
            'action': 'given' if give_consent else 'withdrawn',
        },
        request=request,
    )
    
    return JsonResponse({
        'success': True,
        'consent_type': consent_type,
        'given': consent.given,
    })


@login_required
def export_my_data(request):
    """Export user's own data (GDPR data portability)."""
    export_data = DataPrivacy.export_user_data(request.user)
    
    # Log the export
    SafetyAuditLog.log(
        'data_export',
        user=request.user,
        details={'self_service': True},
        request=request,
    )
    
    response = HttpResponse(
        json.dumps(export_data, indent=2, default=str),
        content_type='application/json'
    )
    response['Content-Disposition'] = f'attachment; filename="my_data_{request.user.username}.json"'
    
    return response


@login_required
@require_http_methods(["POST"])
def delete_my_data(request):
    """Delete user's own data (GDPR right to erasure)."""
    try:
        data = json.loads(request.body)
        confirm = data.get('confirm', False)
        keep_account = data.get('keep_account', True)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    
    if not confirm:
        return JsonResponse({'error': 'Confirmation required'}, status=400)
    
    # Log before deletion
    SafetyAuditLog.log(
        'data_delete',
        user=request.user,
        details={
            'self_service': True,
            'keep_account': keep_account,
        },
        request=request,
    )
    
    # Delete data (keep anonymized for analytics)
    DataPrivacy.delete_user_data(request.user, keep_anonymized=True)
    
    if not keep_account:
        # Delete the account
        request.user.delete()
        return JsonResponse({
            'success': True,
            'message': 'Account and all data deleted',
            'redirect': '/',
        })
    
    return JsonResponse({
        'success': True,
        'message': 'All learning data has been deleted',
    })


def privacy_policy(request):
    """Display privacy policy."""
    return render(request, 'safety/privacy_policy.html')


def terms_of_service(request):
    """Display terms of service."""
    return render(request, 'safety/terms_of_service.html')


@login_required
def parental_consent_form(request):
    """Form for parental consent (for users under 16)."""
    if request.method == 'POST':
        parent_email = request.POST.get('parent_email', '')
        parent_name = request.POST.get('parent_name', '')
        
        consent, _ = ConsentRecord.objects.get_or_create(
            user=request.user,
            consent_type=ConsentRecord.ConsentType.PARENTAL,
        )
        
        consent.parent_email = parent_email
        consent.parent_name = parent_name
        consent.ip_address = get_client_ip(request)
        consent.save()
        
        # TODO: Send verification email to parent
        
        return redirect('safety:privacy_dashboard')
    
    return render(request, 'safety/parental_consent.html')


def get_client_ip(request):
    """Get client IP address from request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')
