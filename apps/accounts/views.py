"""
Authentication views - Register, Login, Logout
"""

from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.models import User
from django.contrib import messages
from apps.accounts.models import Institution, Membership, StudentProfile


def register_view(request):
    """Register a new student account."""
    if request.user.is_authenticated:
        return redirect('tutoring:catalog')
    
    # Get school choices for the form
    school_choices = StudentProfile.SCHOOL_CHOICES
    grade_choices = StudentProfile.GradeLevel.choices
    
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        password_confirm = request.POST.get('password_confirm', '')
        first_name = request.POST.get('first_name', '').strip()
        school = request.POST.get('school', '')
        grade_level = request.POST.get('grade_level', '')
        
        # Validation
        errors = []
        
        if not first_name:
            errors.append("Please enter your name.")
        
        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        
        if User.objects.filter(username=username).exists():
            errors.append("Username already taken.")
        
        if not email or '@' not in email:
            errors.append("Please enter a valid email.")
        
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        
        if password != password_confirm:
            errors.append("Passwords don't match.")
        
        if not school:
            errors.append("Please select your school.")
        
        if not grade_level:
            errors.append("Please select your grade level.")
        
        if errors:
            return render(request, 'accounts/register.html', {
                'errors': errors,
                'username': username,
                'email': email,
                'first_name': first_name,
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
        )
        
        # Create student profile with school and grade
        StudentProfile.objects.create(
            user=user,
            school=school,
            grade_level=grade_level,
        )
        
        # Auto-assign to Seychelles institution (or first active institution)
        institution = Institution.objects.filter(
            slug='seychelles-secondary',
            is_active=True
        ).first() or Institution.objects.filter(is_active=True).first()
        
        if institution:
            Membership.objects.create(
                user=user,
                institution=institution,
                role=Membership.Role.STUDENT,
                is_active=True,
            )
        
        # Log them in
        login(request, user)
        messages.success(request, f"Welcome, {first_name}! 🎉")
        return redirect('tutoring:catalog')
    
    return render(request, 'accounts/register.html', {
        'school_choices': school_choices,
        'grade_choices': grade_choices,
    })


def login_view(request):
    """Login page."""
    if request.user.is_authenticated:
        return redirect('tutoring:catalog')
    
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            login(request, user)
            next_url = request.GET.get('next', '/tutor/')
            return redirect(next_url)
        else:
            return render(request, 'accounts/login.html', {
                'error': "Invalid username or password.",
                'username': username,
            })
    
    return render(request, 'accounts/login.html')


def logout_view(request):
    """Logout and redirect to login."""
    logout(request)
    messages.info(request, "You've been logged out.")
    return redirect('accounts:login')
