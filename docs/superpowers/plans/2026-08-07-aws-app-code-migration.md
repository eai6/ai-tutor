# AWS Application Code Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every Azure SDK on the application's request path with an AWS equivalent, so the container image can run on ECS Fargate without any Azure dependency.

**Architecture:** Three modules currently import Azure SDKs — media storage, email, and background-job dispatch. Each is rewritten against `boto3` while preserving its existing public interface, so callers elsewhere in the codebase are untouched. The Azure implementations are deleted rather than kept behind a flag: this branch targets AWS only, and Azure production continues to run the image built from `main` until cutover.

**Tech Stack:** Python 3.12, Django 5, `boto3`, `django-storages[s3]`, pytest with pytest-django.

## Global Constraints

Copied from `docs/superpowers/specs/2026-08-07-aws-migration-design.md`. Every task's requirements implicitly include this section.

- Region is `us-east-1`.
- Media is **served through Django** at `/media/<path>`. Never return a presigned S3 URL or a public bucket URL — school networks allowlist only the application's own domain.
- HTTP Range support on media is **required** and its current semantics must be preserved exactly; video scrubbing depends on it.
- `boto3` is the only new cloud SDK. Do not add provider-specific wrappers.
- Do not touch `apps/llm/` — `azure_openai` remains a valid model-vendor choice and is unrelated to hosting.
- Run tests with: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest <path> -v`
- There is no `pytest.ini` or `conftest.py`; `DJANGO_SETTINGS_MODULE` must be set on the command line.
- Test files live at `apps/<app>/tests/test_<feature>.py` and every `tests/` directory needs an `__init__.py`.
- Commit after every task. Do not squash tasks into one commit.

## File Structure

| File | Responsibility |
| --- | --- |
| `apps/media_library/s3_media.py` | **Create.** `S3MediaStorage` + range-aware `serve_media`. Replaces `blob_media.py`. |
| `apps/media_library/blob_media.py` | **Delete** in Task 1. |
| `apps/media_library/tests.py` | **Delete** in Task 1 — 3-line Django stub, and it blocks creating a `tests/` package. |
| `apps/media_library/tests/test_s3_media.py` | **Create.** Range, content-type, and fallback behaviour. |
| `apps/safety/email_backends.py` | **Rewrite.** `AzureCommunicationEmailBackend` → `SESEmailBackend`. |
| `apps/safety/tests/test_ses_email.py` | **Create.** |
| `apps/dashboard/job_dispatch.py` | **Modify.** Azure ARM backend → ECS `RunTask`. |
| `apps/dashboard/tests/test_job_dispatch.py` | **Create.** |
| `apps/safety/tests/test_client_ip.py` | **Create.** Pin last-hop behaviour under ALB-shaped headers. |
| `config/settings.py` | **Modify** across tasks 1–3: `AZURE_BLOB_MEDIA_*` → `AWS_MEDIA_*`, ACS → SES, job vars → `ECS_*`. |
| `config/urls.py` | **Modify** in Task 1: media route points at the S3 server. |
| `Dockerfile` | **Modify** in Task 5: `CMD` reduced to Gunicorn alone. |
| `requirements.txt` | **Modify** in Task 5: drop Azure SDKs, add AWS. |

---

### Task 1: S3 media storage and range-aware server

**Files:**

- Create: `apps/media_library/s3_media.py`
- Create: `apps/media_library/tests/__init__.py`
- Create: `apps/media_library/tests/test_s3_media.py`
- Delete: `apps/media_library/blob_media.py`
- Delete: `apps/media_library/tests.py`
- Modify: `config/settings.py:287-301` (the Azure blob block)
- Modify: `config/urls.py:69-77` (the media route)

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces: `apps.media_library.s3_media.S3MediaStorage` (a `Storage` subclass whose `.url(name)` returns `/media/<name>`), and `apps.media_library.s3_media.serve_media(request, path) -> HttpResponse`. Task 5 relies on `blob_media.py` no longer existing.

The existing `serve_media` in `blob_media.py` contains production-proven Range and content-type logic. Port it **verbatim** apart from the three Azure calls. In particular keep the rule that a stored content type of `application/octet-stream` loses to an extension-based guess — bulk-migrated files land as octet-stream and browsers would otherwise download PDFs instead of rendering them.

- [ ] **Step 1: Create the tests package and write the failing tests**

Create `apps/media_library/tests/__init__.py` as an empty file, then `apps/media_library/tests/test_s3_media.py`:

```python
"""S3 media serving — Range, content-type, and fallback behaviour."""
from __future__ import annotations

