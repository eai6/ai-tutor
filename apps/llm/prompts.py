"""
Prompt Assembler - Constructs the full system prompt and step instructions.

The prompt has two layers:
1. SYSTEM PROMPT (from PromptPack): Persona, teaching style, safety, formatting
2. STEP CONTEXT (from LessonStep): What to teach/ask right now

This separation lets institutions customize the "how" (prompts) while
content editors control the "what" (curriculum).
"""

import logging
from dataclasses import dataclass
from typing import Optional, List

from apps.llm.models import PromptPack
from apps.curriculum.models import Lesson, LessonStep

logger = logging.getLogger(__name__)


JSON_SUFFIX = "\n\nIMPORTANT: You MUST respond with valid JSON only. No other text."


def _get_tutor_system_prompt_template():
    """Lazy-load the tutor system prompt template to avoid circular imports."""
    from apps.tutoring.conversational_tutor import TUTOR_SYSTEM_PROMPT_TEMPLATE
    return TUTOR_SYSTEM_PROMPT_TEMPLATE


# Static defaults for non-tutor prompts
_STATIC_PROMPT_DEFAULTS = {
    'safety_prompt': "Ensure all interactions are safe and age-appropriate for secondary school students.",
    'content_generation_prompt': (
        "You are an expert curriculum designer creating engaging tutoring content "
        "for secondary students. Return only valid JSON."
    ),
    'exit_ticket_prompt': (
        "You are an expert teacher creating assessment questions. "
        "Return ONLY valid JSON, no other text."
    ),
    'grading_prompt': "You are a fair, encouraging grader. Respond only with valid JSON.",
    'image_generation_prompt': "Educational diagram for secondary school students.",
}


def get_prompt_defaults():
    """Return full PROMPT_DEFAULTS dict, including lazy-loaded tutor template."""
    defaults = dict(_STATIC_PROMPT_DEFAULTS)
    defaults['tutor_system_prompt'] = _get_tutor_system_prompt_template()
    return defaults


# Eagerly-evaluated alias for static contexts (template rendering etc.)
# Callers that need the tutor template should use get_prompt_defaults().
PROMPT_DEFAULTS = _STATIC_PROMPT_DEFAULTS


def get_active_prompt_pack(institution_id):
    """Get active PromptPack for institution, falling back to platform-wide."""
    try:
        if institution_id:
            pack = PromptPack.objects.filter(
                institution_id=institution_id, is_active=True
            ).first()
            if pack:
                return pack
        # Fall back to platform-wide prompt pack
        return PromptPack.objects.filter(
            institution__isnull=True, is_active=True
        ).first()
    except Exception:
        return None


def get_prompt_or_default(institution_id, field_name, default, json_required=False):
    """Get prompt field from PromptPack, fall back to default if empty/missing.

    If json_required=True and the resolved prompt doesn't mention 'json',
    append JSON_SUFFIX to enforce valid JSON output.
    """
    pack = get_active_prompt_pack(institution_id)
    prompt = default
    if pack:
        value = getattr(pack, field_name, '')
        if value and value.strip():
            prompt = value
    if json_required and 'json' not in prompt.lower():
        prompt = prompt.rstrip() + JSON_SUFFIX
    return prompt


@dataclass
class AssembledPrompt:
    """The complete prompt ready to send to LLM."""
    system_prompt: str
    step_instruction: str  # Added to the conversation as context


def get_lesson_media(lesson: Lesson) -> List[dict]:
    """Get all media assets for a lesson."""
    from apps.media_library.models import StepMedia
    
    media_list = []
    attachments = StepMedia.objects.filter(
        lesson_step__lesson=lesson
    ).select_related('media_asset').order_by('lesson_step__order_index', 'order_index')
    
    for att in attachments:
        asset = att.media_asset
        media_list.append({
            'title': asset.title,
            'type': asset.asset_type,
            'url': asset.file.url if asset.file else None,
            'caption': asset.caption,
            'alt_text': asset.alt_text,
            'step_title': att.lesson_step.teacher_script[:50] if att.lesson_step else '',
        })
    
    return media_list


def assemble_system_prompt(prompt_pack: PromptPack, lesson: Lesson) -> str:
    """
    Build the full system prompt from PromptPack + lesson context.
    
    Structure:
    - Base system prompt (persona)
    - Teaching style
    - Safety guidelines  
    - Format rules
    - Lesson context (objective)
    - Available media
    """
    parts = []
    
    # Core persona
    parts.append(prompt_pack.system_prompt)
    
    # Teaching approach
    if prompt_pack.teaching_style_prompt:
        parts.append(prompt_pack.teaching_style_prompt)
    
    # Safety rules
    if prompt_pack.safety_prompt:
        parts.append(prompt_pack.safety_prompt)
    
    # Output formatting
    if prompt_pack.format_rules_prompt:
        parts.append(prompt_pack.format_rules_prompt)
    
    # Lesson context
    lesson_context = f"""
CURRENT LESSON: {lesson.title}
LEARNING OBJECTIVE: {lesson.objective}

You are guiding the student through this lesson step by step. Follow the step instructions provided with each message.
""".strip()
    parts.append(lesson_context)
    
    # Add available media context
    media_list = get_lesson_media(lesson)
    if media_list:
        media_context = """
AVAILABLE MEDIA FOR THIS LESSON:
You have access to the following images/media to help teach this lesson. When appropriate, you can display them by including the exact marker [SHOW_MEDIA:title] in your response, where 'title' is the media title.

"""
        for media in media_list:
            media_context += f"- {media['title']} ({media['type']}): {media['caption'] or media['alt_text'] or 'Educational visual'}\n"
        
        media_context += """
Example usage: "Let me show you a diagram of this concept. [SHOW_MEDIA:Plate Tectonics Diagram]"

IMPORTANT: Only reference media that exists in the list above. When a student asks to see a figure or image, show them relevant media from this list.
"""
        parts.append(media_context)
    
    return "\n\n".join(parts)


