"""
Authentication views - Role-based Register, Login, Logout

User Types:
- Student: Self-registration, access to tutor
- Teacher: Invited by admin, access to teacher dashboard  
- Admin: Invited by system admin, full school access
- System Admin: Django superuser, uses /admin/
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404
from apps.accounts.models import Institution, Membership, StudentProfile, PlatformConfig


def landing_page(request):
    """Main landing page with role selection."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)
    
    return render(request, 'accounts/landing.html')


def redirect_by_role(user):
    """Redirect user to appropriate dashboard based on role."""
    if user.is_staff:
        return redirect('dashboard:home')

    membership = Membership.objects.filter(
        user=user,
        is_active=True
    ).first()

    if membership and membership.role == 'staff':
        return redirect('dashboard:home')

    return redirect('tutoring:catalog')


# ============================================================================
# Student Auth
# ============================================================================

def student_login(request):
    """Student login page."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            messages.success(request, f"Welcome back, {user.first_name or user.username}!")
            # Route through terms-acceptance interstitial if a newer
            # version was published since this user last agreed.
            if _terms_acceptance_pending(user):
                return redirect(f"/terms/accept/?next=/tutor/")
            return redirect('tutoring:catalog')
        else:
            return render(request, 'accounts/student_login.html', {
                'error': "Invalid username or password.",
                'username': username,
            })

    return render(request, 'accounts/student_login.html')


def _terms_acceptance_pending(user) -> bool:
    """True if the user needs to re-accept the active terms."""
    from apps.accounts.models import PlatformTerms
    active_v = PlatformTerms.active_version()
    if active_v == 0:
        return False
    profile = StudentProfile.objects.filter(user=user).first()
    accepted_v = profile.terms_accepted_version if profile else 0
    return accepted_v < active_v


def student_register(request):
    """Student self-registration."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    from apps.accounts.models import PlatformTerms
    school_choices = PlatformConfig.get_school_choices()
    grade_choices = PlatformConfig.get_grade_choices()
    active_terms = PlatformTerms.active()

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        school = request.POST.get('school', '')
        grade_level = request.POST.get('grade_level', '')
        student_id = request.POST.get('student_id', '').strip()
        accepted_terms = request.POST.get('accept_terms') == 'on'

        errors = []

        if not first_name:
            errors.append("Please enter your first name.")

        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")

        if User.objects.filter(username=username).exists():
            errors.append("Username already taken.")

        if email and User.objects.filter(email=email).exists():
            errors.append("Email already registered.")

        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")

        if password != password_confirm:
            errors.append("Passwords don't match.")

        if not school:
            errors.append("Please select your school.")

        if not grade_level:
            errors.append("Please select your grade level.")

        if active_terms and not accepted_terms:
            errors.append("Please read and agree to the platform terms.")

        if errors:
            return render(request, 'accounts/student_register.html', {
                'errors': errors,
                'username': username,
                'email': email,
                'first_name': first_name,
                'last_name': last_name,
                'school': school,
                'grade_level': grade_level,
                'student_id': student_id,
                'school_choices': school_choices,
                'grade_choices': grade_choices,
                'active_terms': active_terms,
            })
        
        # Create user
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )
        
        # Create student profile (record accepted terms version + timestamp)
        from django.utils import timezone as _tz
        StudentProfile.objects.create(
            user=user,
            student_id=student_id,
            school=school,
            grade_level=grade_level,
            terms_accepted_version=active_terms.version if active_terms else 0,
            terms_accepted_at=_tz.now() if active_terms else None,
        )
        
        # Auto-assign to institution based on selected school
        institution = Institution.objects.filter(id=school, is_active=True).first()
        if not institution:
            # Fallback: try matching by slug for legacy school codes
            institution = Institution.objects.filter(slug=school, is_active=True).first()
        if not institution:
            institution = Institution.objects.filter(is_active=True).first()

        if institution:
            Membership.objects.create(
                user=user,
                institution=institution,
                role=Membership.Role.STUDENT,
                is_active=True,
            )

        # Send verification email (soft gate — failure is non-fatal,
        # student can still log in; banner will nag until verified).
        if email:
            from apps.accounts.email_verification import send_verification_email
            send_verification_email(request, user)

        login(request, user)
        if email:
            messages.success(
                request,
                f"Welcome, {first_name}! 🎉 We sent a verification link to {email} — check your inbox.",
            )
        else:
            messages.success(request, f"Welcome, {first_name}! 🎉 Let's start learning!")
        return redirect('tutoring:catalog')
    
    return render(request, 'accounts/student_register.html', {
        'school_choices': school_choices,
        'grade_choices': grade_choices,
        'active_terms': active_terms,
    })


