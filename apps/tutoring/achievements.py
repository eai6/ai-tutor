"""
Achievement award service.

Checks active achievements by trigger_type and awards newly earned ones.
"""

import logging
from typing import List, Optional, Dict

from django.contrib.auth.models import User

from apps.tutoring.skills_models import Achievement, StudentAchievement, StudentKnowledgeProfile
from apps.tutoring.models import StudentLessonProgress

logger = logging.getLogger(__name__)


def check_and_award(student: User, event_type: str, context: Optional[Dict] = None) -> List[Achievement]:
    """Check all active achievements for event_type, award newly earned ones.

    Returns list of newly earned Achievement objects for notification.
    """
    context = context or {}
    newly_earned = []

    # Already earned achievement codes for this student
    earned_codes = set(
        StudentAchievement.objects.filter(student=student)
        .values_list('achievement__code', flat=True)
    )

    # Get candidate achievements matching this trigger type
    candidates = Achievement.objects.filter(trigger_type=event_type, is_active=True)

    for achievement in candidates:
        if achievement.code in earned_codes:
            continue

        if _check_trigger(student, achievement, context):
            StudentAchievement.objects.create(
                student=student,
                achievement=achievement,
                context=context,
            )
            # Award bonus XP if the achievement grants any
            if achievement.xp_reward > 0:
                profiles = StudentKnowledgeProfile.objects.filter(student=student)
                if profiles.exists():
                    profiles.first().add_xp(achievement.xp_reward, reason=f'achievement:{achievement.code}')

            newly_earned.append(achievement)
            logger.info(f"Achievement awarded: {student.username} earned '{achievement.name}'")

    return newly_earned


def _check_trigger(student: User, achievement: Achievement, context: Dict) -> bool:
    """Return True if the student qualifies for this achievement."""
    tt = achievement.trigger_type
    tv = achievement.trigger_value

    if tt == Achievement.TriggerType.FIRST_LESSON:
        mastered = StudentLessonProgress.objects.filter(
            student=student, mastery_level='mastered'
        ).count()
        return mastered >= 1

    elif tt == Achievement.TriggerType.LESSONS_COMPLETED:
        mastered = StudentLessonProgress.objects.filter(
            student=student, mastery_level='mastered'
        ).count()
        return mastered >= tv

    elif tt == Achievement.TriggerType.EXIT_TICKET_PASS:
        # Awarded when event fires — the event itself is the proof
        return True

    elif tt == Achievement.TriggerType.PERFECT_SCORE:
        score = context.get('score', 0)
        total = context.get('total', 0)
        return total > 0 and score == total

    elif tt == Achievement.TriggerType.STREAK_DAYS:
        max_streak = max(
            (p.current_streak_days for p in StudentKnowledgeProfile.objects.filter(student=student)),
            default=0
        )
        return max_streak >= tv

    elif tt == Achievement.TriggerType.XP_THRESHOLD:
        total_xp = sum(
            p.total_xp for p in StudentKnowledgeProfile.objects.filter(student=student)
        )
        return total_xp >= tv

    elif tt == Achievement.TriggerType.LEVEL_REACHED:
        total_xp = sum(
            p.total_xp for p in StudentKnowledgeProfile.objects.filter(student=student)
        )
        level = (total_xp // 1000) + 1
        return level >= tv

    return False
