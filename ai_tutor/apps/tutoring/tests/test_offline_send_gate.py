"""The desktop build must not gate sending on the browser's connectivity.

The chat page queues a student's message into localStorage instead of POSTing
whenever ``NetHelpers.isOnline()`` is false. That is right for a student on a
school's flaky Wi-Fi hitting the hosted app, and exactly wrong on the desktop
build, where the server and the model are both on the same machine: dropping
the Wi-Fi there made the tutor stop answering — clicking an answer filed it in
an outbox and no request was ever sent.

These are asserted against the shipped assets rather than a browser, because
the property that broke is a static one: which signal the send path consults.
"""
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings
from django.test.client import RequestFactory
from django.template.loader import render_to_string

_STATIC = Path(settings.BASE_DIR) / 'ai_tutor' / 'static' / 'js'
_TEMPLATES = Path(settings.BASE_DIR) / 'ai_tutor' / 'templates'


class NetworkHelperTests(TestCase):
    def setUp(self):
        self.source = (_STATIC / 'network-helpers.js').read_text()

    def test_is_online_consults_the_local_server_flag(self):
        """navigator.onLine alone is not an answer on a local server."""
        self.assertIn('AITUTOR_LOCAL_SERVER', self.source)
        self.assertIn('function isOnline()', self.source)
        body = self.source.split('function isOnline()', 1)[1].split('}', 1)[0]
        self.assertIn('localServer()', body)

    def test_connectivity_events_are_not_bound_on_a_local_server(self):
        """An 'offline' event says nothing about a same-machine origin, and
        acting on it would flip the banner and the send path anyway."""
        self.assertIn("if (!localServer())", self.source)


class ChatPageQueueTests(TestCase):
    def setUp(self):
        self.source = (_TEMPLATES / 'tutoring' / 'chat_tutor.html').read_text()

    def test_a_queued_outbox_is_discarded_on_the_desktop_build(self):
        """A message queued by the old gate is an answer to a question that
        has since moved on, so replaying it on reconnect is wrong."""
        self.assertIn("window.AITUTOR_LOCAL_SERVER === true", self.source)
        branch = self.source.split(
            "if (window.AITUTOR_LOCAL_SERVER === true) {", 1)[1].split('} else', 1)[0]
        self.assertIn('saveQueue([])', branch)
        self.assertNotIn('drainQueue()', branch)


class BaseTemplateFlagTests(TestCase):
    """The flag has to reach the page, and reach it before the helper runs."""

    def _render(self):
        request = RequestFactory().get('/')
        request.csp_nonce = 'test-nonce'
        return render_to_string('base.html', request=request)

    @override_settings(DESKTOP_BUILD=True)
    def test_desktop_build_declares_a_local_server(self):
        html = self._render()
        self.assertIn('window.AITUTOR_LOCAL_SERVER = true;', html)

    @override_settings(DESKTOP_BUILD=False)
    def test_hosted_build_keeps_the_browser_signal(self):
        html = self._render()
        self.assertIn('window.AITUTOR_LOCAL_SERVER = false;', html)

    @override_settings(DESKTOP_BUILD=True)
    def test_the_flag_is_set_before_the_helper_loads(self):
        """network-helpers.js reads the flag at parse time; a deferred script
        would run after it and be too late."""
        html = self._render()
        flag_at = html.index('window.AITUTOR_LOCAL_SERVER')
        # Static files are hashed in some storages, so match the stem.
        helper_at = html.index('network-helpers')
        self.assertLess(flag_at, helper_at)

    @override_settings(DESKTOP_BUILD=True)
    def test_no_template_comment_leaks_into_the_page(self):
        """`{# #}` is single-line only; a multi-line one renders as visible
        body text. Caught in review once already."""
        html = self._render()
        self.assertNotIn('{#', html)
        self.assertNotIn('Not deferred:', html)
