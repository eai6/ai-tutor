"""
Lesson Content Generator

Generates complete tutoring content for lessons including:
- Structured lesson steps (5E pedagogy model)
- Media content (images, diagrams, videos)
- Educational materials (vocabulary, worked examples, key points)
- Seychelles-contextualized examples

Uses `instructor` library for guaranteed structured output from any LLM provider.
No manual JSON parsing — Pydantic models define the contract, instructor enforces it.
"""

import re
import time
import logging
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _normalize_enabling_objective(
    question_eo: str,
    canonical_eos: List[str],
    *,
    llm_client=None,
) -> tuple[str, str]:
    """Snap an LLM-emitted enabling_objective to one of the lesson's
    canonical enabling_objectives.

    The LLM is told to copy lesson EOs verbatim, but it drifts —
    paraphrasing, capitalisation changes, trailing punctuation,
    occasional invention. This pass enforces the constraint:

      1. Exact match → keep verbatim. (no LLM call)
      2. Normalised exact match (case/whitespace/punctuation drift) →
         snap. (no LLM call)
      3. ``llm_client`` provided → ask the model to pick the closest
         canonical EO or drop. Replaces the brittle substring-match
         heuristic, which produced ambiguous-drop on EOs whose stems
         shared common substrings ("angles around a point" was a
         substring of every EO in the same lesson, so every emission
         got dropped).
      4. ``llm_client`` not provided → fall back to the substring
         heuristic so callers without LLM access still get something.
      5. No match → drop the tag (return ''). The question itself is
         kept; only the bad tag is cleared.

    Returns (normalised_eo, status), where status is one of:
      - 'exact'    — already matched verbatim
      - 'snapped'  — fuzzy / LLM-snapped to canonical
      - 'dropped'  — no match, tag cleared
      - 'empty'    — input was empty to begin with
    """
    raw = (question_eo or '').strip()
    if not raw:
        return '', 'empty'
    canonical = [(eo or '').strip() for eo in canonical_eos if (eo or '').strip()]
    if not canonical:
        return '', 'dropped'

    if raw in canonical:
        return raw, 'exact'

    def _norm(s: str) -> str:
        import re as _re
        out = _re.sub(r'\s+', ' ', s.lower().strip())
        return _re.sub(r'[\s.,;:!?]+$', '', out)

    raw_n = _norm(raw)
    by_norm = {_norm(eo): eo for eo in canonical}
    if raw_n in by_norm:
        return by_norm[raw_n], 'snapped'

    if llm_client is not None:
        snapped = _llm_snap_eo(raw, canonical, llm_client)
        if snapped:
            return snapped, 'snapped'
        return '', 'dropped'

    # Legacy fallback when no LLM is wired in: substring heuristic.
    # Demonstrably brittle (every EO in a lesson tends to share the
    # broad concept as a substring) — kept only for the no-LLM path.
    contains_matches = [
        eo for eo_n, eo in by_norm.items()
        if eo_n and (eo_n in raw_n or raw_n in eo_n)
    ]
    if len(contains_matches) == 1:
        return contains_matches[0], 'snapped'

    return '', 'dropped'


def _llm_snap_eo(raw: str, canonical: List[str], llm_client) -> str:
    """Ask the LLM to pick the canonical EO that best matches ``raw``.

    Returns the canonical EO string when the model picks one of the
    listed options, or ``''`` when the model says 'none' / outputs
    something off-list. Failures are non-fatal — caller falls back
    to dropping the tag.
    """
    import json
    options_block = "\n".join(
        f"  [{i + 1}] {eo}" for i, eo in enumerate(canonical)
    )
    prompt = (
        "Pick the canonical enabling objective that best matches the "
        "emitted text. Reply with ONLY the integer index (1-based), or "
        "0 if NONE of the options is a clear match.\n\n"
        f"Emitted: {raw[:300]}\n\n"
        "Canonical options:\n"
        f"{options_block}\n"
    )
    sys_prompt = (
        "You are a strict classifier. Output ONLY a single integer — "
        "the 1-based index of the best matching option, or 0 for none. "
        "No prose, no JSON, no code fence."
    )
    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=sys_prompt,
            max_tokens=10,
        )
        raw_out = (response.content or '').strip()
        # Pull the first integer out of the response.
        import re as _re
        m = _re.search(r'\d+', raw_out)
        if not m:
            return ''
        idx = int(m.group(0))
        if idx <= 0 or idx > len(canonical):
            return ''
        return canonical[idx - 1]
    except Exception as e:
        logger.info("[EOSnap] LLM call failed: %s", e)
        return ''


# Hard blocklist for distractor safety scan. Catches the most common
# stereotype patterns the question generator has produced in pilot
# (e.g. "Asians are naturally more intelligent", "Africa has no
# resources", "Women can't do math"). Any match in question_text or
# any choice → reject the question and emit a Layer-4-style log.
# Intentionally narrow — relies on the LLM's content-gen prompt to
# catch nuanced cases. This regex is the hard floor.
_DISTRACTOR_BLOCKLIST_RE = re.compile(
    r"\b(?:"
    # Adjective-form: "Asians are naturally...", "Women aren't capable..."
    r"(?:asian|african|european|american|white|black|hispanic|latino|"
    r"arab|jewish|muslim|christian|hindu|buddhist|indian|chinese|"
    r"japanese|men|women|boys|girls|males?|females?)s?\s+"
    r"(?:are|aren'?t|is|isn'?t|can'?t|cannot|always|never|all|"
    r"naturally|inherently|genetically|biologically)"
    # Bare-region absolutes: "Africa has no resources", "Asia lacks..."
    r"|(?:africa|asia|europe|america)\s+"
    r"(?:has\s+no|have\s+no|lacks?|cannot|can'?t)"
    # "naturally more X" / "naturally less Y"
    r"|naturally\s+(?:more|less)\s+(?:intelligent|smart|stupid|"
    r"capable|gifted|talented|advanced|primitive)"
    # "[group] people are/can't/don't" — common stereotype shape
    r"|(?:black|white|asian|african|brown)\s+people\s+"
    r"(?:are|can'?t|don'?t|always|never)"
    r")\b",
    re.IGNORECASE,
)


def _scan_question_for_unsafe_distractors(q: Dict) -> Optional[str]:
    """Return a rejection reason string when any field of `q` contains
    protected-class stereotype language, or None when the question is
    clean. Checks question_text + all choice variants (option_a..d,
    choices[], answer_data values).
    """
    fields_to_scan: List[str] = []
    fields_to_scan.append(str(q.get('question_text') or q.get('question') or ''))
    for k in ('option_a', 'option_b', 'option_c', 'option_d'):
        v = q.get(k)
        if v:
            fields_to_scan.append(str(v))
    for v in (q.get('choices') or []):
        fields_to_scan.append(str(v))
    ad = q.get('answer_data') or {}
    if isinstance(ad, dict):
        for k in ('text_template', 'data_description', 'figure_description'):
            v = ad.get(k)
            if v:
                fields_to_scan.append(str(v))
        for v in (ad.get('blanks') or []):
            fields_to_scan.append(str(v))
        for p in (ad.get('pairs') or []):
            if isinstance(p, dict):
                fields_to_scan.append(str(p.get('left', '')))
                fields_to_scan.append(str(p.get('right', '')))
    for f in fields_to_scan:
        m = _DISTRACTOR_BLOCKLIST_RE.search(f)
        if m:
            return (
                f"Distractor safety scan rejected: matched "
                f"{m.group(0)!r} in {f[:120]!r}"
            )
    return None


def _coverage_gaps(lesson, exit_ticket) -> list:
    """Return practice steps whose concept_tag has no matching bank
    question on the just-generated exit_ticket.

    Used by the post-generation coverage warning (P4 of
    memory/tutor_no_authoring_plan.md). Non-blocking — runtime can
    always fall back to any same-lesson bank question. The warning
    just surfaces gaps so the teacher review can target them.
    """
    if exit_ticket is None:
        return []
    bank_tags = set(
        (t or '').strip()
        for t in exit_ticket.questions.values_list('concept_tag', flat=True)
        if (t or '').strip()
    )
    gaps = []
    for step in lesson.steps.filter(step_type='practice'):
        tag = (step.concept_tag or '').strip()
        if tag and tag not in bank_tags:
            gaps.append(step)
    return gaps


def combined_objectives_for_lesson(lesson) -> List[str]:
    """Return the lesson's teaching objectives — the SINGLE SOURCE OF
    TRUTH for what objectives a lesson teaches.

    Read order (first non-empty wins, deduped):
      1. parent unit's `terminal_objectives` (broader outcomes)
      2. lesson's `enabling_objectives` (granular skills, expanded via
         `_expand_to_granular_subskills` during content generation)
      3. lesson's `objective` field (the singular CharField populated
         by curriculum parsing — always present)
      4. lesson's `title` (last-ditch fallback so the helper never
         returns an empty list when the lesson exists)

    The fallback chain matters because every downstream consumer —
    content generation, exit ticket tagging, summative tagging, the
    teacher competency map — calls this helper. If they each had
    their own fallback policy (or none), the same lesson could end up
    tagged differently in each system, breaking the join between
    "what students attempted" and "what objectives we report on".

    Each lesson nominally owns ONE teaching objective (parser-enforced
    1:1 contract); the multi-element return supports legacy
    pre-1:1 lessons and unit-level rollups.

    See `~/.claude/projects/.../memory/feedback_teaching_steps_unification.md`
    and `memory/course_regeneration_for_slow_learners.md`.
    """
    eos = list(lesson.enabling_objectives or [])
    tos: List[str] = []
    unit = getattr(lesson, 'unit', None)
    if unit is not None:
        tos = list(unit.terminal_objectives or [])

    # Order: TOs first (broader outcome), then EOs (granular skills).
    # Dedup on case-insensitive whitespace-normalized key, preserve first occurrence.
    seen = set()
    merged: List[str] = []
    for obj in tos + eos:
        if not obj:
            continue
        key = ' '.join(str(obj).split()).lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(str(obj).strip())

    if merged:
        return merged

    # Fallback chain — lesson is real, must produce something.
    fallback = (getattr(lesson, 'objective', '') or '').strip()
    if fallback:
        return [fallback]
    title = (getattr(lesson, 'title', '') or '').strip()
    if title:
        return [title]
    return []


# ============================================================================
# PYDANTIC MODELS — Schema contract for LLM structured output
# ============================================================================

class MediaImage(BaseModel):
    """An image to generate for a lesson step."""
    type: str = Field(description="Image type: diagram, chart, map, or illustration")
    description: str = Field(description=(
        "Specific description for image generation. "
        "For maps: use 'schematic map'. For diagrams: specify labels. "
        "Never request photos of real places. "
        "PRACTICE/QUIZ STEPS: depict the question SETUP only — show the "
        "given values labelled clearly, mark the unknown as a question "
        "mark or 'x = ?'. NEVER label the unknown with its computed "
        "value. The figure helps the student visualise the problem; "
        "the answer must come from the student's working, not from "
        "reading the image. (For TEACH / WORKED_EXAMPLE / SUMMARY "
        "steps the figure can show fully-solved values — those steps "
        "are explanatory, not assessable.)"
    ))
    alt_text: str = Field(description="Accessibility text describing the image")
    caption: str = Field(default="", description="Figure caption")


class StepMedia(BaseModel):
    """Media assets for a lesson step."""
    images: List[MediaImage] = Field(default_factory=list)


class VocabItem(BaseModel):
    """A vocabulary term with definition and example."""
    term: str
    definition: str
    example: str = ""


class WorkedExampleStep(BaseModel):
    """A single step in a worked example."""
    step: int
    action: str
    explanation: str


class WorkedExample(BaseModel):
    """A complete worked example with step-by-step solution."""
    problem: str
    steps: List[WorkedExampleStep]
    final_answer: str


class EducationalContent(BaseModel):
    """Educational content embedded in a lesson step."""
    key_vocabulary: Optional[List[VocabItem]] = None
    key_points: Optional[List[str]] = None
    seychelles_context: Optional[str] = None
    worked_example: Optional[WorkedExample] = None
    common_mistakes: Optional[List[str]] = None


class LessonStepSchema(BaseModel):
    """A single step in the tutoring lesson."""
    order_index: int = Field(description="Sequential index starting from 0")
    phase: str = Field(description="5E model phase: engage, explore, explain, practice, or evaluate")
    step_type: str = Field(description="Step type: teach, worked_example, practice, or quiz")
    concept_tag: str = Field(default="", description=(
        "Concept grouping tag. All steps teaching or practicing the SAME concept "
        "share the same tag (e.g. 'relief_rainfall', 'pythagorean_theorem'). "
        "ENGAGE and EVALUATE steps may use '' (empty). "
        "Each concept block must end with a practice or quiz step."
    ))
    teacher_script: str = Field(description=(
        "The tutor's dialogue/instruction text. "
        "When media is attached, anchor the script in the figure: "
        "'Look at the diagram — you can see…', 'Find angle 5 on the figure…'. "
        "Treat the figure as already visible; never ask the student to "
        "imagine or picture the geometry. NEVER use phrasings like "
        "'imagine two parallel lines', 'picture this', 'envision X', "
        "'in your head, draw…'. For visual concepts without an attached "
        "figure, reconsider — a figure should probably be attached. "
        "If no media, do NOT reference images."
    ))
    question: Optional[str] = Field(default=None, description="Question for the student. Required for practice and quiz steps.")
    answer_type: str = Field(
        default="none",
        description=(
            "Answer type. MUST be one of: 'none', 'free_text', "
            "'multiple_choice', 'short_numeric', 'true_false'. "
            "For multiple_choice: choices field required, expected_answer is letter A/B/C/D. "
            "For true_false: expected_answer is exactly 'True' or 'False'. "
            "For short_numeric: expected_answer is a number (math may include unit). "
            "For free_text: open-ended explanation."
        ),
    )
    expected_answer: Optional[str] = Field(default=None, description="Correct answer. For multiple_choice, the letter (A/B/C/D)")
    choices: Optional[List[str]] = Field(default=None, description="MCQ options: ['A) ...', 'B) ...', 'C) ...', 'D) ...']")
    terminal_objective: Optional[str] = Field(default="", description=(
        "The terminal objective this step works toward. "
        "Must be one of the lesson's TERMINAL OBJECTIVES listed above."
    ))
    enabling_objective: Optional[str] = Field(default="", description=(
        "The specific teaching step/enabling objective this step addresses. "
        "Should be one of the TEACHING STEPS listed above, if provided."
    ))
    hints: Optional[List[str]] = Field(default=None, description="2-3 hints scaffolded from general to specific")
    media: Optional[StepMedia] = Field(default=None, description="Media for this step, only if teacher_script references it")
    educational_content: Optional[EducationalContent] = None
    priority: int = Field(
        default=1,
        ge=1, le=3,
        description=(
            "Step priority for runtime selection by the tutor engine. "
            "1 = REQUIRED (must appear in every session — first engage, "
            "primary teach, primary practice, final quiz/evaluate). "
            "2 = CORE (included by default — worked example, second "
            "practice, synthesise). 3 = ENRICHMENT (dropped on short "
            "sessions — third+ practice, common-mistakes drill, extends). "
            "Aim for ~4 priority-1, ~3 priority-2, ~3 priority-3 across the lesson."
        ),
    )


class SummaryVocab(BaseModel):
    term: str
    definition: str


class LessonSummarySchema(BaseModel):
    """Summary of the lesson."""
    main_concepts: List[str]
    key_vocabulary: Optional[List[SummaryVocab]] = None
    next_steps: str = ""