# ============================================================================
# Teacher/Admin Auth
# ============================================================================

def staff_login(request):
    """Staff login page (teachers and admins combined)."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)
    
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            # Check if user is superadmin (is_staff) or has staff Membership
            has_access = user.is_staff or Membership.objects.filter(
                user=user,
                role='staff',
                is_active=True
            ).exists()

            if has_access:
                login(request, user)
                messages.success(request, f"Welcome, {user.first_name or user.username}!")
                if _terms_acceptance_pending(user):
                    return redirect(f"/terms/accept/?next=/dashboard/")
                return redirect('dashboard:home')
            else:
                return render(request, 'accounts/staff_login.html', {
                    'error': "You don't have staff access. Please use student login.",
                    'username': username,
                })
        else:
            # Check if this is a pending teacher (inactive, never logged in)
            try:
                pending_user = User.objects.get(username=username)
                if not pending_user.is_active and pending_user.last_login is None and \
                   Membership.objects.filter(user=pending_user, role='staff').exists():
                    return render(request, 'accounts/staff_login.html', {
                        'error': "Your account is pending approval by an administrator.",
                        'username': username,
                    })
            except User.DoesNotExist:
                pass

            return render(request, 'accounts/staff_login.html', {
                'error': "Invalid username or password.",
                'username': username,
            })

    return render(request, 'accounts/staff_login.html')


def staff_self_register(request):
    """Teacher self-registration (pending admin approval)."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    from apps.accounts.models import PlatformTerms
    school_choices = PlatformConfig.get_school_choices()
    active_terms = PlatformTerms.active()

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        school = request.POST.get('school', '')
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        accepted_terms = request.POST.get('accept_terms') == 'on'

        errors = []

        if not first_name:
            errors.append("Please enter your first name.")

        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")

        if User.objects.filter(username=username).exists():
            errors.append("Username already taken.")

        if not email:
            errors.append("Email is required for teacher accounts.")
        elif User.objects.filter(email=email).exists():
            errors.append("Email already registered.")

        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")

        if password != password_confirm:
            errors.append("Passwords don't match.")

        if not school:
            errors.append("Please select your school.")

        if active_terms and not accepted_terms:
            errors.append("Please read and agree to the platform terms.")

        if errors:
            return render(request, 'accounts/staff_self_register.html', {
                'errors': errors,
                'first_name': first_name,
                'last_name': last_name,
                'username': username,
                'email': email,
                'school': school,
                'school_choices': school_choices,
                'active_terms': active_terms,
            })

        # Create user (inactive — pending approval)
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            is_active=False,
        )

        # Resolve institution
        institution = Institution.objects.filter(id=school, is_active=True).first()
        if not institution:
            institution = Institution.objects.filter(slug=school, is_active=True).first()
        if not institution:
            institution = Institution.objects.filter(is_active=True).first()

        if institution:
            Membership.objects.create(
                user=user,
                institution=institution,
                role='staff',
                is_active=False,
            )

        # Record terms acceptance — staff use the same StudentProfile slot
        # for now. Even though staff aren't students, this avoids a second
        # acceptance model. (Staff just won't have grade_level set.)
        if active_terms:
            from django.utils import timezone as _tz
            StudentProfile.objects.update_or_create(
                user=user,
                defaults={
                    'terms_accepted_version': active_terms.version,
                    'terms_accepted_at': _tz.now(),
                },
            )

        # Send verification email. Staff email is required (validated
        # above) so this should always have a recipient. Verification
        # status is independent of admin approval — a teacher can
        # verify their email before the admin activates the account.
        from apps.accounts.email_verification import send_verification_email
        send_verification_email(request, user)

        return render(request, 'accounts/staff_pending.html')

    return render(request, 'accounts/staff_self_register.html', {
        'school_choices': school_choices,
        'active_terms': active_terms,
    })


