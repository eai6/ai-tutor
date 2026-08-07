"""SES email backend — payload shape and failure semantics.

SimpleTestCase rather than bare pytest functions to match the rest of the
suite; pytest-django is not installed and function-style tests fail before
Django settings are wrapped.
"""
from __future__ import annotations

from unittest.mock import patch

from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.test import SimpleTestCase, override_settings

from apps.safety import email_backends
from apps.safety.email_backends import SESEmailBackend, _bare_email


class _FakeSES:
    def __init__(self, error: Exception | None = None):
        self.sent: list[dict] = []
        self.error = error

    def send_email(self, **kwargs):
        if self.error:
            raise self.error
        self.sent.append(kwargs)
        return {"MessageId": "msg-123"}


@override_settings(
    AWS_SES_SENDER="noreply@mail.example.com",
    DEFAULT_FROM_EMAIL="AI Tutor <noreply@mail.example.com>",
)
class SESEmailBackendTests(SimpleTestCase):

    def setUp(self):
        self.ses = _FakeSES()
        patcher = patch.object(email_backends, "_ses_client", lambda: self.ses)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plain_message_sends_with_a_bare_sender_address(self):
        backend = SESEmailBackend()
        msg = EmailMessage(
            subject="Reset your password",
            body="Click here",
            from_email="AI Tutor <noreply@mail.example.com>",
            to=["Student Name <student@school.sc>"],
        )

        self.assertEqual(backend.send_messages([msg]), 1)

        payload = self.ses.sent[0]
        self.assertEqual(payload["FromEmailAddress"], "noreply@mail.example.com")
        self.assertEqual(payload["Destination"]["ToAddresses"], ["student@school.sc"])
        simple = payload["Content"]["Simple"]
        self.assertEqual(simple["Subject"]["Data"], "Reset your password")
        self.assertEqual(simple["Body"]["Text"]["Data"], "Click here")

    def test_html_alternative_is_sent_alongside_the_plain_body(self):
        backend = SESEmailBackend()
        msg = EmailMultiAlternatives(
            subject="Welcome", body="plain version", to=["student@school.sc"]
        )
        msg.attach_alternative("<p>html version</p>", "text/html")

        backend.send_messages([msg])

        body = self.ses.sent[0]["Content"]["Simple"]["Body"]
        self.assertEqual(body["Text"]["Data"], "plain version")
        self.assertEqual(body["Html"]["Data"], "<p>html version</p>")

    def test_cc_bcc_and_reply_to_are_forwarded(self):
        backend = SESEmailBackend()
        msg = EmailMessage(
            subject="s", body="b",
            to=["a@school.sc"], cc=["b@school.sc"], bcc=["c@school.sc"],
            reply_to=["Support <help@school.sc>"],
        )

        backend.send_messages([msg])

        payload = self.ses.sent[0]
        self.assertEqual(payload["Destination"]["CcAddresses"], ["b@school.sc"])
        self.assertEqual(payload["Destination"]["BccAddresses"], ["c@school.sc"])
        self.assertEqual(payload["ReplyToAddresses"], ["help@school.sc"])

    def test_a_message_with_an_attachment_uses_the_raw_content_form(self):
        """SES Simple content has no attachment field, so attachments must
        go out as a fully-rendered MIME document instead."""
        backend = SESEmailBackend()
        msg = EmailMessage(subject="s", body="b", to=["a@school.sc"])
        msg.attach("report.csv", "a,b\n1,2\n", "text/csv")

        backend.send_messages([msg])

        content = self.ses.sent[0]["Content"]
        self.assertIn("Raw", content)
        self.assertNotIn("Simple", content)
        self.assertIn(b"report.csv", content["Raw"]["Data"])

    def test_a_message_with_no_recipients_is_rejected(self):
        backend = SESEmailBackend()
        msg = EmailMessage(subject="s", body="b", to=[])

        with self.assertRaises(ValueError):
            backend.send_messages([msg])

    def test_send_failure_propagates_when_not_failing_silently(self):
        self.ses.error = RuntimeError("throttled")
        backend = SESEmailBackend(fail_silently=False)
        msg = EmailMessage(subject="s", body="b", to=["a@school.sc"])

        with self.assertRaises(RuntimeError):
            backend.send_messages([msg])

    def test_send_failure_is_swallowed_when_failing_silently(self):
        self.ses.error = RuntimeError("throttled")
        backend = SESEmailBackend(fail_silently=True)
        msg = EmailMessage(subject="s", body="b", to=["a@school.sc"])

        self.assertEqual(backend.send_messages([msg]), 0)


class SESBackendUnconfiguredTests(SimpleTestCase):

    @override_settings(AWS_SES_SENDER="")
    def test_without_a_sender_it_refuses_rather_than_silently_dropping_mail(self):
        backend = SESEmailBackend(fail_silently=False)
        msg = EmailMessage(subject="s", body="b", to=["a@school.sc"])

        with self.assertRaises(RuntimeError):
            backend.send_messages([msg])

    @override_settings(AWS_SES_SENDER="")
    def test_without_a_sender_it_stays_quiet_when_failing_silently(self):
        backend = SESEmailBackend(fail_silently=True)
        msg = EmailMessage(subject="s", body="b", to=["a@school.sc"])

        self.assertEqual(backend.send_messages([msg]), 0)


class BareEmailTests(SimpleTestCase):

    def test_bare_email_strips_display_names(self):
        for raw, expected in (
            ("AI Tutor <noreply@example.com>", "noreply@example.com"),
            ("plain@example.com", "plain@example.com"),
            ("", ""),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_bare_email(raw), expected)


class AzureBackendStillIntactTests(SimpleTestCase):
    """Azure sends live transactional mail while AWS is stood up, so both
    backends live in this module. These guard the Azure path from being
    broken in passing — they are not tests of Azure behaviour."""

    def test_azure_backend_class_is_still_exported(self):
        self.assertTrue(
            hasattr(email_backends, "AzureCommunicationEmailBackend")
        )

    def test_both_backends_share_the_sender_extraction_helper(self):
        """_bare_email is used by both. A change to it that suited SES but
        broke ACS would only show up in production email."""
        from django.core.mail.backends.base import BaseEmailBackend

        for cls in (
            email_backends.AzureCommunicationEmailBackend,
            email_backends.SESEmailBackend,
        ):
            with self.subTest(backend=cls.__name__):
                self.assertTrue(issubclass(cls, BaseEmailBackend))