class GeneratedLessonContent(BaseModel):
    """Complete generated lesson content following the 5E pedagogical model."""
    steps: List[LessonStepSchema] = Field(description="5-6 concept-grouped lesson steps for a 20-minute lesson")
    lesson_summary: Optional[LessonSummarySchema] = None


# ============================================================================
# LESSON CONTENT GENERATOR
# ============================================================================

class LessonContentGenerator:
    """
    Generates complete tutoring content for lessons.

    Uses the 5E pedagogical model:
    - Engage: Hook student interest
    - Explore: Guided discovery
    - Explain: Direct instruction
    - Practice: Apply knowledge
    - Evaluate: Check understanding
    """

    def __init__(self, institution_id: int):
        self.institution_id = institution_id
        self._init_llm_client()
        self._init_knowledge_base()

    def _init_llm_client(self):
        """Initialize instructor-wrapped LLM client for structured output."""
        import instructor
        from apps.llm.models import ModelConfig

        config = ModelConfig.get_for('generation')
        if not config:
            raise ValueError("No active LLM model configured for generation")

        self._model_config = config
        api_key = config.get_api_key()

        # Map our provider names to instructor's expected format
        PROVIDER_MAP = {
            'anthropic': 'anthropic',
            'openai': 'openai',
            'google': 'google',
            'local_ollama': 'ollama',
        }
        provider = PROVIDER_MAP.get(config.provider, config.provider)

        self.client = instructor.from_provider(
            f"{provider}/{config.model_name}",
            api_key=api_key,
        )
        print(f"[ContentGen] Instructor client ready: {provider}/{config.model_name}", flush=True)

    def _init_knowledge_base(self):
        """Initialize curriculum knowledge base."""
        try:
            from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
            self.kb = CurriculumKnowledgeBase(institution_id=self.institution_id)
            self.kb_available = True
        except Exception as e:
            logger.warning(f"Knowledge base not available: {e}")
            self.kb = None
            self.kb_available = False

    def generate_for_lesson(self, lesson, save_to_db: bool = True) -> Dict:
        """
        Generate complete content for a lesson.

        Args:
            lesson: Lesson model instance
            save_to_db: Whether to save generated steps to database

        Returns:
            Dict with generation results
        """
        logger.info(f"Generating content for: {lesson.title}")

        # Get curriculum context
        curriculum_context = self._get_curriculum_context(lesson)

        # Auto-detect content quality tier (P1.4)
        content_quality = self._determine_content_quality(curriculum_context)
        lesson.content_quality = content_quality
        lesson.save(update_fields=['content_quality'])

        # Expand the lesson's enabling_objectives into 5-8 atomic
        # sub-skills BEFORE generating steps. This makes the pre-test
        # diagnostic (sub-skill map) actually useful — without it, an
        # EO like "Understand maps" gives a coarse 1-bit signal; with
        # it, "Read a legend / Estimate distance / Find a coordinate"
        # gives the tutor a real targeting map.
        if save_to_db:
            self._expand_to_granular_subskills(lesson, curriculum_context)

        # Generate steps
        steps_data = self._generate_steps(lesson, curriculum_context)

        if not steps_data.get('success'):
            return steps_data

        # Save to database if requested
        if save_to_db:
            self._save_steps_to_db(
                lesson,
                steps_data['steps'],
                arith_retry_count=steps_data.get('arithmetic_retry_count', 0),
                arith_unresolved=steps_data.get('arithmetic_unresolved', False),
            )

            # Auto-generate exit ticket after content generation
            exit_ticket_result = self._generate_exit_ticket(lesson)

            # Create skills from enabling objectives (P1.2 — links EOs to SM-2 tracking)
            self._extract_eo_skills(lesson)

        return {
            'success': True,
            'lesson_id': lesson.id,
            'lesson_title': lesson.title,
            'steps_generated': len(steps_data.get('steps', [])),
            'steps': steps_data.get('steps', []),
            'lesson_summary': steps_data.get('lesson_summary', {}),
            'exit_ticket_generated': exit_ticket_result.get('success', False) if save_to_db else False,
        }

    def _expand_to_granular_subskills(self, lesson, curriculum_context: Dict) -> None:
        """Replace the lesson's enabling_objectives with 5-8 atomic
        sub-skills the pre-test diagnostic and exit-ticket
        questions will tag.

        Idempotent contract: if the existing EOs already look granular
        (every entry starts with an action verb AND there are >= 4 of
        them), this is a no-op. Otherwise we call the LLM, parse a
        list, and save back to lesson.enabling_objectives.

        Failure is non-fatal — bad LLM output → keep existing EOs.
        """
        existing = list(lesson.enabling_objectives or [])
        if self._already_granular(existing):
            print(f"[ContentGen] [{lesson.title}] EOs already granular ({len(existing)})", flush=True)
            return

        if not self.client or not self._model_config:
            return  # LLM unavailable; skip silently

        unit = lesson.unit
        course = unit.course if unit else None
        subject = course.title if course else ''
        grade = course.grade_level if course else ''
        terminal_objs = list(unit.terminal_objectives or []) if unit else []

        kb_str = ''
        if curriculum_context.get('related_content'):
            snippets = curriculum_context['related_content'][:3]
            kb_str = "\n".join(f"  - {s.get('text', '')[:200]}" for s in snippets)

        existing_str = "\n".join(f"  - {eo}" for eo in existing) if existing else "  (none)"
        terminal_str = "\n".join(f"  - {to}" for to in terminal_objs) if terminal_objs else "  (none)"

        prompt = f"""Break this lesson into 5-8 ATOMIC sub-skills (granular enabling objectives).

LESSON: {lesson.title}
OBJECTIVE: {lesson.objective or 'Master the concepts in this lesson'}
SUBJECT: {subject}
GRADE: {grade}

EXISTING ENABLING OBJECTIVES (often coarse — refine these):
{existing_str}

PARENT TERMINAL OBJECTIVES (broader outcomes):
{terminal_str}

CURRICULUM CONTEXT:
{kb_str or '(none)'}

REQUIREMENTS — every sub-skill must:
1. Start with a SPECIFIC action verb (Calculate, Identify, Convert, Apply,
   Compare, Solve, Construct, Read, Label, Sketch, etc.) — NEVER
   "Understand", "Know", "Be aware of", "Appreciate".
2. Test ONE concrete thing — a student should be able to FAIL one but
   PASS another. If two sub-skills always go together, merge them.
3. Be testable with a single MCQ or short-answer question.
4. Be specific to THIS lesson's content — not a generic learning
   skill like "think critically".
5. Together cover the full scope of the lesson objective.

GOOD example for "Understand fractions":
- Convert a proper fraction to a decimal
- Add two fractions with the same denominator
- Reduce a fraction to its simplest form
- Compare the size of two fractions with different denominators
- Solve a word problem involving a fraction of a quantity

BAD examples (do NOT generate these):
- Understand fractions          [vague — refine]
- Know what fractions are       [not testable]
- Work with fractions           [meaningless verb]

Output a JSON array of 5-8 strings — nothing else, no prose, no
code fence. Example shape:
["Calculate the area of a rectangle given length and width", "Calculate the area of a triangle from its base and height", ...]"""

        sys_prompt = (
            "You are a curriculum analyst who breaks lesson objectives into "
            "atomic, testable sub-skills. Output ONLY a JSON array of strings."
        )

        try:
            print(f"[ContentGen] [{lesson.title}] Expanding to granular sub-skills...", flush=True)
            # Use a direct LLM call (not via instructor) so we avoid the
            # response_model schema cost for this small free-form list.
            # Previously imported a non-existent ``get_llm_client_for_purpose``
            # which silently raised ImportError every time, swallowed by
            # the except below — every lesson's enabling_objectives stayed
            # empty. Use the same ``ModelConfig.get_for('generation')`` +
            # ``get_llm_client`` chain the rest of the file uses.
            from apps.llm.client import get_llm_client
            from apps.llm.models import ModelConfig
            cfg = ModelConfig.get_for('generation') or self._model_config
            llm = get_llm_client(cfg)
            response = llm.generate(
                [{'role': 'user', 'content': prompt}],
                system_prompt=sys_prompt,
                max_tokens=1500,
            )
            raw = (response.content or '').strip()
        except Exception as e:
            print(f"[ContentGen] [{lesson.title}] sub-skill expansion failed: {e}", flush=True)
            return

        # Parse JSON array. Tolerate code fences, single quotes, etc.
        from apps.llm.json_utils import parse_llm_json
        try:
            parsed = parse_llm_json(raw)
        except Exception as e:
            print(f"[ContentGen] [{lesson.title}] sub-skill JSON parse failed: {e}", flush=True)
            return

        if not isinstance(parsed, list):
            print(f"[ContentGen] [{lesson.title}] sub-skill output not a list: {type(parsed)}", flush=True)
            return

        cleaned = []
        seen = set()
        for item in parsed:
            text = str(item).strip().rstrip('.')
            if not text or len(text) < 8:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)

        # Defense against LLM ignoring the rules — drop anything starting
        # with weak verbs.
        WEAK_OPENERS = ('understand', 'know', 'be aware', 'appreciate', 'learn about', 'study')
        cleaned = [c for c in cleaned if not c.lower().startswith(WEAK_OPENERS)]

        if len(cleaned) < 4:
            print(f"[ContentGen] [{lesson.title}] sub-skill expansion produced too few ({len(cleaned)}) — keeping existing EOs", flush=True)
            return

        # Cap at 10 to keep questions/sub-skill ratio reasonable.
        lesson.enabling_objectives = cleaned[:10]
        lesson.save(update_fields=['enabling_objectives'])
        print(f"[ContentGen] [{lesson.title}] Expanded to {len(lesson.enabling_objectives)} granular sub-skills", flush=True)

    @staticmethod
    def _already_granular(eos: List[str]) -> bool:
        """Heuristic: at least 4 EOs and every one starts with a
        concrete action verb (not 'understand', 'know', etc.)."""
        if len(eos) < 4:
            return False
        WEAK_OPENERS = ('understand', 'know', 'be aware', 'appreciate', 'learn about', 'study', 'familiar')
        for eo in eos:
            text = (eo or '').strip().lower()
            if not text:
                return False
            first_word = text.split()[0] if text.split() else ''
            if any(text.startswith(w) for w in WEAK_OPENERS):
                return False
            # Should look like an action verb — at least 2 words.
            if len(text.split()) < 3:
                return False
        return True

    def _get_curriculum_context(self, lesson) -> Dict:
        """Get curriculum context from knowledge base."""
        if not self.kb_available:
            return self._default_curriculum_context(lesson)

        try:
            unit = lesson.unit
            course = unit.course
            subject = course.title.split()[0] if course else "General"

            context = self.kb.query_for_content_generation(
                lesson_title=lesson.title,
                lesson_objective=lesson.objective or "",
                unit_title=unit.title,
                subject=subject,
                grade_level=course.grade_level if course else "S1"
            )

            # Query for figure descriptions
            figure_descriptions = []
            try:
                figures = self.kb.query_for_figure_descriptions(
                    topic=f"{lesson.title} {lesson.objective or ''}",
                    subject=subject,
                    n_results=5,
                    grade_level=(course.grade_level if course else ''),
                )
                for fig in figures:
                    figure_descriptions.append({
                        'description': fig.get('description', ''),
                        'figure_type': fig.get('figure_type', ''),
                        'figure_number': fig.get('figure_number', ''),
                        'image_url': fig.get('image_url', ''),
                        'source_file': fig.get('source_file', ''),
                    })
            except Exception as e:
                logger.warning(f"Failed to query figure descriptions: {e}")

            related = [c.get('content', '')[:500] for c in context.chunks[:6]]
            print(f"[ContentGen] KB context for '{lesson.title}': {len(context.chunks)} chunks, "
                  f"{len(figure_descriptions)} figures, {len(related)} related excerpts", flush=True)
            if not related:
                print(f"[ContentGen] WARNING: No KB content found for '{lesson.title}' — "
                      f"materials may need reprocessing after embedding change", flush=True)

            return {
                'teaching_strategies': context.teaching_strategies or self._default_strategies(subject),
                'objectives': context.objectives,
                'related_content': related,
                'figure_descriptions': figure_descriptions,
                'subject': subject,
                'grade_level': course.grade_level if course else "S1",
            }
        except Exception as e:
            logger.warning(f"Failed to get KB context: {e}")
            return self._default_curriculum_context(lesson)

    def _default_curriculum_context(self, lesson) -> Dict:
        """Default context when KB is not available."""
        unit = lesson.unit
        course = unit.course if unit else None
        subject = course.title.split()[0] if course else "General"

        return {
            'teaching_strategies': self._default_strategies(subject),
            'objectives': [lesson.objective] if lesson.objective else [],
            'related_content': [],
            'subject': subject,
            'grade_level': course.grade_level if course else "S1",
        }

    def _default_strategies(self, subject: str) -> List[str]:
        """Default teaching strategies by subject."""
        strategies = {
            "Mathematics": [
                "Use concrete examples before abstract concepts",
                "Provide step-by-step worked examples",
                "Connect to real-world Seychelles context",
                "Use visual representations",
                "Build on prior knowledge"
            ],
            "Geography": [
                "Use maps and visual aids",
                "Connect to local Seychelles geography",
                "Compare and contrast regions",
                "Use case studies",
                "Encourage fieldwork thinking"
            ],
        }
        return strategies.get(subject, [
            "Start with what students know",
            "Use examples and non-examples",
            "Check for understanding frequently",
            "Provide scaffolded practice"
        ])

    def _determine_content_quality(self, curriculum_context: Dict) -> str:
        """Determine content quality tier based on available source materials."""
        has_related_content = bool(curriculum_context.get('related_content'))
        has_figures = bool(curriculum_context.get('figure_descriptions'))
        has_objectives = bool(curriculum_context.get('objectives'))

        # Check for exam bank content in KB
        has_exam = False
        if has_related_content:
            for chunk in curriculum_context.get('related_content', []):
                if any(kw in (chunk or '').lower() for kw in ['exam', 'assessment', 'mark scheme', 'past paper']):
                    has_exam = True
                    break

        if has_related_content and has_exam:
            return 'tier_2'  # Syllabus + Exam
        if has_related_content or has_figures:
            return 'tier_1'  # Fully Resourced (has KB content)
        if has_objectives:
            return 'tier_3'  # Syllabus Only
        return 'tier_4'  # Framework Only

    def _profile_rules(self, max_steps: int, is_math: bool) -> Dict:
        """Always-rich generation rules.

        Per the 2026-04-28 design decision, lesson content is generated
        ONCE — rich enough to support slower learners (lots of figures,
        varied recognition + production formats, multiple hint levels).
        Personalisation happens at the tutoring engine, not at content
        generation. So no per-course profile dispatch — every lesson
        gets the same scaffolding-heavy ruleset.

        See memory/course_regeneration_for_slow_learners.md.
        """
        return {
            'description': (
                "Generate rich, scaffolding-heavy content suitable for the "
                "slowest learner in a mixed-ability class. The tutoring "
                "engine decides at runtime which format / hint depth / pace "
                "to use per student — content must offer enough variety for "
                "that adaptation to be possible."
            ),
            'min_figures': max(4, max_steps - 2),
            'figure_required_step_types': ('engage', 'teach', 'worked_example'),
            'min_multiple_choice': 2,
            'min_short_numeric': 1 if is_math else 0,
            'min_true_false': 1,
            'max_free_text': 1,
            'teach_sentence_max': 3,
            'hint_count': 3,
        }

    def _validate_against_profile(self, steps: List[Dict], rules: Dict) -> List[str]:
        """Check that generated steps meet the profile floors. Returns
        a list of human-readable issues; empty list = valid.

        Caller policy: 1 retry with these issues injected as a
        correction prompt; accept-and-warn if the retry still fails.
        """
        issues: List[str] = []

        # Count figures across steps. media may be None or {'images': [...]}.
        figure_count = 0
        steps_missing_required_figure: List[int] = []
        for step in steps:
            media = step.get('media') or {}
            images = media.get('images') if isinstance(media, dict) else None
            n = len(images or [])
            figure_count += n
            if step.get('step_type') in rules.get('figure_required_step_types', ()) and n == 0:
                steps_missing_required_figure.append(step.get('order_index', -1))

        if figure_count < rules['min_figures']:
            issues.append(
                f"Lesson has {figure_count} figures total; "
                f"need at least {rules['min_figures']} for scaffolding-heavy content."
            )
        if steps_missing_required_figure:
            issues.append(
                f"Steps {steps_missing_required_figure} are required step "
                f"types ({list(rules['figure_required_step_types'])}) but have no media. "
                f"Add a figure to each."
            )

        # Count answer_type distribution across practice/quiz steps.
        format_counts = {'multiple_choice': 0, 'short_numeric': 0, 'true_false': 0, 'free_text': 0}
        for step in steps:
            if step.get('step_type') in ('practice', 'quiz'):
                at = (step.get('answer_type') or '').lower()
                # Apply the same alias normalisation as _save_steps_to_db
                # so validation matches what we'd persist.
                aliases = {'short_text': 'free_text', 'free_response': 'free_text',
                           'open_ended': 'free_text', 'mcq': 'multiple_choice',
                           'tf': 'true_false', 'numeric': 'short_numeric'}
                at = aliases.get(at, at)
                if at in format_counts:
                    format_counts[at] += 1

        if format_counts['multiple_choice'] < rules['min_multiple_choice']:
            issues.append(
                f"Found {format_counts['multiple_choice']} multiple_choice questions; "
                f"need at least {rules['min_multiple_choice']}. Convert at least one practice "
                f"step to answer_type='multiple_choice' with a choices list of 4 options."
            )
        if format_counts['short_numeric'] < rules['min_short_numeric']:
            issues.append(
                f"Found {format_counts['short_numeric']} short_numeric questions; "
                f"need at least {rules['min_short_numeric']}. Convert at least one calculation "
                f"step to answer_type='short_numeric'."
            )
        if format_counts['true_false'] < rules['min_true_false']:
            issues.append(
                f"Found {format_counts['true_false']} true_false questions; "
                f"need at least {rules['min_true_false']}. Convert at least one practice step "
                f"to answer_type='true_false' (expected_answer must be exactly 'True' or 'False')."
            )
        if format_counts['free_text'] > rules['max_free_text']:
            issues.append(
                f"Found {format_counts['free_text']} free_text questions; "
                f"at most {rules['max_free_text']} allowed (the tutor engine "
                f"will offer free-text variants of recognition questions to "
                f"advanced students at runtime). Convert excess free_text "
                f"questions to multiple_choice or true_false."
            )

        # Per-step structural checks for question formats.
        for step in steps:
            at = (step.get('answer_type') or '').lower()
            idx = step.get('order_index')
            if at == 'multiple_choice':
                choices = step.get('choices') or []
                if len(choices) < 2:
                    issues.append(
                        f"Step {idx} is multiple_choice but choices list is empty/too short. "
                        f"Provide 4 distractors."
                    )
                expected = (step.get('expected_answer') or '').strip().upper()
                if expected and expected not in ('A', 'B', 'C', 'D'):
                    issues.append(
                        f"Step {idx} is multiple_choice but expected_answer '{step.get('expected_answer')}' "
                        f"is not a single letter A/B/C/D."
                    )
            elif at == 'true_false':
                expected = (step.get('expected_answer') or '').strip().lower()
                if expected and expected not in ('true', 'false'):
                    issues.append(
                        f"Step {idx} is true_false but expected_answer '{step.get('expected_answer')}' "
                        f"is not exactly 'True' or 'False'."
                    )

        return issues

    def _generate_steps(self, lesson, curriculum_context: Dict) -> Dict:
        """Generate lesson steps using instructor for guaranteed structured output."""

        from apps.curriculum.utils import format_grade_display
        unit = lesson.unit
        course = unit.course
        subject = curriculum_context.get('subject', 'General')
        grade = format_grade_display(curriculum_context.get('grade_level', ''))

        # Build context string
        strategies_str = "\n".join(f"- {s}" for s in curriculum_context.get('teaching_strategies', [])[:5])

        # Build knowledge base reference material
        related_content = curriculum_context.get('related_content', [])
        kb_context_str = ""
        if related_content:
            kb_chunks = "\n\n".join(f"--- Excerpt {i+1} ---\n{chunk}" for i, chunk in enumerate(related_content) if chunk.strip())
            if kb_chunks:
                kb_context_str = f"""
REFERENCE MATERIAL FROM TEXTBOOKS AND TEACHING RESOURCES:
Use the following excerpts from uploaded textbooks/teaching materials to ground the lesson content
in what students are actually studying. Align terminology, examples, and depth of coverage accordingly.

{kb_chunks}
"""

        # Build figure descriptions section
        figure_descriptions = curriculum_context.get('figure_descriptions', [])
        figures_str = ""
        if figure_descriptions:
            fig_lines = []
            for fig in figure_descriptions:
                fig_lines.append(
                    f"- [{fig.get('figure_type', 'figure').upper()}] "
                    f"{fig.get('figure_number', 'unlabeled')}: "
                    f"{fig.get('description', '')}"
                )
            figures_str = f"""
TEXTBOOK FIGURES AVAILABLE:
The following figures exist in the uploaded textbook/teaching materials. Where relevant,
base your media descriptions on these figures so generated images match the textbook style.

{chr(10).join(fig_lines)}
"""

        # Build the unified "teaching steps" objectives list (unit's
        # terminal objectives + lesson's enabling objectives, deduped).
        # See `combined_objectives_for_lesson` for the rationale.
        teaching_steps_objectives = combined_objectives_for_lesson(lesson)
        enabling_obj_str = ""
        if teaching_steps_objectives:
            eo_lines = "\n".join(
                f"  TS{i+1}: {obj}" for i, obj in enumerate(teaching_steps_objectives)
            )
            enabling_obj_str = f"""
TEACHING OBJECTIVE(S) (each MUST be covered — every one is a discrete
skill, knowledge, or attitude the student needs to practice intensely):
{eo_lines}
Every teaching objective must be covered by at least one tutoring step.
Set each step's terminal_objective field to the exact text of the
teaching objective it works toward.
Set each step's enabling_objective field to the specific sub-skill or
behaviour the step targets.
For a 20-minute lesson there is typically ONE teaching objective; the
4 tutoring steps drill that single objective intensely (warm-up →
instruction → guided practice → check for understanding).
"""

        # Build teaching steps section (granular enabling objectives from syllabus)
        teaching_steps = (lesson.metadata or {}).get('teaching_steps', [])
        teaching_steps_str = ""
        if teaching_steps:
            ts_lines = "\n".join(f"  - {ts}" for ts in teaching_steps)
            teaching_steps_str = f"""
TEACHING STEPS (from syllabus — use these to guide HOW you teach each objective):
{ts_lines}
These are the granular steps from the curriculum. Use them to structure your
explanation, examples, and practice for each terminal objective above.
"""

        # Build Seychelles context library section
        seychelles_str = ""
        try:
            from apps.curriculum.models import SeychellesContext
            sey_entries = SeychellesContext.objects.filter(is_active=True)
            if subject:
                sey_entries = sey_entries.filter(subject_tags__contains=subject.lower())
            sey_entries = list(sey_entries.values('category', 'title', 'content')[:12])
            if sey_entries:
                sey_lines = "\n".join(
                    f"- [{e['category'].upper()}] {e['title']}: {e['content']}"
                    for e in sey_entries
                )
                seychelles_str = f"""
SEYCHELLES CONTEXT LIBRARY (use these real facts, do NOT invent Seychelles data):
{sey_lines}
"""
        except Exception as e:
            logger.warning(f"Failed to load Seychelles context: {e}")

        # MAX-DEPTH GENERATION (2026-04-29): always generate the
        # deepest version of the lesson (10 steps). The tutoring
        # engine selects the right subset at session start based on
        # the target duration, using the per-step `priority` field —
        # so generation is duration-agnostic. See
        # memory/max_depth_lesson_steps_plan.md.
        max_steps = 10
        # 1 teaching objective per lesson, regardless of step count.
        max_eos = 1
        is_math = bool(lesson.unit and lesson.unit.course and lesson.unit.course.is_math)

        # Always-rich content rules. See
        # memory/course_regeneration_for_slow_learners.md — generation
        # is one-size-fits-the-slowest-learner; the tutoring engine
        # adapts presentation per student at runtime.
        profile_rules = self._profile_rules(max_steps, is_math)
        profile_str = f"""
CONTENT SCAFFOLDING — generate rich content that supports the slowest learner.
{profile_rules['description']}

VISUAL SCAFFOLDING RULES:
- Generate at least {profile_rules['min_figures']} StepMedia images across the lesson.
- Every step of type {list(profile_rules['figure_required_step_types'])} MUST include a media figure.
- Figures must illustrate the concept directly (diagram, schematic, labelled example), not decorative.
- Caption every figure with the key takeaway in 1 short sentence.

QUESTION FORMAT MIX (across practice + quiz steps):
- At least {profile_rules['min_multiple_choice']} multiple_choice question(s). choices is REQUIRED (4 options); expected_answer is exactly the LETTER A/B/C/D.
- At least {profile_rules['min_short_numeric']} short_numeric question(s). expected_answer is the number{' (math: include unit)' if is_math else ''}.
- At least {profile_rules['min_true_false']} true_false question(s). The question must be a single declarative statement; expected_answer is exactly 'True' or 'False'; choices is null.
- At most {profile_rules['max_free_text']} free_text question(s). The tutor engine will offer free-text variants to advanced students at runtime, so don't default to free_text in the source content.
- Every practice or quiz step's answer_type MUST be one of: multiple_choice, short_numeric, true_false, free_text. Do not use 'short_text' or 'free_response' — those are not valid.

TEACHING DEPTH RULES:
- Each TEACH step: at most {profile_rules['teach_sentence_max']} sentences. One concrete example, one analogy.
- Each practice or quiz step: provide exactly {profile_rules['hint_count']} hints, scaffolding general → specific.
"""

        # Build step structures for math vs non-math. The last step is
        # ALWAYS a QUIZ — the rest fill the middle. Order:
        #   Math:     ENGAGE → TEACH → WORKED_EXAMPLE → PRACTICE…+ → QUIZ
        #   Non-math: ENGAGE → TEACH → EXPLORE → PRACTICE…+ → QUIZ
        # For longer lessons we sprinkle a "common mistakes" or
        # "extend" step in the middle.
        if is_math:
            opening = [
                "ENGAGE — real-world Seychelles hook + state today's question. 2 sentences.",
                "TEACH — direct instruction of the concept. 4-6 sentences.",
                "WORKED_EXAMPLE — solve a complete problem showing EVERY line of working.",
            ]
            middle_pool = [
                "PRACTICE — DIFFERENT problem (not the same numbers); question + expected_answer + hints.",
                "PRACTICE — slightly harder DIFFERENT problem; question + expected_answer + hints.",
                "TEACH — call out one common mistake or misconception. 3-5 sentences.",
                "PRACTICE — applied / multi-step problem; question + expected_answer + hints.",
                "PRACTICE — reverse / inverse problem; question + expected_answer + hints.",
                "SYNTHESIZE — student explains the rule in their own words.",
            ]
        else:
            opening = [
                "ENGAGE — real-world Seychelles hook + the lesson question. 2-3 sentences.",
                "TEACH — direct instruction. 4-6 sentences — minimum effective dose.",
                "EXPLORE — student answers a guided question that surfaces the key idea.",
            ]
            middle_pool = [
                "PRACTICE — student applies the idea to a fresh case; question + expected_answer + hints.",
                "TEACH — extend or contextualise (link to prior learning). 4-6 sentences.",
                "PRACTICE — varied scenario, same objective; question + expected_answer + hints.",
                "SYNTHESIZE — student explains in their own words.",
                "PRACTICE — applied / cross-context problem; question + expected_answer + hints.",
                "EXTEND — connect to a real Seychelles situation.",
            ]
        closer = "QUIZ — short evaluation that this objective is mastered."

        # Total = opening (3) + middles + closer (1). The floor of
        # max_steps=4 ensures chosen always has >= 4 items with the
        # quiz at the end.
        middle_count = max(0, max_steps - len(opening) - 1)
        middle_count = min(middle_count, len(middle_pool))
        chosen = opening + middle_pool[:middle_count] + [closer]
        structure = "\n".join(f"Step {i+1} {line}" for i, line in enumerate(chosen))

        # Prompt focuses on CONTENT, not FORMAT — instructor handles the schema
        prompt = f"""Create a complete tutoring session for this lesson.

LESSON: {lesson.title}
OBJECTIVE: {lesson.objective or 'Master the concepts in this lesson'}
UNIT: {unit.title}
SUBJECT: {subject}
GRADE: {grade} (Seychelles secondary school)

TEACHING STRATEGIES TO USE:
{strategies_str}
{kb_context_str}{figures_str}{enabling_obj_str}{teaching_steps_str}{seychelles_str}{profile_str}
Create EXACTLY {max_steps} steps — the FULL DEPTH version of this lesson (~50 minutes if every step were used).
The tutoring engine will SELECT a subset of these steps at session start based on the student's available time, using the per-step `priority` field — so:
  • A 15-minute session uses 3 steps (priority 1 only).
  • A 20-minute session uses 4 steps.
  • A 30-minute session uses 6 steps (priority 1 + 2).
  • A 50-minute session uses all 10 steps.
This means you should produce a coherent FULL lesson, but tag each step's priority so the engine can drop enrichment steps gracefully.

PRIORITY ASSIGNMENT (mandatory per step):
- priority 1 (REQUIRED) — must always run. Reserve for: the FIRST engage step, ONE primary teach step, ONE primary practice step, the FINAL quiz/evaluate step. Aim for ~4 priority-1 steps total.
- priority 2 (CORE) — included by default; first to drop on tight time. Worked example, second practice, synthesise step. Aim for ~3 priority-2 steps total.
- priority 3 (ENRICHMENT) — third+ practice variants, common-mistakes drill, "extend" / Seychelles-context bonus. Aim for ~3 priority-3 steps total.

Drilling ONE teaching objective intensely. Every step progresses up the DOK levels (recall → skill → strategic) for that ONE objective. DO NOT introduce additional objectives — depth, not breadth.

{"MATH LESSON STRUCTURE — exactly " + str(max_steps) + " steps:" if is_math else "LESSON STRUCTURE — exactly " + str(max_steps) + " steps:"}
{structure}

{"MATH DEPTH: worked_example shows every step. Practice/quiz problems use DIFFERENT numbers from the worked example." if lesson.unit.course.is_math else "KEEP TEACH STEPS SHORT: 4-6 sentences max, never a lecture. The student must do most of the talking."}

enabling_objective RULES:
- Set each step's enabling_objective field to the EXACT TEXT of the enabling objective it covers
- ALL steps teaching/practicing the SAME EO share the SAME enabling_objective text
- ENGAGE and QUIZ/EVALUATE steps use "" (empty string)
- The flow MUST end with a practice or quiz step

STEP TYPES:
- teach: Direct instruction (tutor explains)
- worked_example: Step-by-step problem solving
- practice: Student attempts a problem (MUST have question + expected_answer + hints)
- quiz: Assessment question

{"MATH-SPECIFIC CONTENT GUIDELINES:" if lesson.unit.course.is_math else ""}
{'''For this MATHEMATICS lesson:
1. WORKED EXAMPLES are essential — every teach step MUST include a fully worked problem
   with numbered sub-steps showing EVERY line of working (not just the answer)
2. Practice questions must be CALCULATION-BASED — students must show working, not just pick answers
3. Include COMMON MISTAKES for each concept:
   - What students typically get wrong and WHY
   - Example: "Common mistake: 3 + 4 × 2 = 14 (wrong — they added before multiplying)"
4. VARY the practice problems — same concept, different numbers and contexts:
   - Straightforward calculation → word problem → reverse/inverse problem
5. Use Seychelles context in word problems (SCR currency, local shop prices, island distances)
6. For algebra: show substitution step-by-step, line by line
7. For geometry: describe diagrams precisely (label sides, angles, units)
8. Hints should guide the METHOD not give the answer:
   - "What operation comes first in BIDMAS?"
   - "Try drawing a diagram and labelling what you know"
   - NOT "The answer is 35"
9. Expected answers for practice MUST include the working steps, not just final answer
   Example: expected_answer: "20 + 5 × 3 = 20 + 15 = 35 (multiply before adding)"
''' if lesson.unit.course.is_math else ''}
CONTENT GUIDELINES:
1. Use Seychelles context where natural (SCR currency, local places, local examples)
2. Media descriptions MUST be specific for accurate image generation:
   - For maps: specify "schematic map" not "satellite view"
   - For diagrams: specify exactly what should be labelled
   - NEVER request images of real places as "photos"
   - Example GOOD: "Schematic cross-section showing three layers of Earth with labels"
   - Example BAD: "Image of Earth's layers"
3. Hints should scaffold from general to specific
4. For MCQ, make distractors plausible but clearly wrong.
   DISTRACTOR SAFETY RULES (hard requirements — questions that violate
   any of these MUST be rewritten before submission):
     - NEVER reference race, ethnicity, nationality, gender, religion,
       caste, or any protected class in a distractor's rationale.
       Bad: "Asians are naturally more intelligent". "Africa has no
       resources". "Women are bad at math". Reject and rewrite.
     - Distractors must come from concept-plausible mistakes — wrong
       formula application, off-by-one, sign error, common
       misconception, swapped variables. Not stereotypes.
     - No "all of the above" / "none of the above" filler. Every
       option must be a substantive answer the student could pick.
     - Don't make the correct answer obviously longer or differently
       formatted than distractors — that gives the answer away.
5. PROACTIVELY ATTACH AND USE FIGURES. For any step teaching a VISUAL
   concept — geometric shapes, angle relationships, maps, charts,
   diagrams, cross-sections, processes with multiple parts — attach a
   figure via the `media` field. Then anchor the teacher_script in
   that figure: "Look at the diagram below — you can see two
   parallel lines, l and m, cut by a transversal t." The figure is
   the working surface; the script should treat it as already visible.
6. NEVER ASK THE STUDENT TO IMAGINE OR PICTURE GEOMETRY. Banned
   phrasings: "imagine two parallel lines", "picture this", "envision
   a triangle", "think of a diagram where...", "in your head, draw...".
   If the concept requires visualisation, attach a figure (rule 5)
   and reference it. If a figure cannot be attached, describe the
   geometry concretely with named labels — never with imagination prompts.
7. Steps with NO media should NOT have media references in the script.
   But for visual concepts, the absence of media is itself a bug —
   reconsider whether a figure should be attached.
8. Questions must be CLEAR and UNAMBIGUOUS — the student should know exactly what is
   being asked and what form the answer should take
9. Practice/quiz questions should require APPLICATION or CONCEPTUAL understanding,
   not just recall or looking through text. Ask "why", "how", "what would happen if",
   "compare", "explain" rather than "what is" or "which one"
10. NEVER write teacher_script that says "which of these", "which of the following",
   or references a list of options without actually listing them. Either list the
   options explicitly or phrase the question as open-ended
11. The expected_answer MUST directly and completely answer the question as phrased.
   If the question asks "which is smallest", the expected_answer must name the smallest
   item, not just explain a comparison method"""

        from apps.llm.prompts import get_prompt_or_default
        from apps.curriculum.dok_framework import dok_guidance_for
        system_prompt = get_prompt_or_default(
            self.institution_id, 'content_generation_prompt',
            "You are an expert curriculum designer creating engaging tutoring content for Seychelles secondary students.",
        )
        # Webb's DOK rubric — anchors the cognitive demand of each
        # teaching step. See apps/curriculum/dok_framework.py.
        system_prompt = system_prompt + "\n\n" + dok_guidance_for("content")

        print(f"[ContentGen] [{lesson.title}] Calling instructor ({self._model_config.provider}/{self._model_config.model_name})...", flush=True)

        def _call_llm(user_prompt: str):
            """Single instructor call returning (steps_list, summary_dict).
            Raises on parse failure — caller decides whether to retry."""
            create_kwargs = dict(
                response_model=GeneratedLessonContent,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_retries=3,
            )
            if self._model_config.provider == 'google':
                create_kwargs['generation_config'] = {'max_tokens': 16384}
            else:
                create_kwargs['max_tokens'] = 16384
            r = self.client.chat.completions.create(**create_kwargs)
            return (
                [s.model_dump() for s in r.steps],
                r.lesson_summary.model_dump() if r.lesson_summary else {},
            )

        t0 = time.time()
        try:
            steps, summary = _call_llm(prompt)
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[ContentGen] [{lesson.title}] ❌ Failed after {elapsed:.1f}s: {e}", flush=True)
            logger.error(f"[{lesson.title}] Instructor generation failed after {elapsed:.1f}s: {e}")
            import traceback
            traceback.print_exc()
            return {'success': False, 'error': str(e)}

        # Profile validation — one retry if the lesson missed the
        # figure floor or format mix. Accept-and-warn if the second
        # pass still falls short (better than stuck 'generating').
        issues = self._validate_against_profile(steps, profile_rules)
        if issues:
            print(f"[ContentGen] [{lesson.title}] profile retry — issues: {issues}", flush=True)
            correction = (
                "Your previous response did not meet the LEARNER PROFILE requirements.\n"
                "Issues to fix:\n" + "\n".join(f"- {i}" for i in issues) +
                "\n\nRegenerate the entire lesson, fixing every issue while keeping the "
                "educational content and Seychelles context intact. Honour every rule in "
                "the LEARNER PROFILE, VISUAL SCAFFOLDING RULES, and QUESTION FORMAT MIX "
                "sections of the original brief. Original brief follows:\n\n" + prompt
            )
            try:
                steps, summary = _call_llm(correction)
                remaining = self._validate_against_profile(steps, profile_rules)
                if remaining:
                    logger.warning(
                        f"[ContentGen] [{lesson.title}] profile retry still has issues: {remaining}"
                    )
                    print(f"[ContentGen] [{lesson.title}] ⚠️ accepting degraded content — issues remain: {remaining}", flush=True)
                else:
                    print(f"[ContentGen] [{lesson.title}] ✅ profile retry succeeded", flush=True)
            except Exception as e:
                logger.warning(f"[{lesson.title}] profile retry crashed: {e} — keeping original")
                print(f"[ContentGen] [{lesson.title}] ⚠️ profile retry crashed: {e} — keeping original output", flush=True)

        # Layer 3 — arithmetic verify-then-retry. Math lessons only.
        # Run Layer 1 over the (possibly profile-retried) steps. If
        # unresolved detect-only entries remain, retry the LLM call
        # once with a constraint block listing the wrong claims and
        # their correct values. Cap at one retry (latency + cost).
        arith_audit: List[Dict] = []
        arith_retry_count = 0
        arith_unresolved = False
        if is_math:
            from apps.curriculum.content_verifier import (
                build_arithmetic_constraint_block,
                has_unresolved_corrections,
                verify_lesson_step,
            )
            for step in steps:
                verify_lesson_step(step, audit=arith_audit)
            if has_unresolved_corrections(arith_audit):
                print(
                    f"[ContentGen] [{lesson.title}] Layer 3 arithmetic "
                    f"retry — {len(arith_audit)} corrections "
                    f"(detect-only present)",
                    flush=True,
                )
                constraint = build_arithmetic_constraint_block(
                    step_audit=arith_audit,
                )
                try:
                    steps, summary = _call_llm(constraint + "\n\n" + prompt)
                    arith_retry_count = 1
                    # Re-verify with a fresh audit list so we report
                    # only the post-retry state.
                    arith_audit = []
                    for step in steps:
                        verify_lesson_step(step, audit=arith_audit)
                    arith_unresolved = has_unresolved_corrections(arith_audit)
                    if arith_unresolved:
                        print(
                            f"[ContentGen] [{lesson.title}] ⚠️ Layer 3 "
                            f"retry still has unresolved arithmetic — "
                            f"will mark READY_WITH_WARNINGS",
                            flush=True,
                        )
                    else:
                        print(
                            f"[ContentGen] [{lesson.title}] ✅ Layer 3 "
                            f"retry resolved all arithmetic issues",
                            flush=True,
                        )
                except Exception as e:
                    logger.warning(
                        f"[{lesson.title}] arithmetic retry crashed: "
                        f"{e} — keeping original"
                    )
                    print(
                        f"[ContentGen] [{lesson.title}] ⚠️ arithmetic "
                        f"retry crashed: {e} — keeping original output",
                        flush=True,
                    )
                    arith_unresolved = True

        elapsed = time.time() - t0
        print(f"[ContentGen] [{lesson.title}] ✅ {len(steps)} steps generated in {elapsed:.1f}s", flush=True)
        logger.info(f"[{lesson.title}] {len(steps)} steps generated in {elapsed:.1f}s")

        return {
            'success': True,
            'steps': steps,
            'lesson_summary': summary,
            'arithmetic_audit': arith_audit,
            'arithmetic_retry_count': arith_retry_count,
            'arithmetic_unresolved': arith_unresolved,
        }

    def _save_steps_to_db(
        self,
        lesson,
        steps: List[Dict],
        *,
        arith_retry_count: int = 0,
        arith_unresolved: bool = False,
    ):
        """Save generated steps to database.

        Mirrors the budget in _generate_steps so the LLM's output is
        capped to the same ceiling. After upserting the new steps,
        deletes any leftover LessonStep rows with order_index >=
        len(steps) — without this, regenerating a lesson with FEWER
        steps would leave the old tail behind in the DB.

        Layer 1 hook (defensive — idempotent with the same check
        that runs inside `_generate_steps`): when the course is_math,
        every step's verifiable text fields run through
        `verify_calculations` before persistence. Post-Layer-3 the
        audit is usually empty here (retry already fixed things);
        we still record what we see so the metadata is the source
        of truth even if upstream skipped.

        Layer 3 hook: caller passes `arith_retry_count` and
        `arith_unresolved` from `_generate_steps`. We persist the
        retry count to metadata and set
        `content_status=READY_WITH_WARNINGS` when issues remain.
        """
        from apps.curriculum.models import LessonStep
        from apps.curriculum.content_verifier import verify_lesson_step

        # Always-10 max-depth ceiling. The tutor engine selects the
        # right subset at session start; storing all 10 keeps duration
        # changes free of LLM regeneration.
        MAX_STEPS = 10
        if len(steps) > MAX_STEPS:
            logger.warning(
                f"[ContentGen] [{lesson.title}] Trimming {len(steps)} steps to {MAX_STEPS}"
            )
            steps = steps[:MAX_STEPS]

        # Layer 1 — math arithmetic verification on generated content.
        # Math-only gate to avoid false-positive matches on geography
        # prose like "Seychelles has 115 islands".
        try:
            is_math = lesson.unit.course.is_math
        except Exception:
            is_math = False
        step_audit: List[Dict] = []
        if is_math:
            for step_data in steps:
                verify_lesson_step(step_data, audit=step_audit)
            if step_audit:
                fixed = sum(
                    1 for e in step_audit if e.get("auto_corrected")
                )
                detected = len(step_audit) - fixed
                print(
                    f"[ContentGen] [{lesson.title}] Layer 1: {fixed} "
                    f"auto-corrected, {detected} detect-only "
                    f"(answer-key-bound)",
                    flush=True,
                )

        valid_answer_types = set(LessonStep.AnswerType.values)
        # Map common LLM mis-emissions to valid LessonStep.AnswerType
        # values. The schema docstring lists these but providers
        # occasionally drift; normalise instead of failing the save.
        answer_type_aliases = {
            'short_text': 'free_text',
            'free_response': 'free_text',
            'open_ended': 'free_text',
            'mcq': 'multiple_choice',
            'tf': 'true_false',
            'numeric': 'short_numeric',
        }

        def _normalise_answer_type(raw):
            value = (raw or 'none').strip().lower()
            value = answer_type_aliases.get(value, value)
            if value not in valid_answer_types:
                logger.warning(
                    f"[ContentGen] [{lesson.title}] Unknown answer_type '{raw}' "
                    f"on step {step_data.get('order_index')} — coercing to 'free_text'"
                )
                return 'free_text'
            return value

        def _clamp_priority(raw):
            """Clamp LLM-emitted priority to {1, 2, 3}. Default to 1
            (REQUIRED) on missing / garbage so a misformed step still
            shows up in every session rather than silently disappearing.
            """
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return 1
            if value < 1 or value > 3:
                return 1
            return value

        # Snap each step's enabling_objective to one of the lesson's
        # canonical EOs. Same drift problem as exit-ticket questions —
        # the LLM is told to copy lesson EOs verbatim but paraphrases.
        # Mismatches drop the tag (clearing it), keeping the step.
        canonical_eos_for_steps = list(lesson.enabling_objectives or [])
        step_eo_stats = {'exact': 0, 'snapped': 0, 'dropped': 0, 'empty': 0}
        step_eo_dropped_examples: List[Dict] = []
        # LLM-snap client for EO normalization. Without this the
        # function falls into a brittle substring heuristic that
        # silently drops most LLM-emitted EOs (Martin's pilot showed
        # 1/10 steps tagged on "Angles around a point"). Build once
        # per lesson, reuse across all steps in the loop.
        eo_snap_client = None
        try:
            from apps.llm.client import get_llm_client
            from apps.llm.models import ModelConfig
            _cfg = ModelConfig.get_for('generation') or self._model_config
            if _cfg is not None:
                eo_snap_client = get_llm_client(_cfg)
        except Exception as e:
            print(
                f"[ContentGen] [{lesson.title}] EO-snap client unavailable "
                f"(falling back to substring heuristic): {e}",
                flush=True,
            )

        for step_data in steps:
            raw_eo = step_data.get('enabling_objective', '') or ''
            normalised, status = _normalize_enabling_objective(
                raw_eo, canonical_eos_for_steps,
                llm_client=eo_snap_client,
            )
            step_eo_stats[status] = step_eo_stats.get(status, 0) + 1
            step_data['enabling_objective'] = normalised
            if status == 'dropped' and len(step_eo_dropped_examples) < 5:
                step_eo_dropped_examples.append({
                    'order_index': step_data.get('order_index', 0),
                    'step_type': step_data.get('step_type', ''),
                    'rejected_eo': raw_eo[:160],
                })
        print(
            f"[ContentGen] [{lesson.title}] step enabling_objective "
            f"validation: exact={step_eo_stats['exact']}, "
            f"snapped={step_eo_stats['snapped']}, "
            f"dropped={step_eo_stats['dropped']}, "
            f"empty={step_eo_stats['empty']}",
            flush=True,
        )

        for step_data in steps:
            step, created = LessonStep.objects.update_or_create(
                lesson=lesson,
                order_index=step_data.get('order_index', 0),
                defaults={
                    'phase': step_data.get('phase', ''),
                    'step_type': step_data.get('step_type', 'teach'),
                    'concept_tag': step_data.get('concept_tag', ''),
                    'enabling_objective': step_data.get('enabling_objective', ''),
                    'curriculum_context': {
                        **(step_data.get('curriculum_context') or {}),
                        'terminal_objective': step_data.get('terminal_objective', ''),
                    },
                    'teacher_script': step_data.get('teacher_script', ''),
                    'question': step_data.get('question') or '',
                    'answer_type': _normalise_answer_type(step_data.get('answer_type')),
                    'expected_answer': step_data.get('expected_answer') or '',
                    'choices': step_data.get('choices'),
                    'hint_1': (step_data.get('hints') or [''])[0] if step_data.get('hints') else '',
                    'hint_2': (step_data.get('hints') or ['', ''])[1] if len(step_data.get('hints') or []) > 1 else '',
                    'hint_3': (step_data.get('hints') or ['', '', ''])[2] if len(step_data.get('hints') or []) > 2 else '',
                    'media': step_data.get('media'),
                    'educational_content': step_data.get('educational_content'),
                    'curriculum_context': step_data.get('curriculum_context'),
                    # Clamp to {1,2,3}; default to 1 (REQUIRED) if the
                    # LLM omits or sends garbage so the step still
                    # makes it into every session.
                    'priority': _clamp_priority(step_data.get('priority')),
                }
            )

            logger.debug(f"{'Created' if created else 'Updated'} step {step.order_index}: {step.step_type}")

        # Drop orphan tail steps from a previous (longer) generation —
        # otherwise regenerating a 6-step lesson into 4 steps leaves
        # the old step 5 + 6 sitting in the DB.
        kept_indices = {s.get('order_index', 0) for s in steps}
        orphan_qs = LessonStep.objects.filter(lesson=lesson).exclude(
            order_index__in=kept_indices,
        )
        orphan_count = orphan_qs.count()
        if orphan_count:
            logger.info(
                f"[ContentGen] [{lesson.title}] Removing {orphan_count} orphan steps from prior generation"
            )
            orphan_qs.delete()

        # Layers 1 + 3 — persist the per-lesson verification audit
        # to lesson.metadata. Subsequent layers (A2 exit-ticket
        # verifier, Layer 2 cross-check) extend the same blob with
        # sibling keys.
        if is_math:
            from django.utils import timezone
            from apps.curriculum.models import Lesson
            existing = lesson.metadata or {}
            audit_blob = existing.get('verification_audit') or {}
            audit_blob['generated_at'] = timezone.now().isoformat()
            audit_blob['math_check_run'] = True
            audit_blob['step_corrections'] = step_audit
            audit_blob['retry_count'] = arith_retry_count
            audit_blob['step_enabling_objective_validation'] = {
                **step_eo_stats,
                'dropped_examples': step_eo_dropped_examples,
            }
            existing['verification_audit'] = audit_blob
            lesson.metadata = existing
            update_fields = ['metadata']
            if arith_unresolved:
                lesson.content_status = (
                    Lesson.ContentStatus.READY_WITH_WARNINGS
                )
                update_fields.append('content_status')
                print(
                    f"[ContentGen] [{lesson.title}] content_status → "
                    f"READY_WITH_WARNINGS (Layer 3 retry could not "
                    f"resolve all arithmetic issues)",
                    flush=True,
                )
            lesson.save(update_fields=update_fields)
        else:
            # Non-math lessons still record EO validation
            from apps.curriculum.models import Lesson  # noqa: F401
            existing = lesson.metadata or {}
            audit_blob = existing.get('verification_audit') or {}
            audit_blob['step_enabling_objective_validation'] = {
                **step_eo_stats,
                'dropped_examples': step_eo_dropped_examples,
            }
            existing['verification_audit'] = audit_blob
            lesson.metadata = existing
            lesson.save(update_fields=['metadata'])

    def _extract_eo_skills(self, lesson):
        """Create Skill records from enabling objectives for SM-2 mastery tracking."""
        try:
            from apps.tutoring.skill_extraction import SkillExtractionService
            service = SkillExtractionService(self.institution_id)
            skills = service.create_skills_from_enabling_objectives(lesson)
            if skills:
                print(f"[ContentGen] [{lesson.title}] EO skills: {len(skills)} created", flush=True)
        except Exception as e:
            logger.warning(f"EO skill extraction failed for {lesson.title}: {e}")

    def _generate_exit_ticket(self, lesson) -> Dict:
        """Generate a mixed-format exit ticket for a lesson after content generation.

        Skip-if-exists by default — exit-ticket questions are stable
        assessment items, regenerating lesson STEPS doesn't require new
        questions. This keeps the ExitTicket row + its
        ExitTicketAttempt history alive across lesson regenerations.
        Teachers can trigger an explicit exit-ticket regeneration via
        a separate action if they ever want fresh questions.
        """
        try:
            result = generate_exit_ticket_for_lesson(lesson, self.institution_id)
            if result.get('success'):
                print(f"[ContentGen] [{lesson.title}] Exit ticket: {result['questions_created']} questions", flush=True)
            else:
                print(f"[ContentGen] [{lesson.title}] Exit ticket failed: {result.get('error')}", flush=True)
            return result
        except Exception as e:
            logger.warning(f"Exit ticket generation failed for {lesson.title}: {e}")
            return {'success': False, 'error': str(e)}


