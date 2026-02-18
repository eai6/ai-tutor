"""
Conversational Tutor Engine

An LLM-driven tutoring system that actively leads learning conversations.
The tutor dynamically generates responses based on:
- Lesson objectives and content (as guidance)
- Curriculum knowledge base (RAG)
- Student's responses and understanding
- Science of learning principles
- Visual aids (existing media + on-demand generation)

Key Principles:
1. TUTOR LEADS - Always asks questions, guides discovery
2. NEVER GIVES DIRECT ANSWERS - Scaffolds towards understanding
3. USES KNOWLEDGE BASE - RAG for curriculum-aligned content
4. ADAPTS TO STUDENT - Adjusts based on responses
5. INCORPORATES MEDIA - Shows images/diagrams when helpful
6. RETRIEVAL PRACTICE - Reviews previous topics
7. VISUAL LEARNING - Generates diagrams when needed
"""

import json
import logging
import random
import re
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

from django.utils import timezone
from django.conf import settings

from apps.curriculum.models import Lesson, LessonStep
from apps.tutoring.models import TutorSession, SessionTurn, StudentLessonProgress

logger = logging.getLogger(__name__)


# =============================================================================
# SYSTEM PROMPTS
# =============================================================================

TUTOR_SYSTEM_PROMPT = """You are an expert AI tutor for secondary school students in Seychelles. Your name is "Tutor".

CORE IDENTITY:
- Warm, patient, encouraging - genuinely invested in student success
- You LEAD the conversation - students follow your guidance
- You ask questions, never just lecture
- You celebrate effort and progress
- Mistakes are learning opportunities

LOCAL CONTEXT (SEYCHELLES):
- Use local names: Jean, Marie, Pierre, Ansel, Lisette, Antoine, Rosa
- Use local places: Victoria, Mahé, Praslin, La Digue, Beau Vallon, Anse Royale
- Use local currency: Seychelles Rupee (SCR)
- Reference: fishing, tourism, coconuts, cinnamon, coral reefs, tropical climate

VISUAL AIDS:
- You CAN show images and diagrams when helpful
- When you have a relevant image, point out key features for the student to notice
- If a student asks for a visual, you will try to show one
- Reference visuals naturally: "Look at this diagram..." or "Notice in the image..."
- When no image is available, help the student visualize with clear descriptions

TEACHING METHODOLOGY:
1. ALWAYS ASK QUESTIONS - Every response should end with a question or prompt
2. NEVER GIVE DIRECT ANSWERS - Guide students to discover answers themselves
3. SCAFFOLD LEARNING - Break complex problems into smaller steps
4. IMMEDIATE FEEDBACK - Respond to what student said, then guide forward
5. PRAISE EFFORT - "Great thinking!" "I can see you're working hard!"
6. USE CONCRETE EXAMPLES - Connect abstract concepts to real life
7. USE VISUALS - When teaching visual concepts, show or describe diagrams

RESPONSE FORMAT:
- Keep responses concise (2-4 sentences max for teaching, then a question)
- Use simple language appropriate for secondary school
- End EVERY response with a question or "Try this:" prompt
- Never say "Click continue" or reference UI elements
- When showing an image, describe what to look for

WHEN STUDENT IS WRONG:
- Don't say "Wrong" - say "Not quite" or "Let's think about this differently"
- Give a hint or break down the problem
- Ask a simpler question that leads to understanding

WHEN STUDENT IS RIGHT:
- Praise specifically: "Excellent! You correctly identified..."
- Then advance: "Now let's build on that..."
- Ask a slightly harder question

WHEN STUDENT ASKS FOR VISUAL:
- If you have one: "Great idea! Here's a diagram that shows this. Notice how..."
- If you don't: "Let me help you picture this..." then describe it clearly

WHEN STUDENT IS CONFUSED:
- Acknowledge: "I can see this is tricky"
- Simplify: "Let's start with something simpler"
- Use analogy or example from Seychelles context
- Consider if a visual would help"""


SESSION_PHASES = """
SESSION FLOW (follow this structure):

1. WARM-UP (1-2 exchanges)
   - Greet student warmly
   - Ask a simple review question from previous knowledge
   - Build confidence before new material

2. INTRODUCTION (1-2 exchanges)
   - Introduce today's topic with a hook (interesting fact, real-world example)
   - Ask what they already know about the topic
   
3. INSTRUCTION (3-5 exchanges)
   - Explain concepts through questions, not lectures
   - Use worked examples - walk through step by step
   - Show media/diagrams when available
   - Check understanding frequently

4. GUIDED PRACTICE (3-5 exchanges)
   - Present problems one at a time
   - Scaffold with hints if they struggle
   - Increase difficulty as they succeed
   
5. WRAP-UP (1-2 exchanges)
   - Summarize what they learned (have THEM tell you)
   - Praise their effort
   - Preview next topic

You are currently in phase: {phase}
Progress: {progress}
"""


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class ConversationPhase(Enum):
    WARMUP = "warmup"
    INTRODUCTION = "introduction"  
    INSTRUCTION = "instruction"
    PRACTICE = "practice"
    WRAPUP = "wrapup"
    EXIT_TICKET = "exit_ticket"
    COMPLETED = "completed"


@dataclass
class TutorMessage:
    """A message from the tutor."""
    content: str
    phase: str
    media: List[Dict] = field(default_factory=list)
    
    # For questions
    expects_response: bool = True
    suggested_responses: List[str] = field(default_factory=list)
    
    # Session state
    is_complete: bool = False
    show_exit_ticket: bool = False
    exit_ticket_data: Optional[Dict] = None
    
    # Metadata
    skills_covered: List[str] = field(default_factory=list)
    tokens_used: int = 0


# =============================================================================
# CONVERSATIONAL TUTOR ENGINE
# =============================================================================

