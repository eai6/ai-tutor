"""One-off port of every ChromaDB chunk into the new ``CurriculumChunk``
pgvector table. Phase 6 of the pgvector migration — see
``memory/pgvector_migration_plan.md``.

Walks every ``vectordb/institution_<N>/`` directory under the vectordb
root, opens each ChromaDB collection, and bulk-upserts every chunk
(content + embedding + metadata) into ``CurriculumChunk`` via Django
ORM. Existing vectors are preserved byte-for-byte — we read the
embeddings out of ChromaDB rather than re-running the embedding model.

Idempotent: dedup is the ``(institution_id, content_hash)`` unique
constraint. Re-running with the same source data updates rows in
place; the second run reports 0 new + N updated.

Institution-id normalisation: any input institution_id of ``None``,
``0``, or ``Institution.get_global().id`` (= 12 in prod) is mapped to
``CurriculumKnowledgeBase.GLOBAL_INSTITUTION_ID`` (= 0). This catches
the historical mismatch where "All Schools" uploads were indexed under
id=12 while per-school queries merged from id=0 (silent empty
inheritance). See ``_normalise_institution_id`` on the KB class.

Run with:

    # Default: read from MEDIA_ROOT/vectordb (the local + prod path)
    python manage.py port_chromadb_to_pgvector

    # Custom path (useful for one-off prod ports where vectordb is
    # mounted at /app/media/vectordb but we want to point elsewhere)
    python manage.py port_chromadb_to_pgvector --vectordb-path /tmp/snapshot/vectordb

    # Dry-run to count chunks per institution without writing
    python manage.py port_chromadb_to_pgvector --dry-run
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction

logger = logging.getLogger(__name__)


# Same metadata keys the new model + kb_storage.upsert_chunks recognise.
# Anything else in a ChromaDB chunk's metadata dict is silently dropped
# (matches the runtime kb_storage behaviour).
_METADATA_KEYS = (
    'subject', 'grade_level', 'section', 'chunk_type', 'source_file', 'upload_id',
    'source_type', 'material_type', 'material_title',
    'question_number', 'question_type', 'has_answers', 'year', 'paper_number',
    'figure_type', 'figure_page', 'figure_number', 'figure_image_url',
)


class Command(BaseCommand):
    help = "Port every ChromaDB chunk into the CurriculumChunk pgvector table (one-off; idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--vectordb-path',
            type=str,
            default=None,
            help="Path to the vectordb root. Defaults to MEDIA_ROOT/vectordb.",
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Walk the source data + count chunks per institution; do not write.",
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help="ORM bulk_create batch size. Default 500.",
        )

    def handle(self, *args, **opts):
        if connection.vendor != 'postgresql' and not opts['dry_run']:
            self.stderr.write(self.style.ERROR(
                f"Backend is {connection.vendor}; vector upserts require Postgres + pgvector. "
                f"Re-run with --dry-run to inspect source data, or run against the prod DB."
            ))
            return

        # Resolve vectordb root
        if opts['vectordb_path']:
            root = Path(opts['vectordb_path'])
        else:
            root = Path(
                getattr(settings, 'VECTORDB_ROOT',
                        os.path.join(getattr(settings, 'MEDIA_ROOT', '/tmp'), 'vectordb'))
            )
        if not root.exists():
            self.stderr.write(self.style.ERROR(f"vectordb root not found: {root}"))
            return

        self.stdout.write(self.style.NOTICE(f"Walking vectordb root: {root}"))

        institution_dirs = sorted(
            d for d in root.iterdir()
            if d.is_dir() and d.name.startswith('institution_')
        )
        if not institution_dirs:
            self.stderr.write(
                self.style.WARNING(f"No institution_* directories under {root}")
            )
            return

        # Lazy imports — chromadb is only needed here (the re-index path),
        # not in the runtime app code.
        try:
            import chromadb
        except ImportError:
            self.stderr.write(self.style.ERROR(
                "chromadb is not installed. Add it to the env temporarily for the port: "
                "`pip install chromadb`. It can be removed from requirements.txt after the "
                "port lands in prod."
            ))
            return

        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase as KB
        from apps.curriculum.models import CurriculumChunk

        total_read = 0
        total_written = 0
        per_institution: Dict[int, Dict[str, int]] = {}

        for inst_dir in institution_dirs:
            raw_inst_id = self._parse_institution_id(inst_dir.name)
            if raw_inst_id is None:
                self.stdout.write(f"  [skip] {inst_dir.name} — cannot parse institution id")
                continue

            # Defensive normalisation: 12 → 0. The KB enforces this at
            # runtime too, but doing it here keeps the new table
            # internally consistent (no chance of two rows for the
            # same chunk at two different "Global" ids).
            normalised_inst_id = KB._normalise_institution_id(raw_inst_id)
            if normalised_inst_id != raw_inst_id:
                self.stdout.write(
                    f"  [normalise] institution_{raw_inst_id} → institution_{normalised_inst_id} "
                    f"(matches Institution.get_global().id; routing to KB's canonical Global bucket)"
                )

            try:
                client = chromadb.PersistentClient(path=str(inst_dir))
                collections = client.list_collections()
            except Exception as exc:
                self.stderr.write(self.style.WARNING(
                    f"  [skip] {inst_dir.name} — could not open ChromaDB: {exc}"
                ))
                continue

            inst_read = 0
            inst_written = 0
            for col in collections:
                try:
                    result = col.get(include=['documents', 'metadatas', 'embeddings'])
                except Exception as exc:
                    self.stderr.write(self.style.WARNING(
                        f"  [skip] {inst_dir.name}/{col.name} — get() failed: {exc}"
                    ))
                    continue

                # ChromaDB returns numpy arrays for ``embeddings`` —
                # we can't use ``or []`` on those (ambiguous truth value).
                ids = list(result.get('ids') or [])
                documents = list(result.get('documents') or [])
                raw_embeddings = result.get('embeddings')
                embeddings = list(raw_embeddings) if raw_embeddings is not None else []
                metadatas = list(result.get('metadatas') or [])
                n = len(ids)
                if n == 0:
                    self.stdout.write(f"  [empty] {inst_dir.name}/{col.name}: 0 docs")
                    continue

                inst_read += n
                total_read += n

                if opts['dry_run']:
                    self.stdout.write(
                        f"  [dry] {inst_dir.name}/{col.name}: would port {n} chunks"
                    )
                    continue

                # Bulk-upsert in batches. We pre-compute content_hash here
                # rather than relying on kb_storage.upsert_chunks because
                # the latter re-embeds from text; here we want to PRESERVE
                # the existing embeddings byte-for-byte.
                rows: List[CurriculumChunk] = []
                for i in range(n):
                    content = documents[i] or ''
                    if not content.strip():
                        # ChromaDB sometimes has empty docs from a failed
                        # parse. Skip — they wouldn't survive the pgvector
                        # round-trip anyway.
                        continue
                    meta = (metadatas[i] or {}) if i < len(metadatas) else {}
                    fields = {k: meta[k] for k in _METADATA_KEYS if k in meta and meta[k] is not None}
                    rows.append(CurriculumChunk(
                        institution_id=normalised_inst_id,
                        content=content,
                        content_hash=CurriculumChunk.compute_hash(content),
                        embedding=list(embeddings[i]) if i < len(embeddings) else [],
                        **fields,
                    ))

                # Idempotent upsert. Same pattern as kb_storage.upsert_chunks.
                if rows:
                    batch = opts['batch_size']
                    with transaction.atomic():
                        for j in range(0, len(rows), batch):
                            CurriculumChunk.objects.bulk_create(
                                rows[j:j + batch],
                                update_conflicts=True,
                                unique_fields=['institution_id', 'content_hash'],
                                update_fields=[
                                    'content', 'embedding', *_METADATA_KEYS, 'updated_at',
                                ],
                            )
                inst_written += len(rows)
                total_written += len(rows)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [ok]   {inst_dir.name}/{col.name}: read {n}, wrote {len(rows)}"
                    )
                )

            per_institution[normalised_inst_id] = {
                'read': per_institution.get(normalised_inst_id, {}).get('read', 0) + inst_read,
                'written': per_institution.get(normalised_inst_id, {}).get('written', 0) + inst_written,
            }

        # Final report
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(f"Port summary ({'dry-run' if opts['dry_run'] else 'live'}):"))
        for inst_id in sorted(per_institution):
            stats = per_institution[inst_id]
            self.stdout.write(
                f"  institution_id={inst_id:<4}  read={stats['read']:<6}  written={stats['written']}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"TOTAL: read {total_read}, written {total_written}"
            )
        )

        if not opts['dry_run']:
            # Sanity: total rows in pgvector
            try:
                total_rows = CurriculumChunk.objects.count()
                self.stdout.write(f"CurriculumChunk row count after port: {total_rows}")
            except Exception as exc:
                self.stderr.write(self.style.WARNING(f"Count query failed: {exc}"))

    @staticmethod
    def _parse_institution_id(dirname: str) -> int | None:
        """``institution_3`` → 3, ``institution_None`` → None (skip)."""
        if not dirname.startswith('institution_'):
            return None
        suffix = dirname[len('institution_'):]
        if suffix.isdigit() or (suffix.startswith('-') and suffix[1:].isdigit()):
            return int(suffix)
        # ``institution_None`` and similar junk — skip.
        return None