def generate_exit_ticket_for_lesson(lesson, institution_id: int = None, force_regenerate: bool = False) -> Dict:
    """
    Generate a mixed-format exit ticket question bank for a lesson.

    Standalone function usable from both the content pipeline and management commands.
    Generates 35 questions (20 MCQ + 5 fill-in-blank + 4 matching + 3 short-answer +
    3 data-interpretation) and saves them to the database.

    Args:
        force_regenerate: When True, REPLACE the existing ExitTicket's
            questions in place rather than deleting + recreating the
            ExitTicket row. This preserves any ExitTicketAttempt rows
            (and therefore the competency map / matrix data) across
            regenerations. Default False keeps the legacy
            "skip-if-exists" semantics for non-regen callers.
    """
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client
    from apps.tutoring.management.commands.generate_exit_tickets import EXIT_TICKET_PROMPT

    if institution_id is None:
        from apps.accounts.models import Institution
        institution_id = lesson.unit.course.institution_id or Institution.get_global().id

    # Skip if already has exit ticket with any questions — unless the
    # caller explicitly asked for a regen, in which case we replace
    # the questions but KEEP the ExitTicket row (so attempts survive).
    existing = ExitTicket.objects.filter(lesson=lesson).first()
    if existing and existing.questions.exists() and not force_regenerate:
        return {'success': True, 'skipped': True, 'questions_created': 0}

    # P1 (item 1) — EO attachment parity. If the lesson has no
    # enabling_objectives populated, run the same expansion the step
    # generation pipeline runs. Without this, the exit-ticket prompt
    # has no canonical EOs to copy from and the post-hoc normaliser
    # has nothing to snap against — every emission gets dropped and
    # remediation can only target the broad learning objective.
    # See memory/curriculum_tutor_v2_plan.md.
    eos_before = len(lesson.enabling_objectives or [])
    if eos_before == 0:
        try:
            generator = LessonContentGenerator(institution_id=institution_id)
            unit = lesson.unit
            course = unit.course if unit else None
            curriculum_context = {
                'subject': course.title if course else '',
                'grade_level': course.grade_level if course else '',
                'terminal_objectives': list(unit.terminal_objectives or []) if unit else [],
                'lesson_title': lesson.title,
                'lesson_objective': lesson.objective or '',
            }
            generator._expand_to_granular_subskills(lesson, curriculum_context)
            lesson.refresh_from_db()
            eos_after = len(lesson.enabling_objectives or [])
            print(
                f"[ContentGen] [{lesson.title}] EO attachment parity — "
                f"expanded {eos_before} → {eos_after} enabling objectives",
                flush=True,
            )
            if eos_after == 0:
                # Expansion ran but produced nothing usable — the LLM
                # returned <4 items, parse failed, or all were stripped
                # by the weak-verb filter. Without this warning the
                # bank silently generates with concept_tag only and
                # remediation can't target sub-skills.
                logger.warning(
                    f"[{lesson.title}] EO expansion ran but yielded 0 "
                    f"enabling objectives — exit-ticket questions will "
                    f"fall back to broad concepts for tagging."
                )
                print(
                    f"[ContentGen] [{lesson.title}] ⚠️ EO expansion "
                    f"yielded 0 sub-skills — falling back to broad "
                    f"concepts for question tagging",
                    flush=True,
                )
        except Exception as e:
            logger.warning(
                f"[{lesson.title}] EO expansion before exit-ticket regen "
                f"failed: {e}. Continuing — questions will be tagged with "
                f"concept_tag only."
            )

    # Get LLM client
    model_config = ModelConfig.get_for('exit_tickets') or ModelConfig.get_for('tutoring')
    if not model_config:
        return {'success': False, 'error': 'No LLM model configured'}
    llm_client = get_llm_client(model_config)

    subject = lesson.unit.course.title if lesson.unit and lesson.unit.course else "General"

    # KB context for grounding
    exam_context = ""
    try:
        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
        kb = CurriculumKnowledgeBase(institution_id=institution_id)
        exam_questions = kb.query_for_exit_ticket_generation(
            lesson_title=lesson.title,
            lesson_objective=lesson.objective or '',
            subject=subject,
            grade_level=lesson.unit.course.grade_level or '',
            n_results=5,
        )
        exam_context = kb.format_exam_questions_for_prompt(exam_questions)
        if exam_context:
            exam_context = "\n\n" + exam_context + "\n"
    except Exception as e:
        logger.warning(f"KB query failed for exit ticket: {e}")

    # Query vision-extracted worksheet/exam questions as format examples
    assessment_format_context = ""
    try:
        # Find worksheet and exam questions from KB that were vision-extracted
        worksheet_results = kb.query_for_content_generation(
            lesson_title=lesson.title,
            lesson_objective=lesson.objective or '',
            unit_title=lesson.unit.title if lesson.unit else '',
            subject=subject,
            grade_level=lesson.unit.course.grade_level or '',
            n_results=10,
        )
        if worksheet_results and worksheet_results.chunks:
            format_examples = []
            for chunk in worksheet_results.chunks[:8]:
                meta = chunk.get('metadata', {})
                chunk_type = meta.get('chunk_type', '')
                if chunk_type in ('vision_worksheet', 'vision_question_bank'):
                    extracted = meta.get('extracted_data', {})
                    if extracted:
                        q_type = extracted.get('question_type', '')
                        marks = extracted.get('marks', '')
                        cmd = extracted.get('command_word', '')
                        q_text = extracted.get('question_text', chunk.get('content', ''))[:200]
                        format_examples.append(
                            f"- [{q_type}] {cmd}: {q_text}" +
                            (f" ({marks} marks)" if marks else "")
                        )
            if format_examples:
                assessment_format_context = (
                    "\nREAL ASSESSMENT EXAMPLES FROM UPLOADED WORKSHEETS/EXAMS:\n"
                    "Use these as format models — match the question style, command words, and mark allocations:\n"
                    + "\n".join(format_examples)
                    + "\nGenerate exit ticket questions that match this style.\n"
                )
    except Exception:
        pass

    # Query figures from KB for data_interpretation questions
    figure_context = ""
    try:
        figures = kb.query_for_figure_descriptions(
            topic=f"{lesson.title} {lesson.objective or ''}",
            subject=subject,
            n_results=3,
            grade_level=lesson.unit.course.grade_level or '',
        )
        if figures:
            fig_lines = []
            for fig in figures:
                url = fig.get('image_url', '')
                desc = fig.get('description', '')
                if desc:
                    fig_lines.append(f"- [{fig.get('figure_type', 'figure').upper()}] {desc[:200]}" +
                                     (f" (image_url: {url})" if url else ""))
            if fig_lines:
                figure_context = (
                    "\nAVAILABLE FIGURES FROM TEXTBOOKS/WORKSHEETS:\n"
                    + "\n".join(fig_lines)
                    + "\nFor DATA_INTERPRETATION questions, use these figures in data_description as HTML:\n"
                    + "  <img src='IMAGE_URL' style='max-width:100%;margin:8px 0' alt='DESCRIPTION'>\n"
                    + "  followed by the question text.\n"
                    + "For other question types, reference the figure content in question text.\n"
                )
    except Exception:
        pass

    # Seychelles context
    seychelles_context = ""
    try:
        from apps.curriculum.models import SeychellesContext
        entries = SeychellesContext.objects.filter(is_active=True)
        # Filter by subject — use Python-level check for SQLite compat
        entries = [e for e in entries.values('title', 'content', 'subject_tags')
                   if subject.lower() in (e.get('subject_tags') or [])][:8]
        if entries:
            lines = "\n".join(f"- {e['title']}: {e['content']}" for e in entries)
            seychelles_context = f"\nSEYCHELLES CONTEXT (use these real facts):\n{lines}\n"
    except Exception:
        pass

    # Build the unified "teaching steps" objectives list — same merge
    # used for content generation: parent unit's terminal objectives +
    # lesson's enabling objectives, deduplicated. Each item is a
    # discrete skill/knowledge/attitude the assessment must drill.
    objectives_context = ""
    eo_list = combined_objectives_for_lesson(lesson)
    teaching_steps_meta = (lesson.metadata or {}).get('teaching_steps', [])
    # If we still have nothing (no TOs, no EOs), fall back to the
    # syllabus 'teaching_steps' metadata, then to objectives mined from
    # already-generated LessonSteps.
    if not eo_list and teaching_steps_meta:
        eo_list = list(teaching_steps_meta)
    if not eo_list:
        from apps.curriculum.models import LessonStep
        seen = set()
        for step in LessonStep.objects.filter(lesson=lesson).order_by('order_index'):
            eo = step.enabling_objective or ''
            if eo and eo not in seen:
                seen.add(eo)
                eo_list.append(eo)

    # Two distinct lists for the LLM:
    #   - concept_tag list   = the BROAD learning objective(s) for the lesson.
    #   - enabling_objective list = GRANULAR sub-skills (one per question).
    # Without this separation the LLM was conflating the two and the
    # post-hoc normaliser was dropping every emission. See P1 of
    # memory/curriculum_tutor_v2_plan.md.
    granular_eos: List[str] = list(lesson.enabling_objectives or [])

    # Granular fallbacks when expansion produced nothing — keep the
    # legacy fallback chain so step-tagged EOs and teaching_steps
    # metadata still surface. Same chain that used to feed `eo_list`.
    if not granular_eos and teaching_steps_meta:
        granular_eos = list(teaching_steps_meta)
    if not granular_eos:
        from apps.curriculum.models import LessonStep as _LessonStep
        seen_steps = set()
        for step in _LessonStep.objects.filter(lesson=lesson).order_by('order_index'):
            eo = step.enabling_objective or ''
            if eo and eo not in seen_steps:
                seen_steps.add(eo)
                granular_eos.append(eo)

    broad_concepts: List[str] = []
    if lesson.unit and getattr(lesson.unit, 'terminal_objectives', None):
        broad_concepts.extend(list(lesson.unit.terminal_objectives or []))
    if (lesson.objective or '').strip():
        broad_concepts.append(lesson.objective.strip())
    # Dedup broad list (preserve order)
    seen_b = set()
    broad_concepts = [
        c for c in broad_concepts
        if c and c.strip() and not (c.lower() in seen_b or seen_b.add(c.lower()))
    ]
    # If still nothing, fall back to lesson title so the field always
    # has SOMETHING to copy.
    if not broad_concepts:
        title = (lesson.title or '').strip()
        if title:
            broad_concepts = [title]

    # Final canonical EO list the validator will snap against. Prefer
    # granular sub-skills; if expansion failed and we only have broad
    # concepts, fall back to those so the validator doesn't drop every
    # tag.
    canonical_eos_for_prompt: List[str] = (
        granular_eos if granular_eos else list(broad_concepts)
    )

    # Loud trace — leave a breadcrumb so the next "questions are tagged
    # with the broad concept again" mystery has data to debug from.
    print(
        f"[ContentGen] [{lesson.title}] EO sources at exit-ticket time → "
        f"lesson.enabling_objectives={len(lesson.enabling_objectives or [])} "
        f"unit.terminal_objectives={len(getattr(lesson.unit, 'terminal_objectives', []) or []) if lesson.unit else 0} "
        f"granular_eos={len(granular_eos)} "
        f"broad_concepts={len(broad_concepts)} "
        f"canonical={canonical_eos_for_prompt[:3]!r}{'…' if len(canonical_eos_for_prompt) > 3 else ''}",
        flush=True,
    )

    if broad_concepts or canonical_eos_for_prompt:
        broad_lines = "\n".join(f"  • {c}" for c in broad_concepts) or "  (none)"
        eo_lines = "\n".join(
            f"  EO{i+1}: {eo}" for i, eo in enumerate(canonical_eos_for_prompt)
        ) or "  (none)"
        objectives_context += f"""
LEARNING OBJECTIVE — use as the `concept_tag` field on EVERY question
(broad lesson-level outcome, copy verbatim — do NOT invent short labels):
{broad_lines}

ENABLING OBJECTIVES — use as the `enabling_objective` field on EVERY
question (specific sub-skill, copy verbatim from EXACTLY ONE of these):
{eo_lines}

RULES:
1. `concept_tag` MUST exactly match one of the LEARNING OBJECTIVE lines.
2. `enabling_objective` MUST exactly match one of the EOn lines above —
   this is what remediation uses to target the failing sub-skill, so
   tagging accuracy matters.
3. Distribute questions across ALL enabling objectives so every sub-skill
   gets at least 1 question. Do NOT repeat the same EO for every question.
"""

    # Use math-specific or general exit ticket prompt.
    # ``str.format()`` failures here used to silently kill the regen
    # (any literal "{a}" in the prompt template — added as
    # documentation — would raise KeyError before the LLM call).
    # Wrap in try/except + loud log so the same trap doesn't strand
    # a regen again.
    is_math = lesson.unit.course.is_math if lesson.unit and lesson.unit.course else False
    fmt_kwargs = dict(
        lesson_title=lesson.title,
        lesson_objective=lesson.objective or '',
        subject=subject,
        exam_context=exam_context,
        seychelles_context=seychelles_context + objectives_context,
    )
    try:
        if is_math:
            from apps.tutoring.management.commands.generate_exit_tickets import MATH_EXIT_TICKET_PROMPT
            prompt = MATH_EXIT_TICKET_PROMPT.format(**fmt_kwargs)
        else:
            prompt = EXIT_TICKET_PROMPT.format(**fmt_kwargs)
        print(
            f"[ContentGen] [{lesson.title}] exit-ticket prompt built "
            f"({len(prompt)} chars, {'math' if is_math else 'general'})",
            flush=True,
        )
    except KeyError as e:
        # KeyError from .format() means the prompt template has a
        # stray single-brace placeholder. Re-raise with a clearer
        # message so the surrounding handler logs it as a real bug
        # (vs swallowing it as a generic 'a' message).
        msg = (
            f"exit-ticket prompt .format() failed on key {e!r} — likely "
            f"a documentation example with single-brace {{name}} that "
            f"should be {{{{name}}}}. Check the most recent edits to "
            f"MATH_EXIT_TICKET_PROMPT / EXIT_TICKET_PROMPT."
        )
        print(f"[ContentGen] [{lesson.title}] ❌ {msg}", flush=True)
        return {'success': False, 'error': msg}

    # Append real assessment format examples from worksheets/exams
    if assessment_format_context:
        prompt += "\n" + assessment_format_context
    # Append figure context if available
    if figure_context:
        prompt += "\n" + figure_context

    from apps.llm.prompts import get_prompt_or_default
    from apps.curriculum.dok_framework import dok_guidance_for
    exit_sys_prompt = get_prompt_or_default(
        institution_id, 'exit_ticket_prompt',
        "You are an expert educational assessment designer.",
        json_required=True,
    )
    # Webb's DOK rubric — every question targets a stated cognitive level.
    # See apps/curriculum/dok_framework.py.
    exit_sys_prompt = exit_sys_prompt + "\n\n" + dok_guidance_for("assessment")

    try:
        print(
            f"[ContentGen] [{lesson.title}] calling LLM for exit ticket "
            f"({llm_client.__class__.__name__})…",
            flush=True,
        )
        messages = [{"role": "user", "content": prompt}]
        response = llm_client.generate(messages, system_prompt=exit_sys_prompt, max_tokens=16000)

        from apps.llm.json_utils import parse_llm_json
        questions = parse_llm_json(response.content, expect_array=True)

        if not questions or not isinstance(questions, list) or len(questions) < 10:
            print(
                f"[ContentGen] [{lesson.title}] ❌ insufficient LLM "
                f"output: {len(questions) if questions else 0} questions "
                f"parsed",
                flush=True,
            )
            return {'success': False, 'error': f'Insufficient questions: {len(questions) if questions else 0}'}

        # Pre-Layer-4 type counts so the format-mix retry can attribute
        # the LLM's first-pass distribution.
        from collections import Counter
        first_pass_types = Counter(
            (q.get('question_type') or 'short_numeric').strip()
            for q in questions[:35]
        )
        print(
            f"[ContentGen] [{lesson.title}] LLM first-pass: {len(questions)} "
            f"questions, types={dict(first_pass_types)}",
            flush=True,
        )

        questions = questions[:35]

        # Layer 4 — render any parametric templates the LLM emitted.
        # Templated questions get their prose fields filled in by
        # `render_template`, with answers computed deterministically
        # in code. Failed renders (unsatisfiable constraints, bad
        # formula) fail validation and get dropped — the LLM is
        # given another chance via Layer 3 retry below.
        try:
            is_math_lesson_l4 = lesson.unit.course.is_math
        except Exception:
            is_math_lesson_l4 = False
        # Track per-question rejection reasons so Layer 3 retry can
        # tell the LLM exactly why each question was dropped.
        layer4_rejections: List[Dict] = []
        templated_count = 0
        if is_math_lesson_l4:
            from apps.curriculum.parametric_renderer import (
                parse_template,
                render_typed,
                validate_template_typed,
            )
            kept = []
            for i, q in enumerate(questions):
                tmpl_data = q.get('template')
                # MANDATORY in math: a question without a template
                # is rejected outright (per the prompt). Free-form
                # math has no escape hatch.
                if not tmpl_data:
                    layer4_rejections.append({
                        'question_index': i,
                        'reason': 'no_template',
                        'message': (
                            'Math question emitted without a `template` '
                            'object — every math question must be '
                            'parametric.'
                        ),
                        'question_text': (q.get('question') or q.get('question_text') or '')[:120],
                    })
                    continue

                # Resolve the template TYPE from the question's
                # question_type field. Defaults to 'short_numeric'
                # for backward compat with templates the LLM emits
                # without an explicit question_type.
                q_type = (q.get('question_type') or 'short_numeric').strip()

                # Schema validation (pydantic) via parse_template —
                # routes by question_type to the right model class.
                try:
                    tmpl = parse_template(q_type, tmpl_data)
                except Exception as e:
                    layer4_rejections.append({
                        'question_index': i,
                        'reason': 'schema_invalid',
                        'message': f'Template schema invalid for {q_type!r}: {e}',
                        'question_text': (q.get('question') or '')[:120],
                    })
                    continue

                # Behavior validation: sample N parameter sets, run
                # the formula(s), confirm slots fill cleanly + type-
                # specific invariants (distractor uniqueness for MCQ,
                # blank-count match for fill, pair-count achievable
                # for matching, etc.).
                err = validate_template_typed(tmpl, n_samples=10)
                if err is not None:
                    layer4_rejections.append({
                        'question_index': i,
                        'reason': err.kind,
                        'message': err.message,
                        'sample_params': err.sample_params,
                        'question_text': (
                            q.get('question')
                            or getattr(tmpl, 'template_text', '')
                            or getattr(tmpl, 'framing_text', '')
                        )[:120],
                    })
                    continue

                # Render a concrete instance (deterministic seed
                # based on lesson + position so retake re-renders
                # use a different seed).
                rendered = render_typed(q_type, tmpl, seed=lesson.id * 1000 + i)
                if rendered is None:
                    layer4_rejections.append({
                        'question_index': i,
                        'reason': 'render_failed',
                        'message': (
                            f'render_typed({q_type!r}) returned None despite '
                            'passing validate_template_typed — sampling or '
                            'formula edge case'
                        ),
                        'question_text': (q.get('question') or '')[:120],
                    })
                    continue

                # Preserve the LLM-supplied bookkeeping fields
                # (concept_tag, objectives, difficulty) — only the
                # prose / answer fields come from the renderer.
                preserve_keys = (
                    'concept_tag', 'terminal_objective',
                    'enabling_objective', 'difficulty', 'source',
                )
                preserved = {k: q[k] for k in preserve_keys if q.get(k)}
                q.clear()
                q.update(rendered)
                q.update(preserved)
                templated_count += 1
                kept.append(q)
            questions = kept

        if templated_count or layer4_rejections:
            print(
                f"[ContentGen] [{lesson.title}] Layer 4: "
                f"{templated_count} templated OK, "
                f"{len(layer4_rejections)} rejected",
                flush=True,
            )

        # Layer 4 retry — top up the bank if drops brought us below
        # the target. Cap at 1 retry (latency + cost). After retry,
        # if we're still under the FLOOR (25), save with
        # READY_WITH_WARNINGS via the lesson.metadata.verification_audit.
        BANK_TARGET = 35
        BANK_FLOOR = 25
        layer4_retry_count = 0
        layer4_unresolved = False
        if (
            is_math_lesson_l4
            and len(questions) < BANK_TARGET
            and layer4_rejections
        ):
            from apps.curriculum.parametric_renderer import (
                parse_template,
                render_typed,
                validate_template_typed,
            )
            shortfall = BANK_TARGET - len(questions)
            print(
                f"[ContentGen] [{lesson.title}] Layer 4 retry — "
                f"{shortfall} short of target ({len(questions)}/{BANK_TARGET}); "
                f"requesting replacements",
                flush=True,
            )
            # Build the constraint block listing each rejection.
            rejection_lines = []
            for r in layer4_rejections[:10]:  # first 10 to keep prompt sane
                rejection_lines.append(
                    f"  - Q{r['question_index']+1} ({r['reason']}): "
                    f"{r['message']}"
                )
            constraint = (
                "=" * 72 + "\n"
                "PARAMETRIC TEMPLATE VALIDATION FAILURES IN PREVIOUS RESPONSE\n"
                + "=" * 72 + "\n\n"
                f"{len(layer4_rejections)} of your previous "
                f"questions were rejected because their `template` "
                f"field was missing, schema-invalid, or failed "
                f"backend validation. Examples:\n\n"
                + "\n".join(rejection_lines) + "\n\n"
                f"Generate {shortfall} REPLACEMENT questions, all "
                f"properly templated. Re-read the worked examples "
                f"above. EVERY answer_formula must be a closed-form "
                f"arithmetic expression in the parameter names; the "
                f"backend computes it deterministically. Do not "
                f"emit free-form math — there is no escape hatch.\n\n"
                "Original brief follows:\n\n"
            )

            try:
                retry_prompt = constraint + prompt
                retry_response = llm_client.generate(
                    [{"role": "user", "content": retry_prompt}],
                    system_prompt=exit_sys_prompt,
                    max_tokens=16000,
                )
                from apps.llm.json_utils import parse_llm_json
                retry_questions = parse_llm_json(
                    retry_response.content, expect_array=True,
                ) or []
                # Process retry questions through the same Layer 4
                # validator. Anything that passes gets appended to
                # `questions`; failures are logged but not re-retried.
                retry_kept = 0
                for j, rq in enumerate(retry_questions[:shortfall * 2]):
                    tmpl_data = rq.get('template')
                    if not tmpl_data:
                        layer4_rejections.append({
                            'question_index': len(questions) + j,
                            'reason': 'no_template_after_retry',
                            'message': 'retry question still lacked template',
                            'question_text': (rq.get('question') or '')[:120],
                        })
                        continue
                    rq_type = (rq.get('question_type') or 'short_numeric').strip()
                    try:
                        tmpl = parse_template(rq_type, tmpl_data)
                    except Exception as e:
                        layer4_rejections.append({
                            'question_index': len(questions) + j,
                            'reason': 'schema_invalid_after_retry',
                            'message': f'{rq_type}: {e}',
                            'question_text': (rq.get('question') or '')[:120],
                        })
                        continue
                    err = validate_template_typed(tmpl, n_samples=10)
                    if err is not None:
                        layer4_rejections.append({
                            'question_index': len(questions) + j,
                            'reason': f'{err.kind}_after_retry',
                            'message': err.message,
                            'question_text': (
                                rq.get('question')
                                or getattr(tmpl, 'template_text', '')
                                or getattr(tmpl, 'framing_text', '')
                            )[:120],
                        })
                        continue
                    rendered = render_typed(
                        rq_type, tmpl,
                        seed=lesson.id * 1000 + len(questions) + j,
                    )
                    if rendered is None:
                        continue
                    preserve_keys = (
                        'concept_tag', 'terminal_objective',
                        'enabling_objective', 'difficulty', 'source',
                    )
                    preserved = {k: rq[k] for k in preserve_keys if rq.get(k)}
                    if rq.get('question_type') and rq['question_type'] != 'short_numeric':
                        preserved['question_type'] = rq['question_type']
                    rq.clear()
                    rq.update(rendered)
                    rq.update(preserved)
                    questions.append(rq)
                    retry_kept += 1
                    if len(questions) >= BANK_TARGET:
                        break
                layer4_retry_count = 1
                print(
                    f"[ContentGen] [{lesson.title}] Layer 4 retry "
                    f"recovered {retry_kept} questions "
                    f"(bank now {len(questions)}/{BANK_TARGET})",
                    flush=True,
                )
            except Exception as e:
                logger.warning(
                    f"[{lesson.title}] Layer 4 retry crashed: {e}"
                )

            # Final floor check.
            if len(questions) < BANK_FLOOR:
                layer4_unresolved = True
                print(
                    f"[ContentGen] [{lesson.title}] ⚠️ Layer 4 retry "
                    f"left bank at {len(questions)} (below floor "
                    f"{BANK_FLOOR}) — will mark READY_WITH_WARNINGS",
                    flush=True,
                )

        # Format-mix retry — the LLM tends to anchor on short_numeric
        # because it's the simplest template shape. If the bank came
        # back with fewer than 12 of {mcq, fill_in_blank, matching}
        # combined, run a targeted retry asking ONLY for those types.
        # This is the difference between the user seeing 35 identical
        # short_numeric questions vs the locked 7/15/7/6 mix.
        if is_math_lesson_l4 and len(questions) >= BANK_FLOOR:
            from collections import Counter
            type_counts = Counter(
                (q.get('question_type') or 'short_numeric').strip()
                for q in questions
            )
            non_numeric = (
                type_counts.get('mcq', 0)
                + type_counts.get('fill_in_blank', 0)
                + type_counts.get('matching', 0)
            )
            target_non_numeric = 12
            if non_numeric < target_non_numeric:
                from apps.curriculum.parametric_renderer import (
                    parse_template,
                    render_typed,
                    validate_template_typed,
                )
                want_mcq = max(0, 15 - type_counts.get('mcq', 0))
                want_fill = max(0, 7 - type_counts.get('fill_in_blank', 0))
                want_match = max(0, 6 - type_counts.get('matching', 0))
                shortfall_total = want_mcq + want_fill + want_match
                print(
                    f"[ContentGen] [{lesson.title}] Format-mix retry — "
                    f"non_numeric={non_numeric} (target {target_non_numeric}). "
                    f"Asking for {want_mcq} MCQ / {want_fill} fill / "
                    f"{want_match} matching.",
                    flush=True,
                )
                fmt_constraint = (
                    "=" * 72 + "\n"
                    "FORMAT MIX VIOLATION — TARGETED RETRY\n"
                    + "=" * 72 + "\n\n"
                    "The previous response used short_numeric for almost "
                    "every question. The locked format mix requires "
                    "diversity. Generate ONLY the missing types now:\n\n"
                    f"  - {want_mcq} MCQ templates (question_type: 'mcq', "
                    "with correct_formula + 3 distractor_formulas)\n"
                    f"  - {want_fill} fill_in_blank templates "
                    "(question_type: 'fill_in_blank', with "
                    "blank_formulas — one per ___ slot in the prose)\n"
                    f"  - {want_match} matching templates "
                    "(question_type: 'matching', with framing_text + "
                    "left_formula + right_formula + pair_count 4-6)\n\n"
                    "Do NOT emit any short_numeric questions in this "
                    "response. Re-read sections 6 / 7 / 8 of the "
                    "ADDITIONAL TEMPLATE TYPES block in the original "
                    "prompt for the exact schema. Constraints AND "
                    "parameters MUST be satisfiable together (see "
                    "COMMON FAILURE MODES P2).\n\n"
                    "Original brief follows:\n\n"
                )
                try:
                    fmt_response = llm_client.generate(
                        [{"role": "user", "content": fmt_constraint + prompt}],
                        system_prompt=exit_sys_prompt,
                        max_tokens=16000,
                    )
                    from apps.llm.json_utils import parse_llm_json
                    fmt_questions = parse_llm_json(
                        fmt_response.content, expect_array=True,
                    ) or []
                    fmt_kept = 0
                    for j, rq in enumerate(fmt_questions[:shortfall_total * 2]):
                        rq_type = (rq.get('question_type') or '').strip()
                        if rq_type not in ('mcq', 'fill_in_blank', 'matching'):
                            continue
                        # Skip if we already have enough of this type
                        if rq_type == 'mcq' and type_counts.get('mcq', 0) >= 15:
                            continue
                        if rq_type == 'fill_in_blank' and type_counts.get('fill_in_blank', 0) >= 7:
                            continue
                        if rq_type == 'matching' and type_counts.get('matching', 0) >= 6:
                            continue
                        tmpl_data = rq.get('template')
                        if not tmpl_data:
                            continue
                        try:
                            tmpl = parse_template(rq_type, tmpl_data)
                        except Exception:
                            continue
                        err = validate_template_typed(tmpl, n_samples=10)
                        if err is not None:
                            continue
                        rendered = render_typed(
                            rq_type, tmpl,
                            seed=lesson.id * 1000 + len(questions) + j,
                        )
                        if rendered is None:
                            continue
                        preserve_keys = (
                            'concept_tag', 'terminal_objective',
                            'enabling_objective', 'difficulty', 'source',
                        )
                        preserved = {k: rq[k] for k in preserve_keys if rq.get(k)}
                        preserved['question_type'] = rq_type
                        rq.clear()
                        rq.update(rendered)
                        rq.update(preserved)
                        # Drop a same-type short_numeric to make room
                        # if we're at cap. Cap is BANK_TARGET (35).
                        if len(questions) >= BANK_TARGET:
                            for idx, existing_q in enumerate(questions):
                                if (existing_q.get('question_type') or 'short_numeric').strip() == 'short_numeric' and type_counts.get('short_numeric', 0) > 7:
                                    questions.pop(idx)
                                    type_counts['short_numeric'] -= 1
                                    break
                            else:
                                # No short_numeric to displace — give up
                                # adding more.
                                break
                        questions.append(rq)
                        type_counts[rq_type] = type_counts.get(rq_type, 0) + 1
                        fmt_kept += 1
                    print(
                        f"[ContentGen] [{lesson.title}] Format-mix retry "
                        f"added {fmt_kept} non-numeric questions; "
                        f"final mix: {dict(type_counts)}",
                        flush=True,
                    )
                except Exception as e:
                    logger.warning(
                        f"[{lesson.title}] Format-mix retry crashed: {e}"
                    )

        # Save to database — IN-PLACE replacement when an ExitTicket
        # already exists. Honoring the docstring promise: drop the
        # QUESTIONS only (cascade chain doesn't reach back up), keep
        # the ExitTicket row so its `attempts` related set survives.
        # ExitTicketAttempt.exit_ticket is a CASCADE FK to ExitTicket
        # (apps/tutoring/models.py:600-603) — deleting the row would
        # wipe every per-student attempt + score, which we
        # explicitly DO NOT WANT on regen.
        from django.db import transaction
        with transaction.atomic():
            if existing:
                # Drop the OLD questions only. ExitTicketQuestion has
                # no children FKs (attempts hang off ExitTicket, not
                # off individual questions), so this is safe.
                existing.questions.all().delete()
                exit_ticket = existing
                # Refresh top-level metadata in case it changed.
                exit_ticket.passing_score = 8
                exit_ticket.time_limit_minutes = 15
                exit_ticket.instructions = (
                    f"Answer 10 questions about {lesson.title}. "
                    f"You need 8 correct to pass."
                )
                exit_ticket.save(update_fields=[
                    'passing_score', 'time_limit_minutes', 'instructions',
                ])
            else:
                exit_ticket = ExitTicket.objects.create(
                    lesson=lesson,
                    passing_score=8,
                    time_limit_minutes=15,
                    instructions=f"Answer 10 questions about {lesson.title}. You need 8 correct to pass.",
                )

            # Drop questions the deterministic validator flags as broken:
            #   - rationalization patterns in explanation
            #   - sum-with-blank math mismatch (Pattern A)
            #   - MCQ option vs computed value mismatch (Pattern A)
            #   - LAYER 2 cross-check (Patterns B/C/D — pure sum, mult
            #     chain, simple linear equation)
            #
            # Cross-check audit entries are recorded BEFORE drop so the
            # teacher sees what got caught + Layer 3 retry can use the
            # list to reconstruct the constraint prompt.
            from apps.tutoring.question_validator import (
                cross_check_question,
                is_broken,
            )
            answer_key_mismatches: List[Dict] = []
            valid_questions = []
            for i, q in enumerate(questions):
                # Layer 2 — capture cross-check audit before is_broken
                # decides whether to drop. cross_check_question only
                # fires when the stem matches a recognised pattern AND
                # the stored answer disagrees with what the math
                # produces — so a non-None entry is a real mismatch.
                ck = cross_check_question(q, question_index=i)
                if ck is not None:
                    answer_key_mismatches.append(ck)

                reason = is_broken(q)
                if reason:
                    logger.warning(
                        f"[ExitTicket] [{lesson.title}] dropped Q "
                        f"({q.get('question', '')[:60]!r}): {reason}"
                    )
                else:
                    valid_questions.append(q)
            print(
                f"[ContentGen] [{lesson.title}] validator kept "
                f"{len(valid_questions)}/{len(questions)} questions"
                + (
                    f" ({len(answer_key_mismatches)} answer-key mismatch"
                    f"{'es' if len(answer_key_mismatches) != 1 else ''})"
                    if answer_key_mismatches else ""
                ),
                flush=True,
            )
            questions = valid_questions

            # Layer 1 — arithmetic verification on the surviving
            # questions. Auto-correct prose fields (explanation),
            # detect-only on stem + options + answer_key.
            try:
                is_math_lesson = lesson.unit.course.is_math
            except Exception:
                is_math_lesson = False
            question_audit: List[Dict] = []
            if is_math_lesson:
                from apps.curriculum.content_verifier import (
                    verify_exit_ticket_question,
                )
                for i, q in enumerate(questions):
                    # Mirror the question_text → question key the
                    # verifier expects (some upstream paths use one,
                    # some use the other).
                    if "question_text" not in q and "question" in q:
                        q["question_text"] = q["question"]
                    verify_exit_ticket_question(
                        q, question_index=i, audit=question_audit,
                    )
                if question_audit:
                    fixed = sum(
                        1 for e in question_audit if e.get("auto_corrected")
                    )
                    detected = len(question_audit) - fixed
                    print(
                        f"[ContentGen] [{lesson.title}] Layer 1 "
                        f"(exit ticket): {fixed} auto-corrected, "
                        f"{detected} detect-only",
                        flush=True,
                    )

            # Snap each question's enabling_objective to one of the
            # lesson's canonical EOs. The LLM is told to copy verbatim
            # but drifts; this validation makes it deterministic.
            # Mismatches drop the TAG (clearing it), keeping the
            # question itself.
            #
            # Use the SAME list we showed the LLM
            # (canonical_eos_for_prompt) so what the LLM was told to copy
            # is what we accept. Falling back to lesson.enabling_objectives
            # alone caused every tag to drop when expansion failed and
            # the prompt fell back to broad concepts. See P1 of
            # memory/curriculum_tutor_v2_plan.md.
            canonical_eos = list(canonical_eos_for_prompt)
            if not canonical_eos:
                logger.warning(
                    f"[{lesson.title}] no canonical EOs available for "
                    f"validation — every question's enabling_objective "
                    f"will be dropped. Set lesson.enabling_objectives "
                    f"or unit.terminal_objectives and regenerate."
                )
            eo_validation_stats = {
                'exact': 0, 'snapped': 0, 'dropped': 0, 'empty': 0,
            }
            eo_dropped_examples: List[Dict] = []
            # LLM-snap client — same rationale as the steps path:
            # without it, _normalize_enabling_objective falls into a
            # brittle substring heuristic that drops most emitted EOs.
            q_eo_snap_client = None
            try:
                from apps.llm.client import get_llm_client
                from apps.llm.models import ModelConfig
                _cfg = ModelConfig.get_for('generation') or self._model_config
                if _cfg is not None:
                    q_eo_snap_client = get_llm_client(_cfg)
            except Exception as e:
                print(
                    f"[ContentGen] [{lesson.title}] question EO-snap client "
                    f"unavailable (falling back to substring): {e}",
                    flush=True,
                )

            for q in questions:
                raw_eo = q.get('enabling_objective', '') or ''
                normalised, status = _normalize_enabling_objective(
                    raw_eo, canonical_eos,
                    llm_client=q_eo_snap_client,
                )
                eo_validation_stats[status] = eo_validation_stats.get(status, 0) + 1
                q['enabling_objective'] = normalised
                if status == 'dropped' and len(eo_dropped_examples) < 5:
                    eo_dropped_examples.append({
                        'question': (q.get('question_text') or q.get('question', ''))[:100],
                        'rejected_eo': raw_eo[:160],
                    })
            print(
                f"[ContentGen] [{lesson.title}] enabling_objective "
                f"validation: exact={eo_validation_stats['exact']}, "
                f"snapped={eo_validation_stats['snapped']}, "
                f"dropped={eo_validation_stats['dropped']}, "
                f"empty={eo_validation_stats['empty']}",
                flush=True,
            )

            # Distractor safety scan — drop any question whose stem or
            # choices reference protected-class stereotypes. Logged so
            # the teacher can see what was rejected and request a regen.
            unsafe_count = 0
            safe_questions: List[Dict] = []
            for q in questions:
                rejection = _scan_question_for_unsafe_distractors(q)
                if rejection:
                    unsafe_count += 1
                    print(
                        f"[ContentGen] [{lesson.title}] DISTRACTOR REJECTED: "
                        f"{rejection}",
                        flush=True,
                    )
                    continue
                safe_questions.append(q)
            if unsafe_count:
                print(
                    f"[ContentGen] [{lesson.title}] dropped "
                    f"{unsafe_count} question(s) for unsafe distractor "
                    f"content. Regenerate to backfill.",
                    flush=True,
                )
            questions = safe_questions

            for i, q in enumerate(questions):
                q_type = q.get('question_type', 'mcq')
                kwargs = {
                    'exit_ticket': exit_ticket,
                    'question_type': q_type,
                    'question_text': q.get('question_text') or q.get('question', ''),
                    'explanation': q.get('explanation', ''),
                    'concept_tag': q.get('concept_tag', ''),
                    # Sub-objective (specific EO) — already snapped to
                    # one of the lesson's canonical enabling_objectives
                    # by _normalize_enabling_objective above.
                    'enabling_objective': (q.get('enabling_objective') or '')[:500],
                    'difficulty': q.get('difficulty', 'medium'),
                    'order_index': i,
                }
                # Layer 4 — preserve template source for retake
                # re-rendering + teacher visibility.
                if q.get('template_data'):
                    kwargs['template_data'] = q['template_data']
                # terminal_objective stays in answer_data JSON for now —
                # not used by remediation, kept for forensics.
                objective_data = {}
                if q.get('terminal_objective'):
                    objective_data['terminal_objective'] = q['terminal_objective']

                if q_type == 'mcq':
                    # MCQ. For Layer 4 templated MCQ, render_mcq has
                    # already populated option_a/b/c/d + correct_answer
                    # (the letter). For free-form (non-math, no template)
                    # MCQ, the LLM emitted q['correct'] which we map to
                    # correct_answer.
                    kwargs.update({
                        'option_a': q.get('option_a', ''),
                        'option_b': q.get('option_b', ''),
                        'option_c': q.get('option_c', ''),
                        'option_d': q.get('option_d', ''),
                        'correct_answer': q.get('correct_answer') or q.get('correct') or '',
                    })
                    # answer_data carries source HTML, objective linkage,
                    # and (for templated) the full computed payload from
                    # the renderer (parameters, computed value, etc.).
                    mcq_data = q.get('answer_data') or {}
                    if not isinstance(mcq_data, dict):
                        mcq_data = {}
                    if q.get('source'):
                        mcq_data['source'] = q['source']
                    mcq_data.update(objective_data)
                    if mcq_data:
                        kwargs['answer_data'] = mcq_data
                else:
                    ad = q.get('answer_data', {}) or {}
                    ad.update(objective_data)
                    # Drop legacy figure-spec / figure-svg / plot-spec
                    # fields. Per the unified image-gen decision
                    # (memory/feedback_image_gen_unified.md), all
                    # question figures go through gpt-image-2 on-demand
                    # via the exit_ticket_figure_edit view — not the
                    # SVG-template path. Auto-generating figures here
                    # would block the bulk pipeline (~50s × N questions).
                    for legacy in ('figure_spec', 'figure', 'plot_spec', 'figure_svg'):
                        ad.pop(legacy, None)
                    kwargs['answer_data'] = ad
                ExitTicketQuestion.objects.create(**kwargs)

        # Layers 1 + 2 + 4 — append exit-ticket audit to lesson.metadata.
        # Mirrors the step-audit write in _save_steps_to_db. We
        # extend (not replace) verification_audit so the step
        # corrections persisted earlier survive.
        any_layer_audit = (
            question_audit or answer_key_mismatches
            or layer4_rejections
        )
        if is_math_lesson and any_layer_audit:
            from django.utils import timezone
            from apps.curriculum.models import Lesson
            existing = lesson.metadata or {}
            audit_blob = existing.get('verification_audit') or {}
            if question_audit:
                audit_blob['exit_ticket_corrections'] = question_audit
            if answer_key_mismatches:
                audit_blob['answer_key_mismatches'] = answer_key_mismatches
            if layer4_rejections:
                audit_blob['layer4_rejections'] = layer4_rejections
            audit_blob['layer4_retry_count'] = layer4_retry_count
            audit_blob['layer4_unresolved'] = layer4_unresolved
            audit_blob['exit_ticket_questions_created'] = len(questions)
            # Sub-objective tag validation always recorded so the
            # teacher dashboard can surface drift.
            audit_blob['exit_ticket_enabling_objective_validation'] = {
                **eo_validation_stats,
                'dropped_examples': eo_dropped_examples,
            }
            audit_blob.setdefault('math_check_run', True)
            audit_blob.setdefault(
                'generated_at', timezone.now().isoformat(),
            )
            existing['verification_audit'] = audit_blob
            lesson.metadata = existing
            update_fields = ['metadata']
            if layer4_unresolved:
                lesson.content_status = (
                    Lesson.ContentStatus.READY_WITH_WARNINGS
                )
                update_fields.append('content_status')
            lesson.save(update_fields=update_fields)
        else:
            # Non-math (or no math-layer issues): still record the EO
            # validation result so teachers see exit-ticket tag drift
            # in the audit even when no math layer fired.
            existing = lesson.metadata or {}
            audit_blob = existing.get('verification_audit') or {}
            audit_blob['exit_ticket_enabling_objective_validation'] = {
                **eo_validation_stats,
                'dropped_examples': eo_dropped_examples,
            }
            audit_blob['exit_ticket_questions_created'] = len(questions)
            existing['verification_audit'] = audit_blob
            lesson.metadata = existing
            lesson.save(update_fields=['metadata'])

        # Coverage check (P4 of memory/tutor_no_authoring_plan.md):
        # warn if any practice step's concept_tag has no matching bank
        # question. Non-blocking — runtime falls back to any same-lesson
        # bank question. Logged so the teacher review surface can flag
        # gaps before publish.
        try:
            uncovered = _coverage_gaps(lesson, exit_ticket)
            if uncovered:
                print(
                    f"[ContentGen] [{lesson.title}] ⚠️ bank coverage gaps: "
                    f"{len(uncovered)} practice step(s) lack a matching "
                    f"concept_tag in the bank → "
                    f"{[(s.order_index, s.concept_tag) for s in uncovered]}",
                    flush=True,
                )
        except Exception as cov_err:
            logger.warning(
                f"[{lesson.title}] coverage check failed (non-fatal): {cov_err}"
            )

        return {
            'success': True,
            'questions_created': len(questions),
            'layer4_rejections': len(layer4_rejections),
            'layer4_retry_count': layer4_retry_count,
            'layer4_unresolved': layer4_unresolved,
        }

    except Exception as e:
        logger.error(f"Exit ticket generation failed for {lesson.title}: {e}")
        return {'success': False, 'error': str(e)}


