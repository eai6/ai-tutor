"""S3 media serving — Range, content-type, and fallback behaviour.

Written as SimpleTestCase rather than bare pytest functions to match the rest
of the suite. Function-style tests fail here: with no conftest.py, the first
test in a session runs before Django settings are fully wrapped and
override_settings blows up inside RequestFactory.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, override_settings

from ai_tutor.apps.media_library import s3_media


class _FakeBody:
    """Stands in for botocore's StreamingBody."""

    def __init__(self, data: bytes):
        self._data = data

    def iter_chunks(self, chunk_size: int = 8192):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]


class _FakeS3:
    """Minimal S3 client: head_object + ranged get_object."""

    def __init__(self, objects):
        self.objects = objects
        self.ranges_requested = []

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


@override_settings(AWS_MEDIA_BUCKET="test-bucket", MEDIA_URL="media/")
class S3MediaServingTests(SimpleTestCase):
    """serve_media against a private bucket."""

    def setUp(self):
        self.factory = RequestFactory()
        self.client_stub = _FakeS3({"doc.pdf": (BODY, "application/octet-stream")})
        patcher = patch.object(s3_media, "_s3_client", lambda: self.client_stub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _serve(self, key, **extra):
        request = self.factory.get(f"/media/{key}", **extra)
        return s3_media.serve_media(request, key)

    @staticmethod
    def _drain(response) -> bytes:
        return b"".join(response.streaming_content)

    def test_full_request_returns_200_with_length_and_accept_ranges(self):
        response = self._serve("doc.pdf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], "11")
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertNotIn("Content-Range", response)
        self.assertEqual(self._drain(response), BODY)
        self.assertEqual(self.client_stub.ranges_requested, [None])

    def test_byte_range_returns_206_and_the_requested_slice(self):
        response = self._serve("doc.pdf", HTTP_RANGE="bytes=2-5")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 2-5/11")
        self.assertEqual(response["Content-Length"], "4")
        self.assertEqual(self._drain(response), b"2345")
        self.assertEqual(self.client_stub.ranges_requested, ["bytes=2-5"])

    def test_suffix_range_returns_the_last_n_bytes(self):
        response = self._serve("doc.pdf", HTTP_RANGE="bytes=-4")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 7-10/11")
        self.assertEqual(self._drain(response), b"789A")

    def test_open_ended_range_runs_to_the_final_byte(self):
        response = self._serve("doc.pdf", HTTP_RANGE="bytes=8-")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 8-10/11")
        self.assertEqual(self._drain(response), b"89A")

    def test_unparseable_range_is_a_400(self):
        for header in ("bytes=abc", "bytes=-", "kilobytes=0-5"):
            with self.subTest(header=header):
                response = self._serve("doc.pdf", HTTP_RANGE=header)
                self.assertEqual(response.status_code, 400)

    def test_range_beyond_the_object_is_a_416_with_the_real_size(self):
        response = self._serve("doc.pdf", HTTP_RANGE="bytes=50-60")

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], "bytes */11")

    def test_generic_stored_content_type_loses_to_the_extension_guess(self):
        """Bulk-migrated objects arrive as application/octet-stream; serving a
        PDF with that type makes browsers download it instead of rendering."""
        response = self._serve("doc.pdf")

        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_specific_stored_content_type_is_respected(self):
        self.client_stub.objects["clip.bin"] = (BODY, "video/mp4")

        response = self._serve("clip.bin")

        self.assertEqual(response["Content-Type"], "video/mp4")

    def test_missing_object_falls_back_to_the_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                with self.assertRaises(Http404):
                    self._serve("nope.pdf")

    def test_filesystem_fallback_serves_a_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "local.txt").write_bytes(b"local")
            with override_settings(MEDIA_ROOT=tmp):
                response = self._serve("local.txt")

            self.assertEqual(response.status_code, 200)
            response.close()

    def test_path_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                request = self.factory.get("/media/x")
                with self.assertRaises(Http404):
                    s3_media.serve_media(request, "../../etc/passwd")

    def test_a_sibling_directory_sharing_the_prefix_is_refused(self):
        """A bare startswith(MEDIA_ROOT) check would let /app/media-old
        through for a root of /app/media. Escaping into a prefix-sharing
        sibling must 404 like any other traversal."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            root.mkdir()
            sibling = Path(tmp) / "media-old"
            sibling.mkdir()
            (sibling / "secret.txt").write_bytes(b"secret")

            with override_settings(MEDIA_ROOT=str(root)):
                request = self.factory.get("/media/x")
                with self.assertRaises(Http404):
                    s3_media.serve_media(request, "../media-old/secret.txt")


@override_settings(MEDIA_URL="media/")
class S3MediaStorageUrlTests(SimpleTestCase):

    def test_storage_url_points_at_our_own_domain_never_at_s3(self):
        """School networks allowlist only our domain, so .url() must not leak
        an S3 hostname or a presigned URL."""

        class _Bare(s3_media.S3MediaStorage):
            def __init__(self):  # skip the parent __init__, which needs boto3
                pass

        self.assertEqual(_Bare().url("a/b.png"), "/media/a/b.png")


@override_settings(MEDIA_URL="media/")
class AzureBackendStillIntactTests(SimpleTestCase):
    """Azure serves live users while AWS is stood up, so the two backends run
    side by side off the same image. These guard the Azure path against being
    broken in passing by AWS work — they are not tests of Azure behaviour."""

    def test_azure_blob_module_still_imports(self):
        from ai_tutor.apps.media_library import blob_media

        self.assertTrue(callable(blob_media.serve_media))

    def test_azure_storage_url_also_stays_on_our_own_domain(self):
        from ai_tutor.apps.media_library import blob_media

        class _Bare(blob_media.AzureMediaStorage):
            def __init__(self):  # skip the parent __init__, which needs the SDK
                pass

        self.assertEqual(_Bare().url("a/b.png"), "/media/a/b.png")

    def test_the_two_backends_expose_the_same_entry_points(self):
        """Both are wired into config/urls.py by the same name, so a signature
        drift between them would only show up at request time in production."""
        from ai_tutor.apps.media_library import blob_media
        import inspect

        self.assertEqual(
            list(inspect.signature(blob_media.serve_media).parameters),
            list(inspect.signature(s3_media.serve_media).parameters),
        )
