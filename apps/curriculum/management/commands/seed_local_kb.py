"""Seed local ChromaDB at institution_id=0 with curriculum content
for offline / dev fact-checker testing.

The factual_step content judge (and tutor-side fact_verifier) retrieves
KB evidence to verify generated claims. Local SQLite ships empty, which
means the judges skip with `no_kb_evidence` — fine in production where
the KB is populated, but it blocks fact-checker development locally.

This command reads PDFs / DOCX from `test-files/` and indexes them into
ChromaDB at `media/vectordb/institution_0/` so dev smokes have something
to retrieve against.

Run with:
    python manage.py seed_local_kb                          # default doc set
    python manage.py seed_local_kb --file path/to/doc.pdf   # one specific doc
    python manage.py seed_local_kb --reset                  # drop existing index first
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


# Default seed set — covers the Seychelles geography curriculum that
# most local lessons map to. Add to this list as new test docs land.
DEFAULT_DOCS = [
    {
        "path": "test-files/perseverence_docs_syllabi_v2.pdf",
        "subject": "Geography",
        "grade_level": "S3",
    },
]


class Command(BaseCommand):
    help = "Seed local ChromaDB at institution_id=0 with curriculum docs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            help="Index a single specific file (overrides default doc set).",
        )
        parser.add_argument(
            "--subject",
            default="Geography",
            help="Subject for --file (ignored without --file).",
        )
        parser.add_argument(
            "--grade-level",
            default="S3",
            help="Grade level for --file (ignored without --file).",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Wipe the local institution_0 vectordb before indexing.",
        )

    def handle(self, *args, **opts):
        from apps.curriculum.knowledge_base import get_knowledge_base
        from django.conf import settings

        if opts.get("reset"):
            persist_dir = Path(settings.VECTORDB_ROOT) / "institution_0"
            if persist_dir.exists():
                self.stdout.write(
                    self.style.WARNING(f"Wiping {persist_dir} ...")
                )
                import shutil
                shutil.rmtree(persist_dir, ignore_errors=True)

        if opts.get("file"):
            docs = [{
                "path": opts["file"],
                "subject": opts["subject"],
                "grade_level": opts["grade_level"],
            }]
        else:
            docs = DEFAULT_DOCS

        kb = get_knowledge_base(0)
        total_chunks = 0
        total_figures = 0

        for doc in docs:
            path = doc["path"]
            if not os.path.exists(path):
                self.stdout.write(self.style.WARNING(
                    f"Skipping {path} — file not found"
                ))
                continue

            self.stdout.write(f"Indexing {path} ({doc['subject']} / {doc['grade_level']}) ...")
            t = time.time()
            try:
                result = kb.index_curriculum_document(
                    file_path=path,
                    subject=doc["subject"],
                    grade_level=doc["grade_level"],
                )
            except Exception as exc:
                self.stdout.write(self.style.ERROR(
                    f"  FAILED: {type(exc).__name__}: {exc}"
                ))
                continue
            elapsed = time.time() - t
            chunks = result.get("chunks_indexed", 0)
            figs = result.get("figures_indexed", 0)
            total_chunks += chunks
            total_figures += figs
            self.stdout.write(self.style.SUCCESS(
                f"  OK ({elapsed:.1f}s): {chunks} chunks, {figs} figures"
            ))

        # Verify retrieval works
        self.stdout.write("\nVerifying retrieval:")
        for q in [
            "Seychelles climate",
            "geography of Mahe",
            "tropical rainfall",
        ]:
            r = kb.search(q, n_results=2) or []
            self.stdout.write(
                f"  query={q!r}: {len(r)} hits"
            )

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Total: {total_chunks} chunks + {total_figures} figures "
            f"in vectordb at institution_0."
        ))