def _generate_exit_ticket_figures(exit_ticket, lesson, institution_id: int) -> int:
    """DISABLED — figures come from template rendering only.

    This used to pull raster images from KB-extracted curriculum pages
    and attach them as question figures. That path produced bugs like
    a curriculum-document table being shown as the figure for an
    angles-of-a-triangle question (2026-04-27).

    The only valid figure source for exit tickets is now the
    deterministic template renderer (`apps.curriculum.figure_templates`).
    The LLM emits a `kind` + spec; we render it; we cache the SVG on
    `answer_data.figure_svg`. No raster images, no KB matching.
    """
    return 0


def _DEPRECATED_generate_exit_ticket_figures(exit_ticket, lesson, institution_id: int) -> int:
    """OLD impl kept for reference — DO NOT call. Preserved only so a
    grep finds the legacy logic if we need to audit what it used to do.
    """
    from apps.tutoring.models import ExitTicketQuestion

    FIGURE_KEYWORDS = [
        'diagram', 'graph', 'chart', 'map', 'figure', 'illustration',
        'draw', 'sketch', 'image', 'picture', 'photograph',
    ]

    media_service = MediaGenerationService(institution_id)
    figures_generated = 0
    subject = lesson.unit.course.title.split()[0] if lesson.unit and lesson.unit.course else 'General'

    # Pre-query KB for relevant figures from worksheets/exams for this lesson
    kb_figures = []
    kb_text_context = ""
    try:
        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
        kb = CurriculumKnowledgeBase(institution_id=institution_id)

        # Get figures from uploaded worksheets, exams, textbooks
        kb_figures = kb.query_for_figure_descriptions(
            topic=f"{lesson.title} {lesson.objective or ''}",
            subject=subject,
            n_results=8,
            grade_level=lesson.unit.course.grade_level or '',
        ) or []

        # Get text context for enriching figure generation prompts
        text_result = kb.query_for_content_generation(
            lesson_title=lesson.title,
            lesson_objective=lesson.objective or '',
            unit_title=lesson.unit.title,
            subject=subject,
            grade_level=lesson.unit.course.grade_level or 'S1',
            n_results=5,
        )
        if text_result and text_result.chunks:
            kb_text_context = "\n".join(
                c.get('content', '')[:300] for c in text_result.chunks[:3]
                if c.get('content')
            )
    except Exception as e:
        logger.warning(f"KB query for exit ticket figures: {e}")

    # Build a lookup of KB figures by keyword for matching to questions
    def find_matching_kb_figure(question_text):
        """Find the best matching KB figure for a question."""
        q_words = set(question_text.lower().split())
        best_fig = None
        best_score = 0
        for fig in kb_figures:
            if not fig.get('image_url'):
                continue
            desc_words = set((fig.get('description', '') or '').lower().split())
            overlap = len(q_words & desc_words)
            if overlap > best_score:
                best_score = overlap
                best_fig = fig
        return best_fig if best_score >= 2 else None

    questions = ExitTicketQuestion.objects.filter(exit_ticket=exit_ticket)

    for q in questions:
        q_text = (q.question_text or '').lower()
        answer_data = q.answer_data or {}
        if not isinstance(answer_data, dict):
            answer_data = {}
        data_desc = answer_data.get('data_description', '')
        # The LLM now sets figure_description explicitly (the prompt no
        # longer asks for plot_spec / inline-SVG charts). When present
        # it takes priority over the keyword heuristic.
        explicit_desc = (answer_data.get('figure_description') or '').strip()
        needs_figure = bool(explicit_desc) or any(kw in q_text for kw in FIGURE_KEYWORDS)

        # Skip if already has an HTML data block or an existing image
        # URL we'd just be overwriting.
        if (data_desc and '<' in data_desc) or q.image or answer_data.get('figure_url'):
            continue

        # Step 1: Try to find a matching figure from KB (worksheets/exams)
        matched_fig = find_matching_kb_figure(q.question_text or '')

        if matched_fig:
            # Use the KB figure directly
            if q.question_type == 'data_interpretation' and data_desc:
                # Embed above existing text data
                answer_data['data_description'] = (
                    f"<div style='text-align:center;margin-bottom:8px;'>"
                    f"<img src='{matched_fig['image_url']}' style='max-width:100%;border-radius:4px;' "
                    f"alt='{matched_fig.get('description', '')[:100]}'>"
                    f"<div style='font-size:11px;color:#71717a;margin-top:4px;'>"
                    f"Source: {matched_fig.get('source_file', 'textbook')}</div>"
                    f"</div>"
                    f"<div style='font-size:13px;'>{data_desc}</div>"
                )
            else:
                answer_data['figure_url'] = matched_fig['image_url']
                answer_data['figure_source'] = 'worksheet/exam'
                answer_data['figure_description'] = matched_fig.get('description', '')[:200]
            q.answer_data = answer_data
            q.save(update_fields=['answer_data'])
            figures_generated += 1
            continue

        # Step 2: Generate a new figure if the question describes one.
        # Prefer the LLM's explicit figure_description over the question
        # text — it's tailored to be image-prompt friendly.
        if needs_figure:
            if explicit_desc:
                description = (
                    f"Educational chart/diagram for an exit-ticket question: {explicit_desc}"
                )
            else:
                description = f"Educational diagram for assessment: {q.question_text[:200]}"
            if kb_text_context:
                description += f"\nContext from curriculum: {kb_text_context[:300]}"

            generated = media_service._generate_or_find_image(
                description=description,
                image_type='diagram',
                lesson=lesson,
            )

            if generated and generated.get('url'):
                answer_data['figure_url'] = generated['url']
                answer_data['figure_source'] = generated.get('source', 'generated')
                # Preserve the original LLM description for accessibility
                # (used as the alt text in the renderer).
                if explicit_desc and not answer_data.get('figure_description'):
                    answer_data['figure_description'] = explicit_desc
                q.answer_data = answer_data
                q.save(update_fields=['answer_data'])
                figures_generated += 1

    return figures_generated