def build_step_instruction(
    step: LessonStep,
    attempt_number: int = 1,
    previous_answer: Optional[str] = None,
    hint_level: int = 0,
) -> str:
    """
    Build the instruction for a specific step.
    
    This gets prepended to the tutor's context for each turn.
    Includes hint ladder logic when student gets answers wrong.
    
    Args:
        step: The current LessonStep
        attempt_number: Which attempt this is (1-indexed)
        previous_answer: Student's previous wrong answer (if retrying)
        hint_level: How many hints to reveal (0 = none, 1 = hint_1, etc.)
    """
    parts = []
    
    # Step type context
    step_type_instructions = {
        LessonStep.StepType.TEACH: "INSTRUCTION: Teach this concept clearly and check for understanding.",
        LessonStep.StepType.WORKED_EXAMPLE: "INSTRUCTION: Walk through this example step-by-step, then verify the student followed along.",
        LessonStep.StepType.PRACTICE: "INSTRUCTION: Present this practice problem. Encourage the student and provide feedback on their answer.",
        LessonStep.StepType.QUIZ: "INSTRUCTION: Present this quiz question. This is an assessment - be encouraging but accurate in grading.",
        LessonStep.StepType.SUMMARY: "INSTRUCTION: Summarize what the student learned. Celebrate their progress!",
    }
    parts.append(step_type_instructions.get(step.step_type, ""))
    
    # Teacher script (what to say)
    parts.append(f"SAY THIS (in your own words):\n{step.teacher_script}")
    
    # Question (if applicable)
    if step.question:
        parts.append(f"QUESTION TO ASK:\n{step.question}")
        
        # For MCQ, include choices
        if step.answer_type == LessonStep.AnswerType.MULTIPLE_CHOICE and step.choices:
            choices_str = "\n".join(f"  {chr(65+i)}) {choice}" for i, choice in enumerate(step.choices))
            parts.append(f"ANSWER CHOICES:\n{choices_str}")
    
    # Retry context (if student got it wrong before)
    if previous_answer and attempt_number > 1:
        parts.append(f"STUDENT'S PREVIOUS ANSWER: {previous_answer}")
        parts.append(f"This is attempt {attempt_number} of {step.max_attempts}.")
        
        # Reveal hints progressively
        hints = step.hints
        if hint_level > 0 and hints:
            hints_to_show = hints[:hint_level]
            parts.append("HINTS TO GIVE:\n" + "\n".join(f"- {h}" for h in hints_to_show))
    
    # Grading context (for the tutor to know the answer)
    if step.expected_answer:
        parts.append(f"CORRECT ANSWER (for your reference only - don't reveal unless max attempts reached): {step.expected_answer}")
    
    if step.rubric:
        parts.append(f"GRADING RUBRIC:\n{step.rubric}")
    
    return "\n\n".join(parts)


def build_tutor_message(
    prompt_pack: PromptPack,
    lesson: Lesson,
    step: LessonStep,
    conversation_history: list[dict],
    attempt_number: int = 1,
    previous_answer: Optional[str] = None,
    hint_level: int = 0,
) -> tuple[str, list[dict]]:
    """
    Build the complete prompt package for an LLM call.
    
    Returns:
        Tuple of (system_prompt, messages)
        
    The step instruction is injected as a system message in the conversation
    to give the tutor context for this specific step.
    """
    # Build system prompt (persona + lesson context)
    system_prompt = assemble_system_prompt(prompt_pack, lesson)
    
    # Build step instruction
    step_instruction = build_step_instruction(
        step=step,
        attempt_number=attempt_number,
        previous_answer=previous_answer,
        hint_level=hint_level,
    )
    
    # Inject step instruction into the conversation
    # We add it as the most recent context before the LLM responds
    messages = list(conversation_history)  # Copy
    
    # Add step context as a user message with special formatting
    # (The LLM will understand this is instruction, not student input)
    if not messages or messages[-1]["role"] != "user":
        # If no messages yet, or last message was assistant, add step context
        messages.append({
            "role": "user",
            "content": f"[STEP CONTEXT]\n{step_instruction}\n[/STEP CONTEXT]\n\nPlease proceed with this step."
        })
    else:
        # Append step context to the last user message
        messages[-1]["content"] = f"[STEP CONTEXT]\n{step_instruction}\n[/STEP CONTEXT]\n\n" + messages[-1]["content"]
    
    return system_prompt, messages