def staff_register(request, token=None):
    """
    Teacher/Admin registration via invitation token.
    Invited teachers are pre-approved and skip the pending state.
    """
    if request.user.is_authenticated:
        return redirect_by_role(request.user)
    
    # Validate invitation token
    from apps.accounts.models import StaffInvitation
    
    invitation = None
    if token:
        invitation = StaffInvitation.objects.filter(
            token=token,
            is_used=False,
        ).first()
    
    if not invitation:
        return render(request, 'accounts/staff_register.html', {
            'error': "Invalid or expired invitation link. Please contact your school administrator.",
            'no_invitation': True,
        })
    
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()

        # Email: use invitation email if set, otherwise allow user to enter one
        if invitation.email:
            email = invitation.email
        else:
            email = request.POST.get('email', '').strip()

        errors = []

        if not first_name:
            errors.append("Please enter your first name.")

        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")

        if User.objects.filter(username=username).exists():
            errors.append("Username already taken.")

        if email and User.objects.filter(email=email).exists():
            errors.append("Email already registered.")

        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")

        if password != password_confirm:
            errors.append("Passwords don't match.")

        if errors:
            return render(request, 'accounts/staff_register.html', {
                'errors': errors,
                'invitation': invitation,
                'username': username,
                'first_name': first_name,
                'last_name': last_name,
                'email': email,
            })

        # Create user
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        # Create membership with staff role
        Membership.objects.create(
            user=user,
            institution=invitation.institution,
            role='staff',
            is_active=True,
        )

        # Mark invitation as used
        invitation.is_used = True
        invitation.registered_user = user
        invitation.save()

        login(request, user)
        messages.success(request, f"Welcome, {first_name}! Your staff account is ready.")
        return redirect('dashboard:home')

    return render(request, 'accounts/staff_register.html', {
        'invitation': invitation,
    })


# ============================================================================
# Invitation Management (for admins)
# ============================================================================

