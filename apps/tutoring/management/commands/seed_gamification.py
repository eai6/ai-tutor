"""
Seed default tutor personalities and achievements.

Usage: python manage.py seed_gamification
"""

from django.core.management.base import BaseCommand


PERSONALITIES = [
    {
        'name': 'Friendly',
        'emoji': '\U0001f60a',  # 😊
        'description': 'Warm and supportive, like chatting with a helpful friend',
        'system_prompt_modifier': (
            'You are warm, friendly, and supportive. Use an encouraging and conversational tone, '
            'like chatting with a helpful friend. Celebrate small wins. Use phrases like '
            '"Great question!", "You\'re doing awesome!", and "Let\'s figure this out together!"'
        ),
        'is_active': True,
        'sort_order': 1,
    },
    {
        'name': 'Funny',
        'emoji': '\U0001f602',  # 😂
        'description': 'Uses humor and jokes to make learning fun',
        'system_prompt_modifier': (
            'You have a great sense of humor. Use jokes, puns, funny analogies, and lighthearted '
            'comments to make learning fun. Keep the humor age-appropriate and related to the topic '
            'when possible. Make students laugh while they learn!'
        ),
        'is_active': True,
        'sort_order': 2,
    },
    {
        'name': 'Encouraging',
        'emoji': '\U0001f4aa',  # 💪
        'description': 'Like a coach \u2014 motivating and building confidence',
        'system_prompt_modifier': (
            'You are like a motivating coach. Be energetic and empowering. Build the student\'s '
            'confidence with phrases like "You\'ve got this!", "I believe in you!", and '
            '"Look how far you\'ve come!" Frame mistakes as learning opportunities and '
            'celebrate effort and persistence.'
        ),
        'is_active': True,
        'sort_order': 3,
    },
    {
        'name': 'Storyteller',
        'emoji': '\U0001f4d6',  # 📖
        'description': 'Explains through stories and real-world examples',
        'system_prompt_modifier': (
            'You love telling stories! Explain concepts through engaging narratives, real-world '
            'examples, and creative analogies. Start explanations with "Imagine..." or '
            '"Let me tell you a story..." Make abstract ideas vivid and memorable through storytelling.'
        ),
        'is_active': True,
        'sort_order': 4,
    },
    {
        'name': 'Chill',
        'emoji': '\U0001f60e',  # 😎
        'description': 'Relaxed and cool, no pressure',
        'system_prompt_modifier': (
            'You are relaxed, cool, and laid-back. No pressure, no stress. Use a casual tone and '
            'let the student know it\'s totally fine to take their time. Say things like '
            '"No worries!", "Take your time", and "It\'s all good." Keep things low-key and chill.'
        ),
        'is_active': True,
        'sort_order': 5,
    },
]