# ============================================================================
# MEDIA GENERATION SERVICE
# ============================================================================

class MediaGenerationService:
    """
    Generates media assets for lesson steps.

    Integrates with:
    - DALL-E for image generation
    - Media library for existing assets
    - External sources for videos/diagrams
    """

    def __init__(self, institution_id: int):
        self.institution_id = institution_id

    def generate_media_for_step(self, step) -> Dict:
        """
        Generate or find media for a lesson step.

        Args:
            step: LessonStep instance with media descriptions

        Returns:
            Dict with generated/found media URLs
        """
        if not step.media:
            return {'images': [], 'videos': []}

        result = {'images': [], 'videos': []}

        # Process image requests
        for image_req in step.media.get('images', []):
            if image_req.get('url'):
                # Already has URL
                result['images'].append(image_req)
            else:
                # Generate or find image
                generated = self._generate_or_find_image(
                    description=image_req.get('description', ''),
                    image_type=image_req.get('type', 'diagram'),
                    lesson=step.lesson
                )
                if generated:
                    image_req['url'] = generated['url']
                    image_req['source'] = generated['source']
                    result['images'].append(image_req)

        return result

    def _generate_or_find_image(self, description: str, image_type: str, lesson) -> Optional[Dict]:
        """Generate or find an image matching the description."""

        # First, check media library for existing assets
        existing = self._find_in_library(description, lesson)
        if existing:
            return {'url': existing.file.url, 'source': 'library'}

        # Generate new image
        try:
            from apps.tutoring.image_service import ImageGenerationService

            service = ImageGenerationService(
                lesson=lesson,
                institution_id=self.institution_id
            )

            result = service.generate_educational_image(
                prompt=description,
                style=image_type
            )

            if result and result.get('url'):
                return {'url': result['url'], 'source': 'generated'}

        except Exception as e:
            logger.warning(f"Image generation failed: {e}")

        return None

    def _find_in_library(self, description: str, lesson) -> Optional:
        """Search media library for matching asset."""
        try:
            from apps.media_library.models import MediaAsset

            # Simple keyword search
            keywords = description.lower().split()[:5]

            for keyword in keywords:
                if len(keyword) > 3:
                    assets = MediaAsset.objects.filter(
                        institution_id=self.institution_id,
                        tags__icontains=keyword
                    ).first()

                    if assets:
                        return assets

            return None
        except:
            return None


