"""Help-assistant tool catalog + handlers.

Hard safety rule (memory/help_assistant_plan.md):
  - The catalog is an EXPLICIT ALLOWLIST. There is no generic
    Django-ORM or shell tool. The LLM literally cannot call
    anything outside this file.
  - Every handler RE-CHECKS the calling user's permission against
    what it's about to do, even though the catalog already filters
    by audience. Audience filtering is UX; per-handler check is
    security.
  - Read-only tools run inline. Write tools (the small set we DO
    allow) require an explicit confirmation click in the chat.
  - NO tool deletes anything. Ever.

Tool spec shape (Anthropic tool-use compatible):
  {
    'name': 'snake_case_tool_name',
    'description': 'Plain-English description for the LLM',
    'input_schema': {...JSON schema...},
    'audience': 'all' | 'student' | 'staff',
    'requires_confirmation': bool,
    'handler': callable(user, **inputs) -> dict,
  }
"""

import logging
from typing import Dict, List, Optional

from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


# ============================================================================
# Helpers — permission + lookup
# ============================================================================

def _is_staff(user: User) -> bool:
    if not user or not user.is_authenticated:
        return False
    return bool(user.is_staff or user.is_superuser)


def _can_manage_course(user: User, course) -> bool:
    """A staff user can manage a course if they're a super admin OR
    they belong to the course's institution (or it's platform-wide)."""
    if not _is_staff(user):
        return False
    if user.is_superuser:
        return True
    if course.institution_id is None:
        return True  # platform-wide course; any teacher can act
    try:
        from apps.accounts.models import Membership
        return Membership.objects.filter(
            user=user, role__in=['teacher', 'admin'],
            institution_id=course.institution_id, is_active=True,
        ).exists()
    except Exception:
        return False


def _resolve_audience(user: User) -> str:
    if not user or not user.is_authenticated:
        return 'student'  # safest default
    if user.is_superuser:
        return 'super_admin'
    if user.is_staff:
        return 'teacher'
    return 'student'


def _fuzzy_course_lookup(query: str, user: User):
    """Return up to 5 candidate courses matching the query string,
    filtered by what the user can see."""
    from apps.curriculum.models import Course
    from django.db.models import Q
    if not query:
        return []
    qs = Course.objects.filter(
        Q(title__icontains=query) | Q(grade_level__icontains=query)
    )
    # Visibility filter — students see only published; staff see all
    # in their institution OR platform-wide.
    if not _is_staff(user):
        qs = qs.filter(is_published=True)
    elif not user.is_superuser:
        try:
            from apps.accounts.models import Membership
            inst_ids = list(Membership.objects.filter(
                user=user, role__in=['teacher', 'admin'], is_active=True,
            ).values_list('institution_id', flat=True))
            qs = qs.filter(Q(institution_id__in=inst_ids) | Q(institution__isnull=True))
        except Exception:
            pass
    return list(qs.order_by('title')[:5])


def _fuzzy_lesson_lookup(query: str, user: User, course=None):
    from apps.curriculum.models import Lesson
    from django.db.models import Q
    if not query:
        return []
    qs = Lesson.objects.filter(title__icontains=query)
    if course is not None:
        qs = qs.filter(unit__course=course)
    if not _is_staff(user):
        qs = qs.filter(is_published=True)
    return list(qs.select_related('unit', 'unit__course').order_by('title')[:5])


def _fuzzy_student_lookup(query: str, user: User):
    """Teachers can find their institution's students; super admins all."""
    if not _is_staff(user):
        return []
    from apps.accounts.models import Membership
    from django.db.models import Q
    qs = User.objects.filter(
        Q(username__icontains=query)
        | Q(first_name__icontains=query)
        | Q(last_name__icontains=query)
    )
    if not user.is_superuser:
        inst_ids = list(Membership.objects.filter(
            user=user, role__in=['teacher', 'admin'], is_active=True,
        ).values_list('institution_id', flat=True))
        student_ids = list(Membership.objects.filter(
            role='student', is_active=True, institution_id__in=inst_ids,
        ).values_list('user_id', flat=True))
        qs = qs.filter(id__in=student_ids)
    return list(qs.order_by('username')[:5])


# ============================================================================
# Tool handlers
# ============================================================================


def find_help_doc(user, *, query: str) -> Dict:
    """Retrieve top-3 relevant help-doc chunks for an explicit
    follow-up. Mostly for cases where the LLM wants to dig deeper
    after the initial retrieval. Read-only, no side effects."""
    from apps.support.kb import HelpKB
    audience = _resolve_audience(user)
    chunks = HelpKB().query(query, audience=audience, n_results=3)
    return {
        'ok': True,
        'chunks': [
            {
                'section_title': c['section_title'],
                'text': c['text'][:600],
                'anchor': c.get('anchor', ''),
                'source': c['source'],
            }
            for c in chunks
        ],
    }


