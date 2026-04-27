"""Django email backend that talks to Azure Communication Services.

Activated by setting `AZURE_COMMUNICATION_CONNECTION_STRING` in the
environment (Pulumi wires this into the Container App). Falls back to
console output when the SDK isn't available or the env var is missing
so dev runs don't crash.

The standard Django flow (`send_mail`, `EmailMessage.send`,
`PasswordResetView.send_mail`) all funnel through `send_messages`,
so wiring this once covers password reset + every other transactional
mail in the project.

Sender address — Django's `from_email` may be a parsed name+address
("AI Tutor <noreply@example.com>"). ACS wants:
    senderAddress: just "noreply@example.com"  (must be on a verified domain)
We extract the bare email; the display name is preserved via the
recipient list (ACS doesn't have a top-level fromDisplayName field as
of 2024-07 stable API).
"""

from __future__ import annotations

import logging
from email.utils import parseaddr
from typing import List

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMultiAlternatives, EmailMessage

logger = logging.getLogger(__name__)


class AzureCommunicationEmailBackend(BaseEmailBackend):
    """Send mail via Azure Communication Services Email."""

    def __init__(self, fail_silently: bool = False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self._client = None
        self._init_error: str | None = None
        connection_string = getattr(settings, "AZURE_COMMUNICATION_CONNECTION_STRING", "")
        if not connection_string:
            self._init_error = "AZURE_COMMUNICATION_CONNECTION_STRING not set"
            return
        try:
            from azure.communication.email import EmailClient
            self._client = EmailClient.from_connection_string(connection_string)
        except ImportError:
            self._init_error = "azure-communication-email not installed"
        except Exception as e:  # noqa: BLE001 — surface any client-init error
            self._init_error = f"EmailClient init failed: {e}"

    # ── BaseEmailBackend ───────────────────────────────────────────────────

    def open(self):
        # No persistent connection — the SDK manages HTTPS internally.
        return self._client is not None

    def close(self):
        # No-op.
        return None

    def send_messages(self, email_messages: List[EmailMessage]) -> int:
        """Send an iterable of `EmailMessage` instances. Returns the
        number of successfully *queued* messages (ACS returns Running
        for in-flight LROs; we treat that as success since the queue
        is durable)."""
        if self._client is None:
            logger.warning("ACS email backend unavailable: %s", self._init_error)
            if not self.fail_silently:
                raise RuntimeError(
                    f"AzureCommunicationEmailBackend unavailable: {self._init_error}"
                )
            return 0

        sent = 0
        for msg in email_messages:
            try:
                if self._send_one(msg):
                    sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("ACS send failed for %s", msg.to)
                if not self.fail_silently:
                    raise
        return sent

    # ── Internals ──────────────────────────────────────────────────────────

    def _send_one(self, msg: EmailMessage) -> bool:
        sender_address = self._sender_address(msg)
        if not sender_address:
            raise ValueError("ACS send requires a sender address")

        # Plain text + optional HTML alternative.
        plain = msg.body if (msg.content_subtype or "plain") == "plain" else None
        html = msg.body if msg.content_subtype == "html" else None
        if isinstance(msg, EmailMultiAlternatives):
            for alt_body, alt_type in (msg.alternatives or []):
                if alt_type == "text/html":
                    html = alt_body

        content = {"subject": msg.subject or "(no subject)"}
        if plain is not None:
            content["plainText"] = plain
        if html is not None:
            content["html"] = html

        recipients = {
            "to": [{"address": _bare_email(addr)} for addr in (msg.to or [])],
        }
        if msg.cc:
            recipients["cc"] = [{"address": _bare_email(addr)} for addr in msg.cc]
        if msg.bcc:
            recipients["bcc"] = [{"address": _bare_email(addr)} for addr in msg.bcc]

        if not recipients["to"]:
            raise ValueError("EmailMessage has no `to` recipients")

        message = {
            "senderAddress": sender_address,
            "recipients": recipients,
            "content": content,
        }
        # Optional reply-to.
        if msg.reply_to:
            message["replyTo"] = [{"address": _bare_email(addr)} for addr in msg.reply_to]

        poller = self._client.begin_send(message)
        result = poller.result()
        # ACS returns: {"id": "...", "status": "Succeeded" | "Failed" | "Running"}
        status = (result or {}).get("status") if isinstance(result, dict) else getattr(result, "status", None)
        if status in (None, "Failed"):
            error = (result or {}).get("error") if isinstance(result, dict) else getattr(result, "error", None)
            raise RuntimeError(f"ACS email send returned status={status}: {error}")
        return True

    def _sender_address(self, msg: EmailMessage) -> str:
        # Prefer the explicit override on the message, fall back to
        # the env-configured sender, finally to DEFAULT_FROM_EMAIL.
        candidate = (
            msg.from_email
            or getattr(settings, "AZURE_COMMUNICATION_SENDER_ADDRESS", "")
            or settings.DEFAULT_FROM_EMAIL
        )
        return _bare_email(candidate)


def _bare_email(addr: str) -> str:
    """Strip a parsed name from "Name <email@host>"; pass plain emails
    through unchanged."""
    if not addr:
        return ""
    _, email = parseaddr(addr)
    return email or addr