@login_required
def invite_staff(request):
    """Superadmin can invite staff members to a specific school."""
    if not request.user.is_staff:
        messages.error(request, "Only administrators can invite staff.")
        return redirect('dashboard:home')

    from apps.accounts.models import StaffInvitation
    import secrets

    active_schools = Institution.objects.filter(is_active=True).order_by('name')
    if not active_schools.exists():
        messages.error(request, "No active schools found. Please create a school first.")
        return redirect('dashboard:settings')

    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        school_id = request.POST.get('school_id', '')

        # Validate school selection
        institution = active_schools.filter(id=school_id).first() if school_id else None
        if not institution:
            messages.error(request, "Please select a valid school.")
            return redirect('accounts:invite_staff')

        # Validate email only if provided
        if email and '@' not in email:
            messages.error(request, "Please enter a valid email address.")
            return redirect('accounts:invite_staff')

        # Duplicate check only if email provided
        if email:
            existing = StaffInvitation.objects.filter(
                email=email,
                institution=institution,
                is_used=False
            ).first()
            if existing:
                messages.warning(request, f"An invitation was already sent to {email} for {institution.name}.")
                return redirect('accounts:invite_staff')

        # Create invitation (always role=staff)
        invitation = StaffInvitation.objects.create(
            institution=institution,
            email=email,
            role='staff',
            invited_by=request.user,
            token=secrets.token_urlsafe(32),
        )

        from django.urls import reverse
        invite_url = request.build_absolute_uri(reverse('accounts:staff_register', args=[invitation.token]))

        # Send email if address was provided
        if email:
            from django.core.mail import send_mail
            from django.conf import settings
            platform_name = PlatformConfig.load().platform_name or 'AI Tutor'
            try:
                send_mail(
                    subject=f"You're invited to join {institution.name} on {platform_name}",
                    message=(
                        f"Hello,\n\n"
                        f"You've been invited to join {institution.name} as a staff member "
                        f"on {platform_name}.\n\n"
                        f"Click the link below to complete your registration:\n"
                        f"{invite_url}\n\n"
                        f"This invitation was sent by {request.user.get_full_name() or request.user.username}.\n\n"
                        f"— {platform_name}"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[email],
                    fail_silently=False,
                )
                messages.success(request, f"Invitation emailed to {email} at {institution.name}!")
            except Exception as e:
                messages.success(request, f"Invitation created for {email} at {institution.name}! (Email delivery failed: {e})")
        else:
            messages.success(request, f"Link-only invitation created for {institution.name}!")

        return render(request, 'accounts/invite_success.html', {
            'invitation': invitation,
            'invite_url': invite_url,
        })

    # Show pending invitations across all schools
    pending = StaffInvitation.objects.filter(
        is_used=False
    ).select_related('institution').order_by('-created_at')

    return render(request, 'accounts/invite_staff.html', {
        'pending_invitations': pending,
        'active_schools': active_schools,
    })


# ============================================================================
# Legacy endpoints (redirect to new ones)
# ============================================================================

def register_view(request):
    """Legacy register - redirect to student register."""
    return redirect('accounts:student_register')


def login_view(request):
    """Legacy login - show role selection or smart redirect."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)
    
    # If coming from a specific next URL, try to be smart
    next_url = request.GET.get('next', '')
    if 'dashboard' in next_url:
        return redirect('accounts:staff_login')
    
    # Default to landing page
    return redirect('accounts:landing')


def logout_view(request):
    """Logout and redirect to landing."""
    logout(request)
    messages.info(request, "You've been logged out.")
    return redirect('accounts:landing')


@login_required
def delete_account(request):
    """Self-service account deletion.

    GET shows a confirmation page (with password input). POST validates
    the password, audit-logs the deletion, then deletes the user and all
    cascade-linked data (sessions, turns, progress, profile, memberships).
    The user is logged out and redirected to the landing page.
    """
    from django.contrib.auth import authenticate
    from apps.safety import SafetyAuditLog

    if request.method == 'POST':
        password = request.POST.get('password', '')
        confirm = request.POST.get('confirm', '')
        if confirm.strip().lower() != 'delete':
            messages.error(request, 'Please type "DELETE" exactly to confirm.')
            return render(request, 'accounts/delete_account.html')
        # Re-authenticate
        if not authenticate(username=request.user.username, password=password):
            messages.error(request, 'Password incorrect. Account NOT deleted.')
            return render(request, 'accounts/delete_account.html')

        user_id = request.user.id
        username = request.user.username
        SafetyAuditLog.log(
            'account_deleted',
            user=request.user,
            details={'mode': 'self', 'username': username},
            severity='warning',
            request=request,
        )
        # Hard delete — cascades through all FKs (Membership, StudentProfile,
        # TutorSession, SessionTurn, StudentLessonProgress, etc.).
        request.user.delete()
        logout(request)
        messages.success(
            request,
            f"Account '{username}' has been permanently deleted.",
        )
        return redirect('accounts:landing')

    return render(request, 'accounts/delete_account.html')


@login_required
def settings(request):
    """Student-facing settings page — profile info + password change +
    delete-account entry point.

    Form actions are POSTed back to this same URL with a ?action= param
    so we don't need separate routes for each subform.
    """
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm

    user = request.user
    student_profile = StudentProfile.objects.filter(user=user).first()
    membership = user.memberships.filter(is_active=True).first()

    password_form = PasswordChangeForm(user)

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'profile':
            user.first_name = (request.POST.get('first_name') or '').strip()
            user.last_name = (request.POST.get('last_name') or '').strip()
            email = (request.POST.get('email') or '').strip()
            if email and '@' in email:
                user.email = email
            user.save()
            if student_profile:
                grade = (request.POST.get('grade_level') or '').strip()
                if grade:
                    student_profile.grade_level = grade
                    student_profile.save()
            messages.success(request, 'Profile updated.')
            return redirect('accounts:settings')

        if action == 'password':
            password_form = PasswordChangeForm(user, request.POST)
            if password_form.is_valid():
                password_form.save()
                update_session_auth_hash(request, password_form.user)
                messages.success(request, 'Password changed.')
                return redirect('accounts:settings')
            # else: fall through to render with form errors

    return render(request, 'accounts/settings.html', {
        'user_obj': user,
        'student_profile': student_profile,
        'membership': membership,
        'password_form': password_form,
    })


@login_required
def bulk_student_upload(request):
    """Bulk register students via CSV upload.

    CSV format: student_id, first_name, last_name, username, password, grade_level
    All fields required except student_id. School is selected in the form.
    """
    if not request.user.is_staff:
        # Check if user has staff membership
        if not Membership.objects.filter(user=request.user, role='staff', is_active=True).exists():
            messages.error(request, "Staff access required.")
            return redirect('accounts:landing')

    school_choices = PlatformConfig.get_school_choices()

    if request.method == 'POST':
        import csv
        import io

        csv_file = request.FILES.get('csv_file')
        school = request.POST.get('school', '')

        if not csv_file:
            messages.error(request, "Please upload a CSV file.")
            return render(request, 'accounts/bulk_student_upload.html', {
                'school_choices': school_choices,
            })

        if not school:
            messages.error(request, "Please select a school.")
            return render(request, 'accounts/bulk_student_upload.html', {
                'school_choices': school_choices,
            })

        # Find institution
        institution = Institution.objects.filter(id=school, is_active=True).first()
        if not institution:
            institution = Institution.objects.filter(slug=school, is_active=True).first()
        if not institution:
            institution = Institution.objects.filter(is_active=True).first()

        # Read CSV
        try:
            content = csv_file.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(content))
        except Exception as e:
            messages.error(request, f"Could not read CSV: {e}")
            return render(request, 'accounts/bulk_student_upload.html', {
                'school_choices': school_choices,
            })

        created = 0
        skipped = 0
        errors_list = []

        for row_num, row in enumerate(reader, start=2):
            student_id = (row.get('student_id') or '').strip()
            first_name = (row.get('first_name') or '').strip()
            last_name = (row.get('last_name') or '').strip()
            username = (row.get('username') or '').strip()
            password = (row.get('password') or '').strip()
            grade_level = (row.get('grade_level') or '').strip().upper()

            if not first_name or not username or not password:
                errors_list.append(f"Row {row_num}: Missing required field (first_name, username, or password)")
                skipped += 1
                continue

            if len(username) < 3:
                errors_list.append(f"Row {row_num}: Username '{username}' too short (min 3 chars)")
                skipped += 1
                continue

            if User.objects.filter(username=username).exists():
                errors_list.append(f"Row {row_num}: Username '{username}' already exists")
                skipped += 1
                continue

            if grade_level not in ('S1', 'S2', 'S3', 'S4', 'S5', ''):
                errors_list.append(f"Row {row_num}: Invalid grade level '{grade_level}'")
                skipped += 1
                continue

            try:
                user = User.objects.create_user(
                    username=username,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                )

                StudentProfile.objects.create(
                    user=user,
                    student_id=student_id,
                    school=str(school),
                    grade_level=grade_level,
                )

                if institution:
                    Membership.objects.create(
                        user=user,
                        institution=institution,
                        role='student',
                        is_active=True,
                    )

                created += 1
            except Exception as e:
                errors_list.append(f"Row {row_num}: {e}")
                skipped += 1

        messages.success(request, f"Bulk upload complete: {created} students created, {skipped} skipped.")

        return render(request, 'accounts/bulk_student_upload.html', {
            'school_choices': school_choices,
            'results': {
                'created': created,
                'skipped': skipped,
                'errors': errors_list[:20],
            },
        })

    return render(request, 'accounts/bulk_student_upload.html', {
        'school_choices': school_choices,
    })


# ============================================================================
# Platform Terms — public page + acceptance interstitial
# ============================================================================

def terms_page(request):
    """Public, unauthenticated render of the active platform terms."""
    from apps.accounts.models import PlatformTerms
    active = PlatformTerms.active()
    return render(request, 'accounts/terms.html', {
        'terms': active,
    })


@login_required
def terms_accept(request):
    """Interstitial — existing users with an outdated `terms_accepted_version`
    are routed here on next login. POST records acceptance and bounces
    them on to wherever they were headed."""
    from apps.accounts.models import PlatformTerms, StudentProfile
    from django.utils import timezone as _tz

    active = PlatformTerms.active()
    if not active:
        return redirect('tutoring:catalog')

    next_url = request.GET.get('next') or request.POST.get('next') or ''
    if not next_url.startswith('/'):
        next_url = ''

    if request.method == 'POST':
        if request.POST.get('accept_terms') != 'on':
            return render(request, 'accounts/terms_accept.html', {
                'terms': active,
                'next_url': next_url,
                'error': "Please tick the box to confirm you've read and agreed.",
            })
        profile, _ = StudentProfile.objects.get_or_create(user=request.user)
        profile.terms_accepted_version = active.version
        profile.terms_accepted_at = _tz.now()
        profile.save(update_fields=['terms_accepted_version', 'terms_accepted_at'])
        messages.success(request, "Thanks — terms accepted.")
        return redirect(next_url or 'tutoring:catalog')

    return render(request, 'accounts/terms_accept.html', {
        'terms': active,
        'next_url': next_url,
    })


# ============================================================================
# Email verification (soft gate)
# ============================================================================

def verify_email(request, token):
    """Consume an email-verification token and flip the user's
    UserEmailStatus.verified_at. Idempotent — re-clicking is a no-op
    success."""
    from apps.accounts.models import EmailVerificationToken, UserEmailStatus

    row = EmailVerificationToken.objects.filter(token=token).first()
    if row is None:
        messages.error(request, "That verification link is invalid or expired. Request a new one.")
        return redirect('accounts:landing')

    if row.used_at is not None:
        # Already used — but still report success so re-clicks don't confuse.
        status = UserEmailStatus.for_user(row.user)
        if status.is_verified:
            messages.success(request, "Email already verified — you're all set! ✅")
        else:
            messages.error(request, "That verification link was already used.")
        return redirect('accounts:landing')

    if row.is_expired:
        messages.error(request, "That verification link has expired. Request a new one.")
        return redirect('accounts:landing')

    row.consume()
    messages.success(request, "Email verified! ✅ You'll now receive weekly lesson reminders.")

    # If the user is logged in, drop them on their dashboard. Otherwise,
    # send them to login so they can pick up where they left off.
    if request.user.is_authenticated and request.user.id == row.user_id:
        return redirect_by_role(request.user)
    return redirect('accounts:landing')


@login_required
def resend_verification(request):
    """Re-mint and send a verification token for the current user.
    Always rate-limit-safe at the app level (1 token/2 minutes per
    user) — earlier tokens get invalidated on the model side."""
    from apps.accounts.models import EmailVerificationToken, UserEmailStatus
    from apps.accounts.email_verification import send_verification_email

    # If already verified, no-op.
    status = UserEmailStatus.for_user(request.user)
    if status.is_verified:
        messages.info(request, "Your email is already verified.")
        return redirect_by_role(request.user)

    if not request.user.email:
        messages.error(
            request,
            "You don't have an email address on file. Add one in Settings first.",
        )
        return redirect('accounts:settings')

    # Rate-limit: don't issue a fresh token if one was issued in the
    # last 2 minutes. Saves the user from spamming the button.
    from django.utils import timezone as _tz
    from datetime import timedelta
    recent = EmailVerificationToken.objects.filter(
        user=request.user,
        created_at__gte=_tz.now() - timedelta(minutes=2),
    ).first()
    if recent is not None:
        messages.info(
            request,
            f"A verification link was just sent to {request.user.email}. Check your inbox (and spam folder).",
        )
        return redirect_by_role(request.user)

    sent = send_verification_email(request, request.user)
    if sent:
        messages.success(request, f"Verification link sent to {request.user.email}. ✉️")
    else:
        messages.error(
            request,
            "Couldn't send the verification email right now. Try again in a few minutes.",
        )
    return redirect_by_role(request.user)
