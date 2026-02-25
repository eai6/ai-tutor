"""
Index OpenStax textbooks into the global/platform-level knowledge base.

These are general secondary-level textbooks (biology, chemistry, physics, math, etc.)
that serve as platform-level backup content available to all institutions.

Usage:
    python manage.py index_openstax                     # Index all subjects
    python manage.py index_openstax --subject biology    # Index one subject
    python manage.py index_openstax --force              # Re-index everything
    python manage.py index_openstax --list               # Show what would be indexed
"""

import os
import json
import logging

from django.core.management.base import BaseCommand
from django.conf import settings

logger = logging.getLogger(__name__)

# Map directory names to canonical subject names
SUBJECT_MAP = {
    'biology': 'Biology',
    'chemistry': 'Chemistry',
    'physics': 'Physics',
    'mathematics': 'Mathematics',
    'precalculus': 'Mathematics',
    'statistics': 'Mathematics',
}


class Command(BaseCommand):
    help = 'Index OpenStax textbooks into the global/platform knowledge base'

    def add_arguments(self, parser):
        parser.add_argument(
            '--subject',
            type=str,
            help='Index only this subject directory (e.g., biology, chemistry)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Re-index all files, even if already indexed',
        )
        parser.add_argument(
            '--list',
            action='store_true',
            help='List files that would be indexed without actually indexing',
        )

    def handle(self, *args, **options):
        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase

        # Locate openstax directory (note trailing space in dir name)
        base_dir = os.path.join(settings.BASE_DIR, 'openstax_resources ')
        if not os.path.isdir(base_dir):
            # Try without trailing space
            base_dir = os.path.join(settings.BASE_DIR, 'openstax_resources')
            if not os.path.isdir(base_dir):
                self.stderr.write(self.style.ERROR(
                    f"OpenStax directory not found. Expected at: {settings.BASE_DIR}/openstax_resources/"
                ))
                return

        # Get global KB
        kb = CurriculumKnowledgeBase.get_global_kb()

        # Load tracking file for idempotency
        tracking_path = os.path.join(kb.persist_directory, 'indexed_files.json')
        indexed_files = {}
        if os.path.exists(tracking_path):
            with open(tracking_path, 'r') as f:
                indexed_files = json.load(f)

        # Determine which subject directories to process
        if options['subject']:
            subject_dirs = [options['subject'].lower()]
        else:
            subject_dirs = sorted([
                d for d in os.listdir(base_dir)
                if os.path.isdir(os.path.join(base_dir, d))
            ])

        total_indexed = 0
        total_skipped = 0
        total_failed = 0

        for subject_dir in subject_dirs:
            dir_path = os.path.join(base_dir, subject_dir)
            if not os.path.isdir(dir_path):
                self.stderr.write(self.style.WARNING(f"Directory not found: {subject_dir}"))
                continue

            subject = SUBJECT_MAP.get(subject_dir, subject_dir.title())
            self.stdout.write(f"\n--- {subject} ({subject_dir}/) ---")

            pdf_files = sorted([
                f for f in os.listdir(dir_path)
                if f.lower().endswith('.pdf')
            ])

            for pdf_file in pdf_files:
                file_path = os.path.join(dir_path, pdf_file)
                file_stat = os.stat(file_path)
                file_key = f"{subject_dir}/{pdf_file}"

                # Check if already indexed (by path + size + mtime)
                if not options['force'] and file_key in indexed_files:
                    prev = indexed_files[file_key]
                    if (prev.get('size') == file_stat.st_size
                            and prev.get('mtime') == file_stat.st_mtime):
                        self.stdout.write(f"  SKIP {pdf_file} (already indexed)")
                        total_skipped += 1
                        continue

                if options['list']:
                    size_mb = file_stat.st_size / (1024 * 1024)
                    self.stdout.write(f"  WOULD INDEX {pdf_file} ({size_mb:.1f} MB)")
                    continue

                # Index the file
                size_mb = file_stat.st_size / (1024 * 1024)
                self.stdout.write(f"  Indexing {pdf_file} ({size_mb:.1f} MB)...")

                try:
                    result = kb.index_teaching_material(
                        file_path=file_path,
                        subject=subject,
                        grade_level='',  # OpenStax covers multiple grades
                        material_title=pdf_file.replace('.pdf', ''),
                        material_type='reference',
                    )

                    chunks = result.get('chunks_indexed', 0)
                    self.stdout.write(self.style.SUCCESS(
                        f"    OK: {chunks} chunks indexed"
                    ))

                    # Track as indexed
                    indexed_files[file_key] = {
                        'size': file_stat.st_size,
                        'mtime': file_stat.st_mtime,
                        'chunks': chunks,
                        'subject': subject,
                    }
                    total_indexed += 1

                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"    FAILED: {e}"))
                    logger.error(f"Failed to index {file_path}: {e}", exc_info=True)
                    total_failed += 1

        # Save tracking file
        if not options['list']:
            os.makedirs(os.path.dirname(tracking_path), exist_ok=True)
            with open(tracking_path, 'w') as f:
                json.dump(indexed_files, f, indent=2)

        # Summary
        self.stdout.write(f"\nDone: {total_indexed} indexed, {total_skipped} skipped, {total_failed} failed")
        stats = kb.get_collection_stats()
        self.stdout.write(f"Global KB total: {stats.get('total_chunks', '?')} chunks")
