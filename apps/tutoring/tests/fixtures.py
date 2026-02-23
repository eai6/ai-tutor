"""
Shared test fixtures for tutoring tests.
Provides a BaseTutoringTestCase with institution, student, course, unit, lesson,
steps, exit ticket, skills, and prerequisites.
"""

from django.test import TestCase
from django.contrib.auth.models import User

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.tutoring.models import (
    TutorSession, SessionTurn, StudentLessonProgress,
    ExitTicket, ExitTicketQuestion,
)
from apps.tutoring.skills_models import (
    Skill, LessonPrerequisite, StudentSkillMastery,
    StudentKnowledgeProfile,
)


class BaseTutoringTestCase(TestCase):
    """Base test case with full tutoring infrastructure."""

    @classmethod
    def setUpTestData(cls):
        # Institution
        cls.institution = Institution.objects.create(
            name='Test School',
            slug='test-school',
        )

        # Users
        cls.student_user = User.objects.create_user(
            username='student1',
            password='testpass123',
        )
        cls.staff_user = User.objects.create_user(
            username='teacher1',
            password='testpass123',
        )

        # Memberships
        Membership.objects.create(
            user=cls.student_user,
            institution=cls.institution,
            role='student',
        )
        Membership.objects.create(
            user=cls.staff_user,
            institution=cls.institution,
            role='staff',
        )

        # Course > Unit > Lessons
        cls.course = Course.objects.create(
            institution=cls.institution,
            title='Grade 8 Science',
            grade_level='Grade 8',
            is_published=True,
        )
        cls.unit = Unit.objects.create(
            course=cls.course,
            title='Plate Tectonics',
            order_index=0,
        )
        cls.lesson = Lesson.objects.create(
            unit=cls.unit,
            title='Types of Plate Boundaries',
            objective='Identify and describe the three types of plate boundaries',
            order_index=0,
            is_published=True,
        )
        cls.prereq_lesson = Lesson.objects.create(
            unit=cls.unit,
            title='Earth Structure',
            objective='Understand layers of the earth',
            order_index=1,
            is_published=True,
        )

        # Lesson steps
        cls.step1 = LessonStep.objects.create(
            lesson=cls.lesson,
            order_index=0,
            step_type='teach',
            teacher_script='Plate boundaries are where tectonic plates meet.',
            question='',
            answer_type='none',
        )
        cls.step2 = LessonStep.objects.create(
            lesson=cls.lesson,
            order_index=1,
            step_type='practice',
            teacher_script='Let\'s practice identifying boundary types.',
            question='What type of boundary is formed when plates move apart?',
            answer_type='free_text',
            expected_answer='divergent',
        )

        # Exit ticket
        cls.exit_ticket = ExitTicket.objects.create(
            lesson=cls.lesson,
        )
        cls.exit_q1 = ExitTicketQuestion.objects.create(
            exit_ticket=cls.exit_ticket,
            question_text='What type of boundary forms mountains?',
            option_a='Divergent',
            option_b='Convergent',
            option_c='Transform',
            option_d='Subduction',
            correct_answer='B',
            explanation='Convergent boundaries push plates together forming mountains.',
            order_index=0,
        )
        cls.exit_q2 = ExitTicketQuestion.objects.create(
            exit_ticket=cls.exit_ticket,
            question_text='What happens at a divergent boundary?',
            option_a='Plates collide',
            option_b='Plates slide past',
            option_c='Plates move apart',
            option_d='Plates subduct',
            correct_answer='C',
            explanation='At divergent boundaries plates move apart creating new crust.',
            order_index=1,
        )

        # Skills
        cls.skill1 = Skill.objects.create(
            institution=cls.institution,
            code='identify_plate_boundaries',
            name='Identify Plate Boundaries',
            course=cls.course,
            difficulty='intermediate',
        )
        cls.skill1.lessons.add(cls.lesson)

        cls.skill2 = Skill.objects.create(
            institution=cls.institution,
            code='describe_convergent',
            name='Describe Convergent Boundaries',
            course=cls.course,
            difficulty='intermediate',
        )
        cls.skill2.lessons.add(cls.lesson)

        cls.prereq_skill = Skill.objects.create(
            institution=cls.institution,
            code='earth_layers',
            name='Identify Earth Layers',
            course=cls.course,
            difficulty='foundational',
        )
        cls.prereq_skill.lessons.add(cls.prereq_lesson)

        # Prerequisite relationships
        cls.skill1.prerequisites.add(cls.prereq_skill)

        # Lesson prerequisite
        cls.lesson_prereq = LessonPrerequisite.objects.create(
            lesson=cls.lesson,
            prerequisite=cls.prereq_lesson,
            is_direct=True,
            strength=0.8,
        )

    def _create_session(self, **kwargs):
        """Create a TutorSession with sensible defaults."""
        defaults = {
            'institution': self.institution,
            'student': self.student_user,
            'lesson': self.lesson,
            'status': 'active',
            'engine_state': {},
        }
        defaults.update(kwargs)
        return TutorSession.objects.create(**defaults)

    def _create_mastery(self, student=None, skill=None, level=0.5, **kwargs):
        """Create a StudentSkillMastery record."""
        return StudentSkillMastery.objects.create(
            student=student or self.student_user,
            skill=skill or self.skill1,
            mastery_level=level,
            **kwargs,
        )

    def _create_progress(self, student=None, lesson=None, mastery_level='mastered'):
        """Create a StudentLessonProgress record."""
        return StudentLessonProgress.objects.create(
            institution=self.institution,
            student=student or self.student_user,
            lesson=lesson or self.prereq_lesson,
            mastery_level=mastery_level,
        )
