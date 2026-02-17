"""
Safety & Privacy Module for AI Tutor

This module implements:
1. Content Safety - Filter harmful/inappropriate content
2. Child Protection - COPPA compliance, age-appropriate interactions
3. Data Privacy - GDPR/FERPA compliance, data minimization
4. Rate Limiting - Prevent abuse
5. Audit Logging - Compliance tracking

All AI interactions pass through these safety checks.
"""

import re
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================

class SafetyConfig:
    """Central configuration for safety settings."""
    
    # Rate limits (per user)
    MAX_MESSAGES_PER_MINUTE = 10
    MAX_MESSAGES_PER_HOUR = 100
    MAX_MESSAGES_PER_DAY = 500
    MAX_SESSIONS_PER_DAY = 20
    
    # Content limits
    MAX_MESSAGE_LENGTH = 2000
    MAX_INPUT_TOKENS = 500
    
    # Data retention (days)
    CONVERSATION_RETENTION_DAYS = 90
    AUDIT_LOG_RETENTION_DAYS = 365
    ANONYMIZE_AFTER_DAYS = 30
    
    # Child protection
    MIN_AGE_FOR_ACCOUNT = 13  # COPPA
    REQUIRE_PARENTAL_CONSENT_UNDER = 16  # GDPR


# ============================================================================
# Content Safety
# ============================================================================

class ContentFlag(Enum):
    """Types of content flags."""
    SAFE = "safe"
    PERSONAL_INFO = "personal_info"
    INAPPROPRIATE = "inappropriate"
    HARMFUL = "harmful"
    OFF_TOPIC = "off_topic"
    MANIPULATION = "manipulation"


@dataclass
class SafetyCheckResult:
    """Result of a safety check."""
    is_safe: bool
    flags: List[ContentFlag]
    filtered_content: str
    warnings: List[str]
    blocked: bool = False
    block_reason: Optional[str] = None