# ============================================================================
# BATCH GENERATION
# ============================================================================

def generate_content_for_unit(unit_id: int, force: bool = False) -> Dict:
    """
    Generate content for all lessons in a unit.

    Args:
        unit_id: Unit ID
        force: If True, regenerate even if content exists
    """
    from apps.curriculum.models import Unit, Lesson

    unit = Unit.objects.get(id=unit_id)
    lessons = unit.lessons.all()

    from apps.accounts.models import Institution
    institution_id = unit.course.institution_id or Institution.get_global().id
    generator = LessonContentGenerator(institution_id=institution_id)

    results = {
        'unit': unit.title,
        'total_lessons': lessons.count(),
        'success': 0,
        'failed': 0,
        'skipped': 0,
        'details': []
    }

    for lesson in lessons:
        # Skip if already has content (unless force)
        if lesson.steps.count() >= 5 and not force:
            results['skipped'] += 1
            results['details'].append({
                'lesson': lesson.title,
                'status': 'skipped',
                'reason': 'Already has content'
            })
            continue

        try:
            result = generator.generate_for_lesson(lesson, save_to_db=True)

            if result.get('success'):
                results['success'] += 1
                results['details'].append({
                    'lesson': lesson.title,
                    'status': 'success',
                    'steps': result.get('steps_generated', 0)
                })
            else:
                results['failed'] += 1
                results['details'].append({
                    'lesson': lesson.title,
                    'status': 'failed',
                    'error': result.get('error', 'Unknown')
                })

        except Exception as e:
            results['failed'] += 1
            results['details'].append({
                'lesson': lesson.title,
                'status': 'failed',
                'error': str(e)
            })

    return results


