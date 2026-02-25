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
            return redirect('tutoring:catalog')
        else:
            return render(request, 'accounts/student_login.html', {
                'error': "Invalid username or password.",
                'username': username,
            })
    
    return render(request, 'accounts/student_login.html')


def student_register(request):
    """Student self-registration."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)
    
    school_choices = PlatformConfig.get_school_choices()
    grade_choices = PlatformConfig.get_grade_choices()
    
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        school = request.POST.get('school', '')
        grade_level = request.POST.get('grade_level', '')
        
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
        
        if errors:
            return render(request, 'accounts/student_register.html', {
                'errors': errors,
                'username': username,
                'email': email,
                'first_name': first_name,
                'last_name': last_name,
                'school': school,
                'grade_level': grade_level,
                'school_choices': school_choices,
                'grade_choices': grade_choices,
            })
        
        # Create user
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )
        
        # Create student profile
        StudentProfile.objects.create(
            user=user,
            school=school,
            grade_level=grade_level,
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
        
        login(request, user)
        messages.success(request, f"Welcome, {first_name}! 🎉 Let's start learning!")
        return redirect('tutoring:catalog')
    
    return render(request, 'accounts/student_register.html', {
        'school_choices': school_choices,
        'grade_choices': grade_choices,
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
                return redirect('dashboard:home')
            else:
                return render(request, 'accounts/staff_login.html', {
                    'error': "You don't have staff access. Please use student login.",
                    'username': username,
                })
        else:
            return render(request, 'accounts/staff_login.html', {
                'error': "Invalid username or password.",
                'username': username,
            })
    
    return render(request, 'accounts/staff_login.html')


def staff_register(request, token=None):
    """
    Teacher/Admin registration via invitation token.
    Teachers cannot self-register - they must be invited.
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

        if email:
            messages.success(request, f"Invitation created for {email} at {institution.name}!")
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
