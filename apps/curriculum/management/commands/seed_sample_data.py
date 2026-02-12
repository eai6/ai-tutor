"""
Management command to create sample data for development/testing.

Run with: python manage.py seed_sample_data
"""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.llm.models import PromptPack, ModelConfig


class Command(BaseCommand):
    help = 'Creates sample data for development'

    def handle(self, *args, **options):
        self.stdout.write('Creating sample data...')
        
        # 1. Create institution
        institution, _ = Institution.objects.get_or_create(
            slug='demo-school',
            defaults={
                'name': 'Demo School',
                'timezone': 'America/New_York',
            }
        )
        self.stdout.write(f'  ✓ Institution: {institution.name}')
        
        # 2. Create users and memberships
        admin_user, _ = User.objects.get_or_create(
            username='teacher1',
            defaults={'email': 'teacher@demo.com', 'first_name': 'Jane', 'last_name': 'Teacher'}
        )
        admin_user.set_password('teacher123')
        admin_user.save()
        
        Membership.objects.get_or_create(
            user=admin_user,
            institution=institution,
            defaults={'role': Membership.Role.TEACHER}
        )
        
        student_user, _ = User.objects.get_or_create(
            username='student1',
            defaults={'email': 'student@demo.com', 'first_name': 'Alex', 'last_name': 'Student'}
        )
        student_user.set_password('student123')
        student_user.save()
        
        Membership.objects.get_or_create(
            user=student_user,
            institution=institution,
            defaults={'role': Membership.Role.STUDENT}
        )
        self.stdout.write('  ✓ Users: teacher1, student1')
        
        # 3. Create PromptPack
        prompt_pack, _ = PromptPack.objects.get_or_create(
            institution=institution,
            name='Friendly Elementary Tutor',
            version=1,
            defaults={
                'system_prompt': '''You are a friendly, patient AI tutor helping elementary school students learn.
Your name is "Tutor" and you speak in a warm, encouraging tone.
Always celebrate effort and progress, not just correct answers.
Use simple language appropriate for young learners.''',
                'teaching_style_prompt': '''Teaching approach:
- Break concepts into small, digestible pieces
- Use concrete examples before abstract concepts
- Ask guiding questions rather than giving answers directly
- Provide one question at a time
- Use analogies to everyday objects and experiences''',
                'safety_prompt': '''Safety guidelines:
- Keep all content age-appropriate
- Never discuss violence, adult themes, or inappropriate content
- If asked about non-educational topics, gently redirect to the lesson
- Do not share personal information or ask for student's personal details''',
                'format_rules_prompt': '''Response format:
- Keep responses short (2-3 sentences for explanations)
- Use encouraging language ("Great try!", "You're getting it!")
- End teaching moments with a simple check-in question
- For wrong answers, give a hint before revealing the answer''',
                'is_active': True,
            }
        )
        self.stdout.write(f'  ✓ PromptPack: {prompt_pack.name}')
        
        # 4. Create ModelConfig
        model_config, _ = ModelConfig.objects.get_or_create(
            institution=institution,
            name='Default Claude',
            defaults={
                'provider': ModelConfig.Provider.ANTHROPIC,
                'model_name': 'claude-sonnet-4-20250514',
                'api_key_env_var': 'ANTHROPIC_API_KEY',
                'max_tokens': 1024,
                'temperature': 0.7,
                'is_active': True,
            }
        )
        self.stdout.write(f'  ✓ ModelConfig: {model_config.name}')
        
        # 5. Create sample course with lessons
        course, _ = Course.objects.get_or_create(
            institution=institution,
            title='Grade 3 Math: Addition',
            defaults={
                'description': 'Learn addition with carrying for two-digit numbers',
                'grade_level': 'Grade 3',
                'is_published': True,
            }
        )
        
        unit, _ = Unit.objects.get_or_create(
            course=course,
            title='Two-Digit Addition',
            defaults={'order_index': 0}
        )
        
        lesson, created = Lesson.objects.get_or_create(
            unit=unit,
            title='Adding Two-Digit Numbers (No Carrying)',
            defaults={
                'objective': 'Students will be able to add two-digit numbers without carrying',
                'estimated_minutes': 10,
                'mastery_rule': Lesson.MasteryRule.STREAK_3,
                'order_index': 0,
                'is_published': True,
            }
        )
        
        if created:
            # Create lesson steps
            steps_data = [
                {
                    'order_index': 0,
                    'step_type': LessonStep.StepType.TEACH,
                    'teacher_script': "Today we're going to learn how to add two-digit numbers! Let's start with something simple. When we add numbers like 23 + 14, we add the ones place first (3 + 4 = 7), then the tens place (2 + 1 = 3). So 23 + 14 = 37!",
                    'answer_type': LessonStep.AnswerType.NONE,
                },
                {
                    'order_index': 1,
                    'step_type': LessonStep.StepType.WORKED_EXAMPLE,
                    'teacher_script': "Let me show you another example: 42 + 35. First, I add the ones: 2 + 5 = 7. Then I add the tens: 4 + 3 = 7. So 42 + 35 = 77. Does that make sense?",
                    'question': "In the problem 42 + 35, what did we get when we added the ones place (2 + 5)?",
                    'answer_type': LessonStep.AnswerType.SHORT_NUMERIC,
                    'expected_answer': '7',
                    'hint_1': "Look at the ones place: 2 + 5 = ?",
                },
                {
                    'order_index': 2,
                    'step_type': LessonStep.StepType.PRACTICE,
                    'teacher_script': "Now it's your turn! Try this one:",
                    'question': "What is 21 + 34?",
                    'answer_type': LessonStep.AnswerType.SHORT_NUMERIC,
                    'expected_answer': '55',
                    'hint_1': "First add the ones place: 1 + 4 = ?",
                    'hint_2': "Then add the tens place: 2 + 3 = ?",
                    'hint_3': "Put them together: you should get a number in the 50s",
                    'max_attempts': 3,
                },
                {
                    'order_index': 3,
                    'step_type': LessonStep.StepType.PRACTICE,
                    'teacher_script': "Great effort! Here's another one:",
                    'question': "What is 52 + 26?",
                    'answer_type': LessonStep.AnswerType.SHORT_NUMERIC,
                    'expected_answer': '78',
                    'hint_1': "Ones place: 2 + 6 = ?",
                    'hint_2': "Tens place: 5 + 2 = ?",
                    'max_attempts': 3,
                },
                {
                    'order_index': 4,
                    'step_type': LessonStep.StepType.QUIZ,
                    'teacher_script': "Last one! You've got this:",
                    'question': "What is 43 + 31?",
                    'answer_type': LessonStep.AnswerType.MULTIPLE_CHOICE,
                    'choices': ['73', '74', '75', '64'],
                    'expected_answer': '74',
                    'max_attempts': 2,
                },
                {
                    'order_index': 5,
                    'step_type': LessonStep.StepType.SUMMARY,
                    'teacher_script': "Awesome work! Today you learned how to add two-digit numbers by adding the ones place first, then the tens place. Keep practicing and you'll be a pro!",
                    'answer_type': LessonStep.AnswerType.NONE,
                },
            ]
            
            for step_data in steps_data:
                LessonStep.objects.create(lesson=lesson, **step_data)
        
        self.stdout.write(f'  ✓ Course: {course.title}')
        self.stdout.write(f'  ✓ Lesson with {lesson.steps.count()} steps')
        
        self.stdout.write(self.style.SUCCESS('\n✅ Sample data created successfully!'))
        self.stdout.write('\nYou can now:')
        self.stdout.write('  - Login to admin: admin / admin123')
        self.stdout.write('  - Teacher account: teacher1 / teacher123')
        self.stdout.write('  - Student account: student1 / student123')