def generate_content_for_course(course_id: int, force: bool = False) -> Dict:
    """
    Generate content for all lessons in a course.

    Args:
        course_id: Course ID
        force: If True, regenerate even if content exists
    """
    from apps.curriculum.models import Course

    course = Course.objects.get(id=course_id)
    units = course.units.all()

    results = {
        'course': course.title,
        'total_units': units.count(),
        'unit_results': []
    }

    for unit in units:
        unit_result = generate_content_for_unit(unit.id, force=force)
        results['unit_results'].append(unit_result)

    # Aggregate stats
    results['total_lessons'] = sum(u['total_lessons'] for u in results['unit_results'])
    results['total_success'] = sum(u['success'] for u in results['unit_results'])
    results['total_failed'] = sum(u['failed'] for u in results['unit_results'])
    results['total_skipped'] = sum(u['skipped'] for u in results['unit_results'])

    return results


def generate_content_for_lesson(lesson_id: int, force: bool = False) -> Dict:
    """
    Generate content for a single lesson.

    Args:
        lesson_id: Lesson ID
        force: If True, regenerate even if content exists
    """
    from apps.curriculum.models import Lesson

    lesson = Lesson.objects.get(id=lesson_id)

    # Check existing content
    if lesson.steps.count() >= 5 and not force:
        return {
            'success': False,
            'lesson': lesson.title,
            'error': 'Already has content. Use force=True to regenerate.'
        }

    from apps.accounts.models import Institution
    institution_id = lesson.unit.course.institution_id or Institution.get_global().id
    generator = LessonContentGenerator(institution_id=institution_id)

    return generator.generate_for_lesson(lesson, save_to_db=True)
