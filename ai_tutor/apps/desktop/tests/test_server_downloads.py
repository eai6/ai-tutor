"""The /download/ page and the server artefacts it links to.

This page is how a ministry gets the software at all, so the failure that
matters is not an exception — it is a page that confidently offers a link
which 404s, or a version that quietly lags the current release.
"""
import pytest
from django.test import Client
from django.test.utils import override_settings
from django.urls import reverse

from ai_tutor.apps.desktop.public_views import server_artefacts

BUCKET = dict(AWS_DOWNLOADS_BUCKET='dl.example', AWS_MEDIA_REGION='us-east-1')


class TestArtefactNames:

    @override_settings(SERVER_WHEEL_VERSION='')
    def test_nothing_is_offered_before_a_release(self):
        """pip parses the version out of the filename, so there is no stable
        `latest` name we could link to without knowing it."""
        assert server_artefacts() == {}

    @override_settings(SERVER_WHEEL_VERSION='1.2.0')
    def test_names_carry_the_real_version(self):
        a = server_artefacts()
        assert a['wheel'] == 'ai_tutor-1.2.0-py3-none-any.whl'
        assert a['sdist'] == 'ai_tutor-1.2.0.tar.gz'

    @override_settings(SERVER_WHEEL_VERSION='1.2.0')
    def test_the_wheel_name_is_installable_by_pip(self):
        """pip rejects a wheel whose filename it cannot parse, so an alias like
        ai_tutor-latest-... would download fine and refuse to install."""
        from packaging.utils import parse_wheel_filename
        name, version, _, _ = parse_wheel_filename(server_artefacts()['wheel'])
        assert str(version) == '1.2.0'


@pytest.mark.django_db
class TestRedirect:

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_redirects_to_the_bucket(self):
        r = Client().get(reverse('downloads:server', args=['wheel']))
        assert r.status_code == 302
        assert r.headers['Location'] == (
            'https://dl.example.s3.us-east-1.amazonaws.com/'
            'public/server/latest/ai_tutor-1.2.0-py3-none-any.whl')

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_unknown_artefacts_are_refused(self):
        """The name is an allowlist key, never a path. Taking a filename from
        the URL would let anyone redirect through this domain to any key in
        the bucket."""
        assert Client().get('/download/server/passwd/').status_code == 404

    @override_settings(SERVER_WHEEL_VERSION='', **BUCKET)
    def test_refuses_before_anything_is_published(self):
        assert Client().get(reverse('downloads:server', args=['wheel'])).status_code == 404

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', AWS_DOWNLOADS_BUCKET='')
    def test_refuses_when_no_bucket_is_configured(self):
        assert Client().get(reverse('downloads:server', args=['wheel'])).status_code == 404

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_head_works_too(self):
        """Link previewers and download managers probe with HEAD first."""
        assert Client().head(reverse('downloads:server', args=['wheel'])).status_code == 302


@pytest.mark.django_db
class TestPage:

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_offers_both_server_routes(self):
        body = Client().get('/self-hosting/').content.decode()
        assert 'Docker' in body
        assert 'ai_tutor-1.2.0-py3-none-any.whl' in body

    @override_settings(SERVER_WHEEL_VERSION='', **BUCKET)
    def test_never_offers_a_link_it_cannot_serve(self):
        """Before a release the page must not show a pip command pointing at a
        wheel that does not exist.

        It still has to show the procedure. The page used to hide all eight pip
        steps behind the wheel being published, so on a deployment with no
        downloads bucket the "with pip, no Docker" route rendered as a heading
        and an apology. Only the install line depends on the artefact: without
        a wheel it installs the published package by name, which is what
        docs/self-hosting.md tells you to do.
        """
        body = Client().get('/self-hosting/').content.decode()
        assert 'py3-none-any.whl' not in body
        assert 'pip install ai-tutor' in body
        assert 'python3 -m venv /opt/ai-tutor/venv' in body
        assert 'run collectstatic --noinput' in body

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_still_offers_the_desktop_installers(self):
        body = Client().get('/download/').content.decode()
        assert 'macOS' in body and 'Windows' in body


@pytest.mark.django_db
class TestSelfHostingPage:
    """The one page that tells you how to deploy.

    There is no separate manual page: rendering all 683 lines of
    docs/self-hosting.md here buried the commands under AWS cost tables and a
    Pulumi walkthrough. This page carries what a person types, and the few
    operational facts they cannot safely leave without.
    """

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_carries_the_actual_commands(self, client):
        body = client.get('/self-hosting/').content.decode()
        for cmd in ('docker compose up -d', 'ai-tutor init', 'ai-tutor migrate',
                    'createsuperuser', 'systemctl enable --now ai-tutor'):
            assert cmd in body, f'missing: {cmd}'

    def test_says_what_must_be_filled_in(self, client):
        """The two 'now edit this file' moments are where a deploy stalls."""
        body = client.get('/self-hosting/').content.decode()
        for name in ('SECRET_KEY', 'ALLOWED_HOSTS', 'CSRF_TRUSTED_ORIGINS'):
            assert name in body

    def test_covers_backups_and_upgrades(self, client):
        """A ministry that deploys and never backs up loses a term of work."""
        body = client.get('/self-hosting/').content.decode()
        assert 'pg_dump' in body and 'backup.sh' in body
        assert 'systemctl restart ai-tutor' in body

    def test_warns_about_the_silent_failure(self, client):
        """Login 403 with no error anywhere is the commonest way this is
        misconfigured, and nothing in the logs says so."""
        body = client.get('/self-hosting/').content.decode()
        assert 'HTTPS_EDGE' in body

    def test_says_what_leaves_the_network(self, client):
        """A ministry cannot evaluate this deployment without knowing it."""
        body = client.get('/self-hosting/').content.decode()
        assert 'Anthropic' in body and 'leaving your network' in body

    def test_there_is_no_separate_manual_page(self, client):
        assert client.get('/self-hosting/manual/').status_code == 404

    def test_renders_without_a_published_wheel(self, client):
        with override_settings(AWS_DOWNLOADS_BUCKET='', SERVER_WHEEL_VERSION=''):
            body = client.get('/self-hosting/').content.decode()
        assert 'docker compose up -d' in body
        assert 'py3-none-any.whl' not in body

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_offers_the_wheel_when_one_is_published(self, client):
        body = client.get('/self-hosting/').content.decode()
        assert 'ai_tutor-1.2.0-py3-none-any.whl' in body

    def test_the_two_pages_link_to_each_other(self, client):
        """Separate audiences, but someone always lands on the wrong one."""
        assert '/self-hosting/' in client.get('/download/').content.decode()
        assert '/download/' in client.get('/self-hosting/').content.decode()

    def test_the_desktop_page_no_longer_carries_server_instructions(self, client):
        with override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET):
            body = client.get('/download/').content.decode()
        assert 'python3.12 -m venv' not in body
        assert 'macOS' in body


@pytest.mark.django_db
class TestLandingPage:

    def test_offers_self_hosting(self, client):
        """A ministry evaluating the platform arrives at the root, not at
        /download/. Without a link here they have no way to discover that
        running it themselves is even possible."""
        body = client.get('/').content.decode()
        assert '/self-hosting/' in body

    def test_it_is_in_the_footer_not_the_main_choice(self, client):
        """Students and teachers land here to sign in. The two big cards stay
        theirs; self-hosting sits with the other secondary links."""
        import re
        body = client.get('/').content.decode()
        footer = re.search(r'<footer.*?</footer>', body, re.S)
        assert footer and '/self-hosting/' in footer.group(0)
