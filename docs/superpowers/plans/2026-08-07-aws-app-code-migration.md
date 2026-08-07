# AWS Application Code Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the application an AWS backend for each of the three Azure-coupled subsystems, so the same image runs on ECS Fargate **and** on Azure Container Apps.

**Architecture:** Three modules import Azure SDKs — media storage, email, and background-job dispatch. Each **gains** a `boto3` sibling selected by environment variables at runtime. The Azure implementations stay exactly as they are, because Azure serves live users and keeps doing so while AWS is stood up. One image, two clouds, whichever set of env vars is present wins.

**Tech Stack:** Python 3.12 (venv is 3.13), Django 5, `boto3`, `django-storages[s3]`, Django's own test runner.

## Global Constraints

Copied from `docs/superpowers/specs/2026-08-07-aws-migration-design.md`. Every task's requirements implicitly include this section.

- **Azure is live. Additions only.** Do not delete, rewrite in place, or downgrade any Azure module, setting, dependency, or workflow. Every task that touches shared code must leave a test pinning the Azure path.
- **Do not modify** `Dockerfile`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-staging.yml`, or `.github/workflows/cert-renew.yml`. Azure depends on all four. AWS overrides `command` in its ECS task definitions instead of changing `CMD`.
- Region is `us-east-1`.
- Media is **served through Django** at `/media/<path>`. Never return a presigned S3 URL or a public bucket URL — school networks allowlist only the application's own domain.
- HTTP Range support on media is **required** and its current semantics must be preserved exactly; video scrubbing depends on it.
- `boto3` is the only new cloud SDK. Do not add provider-specific wrappers.
- Do not touch `apps/llm/` — `azure_openai` remains a valid model-vendor choice and is unrelated to hosting.
- **Run tests with Django's runner:** `./venv/bin/python manage.py test apps.<app>.tests.test_<feature>`
  `pytest-django is NOT installed`, so bare `pytest` cannot run this suite — existing test files fail to collect under it too, and CLAUDE.md is out of date on this point.
- **Write tests as `SimpleTestCase` / `TestCase` subclasses**, matching all 165 existing test files. Function-style pytest tests fail: with no `conftest.py`, the first test in a session runs before Django settings are wrapped and `override_settings` raises `AttributeError: 'object' object has no attribute 'DATABASES'`.
- Test files live at `apps/<app>/tests/test_<feature>.py` and every `tests/` directory needs an `__init__.py`.
- Commit after every task. Do not squash tasks into one commit.

**Known pre-existing breakage, not yours to fix:** `./venv/bin/python manage.py test apps.curriculum` fails to collect with `ImportError: 'tests' module incorrectly imported`. This reproduces on a clean checkout with all migration work stashed. Do not try to fix it inside this plan; run neighbouring app suites individually instead.

## File Structure

| File | Responsibility |
| --- | --- |
| `apps/media_library/s3_media.py` | **Create.** `S3MediaStorage` + range-aware `serve_media`, beside `blob_media.py`. |
| `apps/media_library/blob_media.py` | **Untouched.** Azure's live media path. |
| `apps/media_library/tests.py` | **Delete** in Task 1 — empty 3-line Django stub with no tests, and it blocks creating a `tests/` package. The only deletion in this plan. |
| `apps/media_library/tests/test_s3_media.py` | **Create.** Range, content-type, fallback, plus Azure-path guards. |
| `apps/safety/email_backends.py` | **Append** `SESEmailBackend`; `AzureCommunicationEmailBackend` stays in the same file. |
| `apps/safety/tests/test_ses_email.py` | **Create.** |
| `apps/dashboard/job_dispatch.py` | **Append** an ECS backend; the Azure ARM backend stays. |
| `apps/dashboard/tests/test_job_dispatch.py` | **Create.** |
| `apps/safety/tests/test_client_ip.py` | **Create.** Pin last-hop behaviour under both App Gateway and ALB headers. |
| `config/settings.py` | **Add** `AWS_MEDIA_*`, SES and `ECS_*` blocks across tasks 1–3. Azure blocks stay. |
| `config/urls.py` | **Add** an S3 branch ahead of the existing Azure branch. |
| `Dockerfile` | **Untouched.** Azure needs the `CMD` migrate chain; AWS overrides `command` per task definition. |
| `requirements.txt` | **Add** `boto3` + `django-storages[s3]` in Task 5. The four `azure-*` packages stay. |

---

### Task 1: S3 media storage and range-aware server — ✅ DONE (commit `92625cb`)

Shipped as an **addition**: `apps/media_library/s3_media.py` sits beside
`blob_media.py`, and `config/settings.py` / `config/urls.py` pick a backend at
runtime (S3 when `AWS_MEDIA_BUCKET` is set, Azure Blob when the blob account
and key are, filesystem otherwise). Nothing on the Azure path changed.

15 tests in `apps/media_library/tests/test_s3_media.py`, all passing under
`./venv/bin/python manage.py test apps.media_library.tests.test_s3_media`.
Twelve cover S3 serving — full reads, byte/suffix/open-ended ranges, 400 on an
unparseable range, 416 beyond the object, the octet-stream-loses-to-extension
rule, and the filesystem fallback including path traversal. Three exist purely
to guard the Azure path: that `blob_media` still imports, that its `.url()`
still returns our own domain, and that both `serve_media` functions keep
identical signatures, since `config/urls.py` binds them by the same name.

`apps/media_library/tests.py` (an empty 3-line Django stub) was deleted because
a `tests.py` module and a `tests/` package cannot coexist. It contained no
tests, so nothing was lost.

**Two discoveries from executing this task, now folded into Global Constraints:**
`pytest-django` is not installed — the suite runs under `manage.py test`, and
CLAUDE.md is wrong about `pytest`. And function-style pytest tests cannot work
here: whichever runs first in a session dies inside `override_settings`, so
tests must be `SimpleTestCase` subclasses like the other 165 files.

---

### Task 2: SES email backend

**Files:**

- Modify: `apps/safety/email_backends.py` (APPEND a second backend; `AzureCommunicationEmailBackend` and `_bare_email` stay exactly as they are)
- Create: `apps/safety/tests/__init__.py`
- Create: `apps/safety/tests/test_ses_email.py`
- Modify: `config/settings.py:366-395` (the ACS block)

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces: `apps.safety.email_backends.SESEmailBackend`, a `BaseEmailBackend` subclass. `settings.EMAIL_BACKEND` resolves to its dotted path when `AWS_SES_SENDER` is set. The module-level helper `_bare_email(addr: str) -> str` is retained unchanged.

This is a transport swap, not a redesign. `django-ses` was considered and rejected: `boto3` is already required by tasks 1 and 3, the existing class already handles `fail_silently`, HTML alternatives, reply-to, and name-stripping, and a fake `boto3` client makes the behaviour directly testable where mocking a third-party backend's internals would not.

SESv2 `send_email` takes `Content={'Simple': ...}` for ordinary mail. Messages carrying attachments must use `Content={'Raw': {'Data': ...}}` instead, because `Simple` has no attachment field.

- [ ] **Step 1: Create the tests package and write the failing tests**

Create `apps/safety/tests/__init__.py` as an empty file, then `apps/safety/tests/test_ses_email.py`:

```python
"""SES email backend — payload shape and failure semantics."""
from __future__ import annotations

