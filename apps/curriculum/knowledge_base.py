"""
Curriculum Knowledge Base

This module provides RAG (Retrieval Augmented Generation) capabilities for curriculum content.
It vectorizes curriculum documents and enables semantic search for:
- Lesson generation with curriculum context
- Tutoring sessions with teaching strategies
- Content generation with aligned objectives

ARCHITECTURE:
1. PARSE: Extract text from PDF/DOCX
2. VECTORIZE: Chunk and embed into ChromaDB
3. GENERATE LESSONS: Query DB to structure curriculum
4. GENERATE CONTENT: Query DB for rich context + media
5. TUTORING: Query DB for teaching strategies and context

INSTALLATION:
    pip install chromadb sentence-transformers

USAGE:
    from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
    
    kb = CurriculumKnowledgeBase(institution_id=1)
    
    # Index a curriculum document
    kb.index_curriculum_document(file_path, subject="Mathematics", grade="S1")
    
    # Query for lesson generation
    context = kb.query_for_lesson_generation(
        unit_title="Algebra",
        lesson_topic="Solving Linear Equations"
    )
    
    # Query during tutoring
    context = kb.query_for_tutoring(
        lesson_id=123,
        student_question="How do I solve 2x + 5 = 15?"
    )
"""

import os
import re
import json
import logging
import hashlib
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class CurriculumChunk:
    """A chunk of curriculum content for vectorization."""
    id: str
    content: str
    metadata: Dict
    # Metadata includes: subject, grade, unit, chunk_type, source_file


@dataclass
class QueryResult:
    """Result from a knowledge base query."""
    chunks: List[Dict]
    context_summary: str
    teaching_strategies: List[str]
    objectives: List[str]
    

# ============================================================================
# CURRICULUM KNOWLEDGE BASE
# ============================================================================

