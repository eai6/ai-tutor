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
    """
    
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

        # Chunk the text (reuse curriculum chunking)
        chunks = self._chunk_curriculum_text(
            text=text,
            subject=subject,
            grade_level=grade_level,
            source_file=os.path.basename(file_path),
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
        
        collection = self._get_collection()
        
        # Build query
        if unit_title:
            query_text = f"{subject} {grade_level} {unit_title} objectives lessons content"
        else:
            query_text = f"{subject} {grade_level} curriculum units objectives"
        
        # Query with filters
        where_filter = {
            "$and": [
                {"subject": {"$eq": subject}},
                {"grade_level": {"$eq": grade_level}}
            ]
        }
        
        results = collection.query(
            query_texts=[query_text],
            n_results=n_results,
            where=where_filter,
            include=["documents", "metadatas"]
        )
        
        return self._process_query_results(results)
    
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
        
        collection = self._get_collection()
        
        # Query for relevant content
        query_text = f"{lesson_title} {lesson_objective} {unit_title} teaching strategies methods"
        
        results = collection.query(
            query_texts=[query_text],
            n_results=n_results,
            where={"subject": {"$eq": subject}},
            include=["documents", "metadatas"]
        )
        
        return self._process_query_results(results)
    
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
        
        collection = self._get_collection()
        
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
        
        results = collection.query(
            query_texts=[query_text],
            n_results=n_results,
            where={"subject": {"$eq": subject}},
            include=["documents", "metadatas"]
        )
        
        return self._process_query_results(results)
    
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