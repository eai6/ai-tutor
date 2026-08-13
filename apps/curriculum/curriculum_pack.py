"""Curriculum packs — teaching content only, for seeding a NEW deployment.

Distinct from the desktop content pack in ``apps/desktop/packs.py``, and the
distinction is the point:

    desktop pack     → a provisioned laptop belonging to THIS institution.
                       Carries a roster, because the device has to bind a
                       student's local login to the server user id their work
                       will sync under. Travels on a USB stick inside one school.

    curriculum pack  → a brand-new server belonging to SOMEONE ELSE.
                       Carries courses, lessons, exit tickets, knowledge-base
                       chunks and media. No roster, no users, no institution
                       identity. There is no sync relationship to establish.

Conflating the two is a data-protection problem, not a tidiness one: seeding a
foreign deployment from a desktop pack would hand that organisation a list of
another country's schoolchildren — names, usernames and year groups. The
importers therefore REFUSE each other's packs (see ``pack_kind`` below) rather
than merely offering the right one, because the mistake is otherwise made by
whoever copies the wrong file, long after anyone is watching.

Build (on a server that already has the content):

    python manage.py build_curriculum_pack --institution 3 --out dist/

Import (on the new one):

    python manage.py import_curriculum_pack dist/curriculum-pack-v1.tar.gz
"""
from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from django.core import serializers
from django.db import transaction
from django.utils import timezone

# Shared format primitives. Imported rather than duplicated so the two pack
# kinds cannot drift apart on checksums or schema-revision detection — the
# fields an importer trusts to decide whether a file is safe to load.
from apps.desktop.packs import (
    PackError,
    _institution_querysets,
    _latest_migration,
    _sha256,
)

MANIFEST_NAME = 'manifest.json'
PACK_FORMAT_VERSION = 1

#: Written into every manifest. ``import_curriculum_pack`` refuses anything
#: else, including a legacy pack with no kind at all — those predate this split
#: and are desktop packs, which means they contain a roster.
PACK_KIND = 'curriculum'

PARTS = ('curriculum.json', 'tickets.json', 'kb_chunks.json')


def build_curriculum_pack(institution_id: int, out_dir: Path,
                          include_media: bool = True) -> dict:
    """Write a curriculum pack. Returns the manifest.

    Content is drawn with the same scoping as the desktop pack — this
    institution's courses PLUS platform-wide ones — so a pack built from a
    school that relies on shared content is not silently missing half of it.
    """
    from django.conf import settings

    qs = _institution_querysets(institution_id)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)

        # One file for the tree so the importer can load it in dependency
        # order inside a single transaction.
        with open(staging / 'curriculum.json', 'w', encoding='utf-8') as handle:
            combined = (list(qs['courses']) + list(qs['units'])
                        + list(qs['lessons']) + list(qs['steps']))
            serializers.serialize('json', combined, stream=handle, indent=None)

        with open(staging / 'tickets.json', 'w', encoding='utf-8') as handle:
            serializers.serialize('json',
                                  list(qs['tickets']) + list(qs['questions']),
                                  stream=handle, indent=None)

        with open(staging / 'kb_chunks.json', 'w', encoding='utf-8') as handle:
            serializers.serialize('json', list(qs['chunks']),
                                  stream=handle, indent=None)

        # NO roster.json and NO institution.json, deliberately. See the module
        # docstring. If either ever appears here, the importer's refusal below
        # is the thing that should have stopped it reaching a foreign server.

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
            'pack_kind': PACK_KIND,
            'built_at': timezone.now().isoformat(),
            'schema_rev': _latest_migration(),
            'counts': {k: v.count() for k, v in qs.items()},
            'media_files': media_files,
            'media_bytes': media_bytes,
            'checksums': {name: _sha256(staging / name) for name in PARTS},
            # Recorded so a recipient can ask where content came from. NOT the
            # institution's name or id — the pack is deliberately anonymous,
            # and a source label is a provenance note, not an identity.
            'source_note': 'Curriculum content only. Contains no user data.',
        }
        with open(staging / MANIFEST_NAME, 'w', encoding='utf-8') as handle:
            json.dump(manifest, handle, indent=2)

        stamp = timezone.now().strftime('%Y%m%d')
        archive_path = out_dir / f'curriculum-pack-{stamp}.tar.gz'
        with tarfile.open(archive_path, 'w:gz') as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)

    manifest['archive'] = str(archive_path)
    manifest['archive_bytes'] = archive_path.stat().st_size
    manifest['archive_sha256'] = _sha256(archive_path)
    return manifest


