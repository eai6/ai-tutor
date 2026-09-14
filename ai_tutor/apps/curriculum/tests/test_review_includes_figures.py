"""The content review audits figures, not just text.

figure_alignment has only ever run at generation time, hooked into
image_service.get_or_generate_image. A figure produced before that hook
existed — or one whose judge call skipped for want of bytes, a provider or
time — was never reviewed and never would be: the catch-up sweep read step
text and exit-ticket questions and walked past the images.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_tutor.apps.accounts.models import Institution
from ai_tutor.apps.curriculum.content_generator import run_figure_judges_for_steps
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit


def _verdict(passed=True, violations=()):
    return SimpleNamespace(
        passed=passed, violations=list(violations), reasoning='r',
        recommended_fix='', provider='google', model_name='m',
        skipped=False, skip_reason='',
    )


@pytest.fixture
def lesson(db):
    school = Institution.objects.create(name='B', slug='b')
    course = Course.objects.create(title='Mathematics S3', institution=school,
                                   subject_code='mathematics', grade_level='S3')
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    return Lesson.objects.create(unit=unit, title='Angles', objective='find x',
                                 order_index=0)


def _step(lesson, images):
    return LessonStep.objects.create(
        lesson=lesson, order_index=0, teacher_script='t',
        question='Calculate the missing angle', media={'images': images})


def test_a_generated_figure_gets_reviewed(lesson):
    step = _step(lesson, [{'url': '/media/f.png', 'description': 'an angle diagram'}])

    with patch('ai_tutor.apps.tutoring.image_service.ImageGenerationService'
               '._read_image_bytes', return_value=b'PNG'), \
         patch('ai_tutor.apps.curriculum.content_judges.figure_alignment'
               '.run_figure_alignment_judge', return_value=_verdict()) as judge:
        reviewed, skipped = run_figure_judges_for_steps(lesson, [step])

    assert (reviewed, skipped) == (1, 0)
    step.refresh_from_db()
    verdict = step.media['images'][0]['judge_outputs']['figure_alignment']
    assert verdict['passed'] is True
    # The step's own objective reaches the judge — the generation-time hook
    # cannot pass one, because image_service takes no step argument.
    assert 'missing angle' in judge.call_args.kwargs['step_objective']


def test_an_already_reviewed_figure_is_skipped(lesson):
    """Idempotent, like the rest of the sweep — vision calls are not cheap."""
    step = _step(lesson, [{
        'url': '/media/f.png', 'description': 'd',
        'judge_outputs': {'figure_alignment': {'passed': True}},
    }])

    with patch('ai_tutor.apps.curriculum.content_judges.figure_alignment'
               '.run_figure_alignment_judge') as judge:
        reviewed, skipped = run_figure_judges_for_steps(lesson, [step])

    assert (reviewed, skipped) == (0, 1)
    judge.assert_not_called()


def test_force_rejudge_reviews_it_again(lesson):
    step = _step(lesson, [{
        'url': '/media/f.png', 'description': 'd',
        'judge_outputs': {'figure_alignment': {'passed': True}},
    }])

    with patch('ai_tutor.apps.tutoring.image_service.ImageGenerationService'
               '._read_image_bytes', return_value=b'PNG'), \
         patch('ai_tutor.apps.curriculum.content_judges.figure_alignment'
               '.run_figure_alignment_judge',
               return_value=_verdict(passed=False, violations=['FIGURE_OFF_OBJECTIVE'])):
        reviewed, _ = run_figure_judges_for_steps(lesson, [step],
                                                  force_rejudge=True)

    assert reviewed == 1
    step.refresh_from_db()
    v = step.media['images'][0]['judge_outputs']['figure_alignment']
    assert v['passed'] is False and v['violations'] == ['FIGURE_OFF_OBJECTIVE']


def test_an_image_with_no_url_is_left_for_the_image_sweep(lesson):
    """Nothing generated yet is not a figure to review — it is pending work
    for the image run, and judging it would just burn a vision call."""
    step = _step(lesson, [{'description': 'not generated yet'}])

    with patch('ai_tutor.apps.curriculum.content_judges.figure_alignment'
               '.run_figure_alignment_judge') as judge:
        reviewed, skipped = run_figure_judges_for_steps(lesson, [step])

    assert (reviewed, skipped) == (0, 0)
    judge.assert_not_called()


def test_an_unreadable_image_is_recorded_not_passed_over(lesson):
    """A stored URL that cannot be fetched IS a broken figure. Recording it
    is the point of a review; staying silent is what this replaces."""
    step = _step(lesson, [{'url': '/media/gone.png', 'description': 'd'}])

    with patch('ai_tutor.apps.tutoring.image_service.ImageGenerationService'
               '._read_image_bytes', return_value=None):
        reviewed, skipped = run_figure_judges_for_steps(lesson, [step])

    assert (reviewed, skipped) == (0, 1)
    step.refresh_from_db()
    v = step.media['images'][0]['judge_outputs']['figure_alignment']
    assert v['violations'] == ['FIGURE_UNREACHABLE']