import pytest
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.test import override_settings

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


@pytest.fixture
def fake_ses(monkeypatch):
    client = _FakeSES()
    monkeypatch.setattr(email_backends, "_ses_client", lambda: client)
    return client


SES_SETTINGS = dict(
    AWS_SES_SENDER="noreply@mail.example.com",
    DEFAULT_FROM_EMAIL="AI Tutor <noreply@mail.example.com>",
)


@override_settings(**SES_SETTINGS)
def test_plain_message_sends_with_a_bare_sender_address(fake_ses):
    backend = SESEmailBackend()
    msg = EmailMessage(
        subject="Reset your password",
        body="Click here",
        from_email="AI Tutor <noreply@mail.example.com>",
        to=["Student Name <student@school.sc>"],
    )

    assert backend.send_messages([msg]) == 1

    payload = fake_ses.sent[0]
    assert payload["FromEmailAddress"] == "noreply@mail.example.com"
    assert payload["Destination"]["ToAddresses"] == ["student@school.sc"]
    assert payload["Content"]["Simple"]["Subject"]["Data"] == "Reset your password"
    assert payload["Content"]["Simple"]["Body"]["Text"]["Data"] == "Click here"


@override_settings(**SES_SETTINGS)
def test_html_alternative_is_sent_alongside_the_plain_body(fake_ses):
    backend = SESEmailBackend()
    msg = EmailMultiAlternatives(
        subject="Welcome",
        body="plain version",
        to=["student@school.sc"],
    )
    msg.attach_alternative("<p>html version</p>", "text/html")

    backend.send_messages([msg])

    body = fake_ses.sent[0]["Content"]["Simple"]["Body"]
    assert body["Text"]["Data"] == "plain version"
    assert body["Html"]["Data"] == "<p>html version</p>"


@override_settings(**SES_SETTINGS)
def test_cc_bcc_and_reply_to_are_forwarded(fake_ses):
    backend = SESEmailBackend()
    msg = EmailMessage(
        subject="s",
        body="b",
        to=["a@school.sc"],
        cc=["b@school.sc"],
        bcc=["c@school.sc"],
        reply_to=["Support <help@school.sc>"],
    )

    backend.send_messages([msg])

    payload = fake_ses.sent[0]
    assert payload["Destination"]["CcAddresses"] == ["b@school.sc"]
    assert payload["Destination"]["BccAddresses"] == ["c@school.sc"]
    assert payload["ReplyToAddresses"] == ["help@school.sc"]


