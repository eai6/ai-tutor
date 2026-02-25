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

    GLOBAL_INSTITUTION_ID = 0
    # Minimum number of results from institution KB before we skip global fallback
    FALLBACK_THRESHOLD = 3

    @classmethod
    def get_global_kb(cls):
        """Get the global/platform-level knowledge base (OpenStax, shared resources)."""
        return cls(institution_id=cls.GLOBAL_INSTITUTION_ID)

    def __init__(self, institution_id: int, persist_directory: str = None):
        """
        Initialize the knowledge base.
        
        Args:
            institution_id: ID of the institution (for data isolation)
            persist_directory: Where to store the vector DB (default: MEDIA_ROOT/vectordb/)
        """
        self.institution_id = institution_id
        
        # Set up persistence directory
        if persist_directory is None:
            from django.conf import settings
            persist_directory = os.path.join(
                getattr(settings, 'MEDIA_ROOT', '/tmp'),
                'vectordb',
                f'institution_{institution_id}'
            )
        
        self.persist_directory = persist_directory
        os.makedirs(persist_directory, exist_ok=True)
        
        # Initialize ChromaDB
        self._init_chromadb()
        
        # Collection name for this institution
        self.collection_name = f"curriculum_{institution_id}"
    
    def _init_chromadb(self):
        """Initialize ChromaDB client and embedding function."""
        try:
            import chromadb
            
            # Use the new persistent client API
            self.chroma_client = chromadb.PersistentClient(
                path=self.persist_directory
            )
            
            # Use sentence-transformers for embeddings
            from chromadb.utils import embedding_functions
            self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"  # Fast, good quality
            )
            
            self._chromadb_available = True
            logger.info(f"ChromaDB initialized at {self.persist_directory}")
            
        except ImportError as e:
            logger.warning(f"ChromaDB not available: {e}. Install with: pip install chromadb sentence-transformers")
            self._chromadb_available = False
            self.chroma_client = None
            self.embedding_fn = None
    
    def _get_collection(self):
        """Get or create the curriculum collection."""
        if not self._chromadb_available:
            return None
        
        return self.chroma_client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=self.embedding_fn,
            metadata={"institution_id": self.institution_id}
        )
    
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
        
        return {
            "success": True,
            "file_path": file_path,
            "text_length": len(text),
            "chunks_created": len(chunks),
            "chunks_indexed": result.get("indexed", 0),
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
        
        def create_chunk(content: str, section: str, chunk_type: str) -> CurriculumChunk:
            """Create a chunk with metadata."""
            content = content.strip()
            if not content or len(content) < 20:
                return None
            
            chunk_id = hashlib.md5(
                f"{source_file}:{section}:{content[:100]}".encode()
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
        """Index chunks into the vector database."""
        if not self._chromadb_available:
            logger.warning("ChromaDB not available, skipping indexing")
            return {"indexed": 0, "error": "ChromaDB not installed"}
        
        collection = self._get_collection()
        
        # Prepare data for ChromaDB
        ids = [chunk.id for chunk in chunks]
        documents = [chunk.content for chunk in chunks]
        metadatas = [chunk.metadata for chunk in chunks]
        
        # Add to collection (upsert to handle duplicates)
        collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas
        )
        
        # PersistentClient auto-persists, no need to call persist()
        
        logger.info(f"Indexed {len(chunks)} chunks into ChromaDB")
        return {"indexed": len(chunks)}
    
    def index_teaching_material(
        self,
        file_path: str,
        subject: str,
        grade_level: str,
        material_title: str,
        material_type: str = 'textbook',
        upload_id: int = None
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

        Returns:
            Dict with indexing statistics
        """
        from apps.curriculum.curriculum_parser import extract_text_from_file

        logger.info(f"Parsing teaching material: {file_path}")
        text, file_type = extract_text_from_file(file_path)

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

        return {
            "success": True,
            "file_path": file_path,
            "text_length": len(text),
            "chunks_created": len(chunks),
            "chunks_indexed": result.get("indexed", 0),
        }

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
        if not self._chromadb_available:
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
        where_filter = {
            "$and": [
                {"subject": {"$eq": subject}},
                {"grade_level": {"$eq": grade_level}}
            ]
        }

        merged = self.query_with_global_fallback(
            query_text=query_text,
            n_results=n_results,
            where_filter=where_filter,
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
        if not self._chromadb_available:
            return QueryResult(
                chunks=[],
                context_summary="",
                teaching_strategies=self._default_teaching_strategies(subject),
                objectives=[lesson_objective]
            )
        
        # Query for relevant content with global fallback
        query_text = f"{lesson_title} {lesson_objective} {unit_title} teaching strategies methods"

        merged = self.query_with_global_fallback(
            query_text=query_text,
            n_results=n_results,
            where_filter={"subject": {"$eq": subject}},
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
        if not self._chromadb_available:
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
        if not self._chromadb_available:
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
    ) -> List[Dict]:
        """
        Query institution KB first, then fall back to global KB if results are insufficient.

        Uses lazy evaluation: global KB is only queried when institution KB returns
        fewer than FALLBACK_THRESHOLD results. This means zero overhead in the common case.

        Institution results get a distance boost (multiplied by institution_boost < 1.0,
        so lower distance = higher relevance) to prefer local content over global.

        Args:
            query_text: The search query
            n_results: Total results desired
            where_filter: Optional metadata filter (applied to both tiers)
            institution_boost: Multiplier for institution distances (< 1.0 = prefer institution)

        Returns:
            List of dicts with keys: content, metadata, distance, source_tier
        """
        if not self._chromadb_available:
            return []

        # --- Query institution KB ---
        collection = self._get_collection()
        try:
            inst_results = collection.query(
                query_texts=[query_text],
                n_results=n_results,
                where=where_filter,
                include=["documents", "metadatas", "distances"]
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

        # --- Lazy fallback to global KB ---
        if (self.institution_id != self.GLOBAL_INSTITUTION_ID
                and len(merged) < self.FALLBACK_THRESHOLD):
            try:
                global_kb = CurriculumKnowledgeBase(institution_id=self.GLOBAL_INSTITUTION_ID)
                global_collection = global_kb._get_collection()

                # For global, relax institution-specific filters but keep subject filter
                global_filter = None
                if where_filter:
                    # Extract just the subject filter if present
                    if isinstance(where_filter, dict):
                        if "subject" in where_filter:
                            global_filter = where_filter
                        elif "$and" in where_filter:
                            for clause in where_filter["$and"]:
                                if "subject" in clause:
                                    global_filter = clause
                                    break

                global_results = global_collection.query(
                    query_texts=[query_text],
                    n_results=n_results,
                    where=global_filter,
                    include=["documents", "metadatas", "distances"]
                )

                if global_results and global_results.get('documents') and global_results['documents'][0]:
                    for i, doc in enumerate(global_results['documents'][0]):
                        raw_dist = global_results['distances'][0][i] if global_results.get('distances') else 1.0
                        merged.append({
                            "content": doc,
                            "metadata": global_results['metadatas'][0][i] if global_results.get('metadatas') else {},
                            "distance": raw_dist,  # No boost for global (natural distance)
                            "raw_distance": raw_dist,
                            "source_tier": "global",
                        })
            except Exception as e:
                logger.warning(f"Global KB fallback query failed: {e}")

        # Sort by adjusted distance (lower = more relevant), take top N
        merged.sort(key=lambda x: x["distance"])
        return merged[:n_results]

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
        if not self._chromadb_available:
            return {"error": "ChromaDB not available"}
        
        collection = self._get_collection()
        count = collection.count()
        
        return {
            "collection_name": self.collection_name,
            "total_chunks": count,
            "persist_directory": self.persist_directory,
        }
    
    def clear_collection(self):
        """Clear all indexed content for this institution."""
        if not self._chromadb_available:
            return
        
        self.chroma_client.delete_collection(self.collection_name)
        logger.info(f"Cleared collection: {self.collection_name}")
    
    def search(self, query: str, n_results: int = 5, filters: Dict = None) -> List[Dict]:
        """
        Simple semantic search across the curriculum.
        
        Args:
            query: Search query
            n_results: Number of results
            filters: Optional metadata filters
        
        Returns:
            List of matching chunks
        """
        if not self._chromadb_available:
            return []
        
        collection = self._get_collection()
        
        results = collection.query(
            query_texts=[query],
            n_results=n_results,
            where=filters,
            include=["documents", "metadatas", "distances"]
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