"""
Personalized Learning Services

This module provides services for personalized tutoring based on
the science of learning principles:

1. RetrievalService - Selects personalized review questions
2. SpacedRepetitionService - Manages review scheduling
3. SkillAssessmentService - Evaluates skill mastery
4. RemediationService - Handles targeted remediation
"""

import logging
import random
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Q, F, Avg, Count
from django.utils import timezone

from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.tutoring.skills_models import (
    Skill, StudentSkillMastery, LessonPrerequisite,
    SkillPracticeLog, StudentKnowledgeProfile
)

logger = logging.getLogger(__name__)


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class RetrievalQuestion:
    """A question selected for retrieval practice."""
    skill: Skill
    lesson_step: LessonStep
    question_text: str
    choices: List[str]
    expected_answer: str
    source_lesson: str
    priority_reason: str
    retention_estimate: float
    mastery_record: Optional[StudentSkillMastery] = None


@dataclass
class SkillAssessment:
    """Assessment result for a skill."""
    skill: Skill
    was_correct: bool
    quality: int  # 0-5
    time_taken_seconds: int
    hints_used: int
    mastery_before: float
    mastery_after: float


@dataclass
class SessionPersonalization:
    """Personalization data for a tutoring session."""
    retrieval_questions: List[RetrievalQuestion] = field(default_factory=list)
    interleaved_reviews: List[RetrievalQuestion] = field(default_factory=list)
    weak_skills: List[Skill] = field(default_factory=list)
    strong_skills: List[Skill] = field(default_factory=list)
    recommended_pace: str = "normal"  # slow, normal, fast
    personalized_hints: Dict[str, str] = field(default_factory=dict)


# =============================================================================
# RETRIEVAL SERVICE - Personalized Review Questions
# =============================================================================