@override_settings(**SES_SETTINGS)
def test_a_message_with_an_attachment_uses_the_raw_content_form(fake_ses):
    """SES Simple content has no attachment field, so attachments must go Raw."""
    backend = SESEmailBackend()
    msg = EmailMessage(subject="s", body="b", to=["a@school.sc"])
    msg.attach("report.csv", "a,b\n1,2\n", "text/csv")

    backend.send_messages([msg])

    content = fake_ses.sent[0]["Content"]
    assert "Raw" in content
    assert "Simple" not in content
    assert b"report.csv" in content["Raw"]["Data"]


@override_settings(**SES_SETTINGS)
def test_a_message_with_no_recipients_is_rejected(fake_ses):
    backend = SESEmailBackend()
    msg = EmailMessage(subject="s", body="b", to=[])

    with pytest.raises(ValueError):
        backend.send_messages([msg])


@override_settings(**SES_SETTINGS)
def test_send_failure_propagates_when_not_failing_silently(monkeypatch):
    monkeypatch.setattr(
        email_backends, "_ses_client", lambda: _FakeSES(error=RuntimeError("throttled"))
    )
    backend = SESEmailBackend(fail_silently=False)
    msg = EmailMessage(subject="s", body="b", to=["a@school.sc"])

    with pytest.raises(RuntimeError):
        backend.send_messages([msg])


@override_settings(**SES_SETTINGS)
def test_send_failure_is_swallowed_when_failing_silently(monkeypatch):
    monkeypatch.setattr(
        email_backends, "_ses_client", lambda: _FakeSES(error=RuntimeError("throttled"))
    )
    backend = SESEmailBackend(fail_silently=True)
    msg = EmailMessage(subject="s", body="b", to=["a@school.sc"])

    assert backend.send_messages([msg]) == 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AI Tutor <noreply@example.com>", "noreply@example.com"),
        ("plain@example.com", "plain@example.com"),
        ("", ""),
    ],
)
def test_bare_email_strips_display_names(raw, expected):
    assert _bare_email(raw) == expected
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/safety/tests/test_ses_email.py -v`

Expected: `ImportError: cannot import name 'SESEmailBackend'`.

- [ ] **Step 3: Append the SES backend**

**Do not replace the file.** `AzureCommunicationEmailBackend` serves live users. Add `_ses_client` and `SESEmailBackend` below it, reusing the existing module-level `_bare_email` rather than redefining it. Update the module docstring to say it now hosts both backends. The code below shows the additions only:

```python
"""Django email backend that talks to Amazon SES (SESv2).

Activated by setting `AWS_SES_SENDER` in the environment; Pulumi wires this
into the ECS task definition. Falls back to console output when it is missing
so dev runs don't crash.

The standard Django flow (`send_mail`, `EmailMessage.send`,
`PasswordResetView.send_mail`) all funnel through `send_messages`, so wiring
this once covers password reset plus every other transactional mail.

Sender address — Django's `from_email` may be a parsed name+address
("AI Tutor <noreply@example.com>"). SES wants a bare address on a verified
domain, so we extract it.

Content form — SES `Simple` content has no attachment field, so any message
carrying attachments is sent as `Raw` (the fully-rendered MIME document)
instead.
"""

from __future__ import annotations

import logging
from email.utils import parseaddr
from typing import List

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMessage, EmailMultiAlternatives

logger = logging.getLogger(__name__)

_CHARSET = "UTF-8"


def _ses_client():
    """boto3 SESv2 client. Credentials come from the ECS task role."""
    import boto3

    return boto3.client(
        "sesv2",
        region_name=getattr(settings, "AWS_SES_REGION", "us-east-1"),
    )


