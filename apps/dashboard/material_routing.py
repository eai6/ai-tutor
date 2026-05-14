"""Material processing routing.

Single source of truth for the 50-page threshold that decides whether a
teaching-material upload runs in-process (small files) or dispatches to the
Azure Container Apps Job (large files).

Same rule applies across textbooks, references, worksheets, notes, question
banks — material type doesn't enter the decision.
"""

import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

# >=50 pages → Container Apps Job. <50 → in-process.
# Anchor: "longer than a single lesson worth of notes."
MATERIAL_JOB_PAGE_THRESHOLD = 50


def count_pdf_pages(file_path: str) -> int:
    """Cheap page count via PyMuPDF. Returns 0 for non-PDFs or open errors."""
    if not file_path or not file_path.lower().endswith('.pdf'):
        return 0
    if not os.path.exists(file_path):
        return 0
    try:
        import fitz
        doc = fitz.open(file_path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception as exc:
        logger.warning(f"count_pdf_pages: could not open {file_path}: {exc}")
        return 0


def should_dispatch_to_job(file_path: str) -> Tuple[bool, int]:
    """Returns (use_job, page_count).

    Non-PDF files are page_count=0 → in-process always (DOCX/MD/TXT can't
    even hit the vision path).
    """
    pages = count_pdf_pages(file_path)
    return pages >= MATERIAL_JOB_PAGE_THRESHOLD, pages
