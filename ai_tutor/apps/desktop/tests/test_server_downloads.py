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
        wheel that does not exist."""
        body = Client().get('/self-hosting/').content.decode()
        assert 'py3-none-any.whl' not in body
        assert 'No wheel has been published yet' in body

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_still_offers_the_desktop_installers(self):
        body = Client().get('/download/').content.decode()
        assert 'macOS' in body and 'Windows' in body


@pytest.mark.django_db
class TestSelfHostingPage:
    """The manual, served by the application.

    It exists because the repository is private: a public download page cannot
    route a ministry's instructions through a URL they cannot open.
    """

    def test_serves_the_whole_manual(self, client):
        body = client.get('/self-hosting/manual/').content.decode()
        for section in ('Choosing a path', 'Path A', 'Path B',
                        'Path C', 'What leaves your network'):
            assert section in body, f'missing: {section}'

    def test_renders_markdown_rather_than_showing_it(self, client):
        body = client.get('/self-hosting/manual/').content.decode()
        assert '<table' in body and '<h2' in body
        assert '| You provide |' not in body, 'markdown table left unrendered'

    def test_every_in_page_anchor_resolves(self, client):
        """markdown-it does not generate heading ids on its own, so without
        the slugger every anchor on the page — including the buttons at the
        top and the manual's own cross-references — is dead."""
        import re
        body = client.get('/self-hosting/manual/').content.decode()
        ids = set(re.findall(r'<h[1-6] id="([^"]+)"', body))
        targets = {h[1:] for h in re.findall(r'href="(#[^"]+)"', body)}
        assert targets, 'expected in-page links'
        assert not (targets - ids), f'dangling anchors: {sorted(targets - ids)}'

    def test_uses_github_slugs(self, client):
        """The document is written and reviewed on GitHub and its own
        cross-references are GitHub-style anchors."""
        body = client.get('/self-hosting/manual/').content.decode()
        assert 'id="3-path-a--your-own-server"' in body

    def test_renders_without_the_bucket_configured(self, client):
        """The manual is the point of the page; the download buttons are not."""
        with override_settings(AWS_DOWNLOADS_BUCKET='', SERVER_WHEEL_VERSION=''):
            body = client.get('/self-hosting/manual/').content.decode()
        assert 'Choosing a path' in body
        assert 'Download .whl' not in body

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_offers_the_wheel_when_one_is_published(self, client):
        body = client.get('/self-hosting/').content.decode()
        assert 'ai_tutor-1.2.0-py3-none-any.whl' in body

    def test_the_quick_start_is_short(self, client):
        """It exists because rendering all 683 manual lines here buried the six
        commands someone actually needs. If it grows back, that is a regression
        in the thing the page is for."""
        quick = len(client.get('/self-hosting/').content)
        full = len(client.get('/self-hosting/manual/').content)
        assert quick < full / 3, f'quick start {quick} vs manual {full}'

    @override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET)
    def test_the_quick_start_carries_the_actual_commands(self, client):
        body = client.get('/self-hosting/').content.decode()
        for cmd in ('docker compose up -d', 'ai-tutor init', 'createsuperuser'):
            assert cmd in body, f'missing: {cmd}'

    def test_the_quick_start_links_to_the_manual(self, client):
        assert '/self-hosting/manual/' in client.get('/self-hosting/').content.decode()

    def test_the_two_pages_link_to_each_other(self, client):
        """Separate audiences, but someone always lands on the wrong one."""
        assert '/self-hosting/' in client.get('/download/').content.decode()
        assert '/download/' in client.get('/self-hosting/').content.decode()

    def test_the_desktop_page_no_longer_carries_server_instructions(self, client):
        with override_settings(SERVER_WHEEL_VERSION='1.2.0', **BUCKET):
            body = client.get('/download/').content.decode()
        assert 'python3.12 -m venv' not in body
        assert 'macOS' in body


class TestSlug:

    @pytest.mark.parametrize('heading,expected', [
        ('3. Path A — your own server', '3-path-a--your-own-server'),
        ('What leaves your network', 'what-leaves-your-network'),
        ('Step 1 — Install', 'step-1--install'),
    ])
    def test_matches_github(self, heading, expected):
        from ai_tutor.apps.desktop.public_views import _slug
        assert _slug(heading) == expected
