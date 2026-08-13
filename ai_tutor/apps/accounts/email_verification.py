"""Email verification helpers — token issue + send.

Usage:
    from ai_tutor.apps.accounts.email_verification import send_verification_email
    send_verification_email(request, user)

Soft gate: an unverified user can still log in. The verify link
flips UserEmailStatus.verified_at; the dashboard banner reads from
that field. Failures to send don't block sign-up.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from ai_tutor.apps.accounts.models import EmailVerificationToken, PlatformConfig

logger = logging.getLogger(__name__)


def send_verification_email(request, user) -> bool:
    """Mint a fresh token and email the verify link to `user.email`.

    Returns True on success, False on any failure (no email, send
    error, etc.). Failure is non-fatal — sign-up continues either
    way per the soft-gate decision.
    """
    if not user.email:
        return False

    token = EmailVerificationToken.issue(user)
    verify_url = request.build_absolute_uri(
        reverse('accounts:verify_email', args=[token.token])
    )
    platform_name = PlatformConfig.load().platform_name or 'AI Tutor'
    first_name = user.first_name or user.username

    context = {
        'first_name': first_name,
        'verify_url': verify_url,
        'platform_name': platform_name,
    }

    text_body = render_to_string('email/verify_email.txt', context)
    try:
        html_body = render_to_string('email/verify_email.html', context)
    except Exception:
        html_body = None  # template optional

    try:
        send_mail(
            subject=f"Verify your email — {platform_name}",
            message=text_body,
            html_message=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info(f"verification email sent to {user.email}")
        return True
    except Exception as e:
        logger.warning(f"verification email to {user.email} failed: {e}")
        return False