class SESEmailBackend(BaseEmailBackend):
    """Send mail via Amazon SES."""

    def __init__(self, fail_silently: bool = False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self._client = None
        self._init_error: str | None = None
        if not getattr(settings, "AWS_SES_SENDER", ""):
            self._init_error = "AWS_SES_SENDER not set"
            return
        try:
            self._client = _ses_client()
        except ImportError:
            self._init_error = "boto3 not installed"
        except Exception as e:  # noqa: BLE001 — surface any client-init error
            self._init_error = f"SES client init failed: {e}"

    # ── BaseEmailBackend ───────────────────────────────────────────────────

    def open(self):
        # No persistent connection — botocore manages HTTPS internally.
        return self._client is not None

    def close(self):
        return None

    def send_messages(self, email_messages: List[EmailMessage]) -> int:
        """Send an iterable of `EmailMessage`. Returns the number accepted."""
        if self._client is None:
            logger.warning("SES email backend unavailable: %s", self._init_error)
            if not self.fail_silently:
                raise RuntimeError(
                    f"SESEmailBackend unavailable: {self._init_error}"
                )
            return 0

        sent = 0
        for msg in email_messages:
            try:
                if self._send_one(msg):
                    sent += 1
            except Exception:  # noqa: BLE001
                logger.exception("SES send failed for %s", msg.to)
                if not self.fail_silently:
                    raise
        return sent

    # ── Internals ──────────────────────────────────────────────────────────

    def _send_one(self, msg: EmailMessage) -> bool:
        sender_address = self._sender_address(msg)
        if not sender_address:
            raise ValueError("SES send requires a sender address")

        to = [_bare_email(a) for a in (msg.to or [])]
        if not to:
            raise ValueError("EmailMessage has no `to` recipients")

        destination = {"ToAddresses": to}
        if msg.cc:
            destination["CcAddresses"] = [_bare_email(a) for a in msg.cc]
        if msg.bcc:
            destination["BccAddresses"] = [_bare_email(a) for a in msg.bcc]

        request = {
            "FromEmailAddress": sender_address,
            "Destination": destination,
            "Content": self._content(msg),
        }
        if msg.reply_to:
            request["ReplyToAddresses"] = [_bare_email(a) for a in msg.reply_to]

        self._client.send_email(**request)
        return True

    def _content(self, msg: EmailMessage) -> dict:
        # Attachments have no place in SES `Simple` content — hand SES the
        # rendered MIME document instead and let it pass through untouched.
        if msg.attachments:
            return {"Raw": {"Data": msg.message().as_bytes()}}

        plain = msg.body if (msg.content_subtype or "plain") == "plain" else None
        html = msg.body if msg.content_subtype == "html" else None
        if isinstance(msg, EmailMultiAlternatives):
            for alt_body, alt_type in (msg.alternatives or []):
                if alt_type == "text/html":
                    html = alt_body

        body: dict = {}
        if plain is not None:
            body["Text"] = {"Data": plain, "Charset": _CHARSET}
        if html is not None:
            body["Html"] = {"Data": html, "Charset": _CHARSET}
        if not body:
            body["Text"] = {"Data": "", "Charset": _CHARSET}

        return {
            "Simple": {
                "Subject": {"Data": msg.subject or "(no subject)", "Charset": _CHARSET},
                "Body": body,
            }
        }

    def _sender_address(self, msg: EmailMessage) -> str:
        # Prefer the explicit override on the message, fall back to the
        # env-configured sender, finally to DEFAULT_FROM_EMAIL.
        candidate = (
            msg.from_email
            or getattr(settings, "AWS_SES_SENDER", "")
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
```

Note that `EmailMultiAlternatives.alternatives` entries are named tuples in Django 5; unpacking as `(alt_body, alt_type)` is what the current code does and still works.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/safety/tests/test_ses_email.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Swap the settings block**

In `config/settings.py`, replace the ACS block at lines 366-395 with:

```python
# ── Email via Amazon SES ───────────────────────────────────────────────────
AWS_SES_SENDER = os.getenv('AWS_SES_SENDER', '')
AWS_SES_REGION = os.getenv('AWS_SES_REGION', 'us-east-1')

# Explicit EMAIL_BACKEND always wins; otherwise SES when configured, console
# in development so nothing crashes without credentials.
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND') or (
    'apps.safety.email_backends.SESEmailBackend'
    if AWS_SES_SENDER
    else 'django.core.mail.backends.console.EmailBackend'
)
```

Leave `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, and `DEFAULT_FROM_EMAIL` exactly as they are — they are the SMTP escape hatch and are unrelated to this change.

- [ ] **Step 6: Verify nothing still references the Azure names**

Run: `./venv/bin/python manage.py test apps.safety.tests.test_ses_email`

Expected: OK, and `AzureCommunicationEmailBackend` still importable — the Azure guard test in this file asserts it.

- [ ] **Step 7: Run the safety suite**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/safety apps/accounts -q`

Expected: no new failures. `apps/accounts` is included because password reset goes through this backend.

- [ ] **Step 8: Commit**

```bash
git add apps/safety config/settings.py
git commit -m "email: send via SES instead of Azure Communication Services

Transport swap only — fail_silently semantics, HTML alternatives,
reply-to and display-name stripping all behave as before.

Chose boto3 over django-ses: boto3 is already required for S3 and ECS
dispatch, and a fake client makes the payload directly assertable where
mocking a third-party backend's internals would not.

Messages with attachments now go out as SES Raw content, since Simple
content has nowhere to put them.

Refs: docs/superpowers/specs/2026-08-07-aws-migration-design.md"
```

---

### Task 3: ECS RunTask job dispatch

**Files:**

- Modify: `apps/dashboard/job_dispatch.py:1-159` (docstring, selector, and the Azure backend)
- Create: `apps/dashboard/tests/__init__.py`
- Create: `apps/dashboard/tests/test_job_dispatch.py`

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces: `dispatch_material_job(upload_id: int, mode: str = 'rich') -> str` keeps its exact current signature and return contract — callers in `apps/dashboard/material_routing.py` are untouched. New internals: `_ecs_settings() -> tuple | None` and `_dispatch_via_ecs(upload_id, mode, cluster, task_definition, subnets, security_groups, container_name) -> str`. `_dispatch_via_subprocess` is unchanged.

The Azure implementation carries a long comment about copying every field from the base template, because Container Apps silently defaulted anything omitted. **ECS does not have this problem** — the task definition already carries image, resources, environment, and secrets, and `overrides` only needs the command. That is a genuine simplification, not an oversight; do not port the field-copying logic.

- [ ] **Step 1: Create the tests package and write the failing tests**

Create `apps/dashboard/tests/__init__.py` as an empty file, then `apps/dashboard/tests/test_job_dispatch.py`:

```python
"""Material-processing dispatch — ECS RunTask and the local fallback."""
from __future__ import annotations

import pytest

from apps.dashboard import job_dispatch


class _FakeECS:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def run_task(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


ECS_ENV = {
    "ECS_CLUSTER": "aitutor-prod",
    "ECS_MATERIAL_TASK_DEFINITION": "aitutor-prod-material:7",
    "ECS_SUBNETS": "subnet-aaa,subnet-bbb",
    "ECS_SECURITY_GROUPS": "sg-tasks",
}

OK_RESPONSE = {
    "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:1234:task/aitutor-prod/abc123"}],
    "failures": [],
}