class RetrievalService:
    """
    Selects personalized retrieval questions for a student starting a lesson.
    
    This is the core of embedded spaced repetition - review questions are
    woven into the forward learning flow rather than requiring separate
    review sessions.
    
    Selection criteria (in priority order):
    1. Skills due for spaced review
    2. Prerequisites for the current lesson
    3. Skills with low retention estimates
    4. Recently learned skills needing consolidation
    """
    
    def __init__(self, student, lesson: Lesson):
        self.student = student
        self.lesson = lesson
        self.course = lesson.unit.course
    
    def get_retrieval_questions(self, count: int = 3) -> List[RetrievalQuestion]:
        """
        Get personalized retrieval questions for the session start.
        
        Args:
            count: Number of questions to return (2-4 recommended)
        
        Returns:
            List of RetrievalQuestion objects
        """
        candidates = self._get_review_candidates()
        
        if not candidates:
            logger.info(f"No retrieval candidates for {self.student} on {self.lesson}")
            return []
        
        # Sort by priority score
        candidates.sort(key=lambda x: x['priority'], reverse=True)
        
        # Select top candidates, ensuring variety
        selected = self._select_diverse_questions(candidates, count)
        
        # Build RetrievalQuestion objects
        questions = []
        for candidate in selected:
            question = self._build_retrieval_question(candidate)
            if question:
                questions.append(question)
        
        logger.info(
            f"Selected {len(questions)} retrieval questions for "
            f"{self.student} starting {self.lesson.title}"
        )
        
        return questions
    
    def _get_review_candidates(self) -> List[Dict]:
        """Get all candidate skills for review with their priorities."""
        candidates = []
        
        # Get all skills from previous lessons in this course
        previous_lessons = Lesson.objects.filter(
            unit__course=self.course,
        ).exclude(
            id=self.lesson.id
        ).filter(
            # Only lessons that come before (by unit then lesson order)
            Q(unit__order_index__lt=self.lesson.unit.order_index) |
            Q(
                unit__order_index=self.lesson.unit.order_index,
                order_index__lt=self.lesson.order_index
            )
        )
        
        # Get student's mastery records for skills in these lessons
        skill_ids = Skill.objects.filter(
            lessons__in=previous_lessons
        ).values_list('id', flat=True).distinct()
        
        mastery_records = StudentSkillMastery.objects.filter(
            student=self.student,
            skill_id__in=skill_ids,
            mastery_level__gt=0  # Only skills they've started learning
        ).select_related('skill', 'skill__primary_lesson')
        
        # Get prerequisites for current lesson
        prereq_lessons = LessonPrerequisite.objects.filter(
            lesson=self.lesson
        ).values_list('prerequisite_id', flat=True)
        
        prereq_skills = set(
            Skill.objects.filter(
                primary_lesson_id__in=prereq_lessons
            ).values_list('id', flat=True)
        )
        
        for mastery in mastery_records:
            priority, reason = self._calculate_priority(mastery, prereq_skills)
            
            if priority > 0:
                candidates.append({
                    'mastery': mastery,
                    'skill': mastery.skill,
                    'priority': priority,
                    'reason': reason,
                    'retention': mastery.calculate_retention(),
                })
        
        return candidates
    
    def _calculate_priority(
        self, 
        mastery: StudentSkillMastery, 
        prereq_skills: set
    ) -> Tuple[float, str]:
        """
        Calculate review priority for a skill.
        
        Returns (priority_score, reason_string)
        """
        priority = 0.0
        reasons = []
        
        # Factor 1: Due for spaced review (highest priority)
        if mastery.is_due_for_review():
            days_overdue = -mastery.days_until_review()
            priority += 50 + min(days_overdue * 10, 50)
            reasons.append(f"Due for review ({days_overdue} days overdue)")
        
        # Factor 2: Prerequisite for current lesson
        if mastery.skill_id in prereq_skills:
            priority += 40
            reasons.append("Prerequisite for this lesson")
        
        # Factor 3: Low retention estimate
        retention = mastery.calculate_retention()
        if retention < 0.7:
            priority += (1 - retention) * 35
            reasons.append(f"Low retention ({retention:.0%})")
        
        # Factor 4: Recently learned, needs consolidation
        if mastery.repetition_count > 0 and mastery.repetition_count < 3:
            priority += 20
            reasons.append("Needs consolidation")
        
        # Factor 5: Skill importance
        priority += mastery.skill.importance * 10
        
        # Factor 6: Previous struggles (low accuracy)
        if mastery.total_attempts > 2 and mastery.accuracy < 0.6:
            priority += 15
            reasons.append("Previous difficulty")
        
        reason = reasons[0] if reasons else "General review"
        return priority, reason
    
    def _select_diverse_questions(
        self, 
        candidates: List[Dict], 
        count: int
    ) -> List[Dict]:
        """
        Select diverse questions from candidates.
        
        Ensures variety by:
        - Not selecting multiple skills from the same lesson
        - Balancing difficulty levels
        - Mixing different Bloom's taxonomy levels
        """
        selected = []
        used_lessons = set()
        used_difficulties = []
        
        for candidate in candidates:
            if len(selected) >= count:
                break
            
            skill = candidate['skill']
            
            # Skip if we already have a question from this lesson
            if skill.primary_lesson_id in used_lessons:
                continue
            
            # Slight preference for variety in difficulty
            if len(used_difficulties) >= 2:
                difficulty_counts = {d: used_difficulties.count(d) for d in set(used_difficulties)}
                if skill.difficulty in difficulty_counts and difficulty_counts[skill.difficulty] >= 2:
                    continue
            
            selected.append(candidate)
            used_lessons.add(skill.primary_lesson_id)
            used_difficulties.append(skill.difficulty)
        
        return selected
    
    def _build_retrieval_question(self, candidate: Dict) -> Optional[RetrievalQuestion]:
        """Build a RetrievalQuestion from a candidate."""
        skill = candidate['skill']
        mastery = candidate['mastery']
        
        # Find a practice step that tests this skill
        step = LessonStep.objects.filter(
            lesson__skills=skill,
            answer_type__in=['multiple_choice', 'short_numeric', 'true_false', 'free_text'],
            question__isnull=False
        ).exclude(
            question=''
        ).order_by('?').first()
        
        if not step:
            # Try to find any step from the skill's primary lesson
            if skill.primary_lesson:
                step = LessonStep.objects.filter(
                    lesson=skill.primary_lesson,
                    step_type__in=['practice', 'quiz'],
                    answer_type__in=['multiple_choice', 'short_numeric', 'true_false'],
                ).exclude(
                    question=''
                ).order_by('?').first()
        
        if not step:
            logger.warning(f"No suitable question found for skill {skill.code}")
            return None
        
        return RetrievalQuestion(
            skill=skill,
            lesson_step=step,
            question_text=step.question,
            choices=step.choices or [],
            expected_answer=step.expected_answer,
            source_lesson=step.lesson.title,
            priority_reason=candidate['reason'],
            retention_estimate=candidate['retention'],
            mastery_record=mastery,
        )