def start_lesson(user, *, lesson_id: int) -> Dict:
    """Return a deep-link to the lesson chat. Confirmation gates the
    actual navigation (so the user sees the proposed lesson before
    being whisked off)."""
    from apps.curriculum.models import Lesson
    lesson = Lesson.objects.filter(id=lesson_id, is_published=True).first()
    if not lesson:
        return {'ok': False, 'human_msg': 'That lesson is not available.'}
    return {
        'ok': True,
        'url': f'/tutor/lesson/{lesson.id}/',
        'label': f"Start '{lesson.title}'",
    }


def take_baseline(user, *, course_query: str) -> Dict:
    """Deep-link to the course summative for a baseline attempt."""
    candidates = _fuzzy_course_lookup(course_query, user)
    if not candidates:
        return {'ok': False, 'human_msg': f'No course found matching "{course_query}".'}
    if len(candidates) > 1:
        return {
            'ok': False,
            'candidates': [{'id': c.id, 'title': c.title} for c in candidates],
            'human_msg': 'Which course did you mean?',
        }
    course = candidates[0]
    return {
        'ok': True,
        'url': f'/tutor/summative/{course.id}/',
        'label': f"Take {course.title} baseline",
    }


# ----- Navigation tools (teacher-facing reads) ------------------------------


def open_class_competency_map(user, *, course_query: str) -> Dict:
    if not _is_staff(user):
        return {'ok': False, 'human_msg': 'Teacher access required.'}
    candidates = _fuzzy_course_lookup(course_query, user)
    if not candidates:
        return {'ok': False, 'human_msg': f'No course matching "{course_query}".'}
    if len(candidates) > 1:
        return {'ok': False, 'candidates': [
            {'id': c.id, 'title': c.title} for c in candidates
        ], 'human_msg': 'Which course?'}
    c = candidates[0]
    return {
        'ok': True,
        'url': f'/dashboard/curriculum/course/{c.id}/competencies/',
        'label': f"{c.title} — Competency Map",
    }


def open_class_readiness(user, *, course_query: str) -> Dict:
    if not _is_staff(user):
        return {'ok': False, 'human_msg': 'Teacher access required.'}
    candidates = _fuzzy_course_lookup(course_query, user)
    if not candidates:
        return {'ok': False, 'human_msg': f'No course matching "{course_query}".'}
    if len(candidates) > 1:
        return {'ok': False, 'candidates': [
            {'id': c.id, 'title': c.title} for c in candidates
        ], 'human_msg': 'Which course?'}
    c = candidates[0]
    return {
        'ok': True,
        'url': f'/dashboard/class/{c.id}/readiness/',
        'label': f"{c.title} — Class Readiness",
    }


def open_summative_review(user, *, course_query: str) -> Dict:
    if not _is_staff(user):
        return {'ok': False, 'human_msg': 'Teacher access required.'}
    candidates = _fuzzy_course_lookup(course_query, user)
    if not candidates:
        return {'ok': False, 'human_msg': f'No course matching "{course_query}".'}
    if len(candidates) > 1:
        return {'ok': False, 'candidates': [
            {'id': c.id, 'title': c.title} for c in candidates
        ], 'human_msg': 'Which course?'}
    c = candidates[0]
    return {
        'ok': True,
        'url': f'/dashboard/curriculum/course/{c.id}/summative/',
        'label': f"{c.title} — Summative Exam",
    }


def open_lesson_detail(user, *, lesson_query: str) -> Dict:
    if not _is_staff(user):
        return {'ok': False, 'human_msg': 'Teacher access required.'}
    candidates = _fuzzy_lesson_lookup(lesson_query, user)
    if not candidates:
        return {'ok': False, 'human_msg': f'No lesson matching "{lesson_query}".'}
    if len(candidates) > 1:
        return {'ok': False, 'candidates': [
            {'id': l.id, 'title': l.title, 'course': l.unit.course.title}
            for l in candidates
        ], 'human_msg': 'Which lesson?'}
    l = candidates[0]
    return {
        'ok': True,
        'url': f'/dashboard/curriculum/lesson/{l.id}/',
        'label': f"{l.title} ({l.unit.course.title})",
    }


def open_student_chat_history(user, *, student_query: str) -> Dict:
    if not _is_staff(user):
        return {'ok': False, 'human_msg': 'Teacher access required.'}
    candidates = _fuzzy_student_lookup(student_query, user)
    if not candidates:
        return {'ok': False, 'human_msg': f'No student matching "{student_query}".'}
    if len(candidates) > 1:
        return {'ok': False, 'candidates': [
            {'id': s.id, 'username': s.username, 'name': s.get_full_name()}
            for s in candidates
        ], 'human_msg': 'Which student?'}
    s = candidates[0]
    return {
        'ok': True,
        'url': f'/dashboard/students/{s.id}/',
        'label': f"@{s.username} — Student Detail",
    }