def read_manifest(archive: Path) -> dict:
    with tarfile.open(archive, 'r:gz') as tar:
        try:
            handle = tar.extractfile(MANIFEST_NAME)
        except KeyError:
            handle = None
        if handle is None:
            raise PackError(f'{Path(archive).name} is not a pack '
                            f'(no {MANIFEST_NAME}).')
        return json.load(handle)


def assert_carries_no_user_data(archive: Path) -> None:
    """Refuse anything that is not a curriculum pack.

    Checked twice on purpose — the declared kind AND the actual member list.
    The manifest is what a well-formed pack says about itself; the member list
    is what it actually contains. Only the second one catches a hand-edited
    manifest, and this is the check standing between another country's pupil
    roster and a third party's database.
    """
    manifest = read_manifest(archive)
    kind = manifest.get('pack_kind')

    if kind != PACK_KIND:
        raise PackError(
            f"This is not a curriculum pack (pack_kind={kind!r}). "
            f"Desktop content packs carry a student roster and must never be "
            f"loaded onto another organisation's server. Build one with "
            f"`manage.py build_curriculum_pack` instead."
        )

    with tarfile.open(archive, 'r:gz') as tar:
        names = set(tar.getnames())
    forbidden = {'roster.json', 'institution.json'} & names
    if forbidden:
        raise PackError(
            f"Refusing {Path(archive).name}: it declares pack_kind="
            f"'{PACK_KIND}' but contains {', '.join(sorted(forbidden))}, which "
            f"carries user data. Do not load this."
        )


def import_curriculum_pack(archive: Path, *, force: bool = False,
                           strict_schema: bool = True) -> dict:
    """Load a curriculum pack into this database. Returns the manifest.

    All-or-nothing: a failure part-way leaves the previous content intact,
    because a half-imported curriculum is worse than none — a student reaches a
    lesson whose steps are missing.
    """
    from django.conf import settings

    from apps.curriculum.models import Course

    archive = Path(archive)
    if not archive.exists():
        raise PackError(f'{archive} does not exist.')

    assert_carries_no_user_data(archive)
    manifest = read_manifest(archive)

    if manifest.get('pack_format') != PACK_FORMAT_VERSION:
        raise PackError(
            f"Pack format v{manifest.get('pack_format')} is not supported "
            f"(expects v{PACK_FORMAT_VERSION})."
        )

    if strict_schema and manifest.get('schema_rev') != _latest_migration():
        raise PackError(
            'This pack was built against a different database schema. '
            'Update this deployment to match the pack, or rebuild the pack.'
        )

    # Seeding is for a NEW deployment. Loading over existing courses would
    # overwrite them by primary key, which on a live server means silently
    # replacing a school's own content with someone else's.
    if Course.objects.exists() and not force:
        raise PackError(
            f'This deployment already has {Course.objects.count()} course(s). '
            f'Curriculum packs are for seeding a new installation. Re-run with '
            f'--force only if you intend to overwrite content by id.'
        )

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, 'r:gz') as tar:
            # filter='data' refuses absolute paths and parent traversal in
            # member names. A pack arrives from outside the trust boundary.
            tar.extractall(staging, filter='data')

        for name in PARTS:
            expected = (manifest.get('checksums') or {}).get(name)
            actual = _sha256(staging / name)
            if expected and expected != actual:
                raise PackError(f'{name} is corrupt (checksum mismatch). '
                                f'Download the pack again.')

        with transaction.atomic():
            for name in PARTS:
                with open(staging / name, encoding='utf-8') as handle:
                    for obj in serializers.deserialize('json', handle):
                        _normalise_scope(obj.object)
                        obj.save()

        media_src = staging / 'media'
        if media_src.exists():
            shutil.copytree(media_src, Path(settings.MEDIA_ROOT),
                            dirs_exist_ok=True)

    return manifest


def _normalise_scope(instance) -> None:
    """Re-scope imported content to platform-wide.

    The source rows point at the institution they were built from, whose id
    means nothing here — following it would leave every course owned by a
    school that does not exist on this server.

    Platform-wide is the honest target: seeded curriculum belongs to no single
    school and should be visible to all of them. That is what
    ``institution=None`` means for a Course (CLAUDE.md's scoping rule reads
    ``Q(institution=inst) | Q(institution__isnull=True)``), and what the
    normalised ``0`` bucket means for a knowledge-base chunk, which stores a
    loose integer rather than a foreign key.
    """
    from apps.curriculum.models import Course, CurriculumChunk

    if isinstance(instance, Course):
        instance.institution = None
    elif isinstance(instance, CurriculumChunk):
        instance.institution_id = 0