class ContentSafetyFilter:
    """
    Filters and checks content for safety issues.
    """
    
    # Patterns that indicate personal info
    PII_PATTERNS = [
        (r'\b\d{3}[-.]?\d{2}[-.]?\d{4}\b', 'SSN'),
        (r'\b\d{16}\b', 'credit card'),
        (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', 'email'),
        (r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', 'phone number'),
        (r'\b\d{1,5}\s+\w+\s+(street|st|avenue|ave|road|rd|drive|dr)\b', 'address'),
    ]
    
    # Patterns that indicate harmful content
    HARMFUL_PATTERNS = [
        r'\b(kill|hurt|harm|attack|weapon)\s+(myself|yourself|someone|people)\b',
        r'\b(suicide|self.?harm)\b',
        r'\b(bomb|explosive|poison)\s+(make|build|create)\b',
    ]
    
    # Patterns indicating manipulation/jailbreak attempts
    MANIPULATION_PATTERNS = [
        r'ignore\s+(previous|all|your)\s+(instructions|rules|guidelines)',
        r'pretend\s+(you\'?re|to\s+be)\s+(not|a|an)',
        r'act\s+as\s+if\s+(you|there)\s+(don\'?t|are\s+no)',
        r'bypass\s+(safety|content|filter)',
        r'(DAN|jailbreak|developer\s+mode)',
        r'from\s+now\s+on\s+(you\s+will|ignore)',
    ]
    
    # Off-topic patterns (for educational context)
    OFF_TOPIC_PATTERNS = [
        r'\b(dating|relationship|boyfriend|girlfriend)\b',
        r'\b(gambling|betting|casino)\b',
        r'\b(drugs|alcohol|smoking)\b(?!\s*(education|awareness|prevention))',
    ]
    
    @classmethod
    def check_content(cls, content: str, context: str = "student_input") -> SafetyCheckResult:
        """Check content for safety issues."""
        flags = []
        warnings = []
        filtered_content = content
        blocked = False
        block_reason = None
        
        # Check for PII
        for pattern, pii_type in cls.PII_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                flags.append(ContentFlag.PERSONAL_INFO)
                warnings.append(f"Detected possible {pii_type}")
                filtered_content = re.sub(pattern, f"[REDACTED {pii_type.upper()}]", filtered_content, flags=re.IGNORECASE)
        
        # Check for harmful content
        for pattern in cls.HARMFUL_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                flags.append(ContentFlag.HARMFUL)
                warnings.append("Detected potentially harmful content")
                blocked = True
                block_reason = "Content flagged for safety review"
                break
        
        # Check for manipulation attempts
        if context == "student_input":
            for pattern in cls.MANIPULATION_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    flags.append(ContentFlag.MANIPULATION)
                    warnings.append("Detected potential manipulation attempt")
                    break
        
        # Check for off-topic content
        for pattern in cls.OFF_TOPIC_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                flags.append(ContentFlag.OFF_TOPIC)
                warnings.append("Content may be off-topic for educational context")
                break
        
        # Check message length
        if len(content) > SafetyConfig.MAX_MESSAGE_LENGTH:
            warnings.append(f"Message exceeds {SafetyConfig.MAX_MESSAGE_LENGTH} characters")
            filtered_content = content[:SafetyConfig.MAX_MESSAGE_LENGTH] + "... [truncated]"
        
        is_safe = len(flags) == 0 or (
            ContentFlag.HARMFUL not in flags and 
            ContentFlag.MANIPULATION not in flags
        )
        
        return SafetyCheckResult(
            is_safe=is_safe,
            flags=flags,
            filtered_content=filtered_content,
            warnings=warnings,
            blocked=blocked,
            block_reason=block_reason,
        )
    
    @classmethod
    def get_safe_response(cls, flag: ContentFlag) -> str:
        """Get appropriate response for flagged content."""
        responses = {
            ContentFlag.PERSONAL_INFO: (
                "I noticed you shared some personal information. "
                "For your safety, please don't share personal details like emails, "
                "phone numbers, or addresses. Let's focus on the lesson!"
            ),
            ContentFlag.HARMFUL: (
                "I'm concerned about what you've shared. If you're going through "
                "a difficult time, please talk to a trusted adult, teacher, or counselor. "
                "You can also reach out to a helpline for support."
            ),
            ContentFlag.MANIPULATION: (
                "I'm here to help you learn! Let's stay focused on the lesson. "
                "What questions do you have about the topic we're studying?"
            ),
            ContentFlag.OFF_TOPIC: (
                "That's a bit outside what we're learning today. "
                "Let's get back to the lesson - I'm here to help you succeed!"
            ),
        }
        return responses.get(flag, "Let's focus on learning together!")


# ============================================================================
# Child Protection
# ============================================================================

class ChildProtection:
    """Implements child protection measures."""
    
    @staticmethod
    def check_age_compliance(user) -> Tuple[bool, Optional[str]]:
        """Check if user meets age requirements."""
        from django.utils import timezone
        
        profile = getattr(user, 'profile', None)
        if not profile or not getattr(profile, 'date_of_birth', None):
            return True, None  # Can't verify, assume compliant
        
        age = (timezone.now().date() - profile.date_of_birth).days // 365
        
        if age < SafetyConfig.MIN_AGE_FOR_ACCOUNT:
            return False, f"User must be at least {SafetyConfig.MIN_AGE_FOR_ACCOUNT} years old"
        
        if age < SafetyConfig.REQUIRE_PARENTAL_CONSENT_UNDER:
            if not getattr(profile, 'parental_consent_given', False):
                return False, "Parental consent required for users under 16"
        
        return True, None
    
    @staticmethod
    def get_age_appropriate_system_prompt(user) -> str:
        """Get additional system prompt for age-appropriate interaction."""
        return """

CHILD SAFETY GUIDELINES:

You are tutoring a student. Always:
1. Use age-appropriate language and examples
2. Never discuss violence, adult content, or inappropriate topics
3. Encourage the student to talk to a trusted adult if they share personal problems
4. Do not ask for or store personal information
5. Maintain professional, supportive boundaries
6. If a student seems distressed, suggest they speak with a teacher or counselor
7. Focus solely on educational content

If a student asks about inappropriate topics, gently redirect:
"That's not something I can help with, but I'm happy to help you with your studies!"

If a student shares concerning information about their wellbeing:
"I'm glad you trust me, but I'm an AI tutor. Please talk to a parent, teacher, or counselor about this - they can really help."
"""
    
    @staticmethod
    def filter_ai_response_for_children(response: str) -> str:
        """Filter AI response to ensure it's appropriate for children."""
        result = ContentSafetyFilter.check_content(response, context="ai_output")
        
        if not result.is_safe:
            logger.warning(f"AI response flagged: {result.warnings}")
            return (
                "I want to make sure I'm helping you appropriately. "
                "Let's continue with the lesson - what would you like to learn about?"
            )
        
        return result.filtered_content


# ============================================================================
# Rate Limiting
# ============================================================================

class RateLimiter:
    """Rate limiting to prevent abuse."""
    
    @staticmethod
    def _get_cache_key(user_id: int, window: str) -> str:
        """Generate cache key for rate limiting."""
        return f"ratelimit:{user_id}:{window}"
    
    @classmethod
    def check_rate_limit(cls, user_id: int) -> Tuple[bool, Optional[str]]:
        """Check if user is within rate limits."""
        from django.core.cache import cache
        from django.utils import timezone
        
        now = timezone.now()
        
        # Check per-minute limit
        minute_key = cls._get_cache_key(user_id, now.strftime("%Y%m%d%H%M"))
        minute_count = cache.get(minute_key, 0)
        if minute_count >= SafetyConfig.MAX_MESSAGES_PER_MINUTE:
            return False, "Too many messages. Please wait a moment."
        
        # Check per-hour limit
        hour_key = cls._get_cache_key(user_id, now.strftime("%Y%m%d%H"))
        hour_count = cache.get(hour_key, 0)
        if hour_count >= SafetyConfig.MAX_MESSAGES_PER_HOUR:
            return False, "Hourly limit reached. Please take a break."
        
        # Check per-day limit
        day_key = cls._get_cache_key(user_id, now.strftime("%Y%m%d"))
        day_count = cache.get(day_key, 0)
        if day_count >= SafetyConfig.MAX_MESSAGES_PER_DAY:
            return False, "Daily limit reached. Come back tomorrow!"
        
        return True, None
    
    @classmethod
    def record_message(cls, user_id: int):
        """Record a message for rate limiting."""
        from django.core.cache import cache
        from django.utils import timezone
        
        now = timezone.now()
        
        minute_key = cls._get_cache_key(user_id, now.strftime("%Y%m%d%H%M"))
        cache.set(minute_key, cache.get(minute_key, 0) + 1, timeout=60)
        
        hour_key = cls._get_cache_key(user_id, now.strftime("%Y%m%d%H"))
        cache.set(hour_key, cache.get(hour_key, 0) + 1, timeout=3600)
        
        day_key = cls._get_cache_key(user_id, now.strftime("%Y%m%d"))
        cache.set(day_key, cache.get(day_key, 0) + 1, timeout=86400)


def rate_limit_required(view_func):
    """Decorator to enforce rate limiting on views."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        from django.http import JsonResponse
        
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)
        
        allowed, reason = RateLimiter.check_rate_limit(request.user.id)
        if not allowed:
            return JsonResponse({"error": reason, "rate_limited": True}, status=429)
        
        RateLimiter.record_message(request.user.id)
        return view_func(request, *args, **kwargs)
    
    return wrapper


# ============================================================================
# Data Privacy
# ============================================================================

class DataPrivacy:
    """Data privacy and GDPR/FERPA compliance."""
    
    @staticmethod
    def anonymize_user_id(user_id: int) -> str:
        """Create anonymized user identifier."""
        from django.conf import settings
        salt = getattr(settings, 'SECRET_KEY', 'default-salt')[:16]
        return hashlib.sha256(f"{salt}{user_id}".encode()).hexdigest()[:16]
    
    @staticmethod
    def anonymize_content(content: str) -> str:
        """Remove PII from content for analytics/logging."""
        result = ContentSafetyFilter.check_content(content)
        return result.filtered_content
    
    @staticmethod
    def get_data_retention_date():
        """Get the date before which data should be deleted."""
        from django.utils import timezone
        return timezone.now() - timedelta(days=SafetyConfig.CONVERSATION_RETENTION_DAYS)
    
    @classmethod
    def cleanup_old_data(cls):
        """Clean up data older than retention period."""
        from apps.tutoring.models import SessionTurn, TutorSession
        from apps.safety.models import SafetyAuditLog
        from django.utils import timezone
        
        retention_date = cls.get_data_retention_date()
        
        old_turns = SessionTurn.objects.filter(created_at__lt=retention_date)
        turns_deleted = old_turns.count()
        old_turns.delete()
        
        old_sessions = TutorSession.objects.filter(
            started_at__lt=retention_date,
            status='completed'
        )
        sessions_deleted = old_sessions.count()
        old_sessions.delete()
        
        audit_retention = timezone.now() - timedelta(days=SafetyConfig.AUDIT_LOG_RETENTION_DAYS)
        old_logs = SafetyAuditLog.objects.filter(timestamp__lt=audit_retention)
        logs_deleted = old_logs.count()
        old_logs.delete()
        
        logger.info(f"Data cleanup: {turns_deleted} turns, {sessions_deleted} sessions, {logs_deleted} audit logs deleted")
        
        return {
            'turns_deleted': turns_deleted,
            'sessions_deleted': sessions_deleted,
            'logs_deleted': logs_deleted,
        }
    
    @staticmethod
    def export_user_data(user) -> Dict:
        """Export all user data for GDPR data portability."""
        from apps.tutoring.models import TutorSession, StudentLessonProgress
        
        sessions = TutorSession.objects.filter(student=user)
        
        export_data = {
            'user': {
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'date_joined': user.date_joined.isoformat(),
            },
            'sessions': [],
            'progress': [],
        }
        
        for session in sessions:
            session_data = {
                'lesson': session.lesson.title,
                'started_at': session.started_at.isoformat(),
                'ended_at': session.ended_at.isoformat() if session.ended_at else None,
                'status': session.status,
                'mastery_achieved': session.mastery_achieved,
                'turns': [],
            }
            
            for turn in session.turns.all():
                session_data['turns'].append({
                    'role': turn.role,
                    'content': turn.content,
                    'timestamp': turn.created_at.isoformat(),
                })
            
            export_data['sessions'].append(session_data)
        
        progress = StudentLessonProgress.objects.filter(student=user)
        for p in progress:
            export_data['progress'].append({
                'lesson': p.lesson.title,
                'mastery_level': p.mastery_level,
                'total_attempts': p.total_attempts,
                'total_correct': p.total_correct,
            })
        
        return export_data
    
    @staticmethod
    def delete_user_data(user, keep_anonymized: bool = True):
        """Delete all user data for GDPR right to erasure."""
        from apps.tutoring.models import TutorSession, SessionTurn, StudentLessonProgress
        
        if keep_anonymized:
            sessions = TutorSession.objects.filter(student=user)
            for session in sessions:
                for turn in session.turns.all():
                    turn.content = DataPrivacy.anonymize_content(turn.content)
                    turn.save()
        else:
            SessionTurn.objects.filter(session__student=user).delete()
            TutorSession.objects.filter(student=user).delete()
        
        StudentLessonProgress.objects.filter(student=user).delete()
        
        logger.info(f"User data {'anonymized' if keep_anonymized else 'deleted'} for user {user.id}")


# ============================================================================
# Audit Logging Helper
# ============================================================================

class SafetyAuditLog:
    """Helper class for audit logging (uses the model)."""
    
    @classmethod
    def log(cls, event_type: str, user=None, session_id: int = None, 
            details: dict = None, severity: str = 'info', request=None):
        """Create an audit log entry."""
        from apps.safety.models import SafetyAuditLog as AuditLogModel
        
        log_entry = AuditLogModel(
            event_type=event_type,
            user_id=user.id if user else None,
            user_hash=DataPrivacy.anonymize_user_id(user.id) if user else '',
            session_id=session_id,
            details=details or {},
            severity=severity,
        )
        
        if request:
            log_entry.ip_address = cls._get_client_ip(request)
            log_entry.user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]
        
        log_entry.save()
        
        log_message = f"Safety Event: {event_type} | User: {log_entry.user_hash} | Details: {details}"
        if severity == 'critical':
            logger.critical(log_message)
        elif severity == 'warning':
            logger.warning(log_message)
        else:
            logger.info(log_message)
        
        return log_entry
    
    @staticmethod
    def _get_client_ip(request):
        """Get client IP address from request."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR')


# ============================================================================
# Safety Middleware
# ============================================================================

class SafetyMiddleware:
    """Django middleware for safety checks."""
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        if request.path.startswith('/tutor/api/'):
            if request.user.is_authenticated:
                compliant, reason = ChildProtection.check_age_compliance(request.user)
                if not compliant:
                    from django.http import JsonResponse
                    SafetyAuditLog.log(
                        'age_check',
                        user=request.user,
                        details={'reason': reason},
                        severity='warning',
                        request=request,
                    )
                    return JsonResponse({'error': reason}, status=403)
        
        response = self.get_response(request)
        return response