ACHIEVEMENTS = [
    {
        'code': 'first_lesson', 'name': 'First Steps', 'emoji': '\U0001f3af',
        'description': 'Complete your first lesson',
        'category': 'milestone', 'trigger_type': 'first_lesson', 'trigger_value': 0,
        'xp_reward': 50, 'sort_order': 1,
    },
    {
        'code': 'lessons_5', 'name': 'Quick Learner', 'emoji': '\U0001f4da',
        'description': 'Complete 5 lessons',
        'category': 'milestone', 'trigger_type': 'lessons_completed', 'trigger_value': 5,
        'xp_reward': 100, 'sort_order': 2,
    },
    {
        'code': 'lessons_10', 'name': 'Knowledge Seeker', 'emoji': '\U0001f9e0',
        'description': 'Complete 10 lessons',
        'category': 'milestone', 'trigger_type': 'lessons_completed', 'trigger_value': 10,
        'xp_reward': 200, 'sort_order': 3,
    },
    {
        'code': 'lessons_25', 'name': 'Scholar', 'emoji': '\U0001f393',
        'description': 'Complete 25 lessons',
        'category': 'milestone', 'trigger_type': 'lessons_completed', 'trigger_value': 25,
        'xp_reward': 500, 'sort_order': 4,
    },
    {
        'code': 'perfect_score', 'name': 'Perfect Score', 'emoji': '\U0001f4af',
        'description': 'Get a perfect score on an exit ticket',
        'category': 'mastery', 'trigger_type': 'perfect_score', 'trigger_value': 0,
        'xp_reward': 100, 'sort_order': 5,
    },
    {
        'code': 'streak_3', 'name': 'Getting Started', 'emoji': '\U0001f525',
        'description': '3-day learning streak',
        'category': 'streak', 'trigger_type': 'streak_days', 'trigger_value': 3,
        'xp_reward': 50, 'sort_order': 6,
    },
    {
        'code': 'streak_7', 'name': 'Week Warrior', 'emoji': '\U0001f525',
        'description': '7-day learning streak',
        'category': 'streak', 'trigger_type': 'streak_days', 'trigger_value': 7,
        'xp_reward': 150, 'sort_order': 7,
    },
    {
        'code': 'streak_14', 'name': 'Unstoppable', 'emoji': '\u26a1',
        'description': '14-day learning streak',
        'category': 'streak', 'trigger_type': 'streak_days', 'trigger_value': 14,
        'xp_reward': 300, 'sort_order': 8,
    },
    {
        'code': 'streak_30', 'name': 'Legendary', 'emoji': '\U0001f3c6',
        'description': '30-day learning streak',
        'category': 'streak', 'trigger_type': 'streak_days', 'trigger_value': 30,
        'xp_reward': 500, 'sort_order': 9,
    },
    {
        'code': 'xp_1000', 'name': 'Level 2 Unlocked', 'emoji': '\u2b50',
        'description': 'Earn 1,000 XP',
        'category': 'milestone', 'trigger_type': 'xp_threshold', 'trigger_value': 1000,
        'xp_reward': 0, 'sort_order': 10,
    },
    {
        'code': 'xp_5000', 'name': 'XP Master', 'emoji': '\U0001f31f',
        'description': 'Earn 5,000 XP',
        'category': 'milestone', 'trigger_type': 'xp_threshold', 'trigger_value': 5000,
        'xp_reward': 0, 'sort_order': 11,
    },
    {
        'code': 'level_5', 'name': 'Rising Star', 'emoji': '\U0001f31f',
        'description': 'Reach level 5',
        'category': 'milestone', 'trigger_type': 'level_reached', 'trigger_value': 5,
        'xp_reward': 100, 'sort_order': 12,
    },
    {
        'code': 'level_10', 'name': 'Academic Champion', 'emoji': '\U0001f451',
        'description': 'Reach level 10',
        'category': 'milestone', 'trigger_type': 'level_reached', 'trigger_value': 10,
        'xp_reward': 250, 'sort_order': 13,
    },
    {
        'code': 'exit_ticket_pass', 'name': 'Quiz Ace', 'emoji': '\u2705',
        'description': 'Pass an exit ticket',
        'category': 'milestone', 'trigger_type': 'exit_ticket_pass', 'trigger_value': 0,
        'xp_reward': 0, 'sort_order': 14,
    },
]


class Command(BaseCommand):
    help = 'Seed default tutor personalities and achievements'

    def handle(self, *args, **options):
        self._seed_personalities()
        self._seed_achievements()
        self.stdout.write(self.style.SUCCESS('Done.'))

    def _seed_personalities(self):
        from apps.accounts.models import TutorPersonality

        created = 0
        for data in PERSONALITIES:
            _, was_created = TutorPersonality.objects.update_or_create(
                name=data['name'],
                defaults=data,
            )
            if was_created:
                created += 1

        self.stdout.write(f'Personalities: {created} created, {len(PERSONALITIES) - created} updated')

    def _seed_achievements(self):
        from apps.tutoring.skills_models import Achievement

        created = 0
        for data in ACHIEVEMENTS:
            _, was_created = Achievement.objects.update_or_create(
                code=data['code'],
                defaults=data,
            )
            if was_created:
                created += 1

        self.stdout.write(f'Achievements: {created} created, {len(ACHIEVEMENTS) - created} updated')
