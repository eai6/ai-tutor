"""
Accounts app - Institution and Membership models.

Multi-tenancy pattern: Every record in the system is tied to an Institution.
Users can belong to multiple institutions with different roles.
"""

from django.db import models
from django.contrib.auth.models import User


class Institution(models.Model):
    """
    Top-level tenant. Schools, organizations, etc.
    All data is scoped to an institution.
    """
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, help_text="URL-friendly identifier")
    timezone = models.CharField(max_length=50, default='UTC')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']


class Membership(models.Model):
    """
    Links users to institutions with a specific role.
    A user can have different roles in different institutions.
    """
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Institution Admin'
        TEACHER = 'teacher', 'Teacher'
        EDITOR = 'editor', 'Content Editor'
        STUDENT = 'student', 'Student'

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='memberships'
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.STUDENT
    )
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # A user can only have one role per institution
        unique_together = ['user', 'institution']
        ordering = ['institution', 'user']

    def __str__(self):
        return f"{self.user.username} - {self.institution.name} ({self.role})"

    @property
    def is_staff(self):
        """Returns True if user has admin, teacher, or editor role."""
        return self.role in [self.Role.ADMIN, self.Role.TEACHER, self.Role.EDITOR]
