"""Flash messages: one per event, shown where they are raised.

Four banners stacked on the dashboard at once — "You've been logged out",
"Welcome back", "You've been logged out", "Welcome" — because neither the auth
templates nor the student shell rendered a message region. Anything queued
there stayed in the session, unread, until the user reached the one page that
did render messages, which then printed the whole backlog.

Three things fix that and are pinned here: the logout notice is gone, the two
login views agree on one wording, and the student shell renders messages so
they are consumed where they happen.
"""
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership

User = get_user_model()
BACKEND = 'django.contrib.auth.backends.ModelBackend'
PASSWORD = 'F7k2!qvLmz9'


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Alpha', slug='alpha', is_active=True)


def _texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class TestLogout:

    def test_it_queues_nothing(self, school):
        """The signed-out page is the confirmation. The notice could not be
        rendered where it was raised anyway — no auth template has a message
        region — so it only ever arrived late, next to a contradicting one."""
        user = User.objects.create_user(
            username='someone', email='s@example.com', password=PASSWORD)
        client = Client()
        client.force_login(user, backend=BACKEND)

        response = client.get(reverse('accounts:logout'))
        assert _texts(response) == []

    def test_the_next_page_is_clean(self, school):
        """The real symptom: a stale notice waiting on the next render."""
        user = User.objects.create_user(
            username='someone2', email='s2@example.com', password=PASSWORD,
            is_staff=True)
        client = Client()
        client.force_login(user, backend=BACKEND)
        client.get(reverse('accounts:logout'))

        client.force_login(user, backend=BACKEND)
        body = client.get(reverse('dashboard:home')).content.decode()
        assert "You&#x27;ve been logged out" not in body
        assert "You've been logged out" not in body


class TestLoginGreeting:

    def test_staff_login_says_welcome_back(self, school):
        user = User.objects.create_user(
            username='teach', email='t@example.com', password=PASSWORD,
            first_name='Grace')
        Membership.objects.create(
            user=user, institution=school, role='staff', is_active=True)

        client = Client()
        response = client.post(reverse('accounts:staff_login'),
                               {'username': 'teach', 'password': PASSWORD})
        assert _texts(response) == ['Welcome back, Grace!']

    def test_student_login_says_the_same_thing(self, school):
        user = User.objects.create_user(
            username='pupil', email='p@example.com', password=PASSWORD,
            first_name='Ana')
        Membership.objects.create(
            user=user, institution=school, role='student', is_active=True)

        client = Client()
        response = client.post(reverse('accounts:student_login'),
                               {'username': 'pupil', 'password': PASSWORD})
        assert _texts(response) == ['Welcome back, Ana!']

    def test_signing_in_leaves_exactly_one_banner(self, school):
        """Not one per page until something finally renders them."""
        user = User.objects.create_user(
            username='root', email='r@example.com', password=PASSWORD,
            first_name='Daniel', is_staff=True)
        client = Client()
        client.post(reverse('accounts:staff_login'),
                    {'username': 'root', 'password': PASSWORD})
        body = client.get(reverse('dashboard:home')).content.decode()
        assert body.count('class="alert alert--') == 1
        assert 'Welcome back, Daniel!' in body


class TestRendering:

    def test_the_student_shell_has_a_message_region(self, school):
        """It had none, which is where the backlog came from."""
        user = User.objects.create_user(
            username='pupil2', email='p2@example.com', password=PASSWORD,
            first_name='Ana')
        Membership.objects.create(
            user=user, institution=school, role='student', is_active=True)

        client = Client()
        client.post(reverse('accounts:student_login'),
                    {'username': 'pupil2', 'password': PASSWORD})
        body = client.get(reverse('tutoring:catalog'), follow=True).content.decode()
        # The region is found by its name, which now travels alongside the
        # utilities that style it — flash.js still looks it up as .messages.
        assert 'class="messages' in body
        assert 'Welcome back, Ana!' in body

    def test_a_message_is_consumed_where_it_renders(self, school):
        """Read once on the student page, it must not reappear later."""
        user = User.objects.create_user(
            username='pupil3', email='p3@example.com', password=PASSWORD,
            first_name='Ana', is_staff=True)
        Membership.objects.create(
            user=user, institution=school, role='student', is_active=True)

        client = Client()
        client.post(reverse('accounts:student_login'),
                    {'username': 'pupil3', 'password': PASSWORD})
        client.get(reverse('tutoring:catalog'), follow=True)
        body = client.get(reverse('dashboard:home')).content.decode()
        assert 'Welcome back, Ana!' not in body

    @pytest.mark.parametrize('template', [
        'ai_tutor/templates/base.html',
        'ai_tutor/templates/dashboard/base.html',
    ])
    def test_extra_tags_do_not_break_the_tone_class(self, template):
        """`message.tags` concatenates extra_tags with the level, so rendering
        `alert--{{ message.tags }}` turned a message sent with
        extra_tags='sticky' into class="alert--sticky success" — the tone skin
        silently gone and an undefined class in its place. Both shells must
        build the skin from level_tag and carry extra tags separately.

        Checked on the source because the failure is in what the template
        emits, and both shells have to agree.
        """
        from django.conf import settings

        body = (Path(settings.BASE_DIR) / template).read_text()
        assert 'alert--{{ message.level_tag' in body
        assert 'alert--{{ message.tags' not in body
        assert '{{ message.extra_tags }}' in body
        # The icon picker reads the level too, for the same reason.
        assert "message.tags == 'success'" not in body
