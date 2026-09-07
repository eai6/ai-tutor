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
from ai_tutor.apps.accounts.auth_utils import login_created_user
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from ai_tutor.apps.accounts.models import (
    Country, CountryMembership, Institution, Membership, PlatformConfig,
    StudentProfile)


def landing_page(request):
    """Main landing page with role selection."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)
    
    return render(request, 'accounts/landing.html')


def _password_errors(password, *, username='', email='', first_name='', last_name=''):
    """Run Django's configured AUTH_PASSWORD_VALIDATORS against a candidate
    password and return a list of human-readable error strings (empty when the
    password passes).

    The registration views collect errors into a list rather than raising, and
    used to hand-roll a bare ``len(password) < 6/8`` check that skipped the
    common-password, all-numeric and user-attribute-similarity validators
    entirely (the 2026-08 assessment's QA-03/QAS-03 finding). Routing every form
    through this helper restores the full chain and a single source of truth for
    the minimum length.
    """
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    # Unsaved probe user so UserAttributeSimilarityValidator can reject passwords
    # derived from the username/email/name before the account exists.
    probe = User(
        username=username or '',
        email=email or '',
        first_name=first_name or '',
        last_name=last_name or '',
    )
    try:
        validate_password(password, user=probe)
    except ValidationError as exc:
        return list(exc.messages)
    return []


def redirect_by_role(user):
    """Redirect user to appropriate dashboard based on role."""
    from ai_tutor.apps.accounts.scope import has_staff_access

    if has_staff_access(user):
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
            messages.success(request, _("Welcome back, %(name)s!") % {"name": user.first_name or user.username})
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
    from ai_tutor.apps.accounts.models import PlatformTerms
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

    from ai_tutor.apps.accounts.models import PlatformTerms
    school_choices = PlatformConfig.get_school_choices()
    grade_choices = PlatformConfig.get_grade_choices()
    active_terms = PlatformTerms.active(locale=get_language())

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

        errors.extend(_password_errors(
            password, username=username, email=email,
            first_name=first_name, last_name=last_name,
        ))

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
            from ai_tutor.apps.accounts.email_verification import send_verification_email
            send_verification_email(request, user)

        # We created this account ourselves, so it never went through
        # authenticate() and carries no .backend — see accounts/auth_utils.py.
        login_created_user(request, user)
        if email:
            # sticky: this one names an address the reader has to go and check,
            # and the page behind it does not repeat it.
            messages.success(
                request,
                f"Welcome, {first_name}! 🎉 We sent a verification link to {email} — check your inbox.",
                extra_tags='sticky',
            )
        else:
            messages.success(request, _("Welcome, %(name)s! 🎉 Let's start learning!") % {"name": first_name})
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
            # Every role the dashboard dispatches over, from the one
            # definition of it. Spelling it out here again is how country
            # accounts and school admins ended up unable to sign in.
            from ai_tutor.apps.accounts.scope import has_staff_access
            has_access = has_staff_access(user)

            if has_access:
                login(request, user)
                messages.success(request, _("Welcome back, %(name)s!") % {"name": user.first_name or user.username})
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
                awaiting = (
                    Membership.objects.filter(
                        user=pending_user,
                        role__in=(Membership.Role.STAFF, Membership.Role.SCHOOL_ADMIN),
                    ).exists()
                    or CountryMembership.objects.filter(user=pending_user).exists()
                )
                if not pending_user.is_active and pending_user.last_login is None and awaiting:
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

    from ai_tutor.apps.accounts.models import PlatformTerms
    school_choices = PlatformConfig.get_school_choices()
    active_terms = PlatformTerms.active(locale=get_language())

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
        elif Membership.objects.filter(role='staff', user__email__iexact=email).exists():
            # Many teachers were registered as STUDENTS during training, so
            # their email is already taken by a student account. Allow them to
            # create a fresh teacher account on the same email (separate User
            # with its own username + password) — only block a duplicate
            # TEACHER account. Password-reset emails include the username, so
            # the student and teacher accounts stay distinguishable.
            errors.append("This email already has a teacher account. Use the teacher login, or reset your password.")

        errors.extend(_password_errors(
            password, username=username, email=email,
            first_name=first_name, last_name=last_name,
        ))

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
        from ai_tutor.apps.accounts.email_verification import send_verification_email
        send_verification_email(request, user)

        return render(request, 'accounts/staff_pending.html')

    return render(request, 'accounts/staff_self_register.html', {
        'school_choices': school_choices,
        'active_terms': active_terms,
    })


# ============================================================================
# Country accounts
# ============================================================================

def country_login(request):
    """Sign-in for a ministry or programme team.

    Its own page rather than a note on the staff one: a ministry officer
    arriving from a procurement conversation should not have to work out that
    they are "staff". The gate is the same `has_staff_access` the dashboard
    uses, narrowed to an active CountryMembership — signing in here and
    landing on a teacher's view of one school would be worse than a refusal.
    """
    from ai_tutor.apps.accounts.scope import has_staff_access

    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)

        if user is not None and CountryMembership.objects.filter(
                user=user, is_active=True).exists():
            login(request, user)
            messages.success(request, _("Welcome back, %(name)s!") % {
                "name": user.first_name or user.username})
            if _terms_acceptance_pending(user):
                return redirect("/terms/accept/?next=/dashboard/")
            return redirect('dashboard:home')

        if user is not None and has_staff_access(user):
            # A real account, just not a country one. Say so rather than
            # "invalid password", which sends them to reset a password that
            # was never the problem.
            return render(request, 'accounts/country_login.html', {
                'error': _("That account is not a country account. Use the teacher and admin sign-in."),
                'username': username,
            })

        pending = User.objects.filter(username=username).first()
        if pending is not None and not pending.is_active and \
                CountryMembership.objects.filter(user=pending).exists():
            return render(request, 'accounts/country_login.html', {
                'error': _("Your country account is awaiting approval."),
                'username': username,
            })

        return render(request, 'accounts/country_login.html', {
            'error': _("Invalid username or password."),
            'username': username,
        })

    return render(request, 'accounts/country_login.html')


def country_self_register(request):
    """Request a country account. Inactive until a platform admin approves it.

    The country is chosen from the ones already on the platform rather than
    typed. An unauthenticated form that creates Country rows would let anyone
    add a country — and a country is the boundary every scoped query is drawn
    against, so an invented one is a tenancy hole rather than a stray record.
    A ministry whose country is not listed yet is told to get in touch.
    """
    from ai_tutor.apps.accounts.models import PlatformTerms

    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    countries = list(Country.objects.filter(is_hidden=False).order_by('name'))
    active_terms = PlatformTerms.active(locale=get_language())

    def _form(**extra):
        return render(request, 'accounts/country_self_register.html', dict(
            {'countries': countries, 'active_terms': active_terms}, **extra))

    if request.method != 'POST':
        return _form()

    first_name = request.POST.get('first_name', '').strip()
    last_name = request.POST.get('last_name', '').strip()
    username = request.POST.get('username', '').strip()
    email = request.POST.get('email', '').strip()
    country_id = request.POST.get('country', '')
    organisation = request.POST.get('organisation', '').strip()
    password = request.POST.get('password', '')
    password_confirm = request.POST.get('password_confirm', '')
    accepted_terms = request.POST.get('accept_terms') == 'on'

    errors = []
    if not first_name:
        errors.append(_("Please enter your first name."))
    if not username or len(username) < 3:
        errors.append(_("Username must be at least 3 characters."))
    elif User.objects.filter(username=username).exists():
        errors.append(_("Username already taken."))
    if not email:
        errors.append(_("Email is required for a country account."))

    country = next((c for c in countries if str(c.pk) == str(country_id)), None)
    if country is None:
        errors.append(_("Please choose the country you represent."))
    if not organisation:
        errors.append(_("Please name the ministry or programme you work for."))

    errors.extend(_password_errors(
        password, username=username, email=email,
        first_name=first_name, last_name=last_name))
    if password != password_confirm:
        errors.append(_("Passwords don't match."))
    if active_terms and not accepted_terms:
        errors.append(_("Please read and agree to the platform terms."))

    if errors:
        return _form(errors=errors, first_name=first_name, last_name=last_name,
                     username=username, email=email, country=country_id,
                     organisation=organisation)

    user = User.objects.create_user(
        username=username, email=email, password=password,
        first_name=first_name, last_name=last_name, is_active=False,
    )
    # Inactive on both records. The account is only a country account once a
    # platform admin says so, and `has_staff_access` reads is_active.
    CountryMembership.objects.create(user=user, country=country, is_active=False)

    if active_terms:
        from django.utils import timezone as _tz
        StudentProfile.objects.update_or_create(user=user, defaults={
            'terms_accepted_version': active_terms.version,
            'terms_accepted_at': _tz.now(),
        })

    from ai_tutor.apps.safety import SafetyAuditLog
    SafetyAuditLog.log(
        'account_created', user=user,
        details={'mode': 'country_self_register', 'country': country.slug,
                 'organisation': organisation[:200]},
        severity='warning', request=request,
    )

    from ai_tutor.apps.accounts.email_verification import send_verification_email
    send_verification_email(request, user)

    return render(request, 'accounts/country_pending.html', {'country': country})


def staff_register(request, token=None):
    """
    Teacher/Admin registration via invitation token.
    Invited teachers are pre-approved and skip the pending state.
    """
    if request.user.is_authenticated:
        return redirect_by_role(request.user)
    
    # Validate invitation token
    from ai_tutor.apps.accounts.models import StaffInvitation
    
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

        if email and Membership.objects.filter(role='staff', user__email__iexact=email).exists():
            # Allow an email already used by a student account; only block a
            # duplicate teacher account (see staff_self_register for rationale).
            errors.append("This email already has a teacher account. Use the teacher login, or reset your password.")

        errors.extend(_password_errors(
            password, username=username, email=email,
            first_name=first_name, last_name=last_name,
        ))

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

        # Self-created account — see accounts/auth_utils.py.
        login_created_user(request, user)
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
    """Deprecated standalone invite page. Staff invites are now managed on the
    centralized Staff page (dashboard:staff_list); redirect any old links there."""
    if not request.user.is_staff:
        messages.error(request, "Only administrators can invite staff.")
        return redirect('dashboard:home')
    return redirect('dashboard:staff_list')


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
    # No flash here. Landing on the signed-out page is itself the confirmation,
    # and this one was never seen where it was raised: the landing and login
    # templates render no message region, so it sat in the session until the
    # next visit to a page that did — arriving stale, next to a welcome that
    # contradicted it.
    return redirect('accounts:landing')


@login_required
def password_change_required(request):
    """Forced password-change view shown after an admin reset.

    The middleware (ai_tutor.apps.accounts.password_reset_middleware) redirects
    here whenever any of the user's memberships has
    ``password_reset_required=True``. On successful change, all of
    the user's memberships have the flag cleared and the user is
    redirected to the appropriate landing page.
    """
    from django.contrib.auth.forms import SetPasswordForm
    from django.contrib.auth import update_session_auth_hash
    from ai_tutor.apps.accounts.models import Membership

    user = request.user

    if request.method == 'POST':
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            # Clear the flag on every membership the user has, so they
            # don't get bounced back to this page on the next request.
            Membership.objects.filter(user=user).update(
                password_reset_required=False,
            )
            update_session_auth_hash(request, user)
            messages.success(
                request,
                "Password updated. You can continue using the platform now.",
            )
            # Send to the landing page; the post-login dispatcher will
            # route to dashboard / chat as appropriate.
            return redirect('accounts:landing')
    else:
        form = SetPasswordForm(user)

    return render(request, 'accounts/password_change_required.html', {
        'form': form,
    })


@login_required
def delete_account(request):
    """Self-service account deletion.

    GET shows a confirmation page (with password input). POST validates
    the password, audit-logs the deletion, then deletes the user and all
    cascade-linked data (sessions, turns, progress, profile, memberships).
    The user is logged out and redirected to the landing page.
    """
    from django.contrib.auth import authenticate
    from ai_tutor.apps.safety import SafetyAuditLog

    if request.method == 'POST':
        password = request.POST.get('password', '')
        confirm = request.POST.get('confirm', '')
        if confirm.strip().lower() != 'delete':
            messages.error(request, 'Please type "DELETE" exactly to confirm.')
            return render(request, 'accounts/delete_account.html')
        # Re-authenticate. The request has to be passed: django-axes rejects an
        # authenticate() call without one, and this is a password check on a
        # destructive action, so it belongs under the same lockout as login
        # rather than outside it (finding F-04).
        if not authenticate(request, username=request.user.username, password=password):
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
    """Settings page for both teachers and students — profile info +
    password change + delete-account entry point.

    Form actions are POSTed back to this same URL with a ?action= param
    so we don't need separate routes for each subform.

    Both audiences can update: first name, last name, email, school
    (institution), and (students only) grade level. School / grade
    are dropdowns sourced from PlatformConfig.
    """
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm

    user = request.user
    student_profile = StudentProfile.objects.filter(user=user).first()
    membership = user.memberships.filter(is_active=True).first()
    is_staff_user = user.is_staff or user.is_superuser

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

            # School (institution) — both teachers and students can
            # update. Re-points the active membership to the new
            # institution. New value comes from a select that lists
            # active institutions; we accept either an id or a slug
            # for backwards compat.
            school = (request.POST.get('school') or '').strip()
            if school and membership:
                new_inst = (
                    Institution.objects.filter(id=school, is_active=True).first()
                    or Institution.objects.filter(slug=school, is_active=True).first()
                )
                if new_inst and new_inst.id != membership.institution_id:
                    membership.institution = new_inst
                    membership.save(update_fields=['institution'])

            # Grade level — students only.
            if student_profile and not is_staff_user:
                grade = (request.POST.get('grade_level') or '').strip()
                if grade:
                    student_profile.grade_level = grade
                    student_profile.save()

            # Preferred locale — any user (student or staff). Drives
            # the LocaleResolverMiddleware's per-user override. Blank
            # → no override, fall back to institution.default_locale.
            # Get-or-create the profile for staff users so they can
            # set a preference too.
            from django.conf import settings as _settings
            preferred_locale = (request.POST.get('preferred_locale') or '').strip()
            valid_codes = set(dict(_settings.LANGUAGES).keys())
            if preferred_locale == '' or preferred_locale in valid_codes:
                if not student_profile:
                    student_profile, _created = StudentProfile.objects.get_or_create(user=user)
                student_profile.preferred_locale = preferred_locale or None
                student_profile.save(update_fields=['preferred_locale'])

            messages.success(request, _('Profile updated.'))
            return redirect('accounts:settings')

        if action == 'password':
            password_form = PasswordChangeForm(user, request.POST)
            if password_form.is_valid():
                password_form.save()
                update_session_auth_hash(request, password_form.user)
                messages.success(request, _('Password changed.'))
                return redirect('accounts:settings')
            # else: fall through to render with form errors

        elif action == 'tutor_mode' and student_profile is not None:
            # Students only. A teacher has no StudentProfile, so there is
            # nothing to set and the form is not rendered for them.
            choice = (request.POST.get('tutor_mode') or '').strip()
            valid = {c for c, _label in StudentProfile.TutorMode.choices}
            if choice not in valid:
                messages.error(request, _('That tutor option is not available.'))
                return redirect('accounts:settings')

            fields = ['tutor_mode']
            student_profile.tutor_mode = choice

            # Which offline model, when the device has more than one. Validated
            # against the installed set rather than trusting the posted id — a
            # stale form or a hand-edited request must not point a student at a
            # model that is not there.
            if 'offline_model' in request.POST:
                from ai_tutor.apps.tutoring.simple_tutor.model_choice import local_options
                raw = (request.POST.get('offline_model') or '').strip()
                if not raw:
                    student_profile.offline_model = None
                    fields.append('offline_model')
                else:
                    allowed = {str(o.pk): o for o in local_options()}
                    picked = allowed.get(raw)
                    if picked is None:
                        messages.error(
                            request, _('That offline tutor is not installed.'))
                        return redirect('accounts:settings')
                    student_profile.offline_model = picked
                    fields.append('offline_model')

            student_profile.save(update_fields=fields)
            messages.success(request, _('Tutor preference saved.'))
            return redirect('accounts:settings')

    # Dropdown choices for the form.
    school_choices = list(
        Institution.objects.filter(is_active=True).order_by('name').values('id', 'name')
    )
    # Country-specific, config-driven (Seychelles 'S1'–'S5', Mozambique
    # '8ª Classe', …). settings.html renders each entry as a bare string,
    # so flatten the (code, name) choices to codes.
    grade_choices = [code for code, _name in PlatformConfig.get_grade_choices()]

    from django.conf import settings as _settings
    # Offline/online tutor choice. Only meaningful where BOTH a local and a
    # cloud tutoring model are configured — i.e. the desktop build. On the
    # hosted platform describe_for_student reports available=False and the
    # template hides the control rather than offering a choice that does
    # nothing.
    tutor_choice = {'available': False}
    if student_profile is not None:
        from ai_tutor.apps.tutoring.simple_tutor.model_choice import describe_for_student
        tutor_choice = describe_for_student(student_profile)

    return render(request, 'accounts/settings.html', {
        'user_obj': user,
        'student_profile': student_profile,
        'tutor_choice': tutor_choice,
        'tutor_mode_choices': StudentProfile.TutorMode.choices,
        'membership': membership,
        'password_form': password_form,
        'is_staff_user': is_staff_user,
        'school_choices': school_choices,
        'grade_choices': grade_choices,
        # M5-wire follow-up: settings page exposes per-user locale
        # preference. Drives LocaleResolverMiddleware. Falls back to
        # institution.default_locale → settings.LANGUAGE_CODE when blank.
        'locale_choices': _settings.LANGUAGES,
        'current_preferred_locale': (
            student_profile.preferred_locale if student_profile else ''
        ) or '',
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
        # Country-specific grade codes (Seychelles 'S1'–'S5', Mozambique
        # '8ª Classe', …). Validate CSV rows against the configured set
        # rather than a hardcoded one. Looked up once, not per row.
        valid_grade_codes = [code for code, _name in PlatformConfig.get_grade_choices()]

        for row_num, row in enumerate(reader, start=2):
            student_id = (row.get('student_id') or '').strip()
            first_name = (row.get('first_name') or '').strip()
            last_name = (row.get('last_name') or '').strip()
            username = (row.get('username') or '').strip()
            password = (row.get('password') or '').strip()
            grade_raw = (row.get('grade_level') or '').strip()

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

            # Case-insensitive match to a configured code; canonicalise to
            # the configured spelling. Empty grade is allowed.
            grade_level = next(
                (c for c in valid_grade_codes
                 if c.strip().lower() == grade_raw.lower()),
                '' if not grade_raw else None,
            )
            if grade_level is None:
                errors_list.append(
                    f"Row {row_num}: Invalid grade level '{grade_raw}' "
                    f"(expected one of: {', '.join(valid_grade_codes)})"
                )
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
    from ai_tutor.apps.accounts.models import PlatformTerms
    active = PlatformTerms.active(locale=get_language())
    return render(request, 'accounts/terms.html', {
        'terms': active,
    })


@login_required
def terms_accept(request):
    """Interstitial — existing users with an outdated `terms_accepted_version`
    are routed here on next login. POST records acceptance and bounces
    them on to wherever they were headed."""
    from ai_tutor.apps.accounts.models import PlatformTerms, StudentProfile
    from django.utils import timezone as _tz

    active = PlatformTerms.active(locale=get_language())
    if not active:
        return redirect('tutoring:catalog')

    # Validate against the host allow-list, not a bare startswith('/'): a
    # protocol-relative value like "//evil.example" also starts with "/" and
    # would redirect off-site (open redirect / phishing vector on a page minors
    # reach). url_has_allowed_host_and_scheme rejects those.
    from django.utils.http import url_has_allowed_host_and_scheme
    next_url = request.GET.get('next') or request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
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
    from ai_tutor.apps.accounts.models import EmailVerificationToken, UserEmailStatus

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
    from ai_tutor.apps.accounts.models import EmailVerificationToken, UserEmailStatus
    from ai_tutor.apps.accounts.email_verification import send_verification_email

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
