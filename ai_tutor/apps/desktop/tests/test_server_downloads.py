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

# Where the self-hosting manual lives now that the application no longer
# renders it. Asserted verbatim: a typo here is a footer link to a 404, and
# nothing else in the suite would notice.
MANUAL_URL = 'https://github.com/eai6/ai-tutor/blob/main/docs/self-hosting.md'


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
    def test_still_offers_the_desktop_installers(self):
        body = Client().get('/download/').content.decode()
        assert 'macOS' in body and 'Windows' in body


@pytest.mark.django_db
class TestLandingPage:

    def test_offers_self_hosting(self, client):
        """A ministry evaluating the platform arrives at the root, not at
        /download/. Without a link here they have no way to discover that
        running it themselves is even possible. The manual is no longer served
        by the application — it lives with the source it describes."""
        body = client.get('/').content.decode()
        assert MANUAL_URL in body

    def test_it_is_in_the_footer_not_the_main_choice(self, client):
        """Students and teachers land here to sign in. The two big cards stay
        theirs; self-hosting sits with the other secondary links."""
        import re
        body = client.get('/').content.decode()
        footer = re.search(r'<footer.*?</footer>', body, re.S)
        assert footer and MANUAL_URL in footer.group(0)

    def test_the_application_no_longer_serves_the_manual(self, client):
        """Deleted rather than redirected: it duplicated docs/self-hosting.md
        and drifted from it."""
        assert client.get('/self-hosting/').status_code == 404
        assert client.get('/download/self-hosting/').status_code == 404