# =============================================================================
# INTERLEAVED PRACTICE SERVICE
# =============================================================================

class InterleavedPracticeService:
    """
    Generates interleaved practice questions that mix new and review content.
    
    Instead of practicing one skill many times in a row (blocked practice),
    this service creates a mixed sequence that improves retention and transfer.
    """
    
    def __init__(self, student, lesson: Lesson):
        self.student = student
        self.lesson = lesson
    
    def get_interleaved_questions(
        self, 
        new_questions: List[LessonStep],
        review_ratio: float = 0.2
    ) -> List[Dict]:
        """
        Create an interleaved sequence of new and review questions.
        
        Args:
            new_questions: The main practice questions for this lesson
            review_ratio: Proportion of review questions to add (0.0 to 0.5)
        
        Returns:
            List of dicts with 'step', 'type' ('new' or 'review'), 'skill'
        """
        # Calculate how many review questions to add
        num_review = max(1, int(len(new_questions) * review_ratio))
        
        # Get review questions
        retrieval_service = RetrievalService(self.student, self.lesson)
        review_questions = retrieval_service.get_retrieval_questions(count=num_review)
        
        # Build interleaved sequence
        sequence = []
        
        # Add new questions
        for step in new_questions:
            sequence.append({
                'step': step,
                'type': 'new',
                'skill': None,  # Could be populated if steps are linked to skills
            })
        
        # Add review questions
        for rq in review_questions:
            sequence.append({
                'step': rq.lesson_step,
                'type': 'review',
                'skill': rq.skill,
                'mastery': rq.mastery_record,
            })
        
        # Shuffle to interleave (but keep first question as new)
        if len(sequence) > 2:
            first = sequence[0]
            rest = sequence[1:]
            random.shuffle(rest)
            sequence = [first] + rest
        
        return sequence


# =============================================================================
# SKILL ASSESSMENT SERVICE
# =============================================================================

class SkillAssessmentService:
    """
    Records and assesses skill practice attempts.
    
    Handles:
    - Recording practice attempts
    - Updating mastery levels
    - Logging practice history
    - Triggering remediation when needed
    """
    
    def __init__(self, student, session=None):
        self.student = student
        self.session = session
    
    def record_practice(
        self,
        skill: Skill,
        was_correct: bool,
        lesson_step: LessonStep = None,
        practice_type: str = 'initial',
        time_taken_seconds: int = None,
        hints_used: int = 0,
        quality: int = None
    ) -> SkillAssessment:
        """
        Record a skill practice attempt.
        
        Args:
            skill: The skill being practiced
            was_correct: Whether the answer was correct
            lesson_step: The step where this was practiced
            practice_type: Type of practice (initial, retrieval, interleaved, etc.)
            time_taken_seconds: How long the student took
            hints_used: Number of hints used
            quality: Optional explicit quality rating (0-5)
        
        Returns:
            SkillAssessment with before/after mastery
        """
        # Get or create mastery record
        mastery, created = StudentSkillMastery.objects.get_or_create(
            student=self.student,
            skill=skill,
            defaults={'mastery_level': 0.0}
        )
        
        mastery_before = mastery.mastery_level
        retention_before = mastery.calculate_retention()
        
        # Infer quality if not provided
        if quality is None:
            quality = self._infer_quality(was_correct, time_taken_seconds, hints_used)
        
        # Update mastery (this also updates spaced repetition schedule)
        mastery.record_attempt(was_correct, quality)
        
        mastery_after = mastery.mastery_level
        
        # Log the practice
        SkillPracticeLog.objects.create(
            student=self.student,
            skill=skill,
            mastery_record=mastery,
            session=self.session,
            lesson_step=lesson_step,
            practice_type=practice_type,
            was_correct=was_correct,
            quality=quality,
            time_taken_seconds=time_taken_seconds,
            hints_used=hints_used,
            mastery_before=mastery_before,
            mastery_after=mastery_after,
            retention_estimate=retention_before,
        )
        
        # Update knowledge profile
        self._update_knowledge_profile(skill.course)
        
        return SkillAssessment(
            skill=skill,
            was_correct=was_correct,
            quality=quality,
            time_taken_seconds=time_taken_seconds or 0,
            hints_used=hints_used,
            mastery_before=mastery_before,
            mastery_after=mastery_after,
        )
    
    def _infer_quality(
        self, 
        was_correct: bool, 
        time_taken: int = None, 
        hints_used: int = 0
    ) -> int:
        """
        Infer quality rating (0-5) from response characteristics.
        
        SM-2 Quality Ratings:
        0 - Complete blackout
        1 - Incorrect; remembered upon seeing answer
        2 - Incorrect; but answer seemed easy to recall
        3 - Correct; but with significant difficulty
        4 - Correct; after hesitation
        5 - Perfect recall
        """
        if not was_correct:
            if hints_used >= 3:
                return 0  # Complete blackout
            elif hints_used >= 1:
                return 1  # Needed hints
            else:
                return 2  # Wrong but no hints
        
        # Correct answer
        if hints_used >= 2:
            return 3  # Correct with significant help
        elif hints_used == 1 or (time_taken and time_taken > 60):
            return 4  # Correct with hesitation
        else:
            return 5  # Perfect recall
    
    def _update_knowledge_profile(self, course: Course):
        """Update the student's aggregated knowledge profile."""
        profile, created = StudentKnowledgeProfile.objects.get_or_create(
            student=self.student,
            course=course
        )
        profile.last_activity = timezone.now()
        profile.recalculate()


