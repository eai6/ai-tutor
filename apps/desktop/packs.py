"""Content pack build + import.

A content pack is everything one institution's students need to be tutored
with no network: the curriculum tree, the exit tickets, the knowledge-base
chunks *with their embeddings already computed*, and the media those lessons
reference. It is built on the server and imported on the device.

Why not the existing ``apps/api/views/offline_pack.py``: that builds a
**per-lesson JSON bundle** for the React Native client, which interprets it
with its own TypeScript state machine. The desktop app runs the real Django
app against a real database, so it needs *rows*, not a bundle to interpret.
Different scope (institution vs lesson), different consumer, different
lifecycle — hence a separate mechanism rather than an overloaded one.

Embeddings ship precomputed. The device must never have to embed a corpus:
that is minutes of CPU on classroom hardware, and it would need the encoder
before the encoder is known to work. Embedding one short query per turn is
all the device does (~7 ms, measured).

Format — a gzipped tar, not zstd: Python has no stdlib zstd before 3.14 and
this is not worth a dependency on three platforms.

    manifest.json     version, institution, schema_rev, counts, sha256 of each part
    institution.json  the Institution row
    curriculum.json   Course -> Unit -> Lesson -> LessonStep
    tickets.json      ExitTicket + ExitTicketQuestion
    kb_chunks.json    CurriculumChunk incl. 384-d embeddings
    media/            files referenced by the above

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path

from django.core import serializers
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

MANIFEST_NAME = 'manifest.json'

# Bumped when the pack layout changes in a way an older importer would
# mis-read. Distinct from schema_rev, which tracks Django migrations.
PACK_FORMAT_VERSION = 1

PARTS = ('institution.json', 'curriculum.json', 'tickets.json', 'kb_chunks.json')


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _latest_migration() -> str:
    """Identify the schema this pack was built against.

    A pack built on a newer server than the installed app would deserialize
    into columns that do not exist yet. Recording the leaf migration lets the
    importer refuse with "update the app" instead of failing halfway through
    a write.
    """
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader
    loader = MigrationLoader(connection)
    leaves = sorted(f'{app}.{name}' for app, name in loader.graph.leaf_nodes())
    return hashlib.sha256('\n'.join(leaves).encode()).hexdigest()[:16]


# ─── Build (server side) ────────────────────────────────────────────────

def _institution_querysets(institution_id: int):
    """Every row a device needs, scoped to one institution.

    Platform-wide content (``institution__isnull=True``) is included
    deliberately: CLAUDE.md's scoping rule is
    ``Q(institution=inst) | Q(institution__isnull=True)``, and a device that
    dropped the global tier would silently lose shared curriculum.
    """
    from django.db.models import Q
    from apps.curriculum.models import Course, Unit, Lesson, LessonStep, CurriculumChunk
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion

    scope = Q(institution_id=institution_id) | Q(institution__isnull=True)
    courses = Course.objects.filter(scope)
    units = Unit.objects.filter(course__in=courses)
    lessons = Lesson.objects.filter(unit__in=units)
    steps = LessonStep.objects.filter(lesson__in=lessons)
    tickets = ExitTicket.objects.filter(lesson__in=lessons)
    questions = ExitTicketQuestion.objects.filter(exit_ticket__in=tickets)

    # KB chunks use a loose integer institution_id where 0 is the normalised
    # platform-wide bucket (see CurriculumChunk's docstring), not a FK, so the
    # Q() pattern above does not apply here.
    chunks = CurriculumChunk.objects.filter(institution_id__in=[institution_id, 0])

    return {
        'courses': courses, 'units': units, 'lessons': lessons, 'steps': steps,
        'tickets': tickets, 'questions': questions, 'chunks': chunks,
    }


def build_pack(institution_id: int, out_dir: Path, include_media: bool = True) -> dict:
    """Write a content pack. Returns the manifest dict."""
    from django.conf import settings
    from apps.accounts.models import Institution
    from apps.curriculum.models import ContentPackVersion

    institution = Institution.objects.get(pk=institution_id)
    qs = _institution_querysets(institution_id)

    version = (
        ContentPackVersion.objects.filter(institution=institution)
        .order_by('-version').values_list('version', flat=True).first() or 0
    ) + 1

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / f'content-pack-{institution_id}-v{version}.tar.gz'

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)

        def dump(name, queryset):
            path = staging / name
            with open(path, 'w', encoding='utf-8') as handle:
                serializers.serialize('json', queryset, stream=handle, indent=None)
            return path

        dump('institution.json', [institution])
        # One file for the curriculum tree so the importer can load it in
        # dependency order within a single transaction.
        with open(staging / 'curriculum.json', 'w', encoding='utf-8') as handle:
            combined = (list(qs['courses']) + list(qs['units'])
                        + list(qs['lessons']) + list(qs['steps']))
            serializers.serialize('json', combined, stream=handle, indent=None)
        with open(staging / 'tickets.json', 'w', encoding='utf-8') as handle:
            serializers.serialize('json', list(qs['tickets']) + list(qs['questions']),
                                  stream=handle, indent=None)
        dump('kb_chunks.json', qs['chunks'])

        media_files = 0
        media_bytes = 0
        if include_media:
            media_root = Path(settings.MEDIA_ROOT)
            if media_root.exists():
                dest = staging / 'media'
                shutil.copytree(media_root, dest, dirs_exist_ok=True)
                for item in dest.rglob('*'):
                    if item.is_file():
                        media_files += 1
                        media_bytes += item.stat().st_size

        manifest = {
            'pack_format': PACK_FORMAT_VERSION,
            'institution_id': institution_id,
            'institution_name': institution.name,
            'version': version,
            'built_at': timezone.now().isoformat(),
            'schema_rev': _latest_migration(),
            'counts': {k: v.count() for k, v in qs.items()},
            'media_files': media_files,
            'media_bytes': media_bytes,
            'checksums': {name: _sha256(staging / name) for name in PARTS},
        }
        with open(staging / MANIFEST_NAME, 'w', encoding='utf-8') as handle:
            json.dump(manifest, handle, indent=2)

        with tarfile.open(archive_path, 'w:gz') as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)

    manifest['archive'] = str(archive_path)
    manifest['archive_bytes'] = archive_path.stat().st_size
    manifest['archive_sha256'] = _sha256(archive_path)

    ContentPackVersion.objects.create(
        institution=institution, version=version,
        schema_rev=manifest['schema_rev'], checksum=manifest['archive_sha256'],
        size_bytes=manifest['archive_bytes'],
        lesson_count=manifest['counts']['lessons'],
        chunk_count=manifest['counts']['chunks'],
    )
    return manifest


# ─── Import (device side) ───────────────────────────────────────────────

class PackError(Exception):
    """Refusal to import, with a message meant for a teacher to read."""


def read_manifest(archive: Path) -> dict:
    with tarfile.open(archive, 'r:gz') as tar:
        try:
            handle = tar.extractfile(MANIFEST_NAME)
        except KeyError:
            handle = None
        if handle is None:
            raise PackError(f'{archive.name} is not a content pack '
                            f'(no {MANIFEST_NAME}).')
        return json.load(handle)


def import_pack(archive: Path, *, force: bool = False,
                strict_schema: bool = True) -> dict:
    """Import a pack into the local database. Returns the manifest.

    Idempotent: re-importing the same version is a no-op unless ``force``.
    All-or-nothing: a failure part-way leaves the previous content intact,
    because a half-imported curriculum is worse than none — the student sees
    lessons whose steps are missing.
    """
    from django.conf import settings
    from apps.desktop.models import DeviceState

    archive = Path(archive)
    if not archive.exists():
        raise PackError(f'{archive} does not exist.')

    manifest = read_manifest(archive)

    if manifest.get('pack_format') != PACK_FORMAT_VERSION:
        raise PackError(
            f"Pack format v{manifest.get('pack_format')} is not supported by "
            f"this app (expects v{PACK_FORMAT_VERSION}). Update the app."
        )

    state = DeviceState.load()

    # Multi-tenancy on a device with no server to enforce it: once bound, a
    # machine accepts packs from one institution only.
    if state.institution_id is not None and \
            state.institution_id != manifest['institution_id'] and not force:
        raise PackError(
            f"This device is set up for institution {state.institution_id}; "
            f"the pack is for {manifest['institution_id']} "
            f"({manifest.get('institution_name')}). Refusing to mix schools."
        )

    if state.pack_version is not None and \
            manifest['version'] <= state.pack_version and not force:
        return {**manifest, 'skipped': 'already at this version or newer'}

    if strict_schema and manifest.get('schema_rev') != _latest_migration():
        raise PackError(
            'This pack was built against a different database schema. '
            'Update the app to match the pack, or rebuild the pack.'
        )

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, 'r:gz') as tar:
            # filter='data' refuses absolute paths and parent traversal in
            # member names. A content pack arrives on a USB stick from
            # outside the app's trust boundary.
            tar.extractall(staging, filter='data')

        for name in PARTS:
            expected = (manifest.get('checksums') or {}).get(name)
            actual = _sha256(staging / name)
            if expected and expected != actual:
                raise PackError(f'{name} is corrupt (checksum mismatch). '
                                f'Re-copy the pack from the installation drive.')

        with transaction.atomic():
            for name in PARTS:
                with open(staging / name, encoding='utf-8') as handle:
                    for obj in serializers.deserialize('json', handle):
                        obj.save()

            media_src = staging / 'media'
            if media_src.exists():
                media_root = Path(settings.MEDIA_ROOT)
                media_root.mkdir(parents=True, exist_ok=True)
                shutil.copytree(media_src, media_root, dirs_exist_ok=True)

            state.institution_id = manifest['institution_id']
            state.pack_version = manifest['version']
            state.pack_imported_at = timezone.now()
            state.save()

    # The embedding matrix cached from before the import is now stale in a way
    # the fingerprint would catch anyway, but clearing is cheap and makes the
    # first post-import query correct without relying on that.
    from apps.curriculum import kb_storage
    kb_storage._MATRIX_CACHE.clear()

    logger.info('[Desktop] imported content pack v%s for institution %s',
                manifest['version'], manifest['institution_id'])
    return manifest