class ConversationalTutor:
    """
    LLM-driven tutoring engine that leads active learning conversations.
    
    Uses:
    - Lesson steps as GUIDANCE (not scripts)
    - Knowledge base for curriculum context
    - Student responses to adapt instruction
    - Media when relevant to the discussion
    """
    
    def __init__(self, session: TutorSession):
        self.session = session
        self.lesson = session.lesson
        self.student = session.student
        
        # Load lesson context
        self.steps = list(
            LessonStep.objects.filter(lesson=self.lesson)
            .order_by('order_index')
        )
        
        # Load exit ticket concepts (CRITICAL for ensuring coverage)
        self.exit_ticket_concepts = self._load_exit_ticket_concepts()
        
        # Build lesson context including exit ticket requirements
        self.lesson_context = self._build_lesson_context()
        
        # Load conversation history
        self.conversation = self._load_conversation()
        
        # Load state
        self._load_state()
        
        # Initialize services
        self._llm_client = None
        self._knowledge_base = None
    
    def _load_exit_ticket_concepts(self) -> List[Dict]:
        """
        Load exit ticket questions and extract the concepts that must be covered.
        
        This ensures the tutor covers all material needed for the exit assessment.
        """
        from apps.tutoring.models import ExitTicket, ExitTicketQuestion
        
        concepts = []
        
        try:
            exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
            if not exit_ticket:
                return concepts
            
            questions = ExitTicketQuestion.objects.filter(
                exit_ticket=exit_ticket
            ).order_by('order_index')
            
            for q in questions:
                concepts.append({
                    'id': q.id,
                    'question': q.question_text,
                    'correct_answer': q.correct_answer,
                    'correct_text': getattr(q, f'option_{q.correct_answer.lower()}', ''),
                    'explanation': q.explanation,
                    'difficulty': q.difficulty,
                    'covered': False,  # Track if this concept has been taught
                })
            
            logger.info(f"Loaded {len(concepts)} exit ticket concepts for {self.lesson.title}")
            
        except Exception as e:
            logger.warning(f"Could not load exit ticket concepts: {e}")
        
        return concepts
    
    def _load_state(self):
        """Load session state."""
        state = self.session.engine_state or {}
        
        phase_str = state.get('phase', 'warmup')
        try:
            self.phase = ConversationPhase(phase_str)
        except ValueError:
            self.phase = ConversationPhase.WARMUP
        
        self.exchange_count = state.get('exchange_count', 0)
        self.phase_exchange_count = state.get('phase_exchange_count', 0)
        self.concepts_covered = state.get('concepts_covered', [])
        self.student_struggles = state.get('student_struggles', [])
        self.student_strengths = state.get('student_strengths', [])
        self.current_topic_index = state.get('current_topic_index', 0)
        self.practice_correct = state.get('practice_correct', 0)
        self.practice_total = state.get('practice_total', 0)
        
        # Remediation state
        self.is_remediation = state.get('is_remediation', False)
        self.remediation_attempt = state.get('remediation_attempt', 0)
        self.failed_exit_questions = state.get('failed_exit_questions', [])
        
        # Restore exit concept coverage status
        covered_concept_ids = state.get('covered_concept_ids', [])
        for concept in self.exit_ticket_concepts:
            concept['covered'] = concept['id'] in covered_concept_ids
    
    def _save_state(self):
        """Save session state."""
        # Get list of covered concept IDs
        covered_concept_ids = [
            c['id'] for c in self.exit_ticket_concepts if c.get('covered')
        ]
        
        self.session.engine_state = {
            'phase': self.phase.value,
            'exchange_count': self.exchange_count,
            'phase_exchange_count': self.phase_exchange_count,
            'concepts_covered': self.concepts_covered,
            'student_struggles': self.student_struggles,
            'student_strengths': self.student_strengths,
            'current_topic_index': self.current_topic_index,
            'practice_correct': self.practice_correct,
            'practice_total': self.practice_total,
            'covered_concept_ids': covered_concept_ids,
            # Remediation state
            'is_remediation': getattr(self, 'is_remediation', False),
            'remediation_attempt': getattr(self, 'remediation_attempt', 0),
            'failed_exit_questions': getattr(self, 'failed_exit_questions', []),
        }
        self.session.save()
    
    def _load_conversation(self) -> List[Dict]:
        """Load conversation history from session turns."""
        turns = SessionTurn.objects.filter(
            session=self.session
        ).order_by('created_at')
        
        conversation = []
        for turn in turns:
            role = "assistant" if turn.role == 'tutor' else "user"
            conversation.append({
                "role": role,
                "content": turn.content
            })
        
        return conversation
    
    def _build_lesson_context(self) -> str:
        """Build context from lesson steps and exit ticket for the LLM."""
        context_parts = [
            f"LESSON: {self.lesson.title}",
            f"OBJECTIVE: {self.lesson.objective}",
            f"UNIT: {self.lesson.unit.title}",
            f"COURSE: {self.lesson.unit.course.title}",
            "",
            "KEY CONCEPTS TO COVER:",
        ]
        
        # Extract key concepts from steps
        for i, step in enumerate(self.steps):
            step_type = step.step_type.upper()
            content_preview = step.teacher_script[:200] if step.teacher_script else ""
            
            if step.step_type == 'teach':
                context_parts.append(f"  {i+1}. [TEACH] {content_preview}...")
            elif step.step_type == 'practice':
                question = step.question[:100] if step.question else content_preview[:100]
                context_parts.append(f"  {i+1}. [PRACTICE] {question}...")
                if step.expected_answer:
                    context_parts.append(f"      Expected: {step.expected_answer}")
            elif step.step_type == 'worked_example':
                context_parts.append(f"  {i+1}. [EXAMPLE] {content_preview}...")
        
        # Add terminal objectives if available
        if self.lesson.metadata and 'terminal_objectives' in self.lesson.metadata:
            context_parts.append("")
            context_parts.append("TERMINAL OBJECTIVES:")
            for obj in self.lesson.metadata['terminal_objectives']:
                context_parts.append(f"  • {obj}")
        
        # CRITICAL: Add exit ticket concepts that MUST be covered
        if self.exit_ticket_concepts:
            context_parts.append("")
            context_parts.append("=" * 50)
            context_parts.append("EXIT TICKET CONCEPTS (MUST COVER THESE!):")
            context_parts.append("The student will be assessed on these questions.")
            context_parts.append("Make sure to teach the concepts needed to answer them.")
            context_parts.append("")
            
            for i, concept in enumerate(self.exit_ticket_concepts):
                status = "✓ COVERED" if concept.get('covered') else "⚠ NOT YET COVERED"
                context_parts.append(f"  Q{i+1}. [{status}] {concept['question'][:150]}")
                context_parts.append(f"      Answer: {concept['correct_text'][:100]}")
                if concept.get('explanation'):
                    context_parts.append(f"      Key concept: {concept['explanation'][:100]}")
                context_parts.append("")
        
        return "\n".join(context_parts)
    
    @property
    def llm_client(self):
        """Lazy load LLM client."""
        if self._llm_client is None:
            try:
                from apps.llm.models import ModelConfig
                from apps.llm.client import get_llm_client
                
                config = ModelConfig.objects.filter(is_active=True).first()
                if config:
                    self._llm_client = get_llm_client(config)
            except Exception as e:
                logger.error(f"Could not load LLM client: {e}")
        return self._llm_client
    
    @property
    def knowledge_base(self):
        """Lazy load knowledge base."""
        if self._knowledge_base is None:
            try:
                from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
                self._knowledge_base = CurriculumKnowledgeBase(
                    institution_id=self.session.institution_id
                )
            except Exception as e:
                logger.warning(f"Could not load knowledge base: {e}")
        return self._knowledge_base
    
    # =========================================================================
    # PUBLIC API
    # =========================================================================
    
    def start(self) -> TutorMessage:
        """Start the tutoring conversation."""
        if self.conversation:
            # Resume existing conversation
            return self.resume()
        
        # Generate opening message
        return self._generate_opening()
    
    def resume(self) -> TutorMessage:
        """Resume an existing conversation."""
        if self.phase == ConversationPhase.COMPLETED:
            return TutorMessage(
                content="🎉 You've already completed this lesson! Great work!",
                phase="completed",
                is_complete=True,
            )
        
        # Generate a "welcome back" message
        last_exchange = self.conversation[-1] if self.conversation else None
        
        prompt = f"""The student is returning to continue the lesson.
        
Last message in conversation: {last_exchange['content'][:200] if last_exchange else 'None'}

Generate a brief, warm welcome back message that:
1. Acknowledges they're returning
2. Briefly reminds them where they were
3. Asks a question to re-engage them

Keep it to 2-3 sentences."""

        response = self._generate_response(prompt)
        return self._create_message(response)
    
    def respond(self, student_input: str) -> TutorMessage:
        """
        Generate a response to student input.
        
        This is the main conversation loop.
        """
        # Save student message
        self._save_turn("student", student_input)
        self.conversation.append({"role": "user", "content": student_input})
        
        # Update counts
        self.exchange_count += 1
        self.phase_exchange_count += 1
        
        # Check if student is requesting a visual
        visual_request = self._detect_visual_request(student_input)
        
        # Get curriculum context from knowledge base
        kb_context = self._get_knowledge_context(student_input)
        
        # Check if we should transition phases
        self._maybe_transition_phase()
        
        # Generate response based on current phase
        if self.phase == ConversationPhase.EXIT_TICKET:
            return self._handle_exit_ticket()
        
        # Determine if we should show media and what media is available
        media = []
        media_context = ""
        
        if visual_request:
            # Student explicitly requested a visual
            media = self._find_matching_media(student_input, min_relevance=0.3)
            if not media:
                # Try to generate one
                generated = self._generate_visual_aid(student_input)
                if generated:
                    media = [generated]
        else:
            # Check if this topic would benefit from a visual
            media = self._get_proactive_media()
        
        # Build media context for the LLM so it knows what's being shown
        if media:
            media_context = self._build_media_context(media)
        
        # Generate response WITH media context so LLM can describe accurately
        response = self._generate_contextual_response(
            student_input, 
            kb_context, 
            media_context=media_context,
            visual_requested=bool(visual_request)
        )
        
        # If response mentions visual but we don't have one, try to generate
        if not media and self._response_needs_visual(response):
            visual_need = self._determine_visual_need(response)
            if visual_need:
                generated = self._generate_visual_aid(visual_need)
                if generated:
                    media = [generated]
        
        # Analyze student response for adaptation
        self._analyze_student_response(student_input, response)
        
        # Save state
        self._save_state()
        
        # Save tutor response
        self._save_turn("tutor", response)
        self.conversation.append({"role": "assistant", "content": response})
        
        return self._create_message(response, media=media)
    
    def _get_proactive_media(self) -> List[Dict]:
        """Get media that would proactively help with current topic."""
        if self.current_topic_index >= len(self.steps):
            return []
        
        step = self.steps[self.current_topic_index]
        
        if not step.media or 'images' not in step.media:
            return []
        
        media = []
        topic_terms = self._extract_topic_terms()
        
        for img in step.media['images'][:1]:
            if not img.get('url'):
                continue
            
            # Check if image matches current topic
            img_description = f"{img.get('alt', '')} {img.get('caption', '')}".lower()
            
            # Require at least one topic term to match
            if any(term in img_description for term in topic_terms):
                media.append({
                    'type': 'image',
                    'url': img['url'],
                    'alt': img.get('alt', ''),
                    'caption': img.get('caption', ''),
                    'description': img.get('alt', '') or img.get('caption', ''),
                })
                break
        
        return media
    
    def _build_media_context(self, media: List[Dict]) -> str:
        """Build context about what images are being shown for the LLM."""
        if not media:
            return ""
        
        context = "\n📷 IMAGES BEING SHOWN TO STUDENT:\n"
        for i, m in enumerate(media):
            desc = m.get('description') or m.get('alt') or m.get('caption') or 'Image'
            context += f"  Image {i+1}: {desc}\n"
        
        context += """
IMPORTANT: You are showing these images with your response.
- Reference the actual image content in your explanation
- Point out specific features the student should notice
- If the image doesn't match what you're explaining, don't reference it
- Describe what the image ACTUALLY shows, not what you wish it showed
"""
        return context
    
    def _response_needs_visual(self, response: str) -> bool:
        """Check if the response references a visual that isn't provided."""
        response_lower = response.lower()
        visual_refs = ['look at', 'see the', 'this diagram', 'this image', 
                       'notice how', 'in the picture', 'the figure shows']
        return any(ref in response_lower for ref in visual_refs)
    
    def _detect_visual_request(self, student_input: str) -> Optional[str]:
        """Detect if student is asking for a visual aid."""
        input_lower = student_input.lower()
        
        visual_triggers = [
            'show me', 'can you show', 'draw', 'diagram', 'picture', 
            'image', 'visual', 'figure', 'illustrate', 'see this',
            'what does it look like', 'visualize', 'graph', 'chart',
            'can i see', 'display', 'example image'
        ]
        
        for trigger in visual_triggers:
            if trigger in input_lower:
                return trigger
        
        return None
    
    def _handle_visual_request(
        self, 
        student_input: str, 
        trigger: str, 
        kb_context: str
    ) -> Tuple[str, List[Dict]]:
        """Handle a request for visual aid."""
        media = []
        
        # First, check if we have existing media that matches
        existing_media = self._find_matching_media(student_input)
        if existing_media:
            media = existing_media
            response = self._generate_visual_explanation(student_input, kb_context, has_image=True)
            return response, media
        
        # Try to generate a new image
        generated = self._generate_visual_aid(student_input)
        if generated:
            media = [generated]
            response = self._generate_visual_explanation(student_input, kb_context, has_image=True)
            return response, media
        
        # Fallback: describe it verbally
        response = self._generate_visual_explanation(student_input, kb_context, has_image=False)
        return response, media
    
    def _find_matching_media(self, query: str, min_relevance: float = 0.4) -> List[Dict]:
        """
        Find existing media that STRONGLY matches the query.
        
        Uses stricter matching to avoid showing irrelevant images.
        Returns media with relevance metadata for the LLM.
        """
        media = []
        query_lower = query.lower()
        
        # Extract meaningful keywords (longer words, no common words)
        stop_words = {'this', 'that', 'what', 'which', 'would', 'could', 'should', 
                      'show', 'have', 'been', 'they', 'their', 'there', 'about',
                      'please', 'want', 'need', 'like', 'help', 'explain'}
        query_words = [w for w in query_lower.split() if len(w) > 3 and w not in stop_words]
        
        if not query_words:
            return []
        
        # Search through all lesson steps for matching media
        candidates = []
        
        for step in self.steps:
            if not step.media or 'images' not in step.media:
                continue
            
            for img in step.media['images']:
                if not img.get('url'):
                    continue
                
                # Build searchable text from image metadata
                img_alt = img.get('alt', '').lower()
                img_caption = img.get('caption', '').lower()
                step_content = (step.teacher_script or '').lower()[:300]
                
                img_text = f"{img_alt} {img_caption} {step_content}"
                
                # Calculate relevance score
                matches = sum(1 for w in query_words if w in img_text)
                relevance = matches / len(query_words) if query_words else 0
                
                # Also check for exact phrase matches (higher weight)
                if any(phrase in img_text for phrase in [query_lower[:20], query_lower[-20:]]):
                    relevance += 0.3
                
                # Check if image description contains topic-specific terms
                topic_terms = self._extract_topic_terms()
                topic_matches = sum(1 for t in topic_terms if t in img_text)
                if topic_terms:
                    relevance += (topic_matches / len(topic_terms)) * 0.3
                
                if relevance >= min_relevance:
                    candidates.append({
                        'type': 'image',
                        'url': img['url'],
                        'alt': img.get('alt', ''),
                        'caption': img.get('caption', ''),
                        'relevance': relevance,
                        'description': img_alt or img_caption or 'Educational diagram',
                    })
        
        # Sort by relevance and take top matches
        candidates.sort(key=lambda x: x['relevance'], reverse=True)
        
        # Only return if we have good matches
        for c in candidates[:2]:
            if c['relevance'] >= min_relevance:
                media.append(c)
        
        return media
    
    def _extract_topic_terms(self) -> List[str]:
        """Extract key topic terms from the lesson for better matching."""
        terms = []
        
        # From lesson title
        title_words = self.lesson.title.lower().split()
        terms.extend([w for w in title_words if len(w) > 4])
        
        # From objective
        if self.lesson.objective:
            obj_words = self.lesson.objective.lower().split()
            terms.extend([w for w in obj_words if len(w) > 5])
        
        return list(set(terms))[:10]  # Limit to 10 unique terms
    
    def _generate_visual_aid(self, request: str) -> Optional[Dict]:
        """Generate a visual aid using DALL-E or similar."""
        try:
            from apps.tutoring.image_service import ImageGenerationService
            
            service = ImageGenerationService(
                lesson=self.lesson,
                institution=self.session.institution
            )
            
            # Determine the type of visual
            request_lower = request.lower()
            if 'diagram' in request_lower:
                category = 'diagram'
            elif 'graph' in request_lower or 'chart' in request_lower:
                category = 'chart'
            elif 'map' in request_lower:
                category = 'map'
            else:
                category = 'diagram'
            
            # Build a good prompt from the lesson context
            prompt = self._build_image_prompt(request)
            
            result = service.get_or_generate_image(prompt, category)
            
            if result:
                logger.info(f"Generated visual aid: {result.get('url', 'unknown')}")
                return {
                    'type': 'image',
                    'url': result['url'],
                    'alt': result.get('alt_text', prompt),
                    'caption': result.get('caption', f"Diagram: {prompt[:100]}"),
                }
            
        except Exception as e:
            logger.warning(f"Could not generate visual aid: {e}")
        
        return None
    
    def _build_image_prompt(self, request: str) -> str:
        """Build a detailed prompt for image generation."""
        # Get current topic context
        current_topic = self._get_current_topic()
        
        prompt_parts = [
            f"Educational diagram for secondary school: {self.lesson.title}.",
            f"Topic: {current_topic[:100]}.",
            f"Student asked for: {request[:100]}.",
        ]
        
        # Add Seychelles context if relevant
        if any(word in self.lesson.title.lower() for word in ['geography', 'map', 'climate', 'island']):
            prompt_parts.append("Context: Seychelles islands in the Indian Ocean.")
        
        return " ".join(prompt_parts)
    
    def _generate_visual_explanation(
        self, 
        student_input: str, 
        kb_context: str, 
        has_image: bool
    ) -> str:
        """Generate explanation to accompany visual (or explain why we can't show one)."""
        if has_image:
            prompt = f"""The student asked: "{student_input}"

I'm showing them a relevant image/diagram.

Generate a brief response that:
1. Acknowledges their request positively
2. Points out key things to notice in the diagram
3. Asks a question about what they see

Keep it to 2-3 sentences + question."""
        else:
            prompt = f"""The student asked: "{student_input}"

I don't have an image to show right now, but I should help them visualize this.

Generate a response that:
1. Acknowledges their request
2. Provides a clear verbal description to help them visualize
3. Suggests they could draw it themselves
4. Asks a question to check understanding

LESSON CONTEXT: {self.lesson_context[:500]}

Keep it to 3-4 sentences + question."""
        
        return self._generate_response(prompt)
    
    def _get_relevant_media_for_response(self, response: str) -> List[Dict]:
        """
        Intelligently select media that would enhance the tutor's response.
        
        Only includes media if it's highly relevant to what's being discussed.
        Falls back to generating new media if no good match exists.
        """
        media = []
        response_lower = response.lower()
        
        # Keywords that suggest visuals would help
        visual_keywords = [
            'diagram', 'shows', 'look at', 'see how', 'notice', 
            'picture', 'imagine', 'visualize', 'example', 'like this',
            'pyramid', 'chart', 'graph', 'map', 'figure'
        ]
        
        # Check if response would benefit from media
        should_show_media = any(kw in response_lower for kw in visual_keywords)
        
        if not should_show_media:
            return media
        
        # Get current step media if available
        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]
            
            if step.media and 'images' in step.media:
                for img in step.media['images'][:1]:  # Only 1 image to be selective
                    if img.get('url'):
                        # Validate the image is relevant to the response
                        img_description = f"{img.get('alt', '')} {img.get('caption', '')}".lower()
                        
                        # Check for topic match
                        topic_terms = self._extract_topic_terms()
                        topic_match = any(term in img_description for term in topic_terms)
                        
                        # Check for response content match
                        response_terms = [w for w in response_lower.split() if len(w) > 5][:10]
                        response_match = sum(1 for t in response_terms if t in img_description)
                        
                        # Only include if there's a reasonable match
                        if topic_match or response_match >= 2:
                            media.append({
                                'type': 'image',
                                'url': img['url'],
                                'alt': img.get('alt', ''),
                                'caption': img.get('caption', ''),
                                'description': img.get('alt', '') or img.get('caption', ''),
                            })
        
        # If no existing media matches well, try to generate one
        if not media and should_show_media:
            # Determine what visual would help
            visual_need = self._determine_visual_need(response)
            if visual_need:
                generated = self._generate_visual_aid(visual_need)
                if generated:
                    media.append(generated)
        
        return media
    
    def _determine_visual_need(self, response: str) -> Optional[str]:
        """Determine what kind of visual would help based on the response."""
        response_lower = response.lower()
        
        # Check for specific visual types mentioned
        if 'pyramid' in response_lower:
            return f"population pyramid chart showing age distribution with males on left and females on right, for {self.lesson.title}"
        elif 'graph' in response_lower or 'chart' in response_lower:
            return f"educational chart or graph related to {self.lesson.title}"
        elif 'map' in response_lower:
            return f"educational map related to {self.lesson.title}"
        elif 'diagram' in response_lower:
            return f"educational diagram explaining {self.lesson.title}"
        
        # Generic visual for the topic
        topic_terms = self._extract_topic_terms()
        if topic_terms:
            return f"educational diagram showing {' '.join(topic_terms[:3])} for secondary school"
        
        return None
    
    # =========================================================================
    # RESPONSE GENERATION
    # =========================================================================
    
    def _generate_opening(self) -> TutorMessage:
        """Generate the opening message for the session."""
        # Get a retrieval question from previous lessons
        retrieval_context = self._get_retrieval_context()
        
        prompt = f"""Generate an opening message for this tutoring session.

{self.lesson_context}

PREVIOUS KNOWLEDGE TO REVIEW:
{retrieval_context}

Generate a warm, engaging opening that:
1. Greets the student warmly (use a Seychelles name if appropriate)
2. Either asks a simple review question from previous topics OR
3. Asks what they already know about today's topic

End with a question. Keep it to 3-4 sentences max."""

        response = self._generate_response(prompt)
        
        # Save
        self._save_turn("tutor", response)
        self.conversation.append({"role": "assistant", "content": response})
        self._save_state()
        
        return self._create_message(response)
    
    def _generate_contextual_response(
        self, 
        student_input: str, 
        kb_context: str,
        media_context: str = "",
        visual_requested: bool = False
    ) -> str:
        """Generate a response based on student input and context."""
        
        # Get current step guidance
        current_guidance = self._get_current_guidance()
        
        # Build phase-specific instructions
        phase_instructions = self._get_phase_instructions()
        
        # Get concept coverage status - CRITICAL for guiding instruction
        concept_coverage = self._get_concept_coverage_summary()
        
        # Get the next uncovered concept to focus on
        next_concept = self._get_next_uncovered_concept()
        
        # Build visual instructions
        visual_instructions = ""
        if media_context:
            visual_instructions = f"""
{media_context}
"""
        elif visual_requested:
            visual_instructions = """
⚠️ VISUAL REQUESTED BUT NOT AVAILABLE:
The student asked for a visual, but no matching image was found.
- Acknowledge their request
- Provide a clear verbal description instead
- Suggest they could sketch it themselves
- Continue with the lesson
"""
        
        prompt = f"""CONVERSATION CONTEXT:
{self._format_recent_conversation(5)}

STUDENT JUST SAID: "{student_input}"

LESSON CONTEXT:
{self.lesson_context}

CURRICULUM KNOWLEDGE:
{kb_context}

CURRENT TEACHING GUIDANCE:
{current_guidance}
{visual_instructions}
{concept_coverage}

{next_concept}

PHASE: {self.phase.value.upper()}
{phase_instructions}

STUDENT PROFILE:
- Struggles with: {', '.join(self.student_struggles) if self.student_struggles else 'Nothing noted yet'}
- Strong at: {', '.join(self.student_strengths) if self.student_strengths else 'Nothing noted yet'}
- Practice score: {self.practice_correct}/{self.practice_total}

Generate your response following these rules:
1. RESPOND to what the student said (acknowledge their answer)
2. If correct: praise specifically, then advance to the next concept
3. If incorrect: encourage, give a hint, ask a simpler question
4. If confused: simplify, use an example
5. PRIORITIZE teaching uncovered exit ticket concepts
6. If an image is being shown, DESCRIBE WHAT IT ACTUALLY SHOWS - don't make up features
7. END with a question or "Try this:" prompt
8. Keep it concise (2-4 sentences + question)
9. Use Seychelles context where natural

YOUR RESPONSE:"""

        return self._generate_response(prompt)
    
    def _get_next_uncovered_concept(self) -> str:
        """Get the next uncovered exit ticket concept to focus on."""
        
        # During remediation, prioritize the failed questions
        if getattr(self, 'is_remediation', False) and getattr(self, 'failed_exit_questions', []):
            failed = self.failed_exit_questions
            
            # Find first failed question not yet re-covered
            failed_ids = {fq['id'] for fq in failed}
            uncovered_failed = [
                c for c in self.exit_ticket_concepts 
                if c['id'] in failed_ids and not c.get('covered')
            ]
            
            if uncovered_failed:
                concept = uncovered_failed[0]
                # Find the matching failed question for more context
                failed_q = next((fq for fq in failed if fq['id'] == concept['id']), None)
                
                return f"""🎯 REMEDIATION FOCUS - This is a concept the student got WRONG on the exit ticket:

Question they missed: "{concept['question']}"
Their wrong answer was: "{failed_q.get('student_answer', '?') if failed_q else '?'}"
Correct answer: "{concept['correct_text']}"
Why it's correct: "{concept.get('explanation', 'This is the key concept to understand')}"

IMPORTANT: The student already attempted this and got it wrong. 
- Approach it from a different angle
- Use a new example or analogy
- Break it down into smaller steps
- Check their understanding before moving on

Guide your teaching to help them truly understand this concept!"""
        
        # Normal flow - get any uncovered concept
        uncovered = self._get_uncovered_concepts()
        
        if not uncovered:
            return "All exit ticket concepts have been covered! Focus on reinforcement and practice."
        
        # Get the first uncovered concept
        concept = uncovered[0]
        
        return f"""PRIORITY CONCEPT TO TEACH NEXT:
Question students will face: "{concept['question']}"
Correct answer: "{concept['correct_text']}"
Key understanding needed: "{concept.get('explanation', 'Understand this concept thoroughly')}"

Guide your teaching toward helping the student understand this concept!"""
    
    def _generate_response(self, prompt: str) -> str:
        """Call the LLM to generate a response."""
        if not self.llm_client:
            # Fallback response if no LLM
            return self._fallback_response()
        
        try:
            # Build messages
            messages = [{"role": "user", "content": prompt}]
            
            # Call LLM (max_tokens and temperature come from ModelConfig)
            response = self.llm_client.generate(
                messages=messages,
                system_prompt=TUTOR_SYSTEM_PROMPT,
            )
            
            return response.content.strip()
            
        except Exception as e:
            logger.error(f"LLM generation failed: {e}")
            return self._fallback_response()
    
    def _fallback_response(self) -> str:
        """Fallback response when LLM is unavailable."""
        fallbacks = [
            "That's interesting! Let me think about that. Can you tell me more about your reasoning?",
            "Good effort! Let's explore this together. What do you think the first step should be?",
            "I see what you're thinking. Let's break this down - what's the key concept here?",
        ]
        return random.choice(fallbacks)
    
    # =========================================================================
    # CONTEXT HELPERS
    # =========================================================================
    
    def _get_knowledge_context(self, student_input: str) -> str:
        """Query knowledge base for relevant context."""
        if not self.knowledge_base:
            return "No additional curriculum context available."
        
        try:
            result = self.knowledge_base.query_for_tutoring(
                lesson=self.lesson,
                student_message=student_input,
                current_topic=self._get_current_topic(),
                n_results=5
            )
            
            if result.chunks:
                context_parts = ["Relevant curriculum content:"]
                for chunk in result.chunks[:3]:
                    context_parts.append(f"- {chunk.get('content', '')[:200]}...")
                return "\n".join(context_parts)
            
            return result.context_summary or "Teaching standard curriculum content."
            
        except Exception as e:
            logger.warning(f"Knowledge base query failed: {e}")
            return "Standard curriculum context."
    
    def _get_retrieval_context(self) -> str:
        """Get context for retrieval practice from previous lessons."""
        try:
            # Get previous lessons in this unit
            previous_lessons = Lesson.objects.filter(
                unit=self.lesson.unit,
                order_index__lt=self.lesson.order_index,
                is_published=True
            ).order_by('-order_index')[:2]
            
            if not previous_lessons:
                return "This is the first lesson in the unit - no previous topics to review."
            
            context_parts = ["Previous topics the student has learned:"]
            for lesson in previous_lessons:
                context_parts.append(f"- {lesson.title}: {lesson.objective}")
            
            return "\n".join(context_parts)
            
        except Exception as e:
            logger.warning(f"Could not get retrieval context: {e}")
            return "Previous topics not available."
    
    def _get_current_guidance(self) -> str:
        """Get guidance from current lesson step."""
        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]
            
            guidance = f"Current topic: {step.step_type}\n"
            guidance += f"Content: {step.teacher_script[:300]}...\n" if step.teacher_script else ""
            
            if step.question:
                guidance += f"Suggested question: {step.question}\n"
            if step.expected_answer:
                guidance += f"Expected answer: {step.expected_answer}\n"
            if step.hint_1:
                guidance += f"Hint if stuck: {step.hint_1}\n"
            
            return guidance
        
        return "All planned topics covered. Move to wrap-up."
    
    def _get_current_topic(self) -> str:
        """Get the current topic being discussed."""
        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]
            return step.teacher_script[:100] if step.teacher_script else self.lesson.title
        return self.lesson.title
    
    def _get_relevant_media(self) -> List[Dict]:
        """Get media relevant to current topic (fallback method)."""
        return self._get_relevant_media_for_response("")
    
    def _format_recent_conversation(self, n: int = 5) -> str:
        """Format recent conversation for context."""
        recent = self.conversation[-(n*2):] if len(self.conversation) > n*2 else self.conversation
        
        formatted = []
        for msg in recent:
            role = "TUTOR" if msg['role'] == 'assistant' else "STUDENT"
            formatted.append(f"{role}: {msg['content']}")
        
        return "\n".join(formatted) if formatted else "Conversation just started."
    
    # =========================================================================
    # PHASE MANAGEMENT
    # =========================================================================
    
    def _get_phase_instructions(self) -> str:
        """Get instructions specific to current phase."""
        # Get uncovered concepts count
        uncovered_count = len(self._get_uncovered_concepts())
        total_concepts = len(self.exit_ticket_concepts)
        
        # Check if we're in remediation mode
        is_remediation = getattr(self, 'is_remediation', False)
        failed_count = len(getattr(self, 'failed_exit_questions', []))
        attempt = getattr(self, 'remediation_attempt', 0)
        
        if is_remediation:
            # Special remediation instructions
            return f"""
🎯 REMEDIATION MODE (Attempt #{attempt})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The student failed the exit ticket and is reviewing the {failed_count} concepts they got wrong.

YOUR GOALS:
1. Focus ONLY on the concepts they missed - don't re-teach everything
2. Use DIFFERENT explanations than before - they didn't understand the first time
3. Use more examples, analogies, and visual descriptions
4. Break concepts into smaller steps
5. Check understanding frequently with mini-questions
6. Be encouraging - failing is part of learning!

PHASE: {self.phase.value.upper()}
Concepts to re-teach: {uncovered_count} remaining

After covering all failed concepts, move to a short practice phase, then back to exit ticket.
Keep the remediation focused and efficient - aim for understanding, not speed."""
        
        # Normal phase instructions
        instructions = {
            ConversationPhase.WARMUP: """
WARMUP PHASE - Goals:
- Build rapport and confidence
- Review one concept from previous learning
- Transition: After 1-2 successful exchanges, move to INTRODUCTION""",
            
            ConversationPhase.INTRODUCTION: f"""
INTRODUCTION PHASE - Goals:
- Hook student interest with real-world relevance
- Find out what they already know
- Preview what they'll learn: There are {total_concepts} key concepts to master
- Transition: After student shows interest/engagement, move to INSTRUCTION""",
            
            ConversationPhase.INSTRUCTION: f"""
INSTRUCTION PHASE - Goals:
- Teach the EXIT TICKET CONCEPTS through questions, not lectures
- Focus on uncovered concepts: {uncovered_count} of {total_concepts} still need to be taught
- Use worked examples that directly relate to exit ticket questions
- Show diagrams/media when available
- Check understanding before moving to next concept
- Transition: After covering key concepts (aim for all {total_concepts}), move to PRACTICE""",
            
            ConversationPhase.PRACTICE: f"""
PRACTICE PHASE - Goals:
- Have student practice problems similar to EXIT TICKET questions
- Concepts still uncovered: {uncovered_count} - prioritize these!
- Scaffold with hints if they struggle
- Track correct/incorrect responses
- Make sure student can answer questions like the ones on the exit ticket
- Transition: After 3-5 practice exchanges AND most concepts covered, move to WRAPUP""",
            
            ConversationPhase.WRAPUP: f"""
WRAPUP PHASE - Goals:
- Have student summarize what they learned
- Review any EXIT TICKET concepts that seem weak
- Concepts covered: {total_concepts - uncovered_count}/{total_concepts}
- Praise their effort specifically
- Prepare them for the exit quiz
- Transition: After student summarizes, move to EXIT_TICKET""",
        }
        
        return instructions.get(self.phase, "Continue the conversation naturally.")
    
    def _maybe_transition_phase(self):
        """Check if we should transition to next phase."""
        
        # Check if we're in remediation mode
        is_remediation = getattr(self, 'is_remediation', False)
        
        if is_remediation:
            # Shorter remediation flow: INSTRUCTION → PRACTICE → EXIT_TICKET
            remediation_transitions = {
                ConversationPhase.INSTRUCTION: (4, ConversationPhase.PRACTICE),  # Shorter instruction
                ConversationPhase.PRACTICE: (3, ConversationPhase.EXIT_TICKET),  # Shorter practice, skip wrapup
            }
            
            if self.phase in remediation_transitions:
                threshold, next_phase = remediation_transitions[self.phase]
                
                # Check if all failed concepts are now covered
                uncovered = self._get_uncovered_concepts()
                
                if self.phase_exchange_count >= threshold:
                    if not uncovered or self.phase == ConversationPhase.PRACTICE:
                        self.phase = next_phase
                        self.phase_exchange_count = 0
                        logger.info(f"Remediation: Transitioned to phase: {self.phase.value}")
            return
        
        # Normal flow
        transitions = {
            ConversationPhase.WARMUP: (2, ConversationPhase.INTRODUCTION),
            ConversationPhase.INTRODUCTION: (3, ConversationPhase.INSTRUCTION),
            ConversationPhase.INSTRUCTION: (6, ConversationPhase.PRACTICE),
            ConversationPhase.PRACTICE: (5, ConversationPhase.WRAPUP),
            ConversationPhase.WRAPUP: (2, ConversationPhase.EXIT_TICKET),
        }
        
        if self.phase in transitions:
            threshold, next_phase = transitions[self.phase]
            
            # Special check before exit ticket: ensure concepts are covered
            if next_phase == ConversationPhase.EXIT_TICKET:
                uncovered = self._get_uncovered_concepts()
                if uncovered and self.phase_exchange_count < threshold + 3:
                    # Don't transition yet - still have uncovered concepts
                    # Give up to 3 extra exchanges to cover them
                    logger.info(f"Delaying exit ticket - {len(uncovered)} concepts uncovered")
                    return
            
            if self.phase_exchange_count >= threshold:
                self.phase = next_phase
                self.phase_exchange_count = 0
                logger.info(f"Transitioned to phase: {self.phase.value}")
    
    def _get_uncovered_concepts(self) -> List[Dict]:
        """Get list of exit ticket concepts not yet covered."""
        return [c for c in self.exit_ticket_concepts if not c.get('covered')]
    
    def _get_concept_coverage_summary(self) -> str:
        """Get summary of concept coverage for the LLM."""
        if not self.exit_ticket_concepts:
            return "No exit ticket concepts to track."
        
        total = len(self.exit_ticket_concepts)
        covered = sum(1 for c in self.exit_ticket_concepts if c.get('covered'))
        uncovered = self._get_uncovered_concepts()
        
        summary = f"EXIT CONCEPT COVERAGE: {covered}/{total} covered\n"
        
        if uncovered:
            summary += "UNCOVERED CONCEPTS (prioritize teaching these!):\n"
            for c in uncovered[:3]:  # Show top 3
                summary += f"  - {c['question'][:100]}...\n"
        
        return summary
        
        if self.phase in transitions:
            threshold, next_phase = transitions[self.phase]
            if self.phase_exchange_count >= threshold:
                self.phase = next_phase
                self.phase_exchange_count = 0
                logger.info(f"Transitioned to phase: {self.phase.value}")
    
    # =========================================================================
    # STUDENT ANALYSIS
    # =========================================================================
    
    def _analyze_student_response(self, student_input: str, tutor_response: str):
        """Analyze student response to adapt future instruction and track concept coverage."""
        input_lower = student_input.lower()
        response_lower = tutor_response.lower()
        combined_text = f"{input_lower} {response_lower}"
        
        # Detect confusion
        confusion_signals = ["i don't know", "confused", "don't understand", "help", "?", "not sure", "what"]
        if any(signal in input_lower for signal in confusion_signals):
            current_topic = self._get_current_topic()[:50]
            if current_topic not in self.student_struggles:
                self.student_struggles.append(current_topic)
        
        # Detect success (if tutor praised them)
        success_signals = ["correct", "excellent", "great", "perfect", "well done", "good job", "exactly", "right"]
        if any(signal in response_lower for signal in success_signals):
            self.practice_correct += 1
            current_topic = self._get_current_topic()[:50]
            if current_topic not in self.student_strengths:
                self.student_strengths.append(current_topic)
        
        # Track practice attempts
        if self.phase == ConversationPhase.PRACTICE:
            self.practice_total += 1
        
        # CRITICAL: Track exit ticket concept coverage
        self._update_concept_coverage(combined_text)
        
        # Advance topic if making progress
        if self.phase in [ConversationPhase.INSTRUCTION, ConversationPhase.PRACTICE]:
            if self.phase_exchange_count > 0 and self.phase_exchange_count % 2 == 0:
                if self.current_topic_index < len(self.steps) - 1:
                    self.current_topic_index += 1
    
    def _update_concept_coverage(self, conversation_text: str):
        """
        Check if any exit ticket concepts were covered in the conversation.
        
        Uses keyword matching to detect when concepts are being discussed.
        """
        conversation_lower = conversation_text.lower()
        
        for concept in self.exit_ticket_concepts:
            if concept.get('covered'):
                continue  # Already covered
            
            # Extract keywords from the question and answer
            question_words = set(
                word.lower() for word in re.findall(r'\b\w{4,}\b', concept['question'])
            )
            answer_words = set(
                word.lower() for word in re.findall(r'\b\w{4,}\b', concept.get('correct_text', ''))
            )
            explanation_words = set(
                word.lower() for word in re.findall(r'\b\w{4,}\b', concept.get('explanation', ''))
            )
            
            # Combine all relevant keywords
            concept_keywords = question_words | answer_words | explanation_words
            
            # Remove common words
            stop_words = {'this', 'that', 'what', 'which', 'would', 'could', 'should', 'with', 'from', 'have', 'been', 'they', 'their', 'there', 'when', 'where', 'about', 'into', 'more', 'some', 'other'}
            concept_keywords -= stop_words
            
            # Check how many keywords appear in the conversation
            if concept_keywords:
                matches = sum(1 for kw in concept_keywords if kw in conversation_lower)
                coverage_ratio = matches / len(concept_keywords)
                
                # Mark as covered if significant overlap (>30% of keywords discussed)
                if coverage_ratio > 0.3 or matches >= 3:
                    concept['covered'] = True
                    logger.info(f"Concept covered: {concept['question'][:50]}... (match ratio: {coverage_ratio:.1%})")
    
    # =========================================================================
    # EXIT TICKET
    # =========================================================================
    
    def _handle_exit_ticket(self) -> TutorMessage:
        """Handle exit ticket phase."""
        from apps.tutoring.models import ExitTicket, ExitTicketQuestion
        
        exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
        
        if not exit_ticket:
            # No exit ticket, just complete
            return self._complete_session()
        
        questions = ExitTicketQuestion.objects.filter(
            exit_ticket=exit_ticket
        ).order_by('order_index')
        
        if not questions:
            return self._complete_session()
        
        # Build exit ticket data for frontend
        exit_data = {
            'questions': [
                {
                    'index': i,
                    'question': q.question_text,
                    'options': [
                        {'letter': 'A', 'text': q.option_a},
                        {'letter': 'B', 'text': q.option_b},
                        {'letter': 'C', 'text': q.option_c},
                        {'letter': 'D', 'text': q.option_d},
                    ],
                    'correct': q.correct_answer,
                }
                for i, q in enumerate(questions)
            ],
            'total': len(questions),
            'passing_score': 8,
        }
        
        return TutorMessage(
            content="Great work on this lesson! Now let's check your understanding with a quick quiz. Answer all questions, then submit.",
            phase="exit_ticket",
            show_exit_ticket=True,
            exit_ticket_data=exit_data,
        )
    
    def _complete_session(self) -> TutorMessage:
        """Complete the tutoring session."""
        self.phase = ConversationPhase.COMPLETED
        self.session.status = TutorSession.Status.COMPLETED
        self.session.ended_at = timezone.now()
        self.session.mastery_achieved = True
        self._save_state()
        self.session.save()
        
        # Update progress
        progress, _ = StudentLessonProgress.objects.get_or_create(
            student=self.student,
            lesson=self.lesson,
            defaults={'institution': self.session.institution}
        )
        progress.mastery_level = 'mastered'
        progress.save()
        
        return TutorMessage(
            content=f"🎉 Congratulations! You've completed {self.lesson.title}! You showed great understanding. Keep up the excellent work!",
            phase="completed",
            is_complete=True,
        )
    
    def submit_exit_ticket(self, answers: List[str]) -> TutorMessage:
        """Process exit ticket submission."""
        from apps.tutoring.models import ExitTicket, ExitTicketQuestion
        
        exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
        if not exit_ticket:
            return self._complete_session()
        
        questions = list(ExitTicketQuestion.objects.filter(
            exit_ticket=exit_ticket
        ).order_by('order_index'))
        
        # Grade
        correct = 0
        results = []
        failed_questions = []
        
        for i, q in enumerate(questions):
            student_answer = answers[i].upper() if i < len(answers) else ''
            is_correct = student_answer == q.correct_answer.upper()
            if is_correct:
                correct += 1
            else:
                # Track failed questions for remediation
                failed_questions.append({
                    'id': q.id,
                    'index': i,
                    'question': q.question_text,
                    'student_answer': student_answer,
                    'correct_answer': q.correct_answer,
                    'correct_text': getattr(q, f'option_{q.correct_answer.lower()}', ''),
                    'explanation': q.explanation,
                })
            
            results.append({
                'index': i,
                'question': q.question_text,
                'selected': student_answer,
                'correct_answer': q.correct_answer,
                'is_correct': is_correct,
                'explanation': q.explanation,
            })
        
        passed = correct >= 8
        
        self.session.mastery_achieved = passed
        
        if passed:
            self._save_state()
            return self._complete_session_with_results(results, correct)
        else:
            # FAILED - Start remediation!
            return self._start_remediation(results, correct, failed_questions)
    
    def _complete_session_with_results(self, results: List[Dict], score: int) -> TutorMessage:
        """Complete the session with exit ticket results."""
        self.phase = ConversationPhase.COMPLETED
        self.session.status = TutorSession.Status.COMPLETED
        self.session.ended_at = timezone.now()
        self.session.mastery_achieved = True
        self._save_state()
        self.session.save()
        
        # Update progress
        progress, _ = StudentLessonProgress.objects.get_or_create(
            student=self.student,
            lesson=self.lesson,
            defaults={'institution': self.session.institution}
        )
        progress.mastery_level = 'mastered'
        progress.save()
        
        return TutorMessage(
            content=f"🎉 Excellent! You scored {score}/{len(results)}! You've mastered this lesson!",
            phase="completed",
            is_complete=True,
            exit_ticket_data={'results': results, 'score': score, 'passed': True},
        )
    
    def _start_remediation(
        self, 
        results: List[Dict], 
        score: int, 
        failed_questions: List[Dict]
    ) -> TutorMessage:
        """
        Start targeted remediation for failed exit ticket questions.
        
        This resets the session to instruction phase, but now focused
        specifically on the concepts the student got wrong.
        """
        # Track remediation state
        self.failed_exit_questions = failed_questions
        self.remediation_attempt = getattr(self, 'remediation_attempt', 0) + 1
        self.is_remediation = True
        
        # Mark failed concepts as NOT covered (need to re-teach)
        failed_ids = {fq['id'] for fq in failed_questions}
        for concept in self.exit_ticket_concepts:
            if concept['id'] in failed_ids:
                concept['covered'] = False
        
        # Reset to instruction phase for targeted review
        self.phase = ConversationPhase.INSTRUCTION
        self.phase_exchange_count = 0
        
        # Save state with remediation info
        self._save_state()
        
        # Generate encouraging remediation message
        failed_count = len(failed_questions)
        message = self._generate_remediation_opening(score, len(results), failed_questions)
        
        # Save the message
        self._save_turn("tutor", message)
        self.conversation.append({"role": "assistant", "content": message})
        
        return TutorMessage(
            content=message,
            phase="remediation",
            is_complete=False,
            exit_ticket_data={
                'results': results, 
                'score': score, 
                'passed': False,
                'remediation_started': True,
                'failed_count': failed_count,
            },
        )
    
    def _generate_remediation_opening(
        self, 
        score: int, 
        total: int, 
        failed_questions: List[Dict]
    ) -> str:
        """Generate an encouraging message to start remediation."""
        # Build context about what they got wrong
        failed_topics = []
        for fq in failed_questions[:3]:  # Show first 3
            failed_topics.append(f"- {fq['question'][:80]}...")
        
        prompt = f"""The student just completed the exit ticket but didn't pass.
Score: {score}/{total} (needed 8 to pass)
Attempt number: {self.remediation_attempt}

Questions they got wrong:
{chr(10).join(failed_topics)}

Generate an encouraging message that:
1. Acknowledges their effort positively (no shame!)
2. Explains we'll review the specific concepts they missed
3. Reassures them this is normal - learning takes practice
4. Starts with a question about one of the concepts they missed

Keep it warm and supportive. 3-4 sentences + a question to start the review."""

        return self._generate_response(prompt)
    
    # =========================================================================
    # HELPERS
    # =========================================================================
    
    def _save_turn(self, role: str, content: str):
        """Save a conversation turn."""
        SessionTurn.objects.create(
            session=self.session,
            role=role,
            content=content,
        )
    
    def _create_message(self, content: str, media: List[Dict] = None) -> TutorMessage:
        """Create a TutorMessage from content."""
        return TutorMessage(
            content=content,
            phase=self.phase.value,
            media=media or [],
            expects_response=self.phase != ConversationPhase.COMPLETED,
        )