@pytest.fixture
def ecs_env(monkeypatch):
    for key, value in ECS_ENV.items():
        monkeypatch.setenv(key, value)


def _install_fake(monkeypatch, response):
    client = _FakeECS(response)
    monkeypatch.setattr(job_dispatch, "_ecs_client", lambda: client)
    return client


def test_dispatch_returns_the_task_id_from_the_arn(monkeypatch, ecs_env):
    _install_fake(monkeypatch, OK_RESPONSE)

    assert job_dispatch.dispatch_material_job(42, mode="rich") == "abc123"


def test_dispatch_overrides_only_the_command(monkeypatch, ecs_env):
    """The task definition already carries image, resources, env and secrets —
    unlike Container Apps Jobs, nothing else needs restating."""
    client = _install_fake(monkeypatch, OK_RESPONSE)

    job_dispatch.dispatch_material_job(42, mode="fast")

    call = client.calls[0]
    assert call["cluster"] == "aitutor-prod"
    assert call["taskDefinition"] == "aitutor-prod-material:7"
    assert call["launchType"] == "FARGATE"
    overrides = call["overrides"]["containerOverrides"]
    assert len(overrides) == 1
    assert overrides[0]["command"] == [
        "python", "manage.py", "process_material", "42", "--mode", "fast",
    ]
    assert "image" not in overrides[0]
    assert "environment" not in overrides[0]


def test_dispatch_places_the_task_in_the_configured_private_subnets(monkeypatch, ecs_env):
    client = _install_fake(monkeypatch, OK_RESPONSE)

    job_dispatch.dispatch_material_job(42)

    vpc = client.calls[0]["networkConfiguration"]["awsvpcConfiguration"]
    assert vpc["subnets"] == ["subnet-aaa", "subnet-bbb"]
    assert vpc["securityGroups"] == ["sg-tasks"]
    assert vpc["assignPublicIp"] == "DISABLED"