# =============================================================================
# REMEDIATION SERVICE
# =============================================================================

class RemediationService:
    """
    Handles targeted remediation when students struggle.
    
    Triggered when:
    - Student fails same question multiple times
    - Student fails exit ticket
    - Mastery drops significantly
    """
    
    def __init__(self, student, lesson: Lesson):
        self.student = student
        self.lesson = lesson
    
    def get_remediation_plan(
        self, 
        failed_skills: List[Skill] = None,
        exit_ticket_score: float = None
    ) -> Dict:
        """
        Generate a targeted remediation plan.
        
        Args:
            failed_skills: Skills the student struggled with
            exit_ticket_score: Exit ticket score (if applicable)
        
        Returns:
            Dict with remediation steps
        """
        plan = {
            'needs_remediation': False,
            'weak_skills': [],
            'prerequisite_gaps': [],
            'review_steps': [],
            'message': '',
        }
        
        # Analyze which skills need strengthening
        if failed_skills:
            plan['weak_skills'] = failed_skills
        elif exit_ticket_score is not None and exit_ticket_score < 0.8:
            plan['weak_skills'] = self._identify_weak_skills()
        
        if not plan['weak_skills']:
            return plan
        
        plan['needs_remediation'] = True
        
        # Find prerequisite gaps
        plan['prerequisite_gaps'] = self._find_prerequisite_gaps(plan['weak_skills'])
        
        # Get review steps for weak skills and prerequisites
        all_skills_to_review = plan['weak_skills'] + plan['prerequisite_gaps']
        plan['review_steps'] = self._get_review_steps(all_skills_to_review)
        
        # Generate message
        plan['message'] = self._generate_remediation_message(plan)
        
        return plan
    
    def _identify_weak_skills(self) -> List[Skill]:
        """Identify skills where the student is weak based on recent performance."""
        weak = []
        
        # Get skills for this lesson
        lesson_skills = Skill.objects.filter(lessons=self.lesson)
        
        for skill in lesson_skills:
            mastery = StudentSkillMastery.objects.filter(
                student=self.student,
                skill=skill
            ).first()
            
            if mastery and mastery.mastery_level < 0.6:
                weak.append(skill)
            elif mastery and mastery.accuracy < 0.5:
                weak.append(skill)
        
        return weak
    
    def _find_prerequisite_gaps(self, weak_skills: List[Skill]) -> List[Skill]:
        """Find prerequisite skills that might be causing the weakness."""
        gaps = []
        
        for skill in weak_skills:
            for prereq in skill.prerequisites.all():
                mastery = StudentSkillMastery.objects.filter(
                    student=self.student,
                    skill=prereq
                ).first()
                
                if not mastery or mastery.mastery_level < 0.7:
                    if prereq not in gaps:
                        gaps.append(prereq)
        
        return gaps
    
    def _get_review_steps(self, skills: List[Skill]) -> List[LessonStep]:
        """Get practice steps for reviewing skills."""
        steps = []
        
        for skill in skills[:5]:  # Limit to 5 skills
            # Get 1-2 practice steps per skill
            skill_steps = LessonStep.objects.filter(
                lesson__skills=skill,
                step_type__in=['practice', 'quiz'],
                answer_type__in=['multiple_choice', 'short_numeric', 'true_false'],
            ).exclude(
                question=''
            ).order_by('?')[:2]
            
            steps.extend(skill_steps)
        
        return steps
    
    def _generate_remediation_message(self, plan: Dict) -> str:
        """Generate a supportive message for the remediation."""
        if plan['prerequisite_gaps']:
            return (
                "Let's strengthen your foundation first. "
                "We'll review some concepts that will help you master this lesson."
            )
        else:
            return (
                "Let's practice these concepts a bit more. "
                "Extra practice will help you master them!"
            )