# ============================================================================
# Catalog
# ============================================================================

# Each entry maps the LLM-visible spec to its server-side handler.
# `requires_confirmation=True` means user-facing navigation that
# changes their location (e.g. start_lesson). The catalog is locked
# to documentation lookup + URL-resolution — no data reads, no writes.
_CATALOG = [
    {
        'name': 'find_help_doc',
        'description': "Retrieve up to 3 help-doc snippets matching a topic. Use this when the user's question needs deeper context than the initial retrieval gave you.",
        'input_schema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Topic or question to search for.'},
            },
            'required': ['query'],
        },
        'audience': 'all',
        'requires_confirmation': False,
        'handler': find_help_doc,
    },
    {
        'name': 'start_lesson',
        'description': 'Get a link to start a specific lesson in the tutor.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'lesson_id': {'type': 'integer'},
            },
            'required': ['lesson_id'],
        },
        'audience': 'student',
        'requires_confirmation': True,
        'handler': start_lesson,
    },
    {
        'name': 'take_baseline',
        'description': 'Get a link to take the baseline summative for a course.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'course_query': {'type': 'string', 'description': 'Course title or fragment.'},
            },
            'required': ['course_query'],
        },
        'audience': 'student',
        'requires_confirmation': True,
        'handler': take_baseline,
    },
    {
        'name': 'open_class_competency_map',
        'description': "Get a link to a course's class competency map (per-objective progress matrix).",
        'input_schema': {
            'type': 'object',
            'properties': {'course_query': {'type': 'string'}},
            'required': ['course_query'],
        },
        'audience': 'staff',
        'requires_confirmation': False,
        'handler': open_class_competency_map,
    },
    {
        'name': 'open_class_readiness',
        'description': "Get a link to a course's class readiness report.",
        'input_schema': {
            'type': 'object',
            'properties': {'course_query': {'type': 'string'}},
            'required': ['course_query'],
        },
        'audience': 'staff',
        'requires_confirmation': False,
        'handler': open_class_readiness,
    },
    {
        'name': 'open_summative_review',
        'description': "Get a link to a course's summative exam review page.",
        'input_schema': {
            'type': 'object',
            'properties': {'course_query': {'type': 'string'}},
            'required': ['course_query'],
        },
        'audience': 'staff',
        'requires_confirmation': False,
        'handler': open_summative_review,
    },
    {
        'name': 'open_lesson_detail',
        'description': 'Get a link to a lesson detail page on the teacher dashboard.',
        'input_schema': {
            'type': 'object',
            'properties': {'lesson_query': {'type': 'string'}},
            'required': ['lesson_query'],
        },
        'audience': 'staff',
        'requires_confirmation': False,
        'handler': open_lesson_detail,
    },
    {
        'name': 'open_student_chat_history',
        'description': "Get a link to a student's chat / progress history.",
        'input_schema': {
            'type': 'object',
            'properties': {'student_query': {'type': 'string'}},
            'required': ['student_query'],
        },
        'audience': 'staff',
        'requires_confirmation': False,
        'handler': open_student_chat_history,
    },
]
# Catalog locked to documentation + navigation only. Data-reading
# tools (count_students, course_attempt_summary, student_summary,
# list_lessons_in_unit, flagged_chats_count, recommend_next_lesson)
# and write tools (assign_lesson_for_week,
# set_default_lesson_duration) were removed for the security
# reason that the help assistant must not expose student/course
# data through prompt-injectable channels. If a user needs that
# data they go to the relevant dashboard page directly.


def catalog_for_audience(audience: str) -> List[Dict]:
    """Return the subset of tools visible to this audience.
    `staff` and `super_admin` see staff + all-audience tools.
    `student` sees student + all-audience tools."""
    visible = []
    for tool in _CATALOG:
        aud = tool.get('audience', 'all')
        if aud == 'all':
            visible.append(tool)
        elif audience in ('staff', 'super_admin', 'teacher') and aud in ('staff', 'student'):
            # Staff can see student tools too — they may want to test
            # a student flow. The handler still re-checks the user's
            # actual context where it matters.
            visible.append(tool)
        elif audience == 'student' and aud == 'student':
            visible.append(tool)
    return visible


def get_tool(name: str) -> Optional[Dict]:
    for tool in _CATALOG:
        if tool['name'] == name:
            return tool
    return None


def llm_tool_specs(audience: str) -> List[Dict]:
    """Anthropic tool-use schemas for the visible catalog."""
    return [
        {
            'name': t['name'],
            'description': t['description'],
            'input_schema': t['input_schema'],
        }
        for t in catalog_for_audience(audience)
    ]