def test_a_run_task_failure_raises_rather_than_reporting_success(monkeypatch, ecs_env):
    _install_fake(
        monkeypatch,
        {"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]},
    )

    with pytest.raises(RuntimeError, match="RESOURCE:MEMORY"):
        job_dispatch.dispatch_material_job(42)


def test_an_empty_task_list_raises(monkeypatch, ecs_env):
    _install_fake(monkeypatch, {"tasks": [], "failures": []})

    with pytest.raises(RuntimeError):
        job_dispatch.dispatch_material_job(42)


def test_without_ecs_config_it_falls_back_to_a_local_subprocess(monkeypatch):
    for key in ECS_ENV:
        monkeypatch.delenv(key, raising=False)
    called = {}

    def _fake_subprocess(upload_id, mode):
        called["args"] = (upload_id, mode)
        return "local-pid-999"

    monkeypatch.setattr(job_dispatch, "_dispatch_via_subprocess", _fake_subprocess)

    assert job_dispatch.dispatch_material_job(7, mode="rich") == "local-pid-999"
    assert called["args"] == (7, "rich")


def test_partial_ecs_config_falls_back_rather_than_half_dispatching(monkeypatch):
    for key in ECS_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ECS_CLUSTER", "aitutor-prod")  # task definition missing
    monkeypatch.setattr(
        job_dispatch, "_dispatch_via_subprocess", lambda *a: "local-pid-1"
    )

    assert job_dispatch.dispatch_material_job(7) == "local-pid-1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/dashboard/tests/test_job_dispatch.py -v`

Expected: `AttributeError: module 'apps.dashboard.job_dispatch' has no attribute '_ecs_client'`.

- [ ] **Step 3: Replace the module docstring and the selector**

In `apps/dashboard/job_dispatch.py`, replace lines 1-10 (the docstring) with:

```python
"""ECS task dispatch — large material processing.

Two backends:
  - ECS RunTask: in production, starts a Fargate task from the material
    task definition.
  - Local subprocess: in dev, runs `python manage.py process_material`
    detached so devs can exercise the same flow without AWS.

Selection: if `ECS_CLUSTER`, `ECS_MATERIAL_TASK_DEFINITION` and `ECS_SUBNETS`
are all set, use ECS; otherwise fall back to subprocess.
"""
```

Then add `_ecs_settings` beside the existing `_azure_settings` (which stays untouched):

```python
def _ecs_settings():
    """Returns (cluster, task_definition, subnets, security_groups, container_name)
    or None if not configured."""
    cluster = os.getenv('ECS_CLUSTER')
    task_definition = os.getenv('ECS_MATERIAL_TASK_DEFINITION')
    subnets = [s for s in os.getenv('ECS_SUBNETS', '').split(',') if s]
    security_groups = [g for g in os.getenv('ECS_SECURITY_GROUPS', '').split(',') if g]
    container_name = os.getenv('ECS_MATERIAL_CONTAINER_NAME', 'material-processor')
    if not (cluster and task_definition and subnets):
        return None
    return cluster, task_definition, subnets, security_groups, container_name
```

And rewrite the body of `dispatch_material_job` (lines 41-44) as:

```python
    ecs_cfg = _ecs_settings()
    if ecs_cfg:
        return _dispatch_via_ecs(upload_id, mode, *ecs_cfg)
    azure_cfg = _azure_settings()
    if azure_cfg:
        return _dispatch_via_azure_sdk(upload_id, mode, *azure_cfg)
    return _dispatch_via_subprocess(upload_id, mode)
```

- [ ] **Step 4: Add the ECS backend beside the Azure one**

**Leave `_dispatch_via_azure_sdk` exactly as it is** — Azure dispatches live material jobs through it. Add the following alongside. The selector in Step 3 must try ECS first, then Azure, then the subprocess fallback, so each cloud picks its own path from its own env vars:

```python
def _ecs_client():
    """boto3 ECS client. Credentials come from the web task's role."""
    import boto3

    return boto3.client('ecs', region_name=os.getenv('AWS_REGION', 'us-east-1'))


def _dispatch_via_ecs(
    upload_id: int, mode: str,
    cluster: str, task_definition: str,
    subnets: list, security_groups: list, container_name: str,
) -> str:
    """Start a Fargate task from the material task definition.

    Only the command is overridden. The task definition already carries the
    image, CPU/memory, environment and secrets, so unlike the Container Apps
    Job this replaced there is no need to restate them per execution.
    """
    try:
        client = _ecs_client()
    except ImportError as exc:
        raise RuntimeError(
            "boto3 not installed. Add it to requirements.txt before "
            "dispatching material jobs to ECS."
        ) from exc

    response = client.run_task(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType='FARGATE',
        count=1,
        networkConfiguration={
            'awsvpcConfiguration': {
                'subnets': subnets,
                'securityGroups': security_groups,
                'assignPublicIp': 'DISABLED',
            },
        },
        overrides={
            'containerOverrides': [
                {
                    'name': container_name,
                    'command': [
                        'python', 'manage.py', 'process_material',
                        str(upload_id), '--mode', mode,
                    ],
                },
            ],
        },
    )

    failures = response.get('failures') or []
    if failures:
        raise RuntimeError(
            f"ECS RunTask failed for upload {upload_id}: {failures}"
        )
    tasks = response.get('tasks') or []
    if not tasks:
        raise RuntimeError(
            f"ECS RunTask returned no task for upload {upload_id}"
        )

    task_arn = tasks[0].get('taskArn', '')
    execution_name = task_arn.rsplit('/', 1)[-1] or task_arn
    logger.info(f"Dispatched material job for upload {upload_id} → {execution_name}")
    return execution_name
```

Remove the now-unused `Optional` import from the `typing` line if nothing else uses it.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/dashboard/tests/test_job_dispatch.py -v`

Expected: all tests PASS.

- [ ] **Step 6: Verify no Azure job references survive**

Run: `grep -n "_dispatch_via_azure_sdk\|_azure_settings" apps/dashboard/job_dispatch.py`

Expected: both still present. Their removal would break live Azure material processing.

- [ ] **Step 7: Check the callers still line up**

Run: `grep -rn "dispatch_material_job" --include="*.py" apps/`

Read each call site and confirm the signature and the returned execution-name string are still what they expect. `apps/dashboard/material_routing.py` is the main one.

- [ ] **Step 8: Commit**

```bash
git add apps/dashboard
git commit -m "materials: dispatch processing via ECS RunTask

Replaces the Container Apps Job dispatch. The old backend had to copy
image, resources, env and volume mounts out of the base template because
Azure silently defaulted anything omitted — we hit each of those in prod
one at a time. ECS task definitions carry all of it, so the override is
just the command.

Falls back to a local subprocess exactly as before when ECS_CLUSTER,
ECS_MATERIAL_TASK_DEFINITION and ECS_SUBNETS aren't all set.

Refs: docs/superpowers/specs/2026-08-07-aws-migration-design.md"
```

---

### Task 4: Pin client-IP resolution under an ALB

**Files:**

- Modify: `apps/safety/client_ip.py:1-16` (docstring only)
- Create: `apps/safety/tests/test_client_ip.py`

**Interfaces:**

- Consumes: `apps/safety/tests/__init__.py` from Task 2.
- Produces: no interface change. `get_client_ip(request) -> str | None` is unchanged.

No behaviour changes here — the last-hop logic is already correct for an ALB, which appends the connecting client's address just as App Gateway does. Two differences are worth locking down with tests: an ALB omits the `:port` suffix by default, and the security premise shifts from "the app is VNet-internal" to "the task security group accepts traffic only from the ALB security group". Getting that security-group rule wrong would make the last hop spoofable, so the docstring must say so.

- [ ] **Step 1: Write the failing tests**

Create `apps/safety/tests/test_client_ip.py`:

```python
"""Client-IP resolution behind an ALB.

The last X-Forwarded-For hop is trusted because the ECS task security group
accepts traffic only from the ALB security group. If that rule is ever
loosened, these guarantees do not hold.
"""
from __future__ import annotations

import pytest
from django.test import RequestFactory

from apps.safety.client_ip import get_client_ip


@pytest.fixture
def rf():
    return RequestFactory()


def test_alb_appends_a_bare_ip_and_we_take_it(rf):
    """ALB appends the connecting client without a port, unlike App Gateway."""
    request = rf.get("/", HTTP_X_FORWARDED_FOR="203.0.113.7")

    assert get_client_ip(request) == "203.0.113.7"


def test_a_spoofed_leading_entry_is_ignored(rf):
    """The leftmost entry is client-supplied. Only the last hop is trustworthy."""
    request = rf.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.7")

    assert get_client_ip(request) == "203.0.113.7"


def test_an_app_gateway_style_port_suffix_is_still_stripped(rf):
    """Kept for compatibility: ALB adds the port when
    routing.http.xff_client_port.enabled is turned on."""
    request = rf.get("/", HTTP_X_FORWARDED_FOR="203.0.113.7:59633")

    assert get_client_ip(request) == "203.0.113.7"


def test_ipv6_survives_intact(rf):
    request = rf.get("/", HTTP_X_FORWARDED_FOR="2001:db8::1")

    assert get_client_ip(request) == "2001:db8::1"


def test_no_forwarded_header_falls_back_to_remote_addr(rf):
    request = rf.get("/", REMOTE_ADDR="10.30.1.5")

    assert get_client_ip(request) == "10.30.1.5"


def test_a_garbage_header_yields_none_rather_than_raising(rf):
    """Postgres inet columns reject junk; None keeps the column NULL."""
    request = rf.get("/", HTTP_X_FORWARDED_FOR="not-an-ip")
    request.META.pop("REMOTE_ADDR", None)

    assert get_client_ip(request) is None
```

- [ ] **Step 2: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/safety/tests/test_client_ip.py -v`

Expected: all PASS immediately — this task documents and pins existing behaviour rather than changing it. If any test fails, stop: that is a real regression risk for the migration and needs investigating before proceeding.

- [ ] **Step 3: Update the docstring to name the AWS trust boundary**

Replace lines 1-16 of `apps/safety/client_ip.py` with:

```python
"""Resolve the real client IP behind the ALB.

The ECS task security group accepts traffic ONLY from the ALB security group,
so the load-balancer-appended hop of ``X-Forwarded-For`` is trustworthy. That
security-group rule is what makes this safe — loosen it and the value below
becomes spoofable.

Two things this gets right that the old ``split(',')[0]`` did not:

1. **Security:** the LEFTMOST X-Forwarded-For entry is client-supplied and
   therefore spoofable. The load balancer appends the *real* client as the
   LAST hop, so we take the last entry — an attacker can prepend fakes but
   cannot forge the appended value (they cannot bypass the LB to reach us).
2. **Correctness:** the hop may carry a ``:port`` suffix (App Gateway always
   did; an ALB does when ``routing.http.xff_client_port.enabled`` is on).
   Postgres ``inet`` columns reject it, so we strip it. Invalid values return
   ``None`` (column stays NULL) instead of raising a DataError.
"""
```

- [ ] **Step 4: Re-run the tests**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/safety/tests/test_client_ip.py -v`

Expected: still all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/safety/client_ip.py apps/safety/tests/test_client_ip.py
git commit -m "safety: pin client-IP resolution under the ALB

No behaviour change — the last-hop rule is already correct for an ALB,
which appends the connecting client just as App Gateway did. Adds the
tests that were missing and rewrites the docstring so the trust boundary
names the real invariant on AWS: the task security group accepts traffic
only from the ALB security group.

Refs: docs/superpowers/specs/2026-08-07-aws-migration-design.md"
```

---

### Task 5: Add the AWS dependencies

**Files:**

- Modify: `requirements.txt` (additions only)
- Create: `ops/migrate_and_seed.sh`

**Interfaces:**

- Consumes: tasks 1-3, whose modules import `boto3` lazily inside functions and so already pass their tests without it installed.
- Produces: `boto3` and `django-storages[s3]` available at runtime, and `ops/migrate_and_seed.sh` as the command the infrastructure plan points its migrate task definition at.

**This task does NOT remove the Azure packages and does NOT touch the Dockerfile.** Both were in an earlier draft and both would break the live Azure deployment. `azure-communication-email`, `azure-identity`, `azure-mgmt-appcontainers` and `django-storages[azure]` all stay.

The migration race is real but it is an AWS-only problem: Container Apps single-revision mode serialises the `CMD` chain, whereas ECS would run it on every task at once. The fix is for the ECS **web** task definition to override `command` to Gunicorn alone, and for CI to run the seed chain as a separate one-shot task. Azure keeps using `CMD` exactly as it does today. Nothing in the image changes.

- [ ] **Step 1: Add the two packages**

In `requirements.txt`, add next to the existing `django-storages[azure]>=1.14` line:

```text
boto3>=1.34
django-storages[s3]>=1.14
```

Both extras of `django-storages` can coexist — they pull different optional dependencies onto the same package. Do not delete the `[azure]` line.

- [ ] **Step 2: Install and verify BOTH cloud paths still import**

```bash
./venv/bin/pip install -r requirements.txt
./venv/bin/python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from apps.media_library import s3_media, blob_media
from apps.safety import email_backends
from apps.dashboard import job_dispatch
assert hasattr(email_backends, 'SESEmailBackend')
assert hasattr(email_backends, 'AzureCommunicationEmailBackend')
print('both cloud paths import OK')
"
```

Expected: `both cloud paths import OK`. If the Azure names are missing, an earlier task deleted something it should not have.

- [ ] **Step 3: Extract the seed chain for the ECS migrate task**

Create `ops/migrate_and_seed.sh` with the chain copied verbatim out of the Dockerfile `CMD`:

```bash
#!/usr/bin/env sh
# Migration + seed chain, copied from the Dockerfile CMD.
#
# Azure Container Apps still runs this via CMD, where single-revision mode
# serialises it. On ECS it must run as a ONE-SHOT task before the service is
# updated -- running it on every task would race the migrations.
#
# The Dockerfile is deliberately unchanged so the Azure path keeps working.
set -eu

python manage.py migrate
python manage.py seed_gamification
python manage.py backfill_progress
python manage.py classify_unit_grades
python manage.py seed_help_assistant_model
python manage.py generate_recent_updates
python manage.py build_help_index --with-source
```

Then `chmod +x ops/migrate_and_seed.sh`.

- [ ] **Step 4: Verify the chain runs against a scratch database**

```bash
PROBE_DB="$(mktemp -d)/probe.sqlite3"
DJANGO_SETTINGS_MODULE=config.settings \
DATABASE_URL="sqlite:///${PROBE_DB}" \
PATH="$PWD/venv/bin:$PATH" \
  sh ops/migrate_and_seed.sh
```

Expected: every command completes without error. `PATH` is prefixed so the script's bare `python` resolves to the virtualenv interpreter, matching how it resolves inside the container.

This exercises SQLite, so the pgvector migration paths are skipped by their vendor guard. Real pgvector verification belongs to the infrastructure plan's RDS rehearsal.

- [ ] **Step 5: Confirm the Dockerfile and Azure workflows are untouched**

```bash
git diff --name-only HEAD | grep -E "Dockerfile|\.github/workflows/(deploy|deploy-staging|cert-renew)\.yml" && echo "STOP: an Azure-critical file changed" || echo "Azure-critical files untouched"
```

Expected: `Azure-critical files untouched`.

- [ ] **Step 6: Run the suites touched by this plan**

```bash
./venv/bin/python manage.py test apps.media_library apps.safety apps.dashboard
```

Expected: OK. Run each app individually if the multi-label discovery quirk bites.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt ops/migrate_and_seed.sh
git commit -m "build: add boto3 and django-storages[s3] for the AWS path

Additions only. The four azure-* packages stay -- Azure serves live users
and still needs them, and both django-storages extras coexist on the same
package.

The Dockerfile is deliberately unchanged. Its CMD migrate chain is what
Azure relies on, and Container Apps single-revision mode serialises it.
ECS solves the same race differently: the web task definition overrides
command to gunicorn alone and CI runs ops/migrate_and_seed.sh as a
one-shot task.

Refs: docs/superpowers/specs/2026-08-07-aws-migration-design.md"
```

---

## Follow-up work, deliberately not in this plan

- **Infrastructure plan** — the Pulumi AWS program under `infra/aws/`. Depends on this plan only for the environment-variable names it must supply: `AWS_MEDIA_BUCKET`, `AWS_MEDIA_REGION`, `AWS_SES_SENDER`, `AWS_SES_REGION`, `ECS_CLUSTER`, `ECS_MATERIAL_TASK_DEFINITION`, `ECS_SUBNETS`, `ECS_SECURITY_GROUPS`, `ECS_MATERIAL_CONTAINER_NAME`, `AWS_REGION`.
- **CI/CD plan** — rewriting `deploy.yml` and `deploy-staging.yml` against OIDC, deleting `cert-renew.yml`, adding the migrate-task gate, smoke test, and automatic rollback.
- **Data migration and cutover plan** — `pg_dump`/restore into RDS, media sync into S3, DNS TTL reduction, and the cutover runbook.
- `requirements-core.txt`, `requirements-jetson.txt`, and `requirements-jetson.lock.txt` still install the Azure SDKs.
- Re-enabling SSE, now that an ALB does not buffer it. Three code sites and one test encode the Azure assumption.