import re

import pytest
from django.http import Http404
from django.test import RequestFactory, override_settings

from apps.media_library import s3_media


class _FakeBody:
    """Stands in for botocore's StreamingBody."""

    def __init__(self, data: bytes):
        self._data = data

    def iter_chunks(self, chunk_size: int = 8192):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]


class _FakeS3:
    """Minimal S3 client: head_object + ranged get_object."""

    def __init__(self, objects: dict[str, tuple[bytes, str | None]]):
        self.objects = objects
        self.ranges_requested: list[str | None] = []

    def head_object(self, Bucket, Key):  # noqa: N803 — boto3 casing
        if Key not in self.objects:
            raise KeyError(Key)
        data, content_type = self.objects[Key]
        head = {"ContentLength": len(data)}
        if content_type is not None:
            head["ContentType"] = content_type
        return head

    def get_object(self, Bucket, Key, Range=None):  # noqa: N803
        self.ranges_requested.append(Range)
        data, _ = self.objects[Key]
        if Range:
            m = re.match(r"bytes=(\d+)-(\d+)", Range)
            start, end = int(m.group(1)), int(m.group(2))
            data = data[start:end + 1]
        return {"Body": _FakeBody(data)}


BODY = b"0123456789A"  # 11 bytes


@pytest.fixture
def fake_s3(monkeypatch):
    client = _FakeS3({"doc.pdf": (BODY, "application/octet-stream")})
    monkeypatch.setattr(s3_media, "_s3_client", lambda: client)
    return client


@pytest.fixture
def rf():
    return RequestFactory()