class CurriculumKnowledgeBase:
    """
    Vector-based knowledge base for curriculum content.

    Uses ChromaDB for local vector storage and sentence-transformers for embeddings.
    Supports two-tier retrieval: institution-specific KB + global/platform KB (institution_id=0).
    """

    # The canonical "platform-wide" bucket id. Every chunk that should
    # be visible to every school's tutor lives at institution_id=0.
    # Historical bug: many call sites used ``Institution.get_global().id``
    # (= 12, the DB Institution row PK) thinking it was the same thing.
    # It isn't — the KB never indexed there. The result was that
    # "All Schools" uploads landed at id=12 while every per-institution
    # query merged from id=0, which was empty → silent inheritance
    # failure. ``__init__`` now defensively normalises 12 → 0 so we
    # can't repeat that. New code should use this constant directly.
    GLOBAL_INSTITUTION_ID = 0
    # Minimum number of results from institution KB before we skip global fallback
    FALLBACK_THRESHOLD = 3

    # Shared embedding function — loaded once, reused across all instances
    _shared_embedding_fn = None

    @classmethod
    def get_global_kb(cls):
        """Get the global/platform-level knowledge base (OpenStax, shared resources)."""
        return cls(institution_id=cls.GLOBAL_INSTITUTION_ID)

    @classmethod
    def _normalise_institution_id(cls, institution_id) -> int:
        """Resolve any caller-supplied "Global" identifier to ``GLOBAL_INSTITUTION_ID``.

        Accepts ``None`` (no course-institution), the canonical 0, or
        the DB row PK of the ``Institution.get_global()`` row (= 12 in
        prod). All three route to the same bucket. Anything else is
        passed through unchanged (= a real per-school institution PK).

        This is the safety net that prevents the inheritance failure
        we hit in production: a teacher uploads under "All Schools",
        the upload code resolves None to ``Institution.get_global().id``
        (= 12), but the KB at runtime queries the global bucket at 0.
        Without this normalisation those two values index different
        buckets and the school-level query gets nothing back from
        global. See memory/pgvector_migration_plan.md.
        """
        if institution_id is None:
            return cls.GLOBAL_INSTITUTION_ID
        if institution_id == cls.GLOBAL_INSTITUTION_ID:
            return cls.GLOBAL_INSTITUTION_ID
        # Resolve the "Global (All Schools)" Institution row's PK once
        # and check against it. Cached at class level so we don't hit
        # the DB on every KB instantiation.
        try:
            global_pk = getattr(cls, '_cached_global_pk', None)
            if global_pk is None:
                from apps.accounts.models import Institution
                global_pk = Institution.get_global().id
                cls._cached_global_pk = global_pk
            if institution_id == global_pk:
                logger.debug(
                    "[KB] normalised Institution.get_global().id (%s) → "
                    "GLOBAL_INSTITUTION_ID (%s). Caller should switch to "
                    "the canonical constant; this normalisation is a "
                    "defensive shim.",
                    global_pk, cls.GLOBAL_INSTITUTION_ID,
                )
                return cls.GLOBAL_INSTITUTION_ID
        except Exception:
            # Don't let a DB failure here block KB init — fall back to
            # the raw value.
            pass
        return institution_id

    def __init__(self, institution_id: int, persist_directory: str = None):
        """Initialize the knowledge base.

        Args:
            institution_id: ID of the institution (for data isolation)
            persist_directory: Ignored (kept for back-compat with the
                ChromaDB era). Storage is now in Postgres via
                ``apps.curriculum.kb_storage`` — see
                ``memory/pgvector_migration_plan.md``.
        """
        # Defensive normalisation. Routes None and
        # Institution.get_global().id (= 12) to the canonical 0.
        # See ``_normalise_institution_id`` for why.
        self.institution_id = self._normalise_institution_id(institution_id)
        self.collection_name = f"curriculum_{self.institution_id}"
        # ``persist_directory`` retained as an attribute purely so
        # legacy log lines / debugging code that inspects it doesn't
        # crash. It points nowhere real now.
        self.persist_directory = persist_directory or f"<pgvector:institution_{self.institution_id}>"

        # Storage backend availability: signal in the same shape the
        # ChromaDB era exposed. Read by ``_index_chunks`` and the query
        # methods to bail out cleanly when vector storage isn't usable.
        #
        # This was ``connection.vendor == 'postgresql'`` until 2026-07-30,
        # which made all eleven guarded methods no-op on SQLite. Combined
        # with the same check inside kb_storage, an offline/SQLite install
        # silently retrieved nothing and the tutor ran ungrounded without
        # a warning. kb_storage now serves both backends (pgvector index on
        # Postgres, brute-force cosine elsewhere), so storage is available
        # everywhere and the flag stays only because callers still read it.
        from django.db import connection
        self._storage_available = True
        logger.debug(
            "[KB] storage backend=%s (%s)",
            connection.vendor,
            'pgvector' if connection.vendor == 'postgresql' else 'bruteforce',
        )

    def _get_collection(self):
        """Back-compat shim — no longer returns a ChromaDB collection.

        Returns a sentinel that's truthy when storage is available so
        the legacy ``if collection is None`` guards in this file still
        short-circuit correctly on SQLite. Direct calls to
        ``collection.upsert`` / ``collection.query`` no longer exist;
        every old call site has been ported to
        ``apps.curriculum.kb_storage`` primitives.
        """
        return self if self._storage_available else None
    
    # ========================================================================
    # STEP 1 & 2: PARSE AND VECTORIZE
    # ========================================================================
    
    def index_curriculum_document(
        self,
        file_path: str,
        subject: str,
        grade_level: str,
        curriculum_upload_id: int = None
    ) -> Dict:
        """
        Parse a curriculum document and index it into the vector database.
        
        This is Steps 1 & 2 of the pipeline:
        1. PARSE: Extract text from PDF/DOCX
        2. VECTORIZE: Chunk and embed into ChromaDB
        
        Args:
            file_path: Path to the curriculum document
            subject: Subject name (e.g., "Mathematics")
            grade_level: Grade level (e.g., "S1", "S2")
            curriculum_upload_id: Optional ID of the CurriculumUpload record
        
        Returns:
            Dict with indexing statistics
        """
        from apps.curriculum.curriculum_parser import extract_text_from_file
        
        # Step 1: Extract text
        logger.info(f"Parsing document: {file_path}")
        text, file_type = extract_text_from_file(file_path)
        
        if not text or len(text) < 100:
            raise ValueError("Could not extract meaningful text from document")
        
        # Step 2: Chunk the text
        chunks = self._chunk_curriculum_text(
            text=text,
            subject=subject,
            grade_level=grade_level,
            source_file=os.path.basename(file_path),
            upload_id=curriculum_upload_id
        )
        
        # Step 3: Index chunks into vector DB
        result = self._index_chunks(chunks)

        # Step 4: Extract and index figures from PDF
        figures_indexed = 0
        if file_path.lower().endswith('.pdf'):
            try:
                from apps.curriculum.curriculum_parser import extract_figures_from_pdf
                figures = extract_figures_from_pdf(file_path, institution_id=self.institution_id)
                if figures:
                    fig_result = self._process_and_index_figures(
                        figures=figures,
                        subject=subject,
                        grade_level=grade_level,
                        source_file=os.path.basename(file_path),
                        upload_id=curriculum_upload_id,
                    )
                    figures_indexed = fig_result.get('figures_indexed', 0)
            except Exception as e:
                logger.warning(f"Figure extraction skipped for {file_path}: {e}")

        return {
            "success": True,
            "file_path": file_path,
            "text_length": len(text),
            "chunks_created": len(chunks),
            "chunks_indexed": result.get("indexed", 0),
            "figures_indexed": figures_indexed,
        }

    def _chunk_curriculum_text(
        self,
        text: str,
        subject: str,
        grade_level: str,
        source_file: str,
        upload_id: int = None
    ) -> List[CurriculumChunk]:
        """
        Split curriculum text into meaningful chunks for vectorization.
        
        Chunks are created based on:
        - Section boundaries (headers, units)
        - Paragraph boundaries
        - Maximum chunk size (~500 tokens)
        """
        chunks = []
        
        # Detect sections using various markers
        section_patterns = [
            r'^#{1,3}\s+(.+)$',  # Markdown headers
            r'^\*\*(.+)\*\*$',    # Bold text on own line
            r'^([A-Z][A-Z\s]+)$',  # ALL CAPS headers
            r'^(Unit\s+\d+[:\.]?\s*.*)$',  # Unit markers
            r'^(\d+\.\s+[A-Z].+)$',  # Numbered sections
        ]
        
        lines = text.split('\n')
        current_section = "Introduction"
        current_chunk = []
        current_chunk_type = "general"
        chunk_counter = [0]  # mutable counter for closure

        def create_chunk(content: str, section: str, chunk_type: str) -> CurriculumChunk:
            """Create a chunk with metadata."""
            content = content.strip()
            if not content or len(content) < 20:
                return None

            chunk_counter[0] += 1
            chunk_id = hashlib.md5(
                f"{source_file}:{chunk_counter[0]}:{section}:{content[:100]}".encode()
            ).hexdigest()[:16]
            
            return CurriculumChunk(
                id=chunk_id,
                content=content,
                metadata={
                    "subject": subject,
                    "grade_level": grade_level,
                    "section": section,
                    "chunk_type": chunk_type,
                    "source_file": source_file,
                    "upload_id": upload_id,
                    "institution_id": self.institution_id,
                }
            )
        
        # Detect chunk types based on content
        def detect_chunk_type(text: str) -> str:
            text_lower = text.lower()
            if any(kw in text_lower for kw in ['objective', 'learner will', 'student will', 'be able to']):
                return "objective"
            elif any(kw in text_lower for kw in ['strategy', 'method', 'approach', 'teaching']):
                return "teaching_strategy"
            elif any(kw in text_lower for kw in ['assess', 'evaluat', 'test', 'quiz']):
                return "assessment"
            elif any(kw in text_lower for kw in ['resource', 'material', 'textbook']):
                return "resource"
            else:
                return "content"
        
        for line in lines:
            line_stripped = line.strip()
            
            # Check if this is a section header
            is_header = False
            for pattern in section_patterns:
                match = re.match(pattern, line_stripped, re.MULTILINE)
                if match:
                    # Save current chunk
                    if current_chunk:
                        chunk_text = '\n'.join(current_chunk)
                        chunk = create_chunk(chunk_text, current_section, current_chunk_type)
                        if chunk:
                            chunks.append(chunk)
                    
                    # Start new section
                    current_section = match.group(1).strip('*# ')[:100]
                    current_chunk = []
                    current_chunk_type = detect_chunk_type(current_section)
                    is_header = True
                    break
            
            if not is_header and line_stripped:
                current_chunk.append(line_stripped)
                
                # Check if chunk is getting too long (roughly 500 tokens ~ 2000 chars)
                chunk_text = '\n'.join(current_chunk)
                if len(chunk_text) > 2000:
                    # Save this chunk and start a new one
                    chunk = create_chunk(chunk_text, current_section, current_chunk_type)
                    if chunk:
                        chunks.append(chunk)
                    current_chunk = []
        
        # Don't forget the last chunk
        if current_chunk:
            chunk_text = '\n'.join(current_chunk)
            chunk = create_chunk(chunk_text, current_section, current_chunk_type)
            if chunk:
                chunks.append(chunk)
        
        logger.info(f"Created {len(chunks)} chunks from document")
        return chunks

    def _chunk_question_bank_text(
        self,
        text: str,
        subject: str,
        grade_level: str,
        source_file: str,
        upload_id: int = None
    ) -> List[CurriculumChunk]:
        """
        Split question bank / exam paper text into individual question chunks.

        Designed to be robust across different exam paper formats:
        - Extracts metadata (year, paper number, marking scheme) from BOTH
          filename and content, with content taking priority
        - Detects question boundaries via numbered patterns (Q1, 1., Question 1, etc.)
        - Classifies questions as MCQ (if A/B/C/D options found) or structured
        - Detects marking schemes from content keywords, not just filename
        - Falls back to standard section chunking if <3 questions detected
        """
        chunks = []

        # --- Extract metadata from filename (optional enrichment) ---
        year = None
        paper_number = None
        is_marking_scheme = False

        filename_lower = source_file.lower()

        # Year from filename (e.g., "2021", "2019")
        year_match = re.search(r'(20\d{2})', source_file)
        if year_match:
            year = year_match.group(1)

        # Paper number from filename
        paper_match = re.search(r'[Pp]aper[_\s\-]*(\d)', source_file)
        if paper_match:
            paper_number = paper_match.group(1)

        # Marking scheme from filename
        ms_patterns = ['marking_scheme', 'mark_scheme', 'markscheme', 'corrig',
                       'answer_key', 'answers', 'memo', 'memorandum']
        if any(p in filename_lower for p in ms_patterns):
            is_marking_scheme = True

        # --- Extract/override metadata from content ---
        # Year from content header (e.g., "June 2021", "November 2020 Examination")
        content_year_match = re.search(
            r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
            r'[\s,]*(\d{4})',
            text[:2000], re.IGNORECASE
        )
        if content_year_match:
            year = content_year_match.group(1)

        # Paper number from content (e.g., "Paper 1", "PAPER 2")
        content_paper_match = re.search(r'[Pp][Aa][Pp][Ee][Rr]\s*(\d)', text[:2000])
        if content_paper_match:
            paper_number = content_paper_match.group(1)

        # Marking scheme from content
        ms_content_patterns = [
            r'mark\s*(?:ing)?\s*scheme', r'mark\s*allocation',
            r'answer\s*key', r'model\s*answers?', r'suggested\s*answers?',
            r'correct\s*answers?', r'memorandum'
        ]
        first_500 = text[:500].lower()
        if any(re.search(p, first_500) for p in ms_content_patterns):
            is_marking_scheme = True

        # --- Detect question boundaries ---
        # Patterns that mark the start of a new question
        question_patterns = [
            r'^\s*(?:Q(?:uestion)?\.?\s*)(\d{1,3})\s*[\.\)\:]',  # Q1. Q.1) Question 1:
            r'^\s*(\d{1,3})\s*[\.\)]\s+(?=[A-Z])',                # 1. What... or 1) What...
            r'^\s*(\d{1,3})\s*[\.\)]\s*\(',                        # 1. (a) ...
        ]

        lines = text.split('\n')
        question_starts = []  # List of (line_index, question_number)

        for i, line in enumerate(lines):
            for pattern in question_patterns:
                match = re.match(pattern, line)
                if match:
                    q_num = int(match.group(1))
                    # Sanity check: question numbers should be reasonable (1-200)
                    if 1 <= q_num <= 200:
                        question_starts.append((i, q_num))
                    break

        # --- Fallback to standard chunking if too few questions detected ---
        if len(question_starts) < 3:
            logger.info(f"Only {len(question_starts)} questions detected in {source_file}, "
                        f"falling back to standard chunking")
            fallback_chunks = self._chunk_curriculum_text(
                text=text, subject=subject, grade_level=grade_level,
                source_file=source_file, upload_id=upload_id
            )
            # Tag fallback chunks with question bank metadata
            for chunk in fallback_chunks:
                chunk.metadata['source_type'] = 'question_bank'
                if is_marking_scheme:
                    chunk.metadata['chunk_type'] = 'marking_scheme'
                if year:
                    chunk.metadata['year'] = year
                if paper_number:
                    chunk.metadata['paper_number'] = paper_number
            return fallback_chunks

        # --- Build question chunks ---
        def _detect_question_type(q_text: str) -> str:
            """Detect if MCQ (has A/B/C/D options) or structured."""
            option_patterns = [
                r'^\s*[A-D]\s*[\.\)\:]',         # A. or A) or A:
                r'^\s*\([A-D]\)',                  # (A)
                r'\b[A-D]\s*[\.\)]\s+\w',          # A. Something
            ]
            option_count = 0
            for line in q_text.split('\n'):
                for pat in option_patterns:
                    if re.match(pat, line.strip()):
                        option_count += 1
                        break
            return 'mcq' if option_count >= 3 else 'structured'

        def _detect_has_answers(q_text: str) -> bool:
            """Check if chunk contains answer indicators."""
            answer_patterns = [
                r'(?:correct|right)\s*(?:answer|option)',
                r'(?:ans(?:wer)?)\s*[:=]',
                r'\b(?:mark|score)\s*[:=]\s*\d',
                r'(?:solution|working)',
            ]
            text_lower = q_text.lower()
            return any(re.search(p, text_lower) for p in answer_patterns)

        for idx, (start_line, q_num) in enumerate(question_starts):
            # Determine end of this question (start of next question or end of text)
            if idx + 1 < len(question_starts):
                end_line = question_starts[idx + 1][0]
            else:
                end_line = len(lines)

            q_text = '\n'.join(lines[start_line:end_line]).strip()

            if not q_text or len(q_text) < 15:
                continue

            # If chunk is very long (>3000 chars), it might contain sub-questions
            # Keep it as one chunk but cap at 4000 chars
            if len(q_text) > 4000:
                q_text = q_text[:4000] + "\n[truncated]"

            question_type = _detect_question_type(q_text)
            has_answers = _detect_has_answers(q_text)

            chunk_type = 'marking_scheme' if is_marking_scheme else 'exam_question'

            chunk_id = hashlib.md5(
                f"{source_file}:q{q_num}:{q_text[:100]}".encode()
            ).hexdigest()[:16]

            metadata = {
                "subject": subject,
                "grade_level": grade_level,
                "section": f"Question {q_num}",
                "chunk_type": chunk_type,
                "source_file": source_file,
                "upload_id": upload_id,
                "institution_id": self.institution_id,
                "source_type": "question_bank",
                "question_number": q_num,
                "question_type": question_type,
                "has_answers": has_answers,
            }

            # Add optional metadata only if available
            if year:
                metadata["year"] = year
            if paper_number:
                metadata["paper_number"] = paper_number

            chunks.append(CurriculumChunk(
                id=chunk_id,
                content=q_text,
                metadata=metadata,
            ))

        logger.info(
            f"Created {len(chunks)} question chunks from {source_file} "
            f"(year={year}, paper={paper_number}, marking_scheme={is_marking_scheme})"
        )
        return chunks

    def _index_chunks(self, chunks: List[CurriculumChunk]) -> Dict:
        """Index chunks into pgvector.

        Was ChromaDB ``collection.upsert(ids, documents, metadatas)``;
        now routes through ``kb_storage.upsert_chunks`` which writes
        to the ``CurriculumChunk`` Postgres model. Dedup key is
        ``(institution_id, content_hash)`` — re-indexing the same
        content updates in place.
        """
        if not self._storage_available:
            logger.warning("[KB] _index_chunks: storage unavailable, skipping")
            return {"indexed": 0, "error": "vector storage unavailable on this backend"}

        from apps.curriculum.kb_storage import upsert_chunks
        result = upsert_chunks(self.institution_id, chunks)
        logger.info(f"[KB] Indexed {result.get('indexed', 0)} chunks via pgvector")
        return result
    
    def index_teaching_material(
        self,
        file_path: str,
        subject: str,
        grade_level: str,
        material_title: str,
        material_type: str = 'textbook',
        upload_id: int = None,
        extract_figures: bool = True,
        progress_cb=None,
    ) -> Dict:
        """
        Parse a teaching material (textbook, reference, worksheet) and index it.

        Uses the same chunking and indexing as curriculum documents, but tags
        chunks with source_type='teaching_material' so they can be distinguished.

        Args:
            file_path: Path to the document
            subject: Subject name
            grade_level: Grade level
            material_title: Title of the material
            material_type: Type (textbook, reference, worksheet, notes, other)
            upload_id: Optional TeachingMaterialUpload ID
            extract_figures: Run vision-LLM figure extraction (Rich pipeline only).
                Fast pipeline passes False so the call stays text-only — fanning
                out vision calls per figure-bearing page is the dominant cost
                for figure-heavy textbooks.
            progress_cb: Optional ``(pages_processed, pages_total, phase)``
                callback forwarded to the vision-OCR fallback so the caller can
                update per-batch progress on the upload row.

        Returns:
            Dict with indexing statistics
        """
        from apps.curriculum.curriculum_parser import extract_text_from_file

        logger.info(f"Parsing teaching material: {file_path}")
        text, file_type = extract_text_from_file(file_path, progress_cb=progress_cb)

        if not text or len(text) < 100:
            raise ValueError("Could not extract meaningful text from document")

        # Route to specialized chunking for question banks
        source_file = os.path.basename(file_path)
        if material_type == 'question_bank':
            chunks = self._chunk_question_bank_text(
                text=text,
                subject=subject,
                grade_level=grade_level,
                source_file=source_file,
                upload_id=upload_id
            )
        else:
            chunks = self._chunk_curriculum_text(
                text=text,
                subject=subject,
                grade_level=grade_level,
                source_file=source_file,
                upload_id=upload_id
            )

        # Tag chunks with teaching material metadata
        for chunk in chunks:
            chunk.metadata['source_type'] = 'teaching_material'
            chunk.metadata['material_type'] = material_type
            chunk.metadata['material_title'] = material_title

        # Index into vector DB
        result = self._index_chunks(chunks)

        # Extract and index figures from PDF (Rich-mode only).
        # Fast mode passes extract_figures=False so it stays a true
        # text-only path with no vision-LLM fan-out.
        figures_indexed = 0
        if extract_figures and file_path.lower().endswith('.pdf'):
            try:
                from apps.curriculum.curriculum_parser import extract_figures_from_pdf
                figures = extract_figures_from_pdf(file_path, institution_id=self.institution_id)
                if figures:
                    fig_result = self._process_and_index_figures(
                        figures=figures,
                        subject=subject,
                        grade_level=grade_level,
                        source_file=source_file,
                        upload_id=upload_id,
                    )
                    figures_indexed = fig_result.get('figures_indexed', 0)
            except Exception as e:
                logger.warning(f"Figure extraction skipped for {file_path}: {e}")

        return {
            "success": True,
            "file_path": file_path,
            "text_length": len(text),
            "chunks_created": len(chunks),
            "chunks_indexed": result.get("indexed", 0),
            "figures_indexed": figures_indexed,
        }

    # ========================================================================
    # FIGURE INDEXING & RETRIEVAL
    # ========================================================================

    def _process_and_index_figures(
        self,
        figures: List[Dict],
        subject: str,
        grade_level: str,
        source_file: str,
        upload_id: int = None,
    ) -> Dict:
        """
        Process extracted figures: save page images as MediaAssets and index
        figure descriptions as CurriculumChunks.

        Args:
            figures: List of figure dicts from extract_figures_from_pdf()
            subject: Subject name
            grade_level: Grade level
            source_file: Original filename
            upload_id: Optional upload record ID

        Returns:
            Dict with figures_found, figures_indexed, media_assets_created
        """
        chunks = []
        media_assets_created = 0

        for fig in figures:
            figure_number = fig.get('figure_number', 'unlabeled')
            figure_type = fig.get('figure_type', 'diagram')
            description = fig.get('description', '')
            educational_context = fig.get('educational_context', '')
            page_number = fig.get('page_number', 0)

            if not description:
                continue

            # Save page image as MediaAsset
            figure_image_url = ''
            page_image_bytes = fig.get('page_image_bytes')
            if page_image_bytes:
                try:
                    from apps.media_library.models import MediaAsset
                    from django.core.files.base import ContentFile
                    from apps.accounts.models import Institution

                    institution = Institution.objects.filter(id=self.institution_id).first()
                    if institution:
                        asset = MediaAsset.objects.create(
                            institution=institution,
                            title=f"{figure_number} - p{page_number} - {source_file}"[:200],
                            asset_type='image',
                            caption=description[:500] if description else '',
                            alt_text=description[:300] if description else '',
                            tags=f"textbook,figure,{subject},{figure_type}"[:200],
                        )
                        filename = f"figure_p{page_number}_{hashlib.md5(description.encode()).hexdigest()[:8]}.png"
                        asset.file.save(filename, ContentFile(page_image_bytes))
                        asset.save()
                        figure_image_url = asset.file.url
                        media_assets_created += 1
                        # Best-effort figure_facts extraction so textbook
                        # figures arrive with the rich metadata the
                        # tutor uses to anchor scaffolding. Non-fatal.
                        try:
                            from apps.curriculum.figure_facts_extractor import (
                                extract_and_save_for_asset,
                            )
                            extract_and_save_for_asset(asset)
                        except Exception as ff_err:
                            logger.warning(
                                f"[FigureFacts] textbook ingest "
                                f"asset #{asset.id} failed: {ff_err}"
                            )
                except Exception as e:
                    logger.warning(f"Failed to save figure MediaAsset: {e}")

            # Build chunk content
            content = f"[FIGURE: {figure_number}] {description}"
            if educational_context:
                content += f" Context: {educational_context}"

            chunk_id = hashlib.md5(
                f"{source_file}:fig:{page_number}:{figure_number}:{description[:80]}".encode()
            ).hexdigest()[:16]

            chunks.append(CurriculumChunk(
                id=chunk_id,
                content=content,
                metadata={
                    "subject": subject,
                    "grade_level": grade_level,
                    "section": f"Figure on page {page_number}",
                    "chunk_type": "figure_description",
                    "source_file": source_file,
                    "upload_id": upload_id,
                    "institution_id": self.institution_id,
                    "figure_type": figure_type,
                    "figure_page": page_number,
                    "figure_number": figure_number,
                    "figure_image_url": figure_image_url,
                },
            ))

        # Index figure chunks
        result = self._index_chunks(chunks) if chunks else {"indexed": 0}

        logger.info(
            f"Indexed {result.get('indexed', 0)} figure descriptions, "
            f"created {media_assets_created} media assets from {source_file}"
        )

        return {
            "figures_found": len(figures),
            "figures_indexed": result.get("indexed", 0),
            "media_assets_created": media_assets_created,
        }

    def query_for_figure_descriptions(
        self,
        topic: str,
        subject: str,
        n_results: int = 5,
        grade_level: str = "",
        course=None,
        institution_boost: float = 0.7,
    ) -> List[Dict]:
        """Retrieve figure descriptions matching ``topic`` from the institution
        KB AND the global KB, merged by distance.

        Inheritance model (parity with ``query_with_global_fallback``):
        every school inherits the global figure library by default. The
        institution's own indexed figures are **additive** — they layer on
        top and are preferred by ``institution_boost`` (lower distance
        multiplier ⇒ higher rank), but the global library is queried
        unconditionally so a school that has no locally indexed figures
        still sees platform-wide figures for the subject + grade.

        Dedupe key: ``figure_image_url`` (same image re-indexed at both
        tiers collapses to the institution copy).

        Args:
            topic: Topic to search for (e.g., lesson title)
            subject: Subject name for filtering
            n_results: Max results to return after merge
            grade_level: Grade level for filtering (e.g., "S1"). Empty = all.
            course: Optional Course context — when set with subject_code +
                grade_levels, the global tier is restricted to chunks from
                platform-wide courses matching that subject + grade. Same
                semantics as ``query_with_global_fallback(course=...)``.
            institution_boost: Multiplier on institution distances. < 1.0
                prefers institution figures in the post-merge ranking.

        Returns:
            List of dicts with keys: description, figure_type, figure_number,
            image_url, source_file, figure_page, source_tier.
        """
        if not self._storage_available:
            return []

        # Build filter — always require figure_description type + subject
        # Optionally filter by grade level if provided
        where_conditions = [
            {"chunk_type": {"$eq": "figure_description"}},
            {"subject": {"$eq": subject}},
        ]
        if grade_level:
            from apps.curriculum.utils import parse_grade_level_string
            grade_list = parse_grade_level_string(grade_level)
            if grade_list:
                grade_match = grade_list + ([grade_level] if len(grade_list) > 1 else []) + ['']
                where_conditions.append({"grade_level": {"$in": grade_match}})

        base_filter = {"$and": where_conditions}

        from apps.curriculum.kb_storage import query_chunks

        # --- Institution tier ---
        try:
            inst_results = query_chunks(
                institution_id=self.institution_id,
                query_text=topic,
                n_results=n_results,
                where_filter=base_filter,
            )
        except Exception as e:
            logger.warning(f"Figure description query (institution) failed: {e}")
            inst_results = None

        merged: List[Dict] = []
        seen_urls = set()

        def _append(doc: str, meta: Dict, raw_dist: float, tier: str, boost: float):
            url = (meta or {}).get('figure_image_url', '')
            if url and url in seen_urls:
                return
            if url:
                seen_urls.add(url)
            merged.append({
                'description': doc,
                'figure_type': (meta or {}).get('figure_type', ''),
                'figure_number': (meta or {}).get('figure_number', ''),
                'image_url': url,
                'source_file': (meta or {}).get('source_file', ''),
                'figure_page': (meta or {}).get('figure_page', 0),
                'distance': raw_dist * boost,
                'raw_distance': raw_dist,
                'source_tier': tier,
            })

        if inst_results and inst_results.get('documents') and inst_results['documents'][0]:
            docs = inst_results['documents'][0]
            metas = inst_results.get('metadatas', [[]])[0] or [{}] * len(docs)
            dists = inst_results.get('distances', [[]])[0] or [1.0] * len(docs)
            for doc, meta, dist in zip(docs, metas, dists):
                _append(doc, meta, dist, 'institution', institution_boost)

        # --- Global tier (always merged when caller is a school) ---
        # Mirrors query_with_global_fallback semantics: global is the
        # baseline, institution is additive. Skipped only when self IS
        # the global KB.
        if self.institution_id != self.GLOBAL_INSTITUTION_ID:
            try:
                global_filter = self._build_global_filter(base_filter, course)
                global_results = query_chunks(
                    institution_id=self.GLOBAL_INSTITUTION_ID,
                    query_text=topic,
                    n_results=n_results,
                    where_filter=global_filter,
                )
            except Exception as e:
                logger.warning(f"Figure description query (global) failed: {e}")
                global_results = None

            if global_results and global_results.get('documents') and global_results['documents'][0]:
                docs = global_results['documents'][0]
                metas = global_results.get('metadatas', [[]])[0] or [{}] * len(docs)
                dists = global_results.get('distances', [[]])[0] or [1.0] * len(docs)
                for doc, meta, dist in zip(docs, metas, dists):
                    _append(doc, meta, dist, 'global', 1.0)

        merged.sort(key=lambda x: x['distance'])
        final = merged[:n_results]

        # Observability: same shape as query_with_global_fallback's log.
        if self.institution_id != self.GLOBAL_INSTITUTION_ID:
            inst_hits = sum(1 for r in final if r.get('source_tier') == 'institution')
            global_hits = sum(1 for r in final if r.get('source_tier') == 'global')
            if global_hits == 0:
                logger.warning(
                    "[KB figures] inheritance MISS: institution_id=%s subject=%s "
                    "grade=%s — %s institution + 0 global figures. Global figure "
                    "library may be empty for this subject/grade.",
                    self.institution_id, subject, grade_level or '*', inst_hits,
                )
            else:
                logger.info(
                    "[KB figures] inheritance hit: institution_id=%s subject=%s "
                    "— %s institution + %s global (post-merge top-%s)",
                    self.institution_id, subject, inst_hits, global_hits, n_results,
                )

        return final

    # ========================================================================
    # STEP 3: GENERATE LESSONS (Query for structure)
    # ========================================================================
    
    def query_for_lesson_generation(
        self,
        subject: str,
        grade_level: str,
        unit_title: str = None,
        n_results: int = 20
    ) -> QueryResult:
        """
        Query the knowledge base to generate lesson structure.
        
        This is Step 3 of the pipeline: Query DB to find units & lessons.
        
        Args:
            subject: Subject to query
            grade_level: Grade level
            unit_title: Optional specific unit to focus on
            n_results: Number of results to return
        
        Returns:
            QueryResult with relevant curriculum chunks
        """
        if not self._storage_available:
            return QueryResult(
                chunks=[],
                context_summary="Vector database not available",
                teaching_strategies=[],
                objectives=[]
            )
        
        # Build query
        if unit_title:
            query_text = f"{subject} {grade_level} {unit_title} objectives lessons content"
        else:
            query_text = f"{subject} {grade_level} curriculum units objectives"

        # Query with filters and global fallback
        from apps.curriculum.utils import parse_grade_level_string
        grade_list = parse_grade_level_string(grade_level)
        if grade_list:
            # Include individual grades and the full CSV to match both old and new data
            grade_match = grade_list + ([grade_level] if len(grade_list) > 1 else [])
            where_filter = {
                "$and": [
                    {"subject": {"$eq": subject}},
                    {"grade_level": {"$in": grade_match}}
                ]
            }
        else:
            where_filter = {"subject": {"$eq": subject}}

        merged = self.query_with_global_fallback(
            query_text=query_text,
            n_results=n_results,
            where_filter=where_filter,
        )

        return self._process_query_results(self._convert_fallback_to_query_results(merged))

    def query_for_competency_extraction(
        self,
        subject: str,
        grade_level: str,
        unit_title: str = "",
        n_results: int = 30,
    ) -> 'QueryResult':
        """
        Query KB for content relevant to extracting competencies/enabling objectives.

        Returns chunks from curriculum documents, worksheets, and teaching materials
        that can be analyzed by an LLM to produce standardized competency statements.
        This is format-agnostic — works regardless of document structure.
        """
        if not self._storage_available:
            return QueryResult(chunks=[], context_summary="", teaching_strategies=[], objectives=[])

        query_text = (
            f"{subject} {grade_level} {unit_title} "
            f"objectives skills knowledge competencies learning outcomes "
            f"students will be able to understand apply"
        )

        merged = self.query_with_global_fallback(
            query_text=query_text,
            n_results=n_results,
            where_filter={"subject": {"$eq": subject}} if subject else None,
        )

        return self._process_query_results(self._convert_fallback_to_query_results(merged))

    # ========================================================================
    # STEP 4: GENERATE CONTENT (Query for rich context)
    # ========================================================================
    
    def query_for_content_generation(
        self,
        lesson_title: str,
        lesson_objective: str,
        unit_title: str,
        subject: str,
        grade_level: str,
        n_results: int = 10
    ) -> QueryResult:
        """
        Query the knowledge base for content generation context.
        
        This is Step 4 of the pipeline: Query DB for rich context for
        generating tutoring steps and media.
        
        Args:
            lesson_title: Title of the lesson
            lesson_objective: Learning objective
            unit_title: Parent unit title
            subject: Subject name
            grade_level: Grade level
            n_results: Number of results
        
        Returns:
            QueryResult with teaching strategies, objectives, and content
        """
        if not self._storage_available:
            return QueryResult(
                chunks=[],
                context_summary="",
                teaching_strategies=self._default_teaching_strategies(subject),
                objectives=[lesson_objective]
            )
        
        # Query for relevant content with global fallback
        # Filter by subject AND grade level so S1 materials are used for S1 lessons etc.
        query_text = f"{lesson_title} {lesson_objective} {unit_title} teaching strategies methods"

        from apps.curriculum.utils import parse_grade_level_string
        grade_list = parse_grade_level_string(grade_level)
        if grade_list:
            grade_match = grade_list + ([grade_level] if len(grade_list) > 1 else []) + ['']
            where_filter = {
                "$and": [
                    {"subject": {"$eq": subject}},
                    {"grade_level": {"$in": grade_match}},
                ]
            }
        else:
            where_filter = {"subject": {"$eq": subject}}

        merged = self.query_with_global_fallback(
            query_text=query_text,
            n_results=n_results,
            where_filter=where_filter,
        )

        return self._process_query_results(self._convert_fallback_to_query_results(merged))
    
    # ========================================================================
    # STEP 5: TUTORING (Query for live session context)
    # ========================================================================
    
    def query_for_tutoring(
        self,
        lesson,  # Lesson model instance
        student_message: str = None,
        current_topic: str = None,
        n_results: int = 8
    ) -> QueryResult:
        """
        Query the knowledge base during a live tutoring session.
        
        This is Step 5 of the pipeline: Provide rich curriculum context
        to the tutoring engine for aligned instruction.
        
        Args:
            lesson: The Lesson model instance
            student_message: Current student question/response
            current_topic: Current topic being discussed
            n_results: Number of results
        
        Returns:
            QueryResult with relevant teaching strategies and content
        """
        if not self._storage_available:
            return QueryResult(
                chunks=[],
                context_summary=f"Teaching {lesson.title}",
                teaching_strategies=self._default_teaching_strategies(
                    lesson.unit.course.title if hasattr(lesson, 'unit') else "General"
                ),
                objectives=[lesson.objective] if lesson.objective else []
            )

        # Build context-aware query
        query_parts = [lesson.title, lesson.objective or ""]
        
        if current_topic:
            query_parts.append(current_topic)
        
        if student_message:
            # Include student's question for relevant context
            query_parts.append(student_message[:200])
        
        query_text = " ".join(query_parts)
        
        # Get the subject from the lesson's course
        subject = "General"
        if hasattr(lesson, 'unit') and hasattr(lesson.unit, 'course'):
            subject = lesson.unit.course.title.split()[0]  # First word of course title
        
        merged = self.query_with_global_fallback(
            query_text=query_text,
            n_results=n_results,
            where_filter={"subject": {"$eq": subject}},
        )

        return self._process_query_results(self._convert_fallback_to_query_results(merged))
    
    # ========================================================================
    # EXIT TICKET GROUNDING
    # ========================================================================

    def query_for_exit_ticket_generation(
        self,
        lesson_title: str,
        lesson_objective: str,
        subject: str,
        grade_level: str = '',
        n_results: int = 5,
    ) -> List[Dict]:
        """
        Query the KB for real exam questions to ground exit ticket generation.

        Searches for exam_question and marking_scheme chunks that are relevant
        to the lesson topic. Uses two-tier retrieval (institution + global fallback).

        Args:
            lesson_title: Title of the lesson
            lesson_objective: Learning objective
            subject: Subject name
            grade_level: Grade level
            n_results: Number of reference questions to return

        Returns:
            List of dicts with keys: content, metadata, distance, source_tier
            Each dict represents a real exam question with available metadata
            (year, paper_number, question_type, has_answers, etc.)
        """
        if not self._storage_available:
            return []

        query_text = f"{lesson_title} {lesson_objective} exam question assessment"

        # Try filtered query first (only exam questions / marking schemes)
        try:
            merged = self.query_with_global_fallback(
                query_text=query_text,
                n_results=n_results,
                where_filter={
                    "$and": [
                        {"subject": {"$eq": subject}},
                        {"chunk_type": {"$in": ["exam_question", "marking_scheme", "assessment"]}},
                    ]
                },
            )
        except Exception:
            # ChromaDB may fail if no chunks have chunk_type field yet; fall back to unfiltered
            merged = []

        # If filtered query returned too few results, try broader subject-only query
        if len(merged) < 2:
            try:
                broader = self.query_with_global_fallback(
                    query_text=query_text,
                    n_results=n_results,
                    where_filter={"subject": {"$eq": subject}},
                )
                # Only add chunks that look like questions (heuristic)
                for r in broader:
                    if r not in merged:
                        content_lower = r.get("content", "").lower()
                        if any(kw in content_lower for kw in [
                            'question', 'marks', 'answer', 'choose',
                            'calculate', 'explain', 'describe', 'state',
                            'a)', 'b)', 'c)', 'd)',
                        ]):
                            merged.append(r)
                merged = merged[:n_results]
            except Exception:
                pass

        return merged

    def format_exam_questions_for_prompt(self, exam_questions: List[Dict]) -> str:
        """
        Format retrieved exam questions into a prompt-ready string.

        Args:
            exam_questions: Results from query_for_exit_ticket_generation()

        Returns:
            Formatted string for insertion into LLM prompts, or empty string if none.
        """
        if not exam_questions:
            return ""

        lines = ["REFERENCE EXAM QUESTIONS (match this style and difficulty level):"]
        for i, q in enumerate(exam_questions, 1):
            meta = q.get("metadata", {})
            content = q.get("content", "").strip()

            # Build label from available metadata
            label_parts = []
            if meta.get("year"):
                label_parts.append(meta["year"])
            if meta.get("paper_number"):
                label_parts.append(f"Paper {meta['paper_number']}")
            if meta.get("question_type"):
                label_parts.append(meta["question_type"].upper())

            label = f" [{', '.join(label_parts)}]" if label_parts else ""

            # Truncate long questions
            if len(content) > 500:
                content = content[:500] + "..."

            lines.append(f"Q{i}{label}: {content}")

        return "\n".join(lines)

    # ========================================================================
    # TWO-TIER RETRIEVAL
    # ========================================================================

    def query_with_global_fallback(
        self,
        query_text: str,
        n_results: int = 10,
        where_filter: Dict = None,
        institution_boost: float = 0.7,
        course=None,
    ) -> List[Dict]:
        """Two-tier retrieval: global baseline + institution additive.

        Inheritance model (canonical):
        - **Global KB is the baseline** every school inherits unconditionally.
          It is queried on EVERY call regardless of whether the institution
          KB returned hits. There is no threshold gate.
        - **Institution KB is purely additive.** When a school has its own
          indexed materials they layer on top of the global baseline and
          are preferred in the post-merge ranking via ``institution_boost``
          (< 1.0 multiplier on distance ⇒ higher rank). Empty institution
          KB ⇒ caller transparently sees global results only.

        Dedupe: identical content across tiers collapses to the
        institution copy (a school that re-indexes a platform-wide PDF
        locally shouldn't double-count).

        History: a prior version of this method gated the global query
        behind a FALLBACK_THRESHOLD ("only query global when institution
        results are thin"). That regressed inheritance for any school
        with even one indexed material. Removed R2.2 — see
        memory/curriculum_material_sharing_plan.md. The method name is
        retained for back-compat; ``query_with_global_merge`` is the
        canonical alias.

        Args:
            query_text: The search query
            n_results: Total results desired (after merge + sort)
            where_filter: Optional metadata filter (applied to both tiers)
            institution_boost: Multiplier for institution distances (< 1.0 = prefer institution)
            course: Optional Course context. When supplied AND course has
                subject_code + grade_levels set, the global filter is
                tightened to chunks belonging to platform-wide courses
                with the SAME subject_code + overlapping grade_levels —
                a robust replacement for free-text subject string matching.

        Returns:
            List of dicts with keys: content, metadata, distance, source_tier
        """
        if not self._storage_available:
            return []

        from apps.curriculum.kb_storage import query_chunks

        # --- Query institution KB ---
        try:
            inst_results = query_chunks(
                institution_id=self.institution_id,
                query_text=query_text,
                n_results=n_results,
                where_filter=where_filter,
            )
        except Exception as e:
            logger.warning(f"Institution KB query failed: {e}")
            inst_results = None

        merged = []
        if inst_results and inst_results.get('documents') and inst_results['documents'][0]:
            for i, doc in enumerate(inst_results['documents'][0]):
                raw_dist = inst_results['distances'][0][i] if inst_results.get('distances') else 1.0
                merged.append({
                    "content": doc,
                    "metadata": inst_results['metadatas'][0][i] if inst_results.get('metadatas') else {},
                    "distance": raw_dist * institution_boost,
                    "raw_distance": raw_dist,
                    "source_tier": "institution",
                })

        # --- Global tier (baseline — queried on every call) ---
        # Global is the default knowledge surface every school inherits.
        # Institution results above are additive, not a replacement.
        # Skipped only when the caller IS the global KB (would double-count
        # the same chunks). No threshold gate — the previous FALLBACK_THRESHOLD
        # behavior was reverted because it broke inheritance the moment a
        # school had even one indexed material.
        if self.institution_id != self.GLOBAL_INSTITUTION_ID:
            try:
                # Build the global filter. Preferred path (when caller
                # passed a Course with subject_code): look up matching
                # platform-wide upload_ids and restrict the global query
                # to chunks from those uploads. Fallback path: relax to
                # subject-string filter (the legacy behavior).
                global_filter = self._build_global_filter(where_filter, course)

                global_results = query_chunks(
                    institution_id=self.GLOBAL_INSTITUTION_ID,
                    query_text=query_text,
                    n_results=n_results,
                    where_filter=global_filter,
                )

                if global_results and global_results.get('documents') and global_results['documents'][0]:
                    # Dedupe by content hash — institution and global may
                    # carry the same uploaded material if a school re-
                    # indexed it locally. Prefer the institution copy.
                    seen_content = {row['content'] for row in merged}
                    for i, doc in enumerate(global_results['documents'][0]):
                        if doc in seen_content:
                            continue
                        raw_dist = global_results['distances'][0][i] if global_results.get('distances') else 1.0
                        merged.append({
                            "content": doc,
                            "metadata": global_results['metadatas'][0][i] if global_results.get('metadatas') else {},
                            "distance": raw_dist,  # No boost for global (natural distance)
                            "raw_distance": raw_dist,
                            "source_tier": "global",
                        })
                        seen_content.add(doc)
            except Exception as e:
                logger.warning(f"Global KB merge query failed: {e}")

        # Sort by adjusted distance (lower = more relevant), take top N
        merged.sort(key=lambda x: x["distance"])
        final = merged[:n_results]

        # Observable inheritance: every query logs the global/institution
        # split so a "global baseline silently empty" regression is visible
        # in containerapp logs immediately. Critical because the upstream
        # default of routing "All Schools" uploads to id=12 (vs the KB's
        # canonical 0) caused exactly this silent failure in prod.
        inst_hits = sum(1 for r in final if r.get('source_tier') == 'institution')
        global_hits = sum(1 for r in final if r.get('source_tier') == 'global')
        if self.institution_id != self.GLOBAL_INSTITUTION_ID:
            if global_hits == 0:
                # The baseline (global) returned 0 — every school query
                # should see global by default, so this is a real signal.
                logger.warning(
                    "[KB] global-baseline MISS: institution_id=%s returned %s "
                    "institution hits + 0 global. Global KB may be empty "
                    "for this subject/grade — verify 'All Schools' uploads "
                    "have been indexed and are filterable by this where.",
                    self.institution_id, inst_hits,
                )
            else:
                logger.info(
                    "[KB] inheritance OK: institution_id=%s — %s institution "
                    "+ %s global (post-merge top-%s; global baseline present)",
                    self.institution_id, inst_hits, global_hits, n_results,
                )
        return final

    # Alias under the new name so future callers can use the more accurate
    # `query_with_global_merge` without changing every existing caller.
    def query_with_global_merge(self, *args, **kwargs):
        return self.query_with_global_fallback(*args, **kwargs)

    def _build_global_filter(self, where_filter: Dict, course=None) -> Optional[Dict]:
        """Build the where filter for the global KB query.

        Preferred path: when ``course`` has subject_code + grade_levels set,
        find all platform-wide upload_ids belonging to courses that match
        (subject_code, grade_levels overlap). Restrict the global query to
        chunks from those uploads. This gives a robust subject_code-based
        match instead of fuzzy free-text subject matching.

        Fallback path: relax to the subject-string filter (legacy behavior),
        which still works when courses don't have subject_code populated.
        """
        # Try the canonical match first
        if course is not None:
            upload_ids = self._global_upload_ids_matching_course(course)
            if upload_ids:
                return {"upload_id": {"$in": list(upload_ids)}}

        # Legacy fallback: extract subject filter from the supplied filter
        if where_filter and isinstance(where_filter, dict):
            if "subject" in where_filter:
                return where_filter
            if "$and" in where_filter:
                for clause in where_filter["$and"]:
                    if isinstance(clause, dict) and "subject" in clause:
                        return clause
        return None

    def _global_upload_ids_matching_course(self, course) -> set:
        """Return the set of TeachingMaterialUpload IDs from platform-wide
        courses whose subject_code matches AND whose grade_levels overlap
        with the supplied course. Empty set when course has no
        subject_code, or no matching platform-wide course exists.
        """
        try:
            subject_code = getattr(course, 'subject_code', '') or ''
            course_grades = set(getattr(course, 'grade_levels', None) or [])
            if not subject_code:
                return set()
        except Exception:
            return set()

        from apps.curriculum.models import Course as CourseModel
        from apps.dashboard.models import TeachingMaterialUpload

        # Platform-wide courses (institution=None) with same subject_code.
        global_courses = CourseModel.objects.filter(
            institution__isnull=True,
            subject_code=subject_code,
        ).only('id', 'grade_levels')

        # Filter by grade_level overlap (any-of). When the school course
        # has no grade_levels set, accept all global courses with the
        # same subject_code (don't over-constrain).
        matching_course_ids = []
        for gc in global_courses:
            gc_grades = set(gc.grade_levels or [])
            if not course_grades or not gc_grades or (course_grades & gc_grades):
                matching_course_ids.append(gc.id)

        if not matching_course_ids:
            return set()

        upload_ids = set(TeachingMaterialUpload.objects.filter(
            course_id__in=matching_course_ids,
        ).values_list('id', flat=True))
        return upload_ids

    def _convert_fallback_to_query_results(self, merged: List[Dict]) -> Dict:
        """Convert query_with_global_fallback() output to ChromaDB query() format
        so it can be passed to _process_query_results()."""
        if not merged:
            return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

        return {
            "documents": [[r["content"] for r in merged]],
            "metadatas": [[r["metadata"] for r in merged]],
            "distances": [[r["distance"] for r in merged]],
        }

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    def _process_query_results(self, results: Dict) -> QueryResult:
        """Process ChromaDB query results into a QueryResult."""
        chunks = []
        teaching_strategies = []
        objectives = []
        
        if results and results.get('documents') and results['documents'][0]:
            for i, doc in enumerate(results['documents'][0]):
                metadata = results['metadatas'][0][i] if results.get('metadatas') else {}
                
                chunks.append({
                    "content": doc,
                    "section": metadata.get('section', ''),
                    "chunk_type": metadata.get('chunk_type', ''),
                    "source": metadata.get('source_file', ''),
                })
                
                # Extract teaching strategies
                if metadata.get('chunk_type') == 'teaching_strategy':
                    strategies = self._extract_strategies_from_text(doc)
                    teaching_strategies.extend(strategies)
                
                # Extract objectives
                if metadata.get('chunk_type') == 'objective':
                    objs = self._extract_objectives_from_text(doc)
                    objectives.extend(objs)
        
        # Build context summary
        context_summary = self._build_context_summary(chunks)
        
        # Remove duplicates
        teaching_strategies = list(dict.fromkeys(teaching_strategies))[:10]
        objectives = list(dict.fromkeys(objectives))[:15]
        
        return QueryResult(
            chunks=chunks,
            context_summary=context_summary,
            teaching_strategies=teaching_strategies,
            objectives=objectives
        )
    
    def _extract_strategies_from_text(self, text: str) -> List[str]:
        """Extract teaching strategies from text."""
        strategies = []
        
        # Look for bullet points
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith(('-', '•', '*')):
                strategy = line.lstrip('-•* ').strip()
                if 10 < len(strategy) < 100:
                    strategies.append(strategy)
        
        return strategies[:5]
    
    def _extract_objectives_from_text(self, text: str) -> List[str]:
        """Extract learning objectives from text."""
        objectives = []
        
        # Look for objective patterns
        patterns = [
            r'(?:students? will|learners? will|be able to)\s+(.+?)(?:\.|$)',
            r'(?:understand|explain|describe|identify|analyze)\s+(.+?)(?:\.|$)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            objectives.extend(matches[:5])
        
        return objectives
    
    def _build_context_summary(self, chunks: List[Dict]) -> str:
        """Build a summary of the retrieved context."""
        if not chunks:
            return ""
        
        sections = set(c.get('section', '') for c in chunks if c.get('section'))
        types = set(c.get('chunk_type', '') for c in chunks if c.get('chunk_type'))
        
        summary_parts = []
        if sections:
            summary_parts.append(f"Sections: {', '.join(list(sections)[:5])}")
        if types:
            summary_parts.append(f"Content types: {', '.join(types)}")
        
        return "; ".join(summary_parts) if summary_parts else "General curriculum content"
    
    def _default_teaching_strategies(self, subject: str) -> List[str]:
        """Return default teaching strategies when KB is not available."""
        strategies = {
            "Mathematics": [
                "Work through examples step-by-step",
                "Use visual representations and diagrams",
                "Practice with graduated difficulty",
                "Connect to real-world applications",
                "Encourage mental math strategies"
            ],
            "Geography": [
                "Use maps and visual aids",
                "Connect to local Seychelles context",
                "Field observation activities",
                "Compare and contrast regions",
                "Analyze geographic data"
            ],
            "Science": [
                "Hands-on experiments",
                "Scientific method approach",
                "Visual models and diagrams",
                "Real-world examples",
                "Inquiry-based learning"
            ]
        }
        
        return strategies.get(subject, [
            "Direct instruction with examples",
            "Guided practice",
            "Independent practice",
            "Discussion and questioning",
            "Visual aids and demonstrations"
        ])
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    def get_collection_stats(self) -> Dict:
        """Get statistics about the indexed curriculum."""
        from apps.curriculum.kb_storage import collection_stats
        return collection_stats(self.institution_id)

    def clear_collection(self):
        """Clear all indexed content for this institution."""
        if not self._storage_available:
            return
        from apps.curriculum.kb_storage import clear_institution
        clear_institution(self.institution_id)
        logger.info(f"Cleared chunks for institution_id={self.institution_id}")

    def search(self, query: str, n_results: int = 5, filters: Dict = None) -> List[Dict]:
        """Simple semantic search across the curriculum.

        Args:
            query: Search query
            n_results: Number of results
            filters: Optional metadata filters

        Returns:
            List of matching chunks
        """
        if not self._storage_available:
            return []

        from apps.curriculum.kb_storage import query_chunks
        results = query_chunks(
            institution_id=self.institution_id,
            query_text=query,
            n_results=n_results,
            where_filter=filters,
        )

        output = []
        if results and results.get('documents') and results['documents'][0]:
            for i, doc in enumerate(results['documents'][0]):
                output.append({
                    "content": doc,
                    "metadata": results['metadatas'][0][i] if results.get('metadatas') else {},
                    "distance": results['distances'][0][i] if results.get('distances') else None,
                })

        return output


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def get_knowledge_base(institution_id: int) -> CurriculumKnowledgeBase:
    """Get or create a knowledge base for an institution."""
    return CurriculumKnowledgeBase(institution_id=institution_id)


def index_curriculum_for_institution(
    institution_id: int,
    file_path: str,
    subject: str,
    grade_level: str
) -> Dict:
    """
    Convenience function to index a curriculum document.
    
    Usage:
        result = index_curriculum_for_institution(
            institution_id=1,
            file_path="/path/to/curriculum.pdf",
            subject="Mathematics",
            grade_level="S1"
        )
    """
    kb = get_knowledge_base(institution_id)
    return kb.index_curriculum_document(
        file_path=file_path,
        subject=subject,
        grade_level=grade_level
    )