# =============================================================================
# SESSION PERSONALIZATION SERVICE
# =============================================================================

class SessionPersonalizationService:
    """
    Main service for personalizing a tutoring session.
    
    Combines all personalization services to create a customized
    learning experience for each student.
    """
    
    def __init__(self, student, lesson: Lesson):
        self.student = student
        self.lesson = lesson
        self.retrieval_service = RetrievalService(student, lesson)
        self.interleaved_service = InterleavedPracticeService(student, lesson)
        self.assessment_service = SkillAssessmentService(student)
    
    def get_session_personalization(self) -> SessionPersonalization:
        """
        Get complete personalization data for a session.
        
        Returns:
            SessionPersonalization with all customization data
        """
        personalization = SessionPersonalization()
        
        # Get retrieval questions (for session start)
        personalization.retrieval_questions = self.retrieval_service.get_retrieval_questions(
            count=3
        )
        
        # Analyze student's current state
        personalization.weak_skills, personalization.strong_skills = self._analyze_skills()
        
        # Determine recommended pace
        personalization.recommended_pace = self._determine_pace()
        
        # Get any personalized hints
        personalization.personalized_hints = self._get_personalized_hints()
        
        return personalization
    
    def _analyze_skills(self) -> Tuple[List[Skill], List[Skill]]:
        """Analyze student's skill mastery for this lesson."""
        weak = []
        strong = []
        
        # Get prerequisite skills for this lesson
        prereq_lessons = LessonPrerequisite.objects.filter(
            lesson=self.lesson
        ).values_list('prerequisite_id', flat=True)
        
        prereq_skills = Skill.objects.filter(
            primary_lesson_id__in=prereq_lessons
        )
        
        for skill in prereq_skills:
            mastery = StudentSkillMastery.objects.filter(
                student=self.student,
                skill=skill
            ).first()
            
            if mastery:
                if mastery.mastery_level >= 0.8:
                    strong.append(skill)
                elif mastery.mastery_level < 0.5:
                    weak.append(skill)
        
        return weak, strong
    
    def _determine_pace(self) -> str:
        """Determine recommended pace based on student's profile."""
        profile = StudentKnowledgeProfile.objects.filter(
            student=self.student,
            course=self.lesson.unit.course
        ).first()
        
        if not profile:
            return "normal"
        
        if profile.average_mastery >= 0.85:
            return "fast"
        elif profile.average_mastery < 0.5:
            return "slow"
        else:
            return "normal"
    
    def _get_personalized_hints(self) -> Dict[str, str]:
        """Get personalized hints based on past struggles."""
        hints = {}
        
        # Get skills for this lesson
        lesson_skills = Skill.objects.filter(lessons=self.lesson)
        
        for skill in lesson_skills:
            mastery = StudentSkillMastery.objects.filter(
                student=self.student,
                skill=skill
            ).first()
            
            if mastery and mastery.accuracy < 0.6 and mastery.total_attempts > 2:
                # Student has struggled with this before
                hints[skill.code] = (
                    f"Remember, you've practiced {skill.name} before. "
                    f"Take your time and think carefully."
                )
        
        return hints