def _drain(response) -> bytes:
    return b"".join(response.streaming_content)


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_full_request_returns_200_with_length_and_accept_ranges(fake_s3, rf):
    response = s3_media.serve_media(rf.get("/media/doc.pdf"), "doc.pdf")

    assert response.status_code == 200
    assert response["Content-Length"] == "11"
    assert response["Accept-Ranges"] == "bytes"
    assert "Content-Range" not in response
    assert _drain(response) == BODY
    assert fake_s3.ranges_requested == [None]


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_byte_range_returns_206_and_the_requested_slice(fake_s3, rf):
    request = rf.get("/media/doc.pdf", HTTP_RANGE="bytes=2-5")
    response = s3_media.serve_media(request, "doc.pdf")

    assert response.status_code == 206
    assert response["Content-Range"] == "bytes 2-5/11"
    assert response["Content-Length"] == "4"
    assert _drain(response) == b"2345"
    assert fake_s3.ranges_requested == ["bytes=2-5"]


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_suffix_range_returns_the_last_n_bytes(fake_s3, rf):
    request = rf.get("/media/doc.pdf", HTTP_RANGE="bytes=-4")
    response = s3_media.serve_media(request, "doc.pdf")

    assert response.status_code == 206
    assert response["Content-Range"] == "bytes 7-10/11"
    assert _drain(response) == b"789A"


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_open_ended_range_runs_to_the_final_byte(fake_s3, rf):
    request = rf.get("/media/doc.pdf", HTTP_RANGE="bytes=8-")
    response = s3_media.serve_media(request, "doc.pdf")

    assert response.status_code == 206
    assert response["Content-Range"] == "bytes 8-10/11"
    assert _drain(response) == b"89A"


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
@pytest.mark.parametrize("header", ["bytes=abc", "bytes=-", "kilobytes=0-5"])
def test_unparseable_range_is_a_400(fake_s3, rf, header):
    request = rf.get("/media/doc.pdf", HTTP_RANGE=header)
    response = s3_media.serve_media(request, "doc.pdf")

    assert response.status_code == 400


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_range_beyond_the_object_is_a_416_with_the_real_size(fake_s3, rf):
    request = rf.get("/media/doc.pdf", HTTP_RANGE="bytes=50-60")
    response = s3_media.serve_media(request, "doc.pdf")

    assert response.status_code == 416
    assert response["Content-Range"] == "bytes */11"


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_generic_stored_content_type_loses_to_the_extension_guess(fake_s3, rf):
    """Bulk-migrated objects arrive as application/octet-stream; serving a PDF
    with that type makes browsers download it instead of rendering it."""
    response = s3_media.serve_media(rf.get("/media/doc.pdf"), "doc.pdf")

    assert response["Content-Type"] == "application/pdf"


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_specific_stored_content_type_is_respected(monkeypatch, rf):
    client = _FakeS3({"clip.bin": (BODY, "video/mp4")})
    monkeypatch.setattr(s3_media, "_s3_client", lambda: client)

    response = s3_media.serve_media(rf.get("/media/clip.bin"), "clip.bin")

    assert response["Content-Type"] == "video/mp4"


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_missing_object_falls_back_to_the_filesystem(fake_s3, rf, tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        with pytest.raises(Http404):
            s3_media.serve_media(rf.get("/media/nope.pdf"), "nope.pdf")


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_filesystem_fallback_serves_a_local_file(fake_s3, rf, tmp_path):
    (tmp_path / "local.txt").write_bytes(b"local")

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        response = s3_media.serve_media(rf.get("/media/local.txt"), "local.txt")

    assert response.status_code == 200


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
def test_path_traversal_is_refused(fake_s3, rf, tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        with pytest.raises(Http404):
            s3_media.serve_media(rf.get("/media/x"), "../../etc/passwd")


@override_settings(MEDIA_URL="media/")
def test_storage_url_points_at_our_own_domain_never_at_s3():
    """School networks allowlist only our domain, so .url() must not leak an
    S3 hostname or a presigned URL."""

    class _Bare(s3_media.S3MediaStorage):
        def __init__(self):  # skip the parent __init__, which needs boto3
            pass

    assert _Bare().url("a/b.png") == "/media/a/b.png"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/media_library/tests/test_s3_media.py -v`

Expected: collection error — `ModuleNotFoundError: No module named 'apps.media_library.s3_media'`.

If instead you get `import file mismatch` for `tests`, you left `apps/media_library/tests.py` in place. Delete it — a module and a package of the same name cannot coexist.

- [ ] **Step 3: Write the implementation**

Create `apps/media_library/s3_media.py`:

```python
"""S3 media storage plus a range-capable, school-network-friendly server.

Why this exists
---------------
School networks allowlist ``www.seselai.sc`` only — they block ``*.amazonaws.com``
the same way they block YouTube — and the bucket is private with no anonymous
access. So media must be SERVED from our own domain rather than handed out as
a bucket URL or a presigned link.

This module:
  * ``S3MediaStorage`` — django-storages S3 backend whose ``.url()`` returns
    ``/media/<name>`` (our domain) instead of an S3 URL.
  * ``serve_media`` — streams ``/media/<path>`` from the private bucket with
    HTTP Range support so videos can be scrubbed. Falls back to ``MEDIA_ROOT``
    for anything not in the bucket, which keeps local development working.
"""
from __future__ import annotations

import mimetypes
import os
import re

from django.conf import settings
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    StreamingHttpResponse,
)

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK_BYTES = 8 * 1024 * 1024


def _media_url_prefix() -> str:
    u = settings.MEDIA_URL or "media/"
    if not u.startswith("/"):
        u = "/" + u
    if not u.endswith("/"):
        u = u + "/"
    return u


# django-storages is only installed where S3 media is used. Guard the import so
# this module stays importable in a bare dev environment; the class below is
# only instantiated when settings.USE_S3_MEDIA is true.
try:
    from storages.backends.s3 import S3Storage as _S3Storage
except Exception:  # pragma: no cover - package absent in local dev
    _S3Storage = object


class S3MediaStorage(_S3Storage):
    """Media stored in S3; URLs point back at our own domain so they work on
    school networks and stay behind the WAF (we serve via serve_media, never a
    bucket URL or a presigned link)."""

    def __init__(self, **kwargs):
        super().__init__(
            bucket_name=settings.AWS_MEDIA_BUCKET,
            region_name=settings.AWS_MEDIA_REGION,
            querystring_auth=False,
            file_overwrite=False,
            **kwargs,
        )

    def url(self, name, *a, **k):
        return _media_url_prefix() + str(name).lstrip("/")


def _s3_client():
    """boto3 S3 client. Credentials come from the ECS task role."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_MEDIA_REGION)


def _filesystem_fallback(path: str):
    """Serve from MEDIA_ROOT for anything not in the bucket."""
    full = os.path.join(settings.MEDIA_ROOT, path)
    root = os.path.abspath(settings.MEDIA_ROOT)
    if not os.path.abspath(full).startswith(root) or not os.path.isfile(full):
        raise Http404(path)
    return FileResponse(open(full, "rb"))


def serve_media(request, path):
    """Stream `/media/<path>` from S3 (range-aware), fall back to MEDIA_ROOT."""
    path = path.lstrip("/")
    bucket = settings.AWS_MEDIA_BUCKET
    client = _s3_client()
    try:
        head = client.head_object(Bucket=bucket, Key=path)
    except Exception:
        # Not in the bucket (or S3 unreachable) → try the local filesystem.
        return _filesystem_fallback(path)

    size = head["ContentLength"]
    # Prefer the extension guess when S3 has no stored type OR a generic one.
    # Bulk-migrated files land as ``application/octet-stream``, which would make
    # browsers download PDFs instead of rendering them inline.
    stored_ct = head.get("ContentType")
    guessed_ct = mimetypes.guess_type(path)[0]
    if guessed_ct and (not stored_ct or stored_ct == "application/octet-stream"):
        content_type = guessed_ct
    else:
        content_type = stored_ct or "application/octet-stream"

    range_header = request.headers.get("Range", "")
    start, end = 0, size - 1
    is_range = False
    if range_header:
        m = _RANGE_RE.match(range_header)
        if not m:
            return HttpResponseBadRequest("Invalid Range")
        g0, g1 = m.group(1), m.group(2)
        if g0 == "" and g1 == "":
            return HttpResponseBadRequest("Invalid Range")
        if g0 == "":  # suffix: last N bytes
            length = int(g1)
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(g0)
            end = int(g1) if g1 else size - 1
        end = min(end, size - 1)
        if start > end:
            resp = HttpResponse(status=416)
            resp["Content-Range"] = f"bytes */{size}"
            return resp
        is_range = True

    length = end - start + 1
    get_kwargs = {"Bucket": bucket, "Key": path}
    if is_range:
        get_kwargs["Range"] = f"bytes={start}-{end}"
    body = client.get_object(**get_kwargs)["Body"]

    resp = StreamingHttpResponse(
        body.iter_chunks(_CHUNK_BYTES),
        status=206 if is_range else 200,
        content_type=content_type,
    )
    resp["Accept-Ranges"] = "bytes"
    resp["Content-Length"] = str(length)
    if is_range:
        resp["Content-Range"] = f"bytes {start}-{end}/{size}"
    return resp
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/media_library/tests/test_s3_media.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Swap the settings block**

In `config/settings.py`, replace the Azure blob block at lines 287-301 with:

```python
# ── Media on S3 ────────────────────────────────────────────────────────────
# Private bucket. Media is served through Django at /media/<path> (see
# apps.media_library.s3_media.serve_media) so school networks only need our
# own domain allowlisted — never a bucket URL, never a presigned link.
AWS_MEDIA_BUCKET = os.getenv('AWS_MEDIA_BUCKET', '')
AWS_MEDIA_REGION = os.getenv('AWS_MEDIA_REGION', 'us-east-1')
USE_S3_MEDIA = bool(AWS_MEDIA_BUCKET)

if USE_S3_MEDIA:
    STORAGES['default'] = {
        'BACKEND': 'apps.media_library.s3_media.S3MediaStorage',
    }
```

`STORAGES` is defined as a dict literal at lines 278-285 with `default` and `staticfiles` keys, and the Azure block mutated `STORAGES['default']` in exactly this shape — so this is a like-for-like replacement, not a restructure.

- [ ] **Step 6: Repoint the media route**

In `config/urls.py`, change the import and the conditional at lines 69-77 from `USE_BLOB_MEDIA` / `apps.media_library.blob_media` to `USE_S3_MEDIA` / `apps.media_library.s3_media`. The route shape does not change.

- [ ] **Step 7: Delete the Azure module and the stub test file**

```bash
git rm apps/media_library/blob_media.py apps/media_library/tests.py
```

- [ ] **Step 8: Verify nothing still references the deleted module**

Run: `grep -rn "blob_media\|USE_BLOB_MEDIA\|AZURE_BLOB_MEDIA" --include="*.py" --include="*.html" apps/ config/`

Expected: no matches. If any appear, fix them before committing.

- [ ] **Step 9: Run the full media_library and dashboard suites for regressions**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest apps/media_library apps/curriculum -q`

Expected: no new failures compared with the pre-change baseline. Record the baseline first with `git stash` if you are unsure.

- [ ] **Step 10: Commit**

```bash
git add apps/media_library config/settings.py config/urls.py
git commit -m "media: serve from S3 instead of Azure Blob

Ports serve_media to boto3, keeping the Range and content-type logic
verbatim — including the rule that a stored application/octet-stream
loses to the extension guess, which is what stops browsers downloading
migrated PDFs instead of rendering them.

.url() still returns /media/<name> so media stays on our own domain;
school networks allowlist that and nothing else.

Refs: docs/superpowers/specs/2026-08-07-aws-migration-design.md"
```

---

### Task 2: SES email backend

**Files:**

- Modify: `apps/safety/email_backends.py` (full rewrite, 154 lines)
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

- [ ] **Step 3: Rewrite the backend**

Replace the whole of `apps/safety/email_backends.py`:

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

Run: `grep -rn "AzureCommunication\|AZURE_COMMUNICATION" --include="*.py" apps/ config/`

Expected: no matches.

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

Then replace `_azure_settings` (lines 24-31) with:

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
    return _dispatch_via_subprocess(upload_id, mode)
```

- [ ] **Step 4: Replace the Azure backend with the ECS one**

Delete `_dispatch_via_azure_sdk` entirely (lines 47-159 of the original file) and put this in its place:

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

Run: `grep -rn "AZURE_RESOURCE_GROUP\|AZURE_MATERIAL_JOB_NAME\|AZURE_SUBSCRIPTION_ID\|appcontainers" --include="*.py" apps/ config/`

Expected: no matches.

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

### Task 5: Drop Azure dependencies and split migrations out of the image CMD

**Files:**

- Modify: `requirements.txt:182-189`
- Modify: `Dockerfile:29-31`

**Interfaces:**

- Consumes: tasks 1–3 must be complete — the Azure packages cannot be removed while anything still imports them.
- Produces: an image whose `CMD` runs Gunicorn only. The infrastructure plan's migrate task definition depends on the seed command chain being available as a documented entrypoint.

The `CMD` currently chains `migrate` plus six seed commands ahead of Gunicorn. Container Apps single-revision mode serialised that by accident; ECS running more than one task would execute it concurrently on every task, racing the migrations. The chain moves to a one-shot task that CI runs and waits on before updating the service.

- [ ] **Step 1: Confirm no Azure imports remain**

Run: `grep -rn "^import azure\|^from azure\|import azure\.\|from azure\." --include="*.py" apps/ config/`

Expected: no matches. If any appear, the corresponding task above is incomplete — go back and finish it. Do not proceed.

- [ ] **Step 2: Swap the dependencies**

In `requirements.txt`, remove these four lines:

```text
azure-communication-email
azure-identity
azure-mgmt-appcontainers
django-storages[azure]>=1.14
```

(the exact pins are at lines 182-189 — read them before deleting, and keep any unrelated neighbours)

and add in their place:

```text
boto3>=1.34
django-storages[s3]>=1.14
```

Leave `requirements-core.txt`, `requirements-jetson.txt`, and `requirements-jetson.lock.txt` alone for now. They carry the same four Azure packages, but the Jetson lock file has to be regenerated on the Jetson toolchain and that is out of scope here. Note it as follow-up work.

- [ ] **Step 3: Install and verify the app still imports**

```bash
./venv/bin/pip install -r requirements.txt
DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/python -c "import django; django.setup(); import apps.media_library.s3_media, apps.safety.email_backends, apps.dashboard.job_dispatch; print('imports OK')"
```

Expected: `imports OK`.

- [ ] **Step 4: Reduce the Dockerfile CMD to Gunicorn**

In `Dockerfile`, replace lines 29-31 with:

```dockerfile
# Migrations and seeding run as a one-shot ECS task before the service is
# updated (see .github/workflows/deploy.yml). Running them here would race
# across tasks — Container Apps only got away with it because single-revision
# mode serialised the rollout.
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", "--threads", "4", "--timeout", "120"]
```

Keep the existing `EXPOSE 8000` if it is already on its own line above — do not duplicate it.

- [ ] **Step 5: Record the migrate command chain where the infrastructure plan can find it**

Create `ops/migrate_and_seed.sh`:

```bash
#!/usr/bin/env sh
# Migration + seed chain, lifted verbatim out of the Dockerfile CMD.
# Run as a ONE-SHOT ECS task before the web service is updated. Running this
# concurrently across tasks would race the migrations.
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

- [ ] **Step 6: Verify the chain still runs against a scratch database**

```bash
PROBE_DB="$(mktemp -d)/probe.sqlite3"
DJANGO_SETTINGS_MODULE=config.settings \
DATABASE_URL="sqlite:///${PROBE_DB}" \
PATH="$PWD/venv/bin:$PATH" \
  sh ops/migrate_and_seed.sh
```

`PATH` is prefixed so the script's bare `python` resolves to the virtualenv interpreter, matching how it will resolve inside the container.

Expected: every command completes without error. Some seed commands are no-ops on an empty database; that is fine, but a traceback is not.

Note this exercises SQLite, so the pgvector-specific migration paths are skipped by their vendor guard. Real pgvector verification belongs to the infrastructure plan's RDS rehearsal.

- [ ] **Step 7: Build the image to prove the Dockerfile is valid**

```bash
docker build --platform linux/amd64 -t aitutor:aws-migration-check .
```

Expected: the build succeeds. This is a large image (roughly 4.7 GB) and an uncached build takes a while — that is expected, and the CI cache work belongs to the CI/CD plan.

If Docker is unavailable in this environment, skip this step and say so explicitly in the commit body rather than silently omitting it.

- [ ] **Step 8: Run the full test suite**

Run: `DJANGO_SETTINGS_MODULE=config.settings ./venv/bin/pytest -q`

Expected: no new failures against the baseline. The suite was reported at 631 passing as of commit `ebb4657`; confirm the count has grown by the tests added in tasks 1–4 and that nothing previously passing now fails.

- [ ] **Step 9: Commit**

```bash
git add requirements.txt Dockerfile ops/migrate_and_seed.sh
git commit -m "build: drop the Azure SDKs and take migrations out of CMD

Nothing imports azure-* any more after the S3, SES and ECS ports, so the
four packages come out and boto3 plus django-storages[s3] go in.

CMD now starts gunicorn and nothing else. The migrate-and-seed chain moves
to ops/migrate_and_seed.sh, which CI runs as a one-shot ECS task before
updating the service — running it in CMD would race across tasks, which
Container Apps only avoided because single-revision mode serialised the
rollout.

requirements-core / requirements-jetson still carry the Azure packages;
the Jetson lock has to be regenerated on that toolchain, so it is tracked
as follow-up rather than done blind here.

Refs: docs/superpowers/specs/2026-08-07-aws-migration-design.md"
```

---

## Follow-up work, deliberately not in this plan

- **Infrastructure plan** — the Pulumi AWS program under `infra/aws/`. Depends on this plan only for the environment-variable names it must supply: `AWS_MEDIA_BUCKET`, `AWS_MEDIA_REGION`, `AWS_SES_SENDER`, `AWS_SES_REGION`, `ECS_CLUSTER`, `ECS_MATERIAL_TASK_DEFINITION`, `ECS_SUBNETS`, `ECS_SECURITY_GROUPS`, `ECS_MATERIAL_CONTAINER_NAME`, `AWS_REGION`.
- **CI/CD plan** — rewriting `deploy.yml` and `deploy-staging.yml` against OIDC, deleting `cert-renew.yml`, adding the migrate-task gate, smoke test, and automatic rollback.
- **Data migration and cutover plan** — `pg_dump`/restore into RDS, media sync into S3, DNS TTL reduction, and the cutover runbook.
- `requirements-core.txt`, `requirements-jetson.txt`, and `requirements-jetson.lock.txt` still install the Azure SDKs.
- Re-enabling SSE, now that an ALB does not buffer it. Three code sites and one test encode the Azure assumption.
