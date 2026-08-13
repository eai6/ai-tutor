"""Curriculum Parser v2 — locale-aware, LLM-based structure extraction.

Replaces the Seychelles-specific regex parsers in
``curriculum_parser_archive``. Single public entry point:

    parse_curriculum(file_path, *, subject_hint, grade_hint, locale,
                     institution_id, progress_cb) -> ParsedCurriculumV2

The v2 pipeline:

  1. extract_text_from_file (unchanged — re-exported from archive)
  2. detect_subject_and_locale (LLM, replaces English-keyword detect_subject)
  3. outline_pass — units only — M3 deliverable
  4. lessons_pass — one fan-out per unit — M4 deliverable
  5. ParseFailure raised on any irrecoverable step — never silently
     fall through to a worse path.

This file is the canonical import path. Until M5 lands the runtime
wiring, the legacy structure parsers (parse_curriculum_with_llm,
parse_mathematics_curriculum, etc.) are still re-exported from the
archive so existing call sites keep working unchanged. After M5 the
archive's structure-parsing layer becomes dead code; archive deletion
deferred to its own plan (see §M8 in
``memory/curriculum_parser_v2_plan.md``).

Architecture refs:
  - memory/curriculum_parser_v2_plan.md — full plan + locked decisions
  - apps/curriculum/locale_prompts.py — locale-aware prompt helpers
  - apps/llm/client.py — BaseLLMClient.generate
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================================
# BACK-COMPAT RE-EXPORTS (from the archive)
# ----------------------------------------------------------------------------
# Keep every callsite-needed symbol importable from this module so the M0
# rename + this v2 rewrite stay invisible at integration time. The
# extraction layer stays here permanently; the structure-parsing
# functions stay re-exported until M5 unwires the orchestrator, at
# which point we can stop re-exporting them. Archive deletion is a
# separate later plan.
# ============================================================================

from ai_tutor.apps.curriculum.curriculum_parser_archive import (  # noqa: F401
    # text extraction (kept forever)
    OCRFailure,
    extract_text_from_file,
    extract_from_pdf,
    extract_from_docx,
    extract_from_image,
    extract_figures_from_pdf,
    extract_curriculum_with_vision,
    _classify_llm_error,
    _render_page_within_b64_limit,
    _strip_nul,
    # legacy structure layer (re-exported as dormant helpers — no
    # runtime path reaches them after M5; kept until archive deletion)
    ParsedCurriculum,
    FigureDescription,
    FigureExtractionResult,
    detect_subject,
    parse_curriculum_file,
    parse_curriculum_with_llm,
    parse_mathematics_curriculum,
    parse_geography_curriculum,
    parse_generic_curriculum,
    create_lessons_from_objectives,
    create_lesson_title,
    create_enabling_objectives,
    create_curriculum_from_structure,
    # process_curriculum_upload + complete_curriculum_upload INTENTIONALLY
    # NOT re-exported — replaced by the v2 versions defined further down
    # in this module. Callers that `from ai_tutor.apps.curriculum.curriculum_parser
    # import process_curriculum_upload` now hit the v2 version.
)


# ============================================================================
# v2 — FAILURE MODEL
# ============================================================================


class ParseFailure(Exception):
    """Structured failure from the v2 parser.

    Carries a stable ``reason`` slug so the upload UI can render an
    actionable error without parsing the message text. The full
    ``detail`` propagates to logs.
    """

    REASONS = (
        'no_text',             # extracted < 100 chars (PDF unreadable / empty doc)
        'subject_unclassified',# LLM couldn't pick a subject with confidence
        'no_units_found',      # outline pass returned 0 units (M3)
        'lesson_pass_failed',  # every per-unit lessons call crashed (M4)
        'llm_unavailable',     # ModelConfig.get_for('generation') is None
        'llm_error',           # provider-level error (rate limit, 4xx, parse)
    )

    def __init__(self, reason: str, detail: str = ''):
        if reason not in self.REASONS:
            reason = 'llm_error'
        self.reason = reason
        self.detail = detail
        super().__init__(f"ParseFailure({reason}): {detail}" if detail else f"ParseFailure({reason})")


# ============================================================================
# v2 — SCHEMAS
# ============================================================================


class LessonV2(BaseModel):
    """A single lesson within a unit. Populated by ``lessons_pass`` (M4)."""
    title: str = Field(description="Short, student-friendly title.")
    objective: str = Field(description="What the student will be able to do after this lesson (terminal objective).")
    enabling_objectives: list[str] = Field(
        default_factory=list,
        description="Granular sub-skills the lesson teaches (action-verb statements).",
    )
    order: int = Field(default=0, description="Position within the unit (1-indexed).")


class UnitOutlineV2(BaseModel):
    """Unit-level shape — produced by ``outline_pass`` (M3) before lessons
    are filled in."""
    title: str = Field(description="Unit / topic / strand name as it appears in the source document.")
    grade_level: str = Field(description="Grade label exactly as used in the source — 'S3', '10ª Classe', etc.")
    description: str = Field(default="", description="One-line description of what the unit covers.")
    source_evidence: str = Field(
        default="",
        description="Verbatim snippet from the document that anchors this unit (anti-hallucination).",
    )


class UnitV2(UnitOutlineV2):
    """Unit + its lessons. Produced by combining ``outline_pass`` +
    ``lessons_pass`` outputs."""
    lessons: list[LessonV2] = Field(default_factory=list)


class ParsedCurriculumV2(BaseModel):
    """Final return shape from ``parse_curriculum``."""
    subject: str
    locale: str = Field(description="BCP-47 locale code, e.g. 'en-us', 'pt-mz'.")
    grade_levels: list[str] = Field(description="Distinct grade labels detected in the doc.")
    description: str = ""
    units: list[UnitV2] = Field(default_factory=list)
    detection_disagreed_with_hint: bool = Field(
        default=False,
        description="True when teacher-supplied hint conflicted with LLM detection; logged for audit.",
    )


# --- LLM response_model schemas (constrained decoding via instructor) ---
# These are what we PASS to instructor.chat.completions.create(...,
# response_model=...). Distinct from the public Pydantic types above
# because the LLM-side schemas include source_evidence, hint_disagreement,
# rationale fields the LLM produces but we don't surface to callers.


class _DetectionResult(BaseModel):
    """Schema for detect_subject_and_locale LLM response."""
    subject: str = Field(description="Academic subject — Mathematics, Biology, Geography, etc. Avoid 'General'.")
    locale: str = Field(description="BCP-47 lowercase. e.g. 'en-us', 'pt-mz'.")
    grade_levels: list[str] = Field(description="Distinct grade labels EXACTLY AS THEY APPEAR in the doc — e.g. '10ª Classe', 'S3', 'Form 4'. Do NOT coerce between systems.")
    hint_disagreement: bool = Field(default=False, description="True iff your detection differs meaningfully from teacher_hints.")
    rationale: str = Field(default="", description="One short sentence explaining the call.")


class _OutlineUnit(BaseModel):
    """One unit-outline entry within an _OutlineResult."""
    title: str = Field(description="Unit name as it appears in the source.")
    grade_level: str = Field(description="Grade label EXACTLY as in the source. Do not translate or coerce.")
    description: str = Field(default="", description="One-line summary of what the unit covers.")
    source_evidence: str = Field(default="", description="30-100 char verbatim snippet from the document anchoring this unit (heading line). Anti-hallucination.")


class _OutlineResult(BaseModel):
    """Schema for outline_pass LLM response."""
    units: list[_OutlineUnit] = Field(default_factory=list)


class _LessonRaw(BaseModel):
    """One lesson entry within a _LessonsResult."""
    title: str = Field(description="Short, student-friendly concept name. Not 'Students will learn X' — 'X'.")
    objective: str = Field(description="Terminal objective in 1 sentence — what the student will be able to do.")
    enabling_objectives: list[str] = Field(default_factory=list, description="3-6 granular sub-skills (action-verb statements).")
    order: int = Field(default=0, description="1-indexed position within the unit.")
    source_evidence: str = Field(default="", description="30-100 char verbatim snippet from the excerpt anchoring this lesson.")


class _LessonsResult(BaseModel):
    """Schema for lessons_pass LLM response."""
    lessons: list[_LessonRaw] = Field(default_factory=list)


# ============================================================================
# v2 — LLM CLIENT WRAPPER
# ============================================================================


def _get_llm_client():
    """Return (BaseLLMClient, ModelConfig) for purpose='generation', or
    raise ParseFailure('llm_unavailable'). Centralised so every v2 LLM
    call surfaces the same structured error when ModelConfig isn't
    configured.
    """
    from ai_tutor.apps.llm.models import ModelConfig
    from ai_tutor.apps.llm.client import get_llm_client

    cfg = ModelConfig.get_for('generation')
    if cfg is None:
        raise ParseFailure(
            'llm_unavailable',
            "No ModelConfig found for purpose='generation'. Configure one in the LLM admin.",
        )
    return get_llm_client(cfg), cfg


def _call_llm_structured(
    *,
    response_model: type,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
):
    """Call the generation LLM and return a typed Pydantic instance of
    ``response_model``. Uses ``instructor.from_provider`` for
    constrained decoding so the response shape is GUARANTEED by the
    provider — no more json.loads + regex repair.

    Mirrors the pattern used by tutor judges
    (``apps/tutoring/judges/_instructor_helper.py``) and content
    generation. See auto-memory feedback_use_instructor_for_structured_output.

    Raises:
        ParseFailure('llm_unavailable') when no ModelConfig exists.
        ParseFailure('llm_error') on provider call failure.
    """
    from ai_tutor.apps.tutoring.judges._instructor_helper import (
        get_instructor_from_client, structured_completion,
    )

    client, cfg = _get_llm_client()
    model_name = getattr(cfg, 'model_name', 'unknown')
    provider = getattr(cfg, 'provider', '') or ''
    logger.info(
        "[parser_v2] LLM call: model=%s schema=%s max_tokens=%d",
        model_name, response_model.__name__, max_tokens,
    )

    instructor_client = get_instructor_from_client(client)
    if instructor_client is None:
        raise ParseFailure(
            'llm_error',
            f"instructor.from_provider failed to wrap {model_name} — "
            "check that the 'instructor' package is installed and the "
            "provider is one of (anthropic, openai, google, ollama).",
        )
    try:
        return structured_completion(
            instructor_client,
            response_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            max_retries=2,
            provider=str(provider).lower(),
        )
    except Exception as e:
        raise ParseFailure(
            'llm_error',
            f"structured call failed for {response_model.__name__} on "
            f"{model_name}: {type(e).__name__}: {e}",
        )


# ============================================================================
# v2 — DETECTION (M2 deliverable)
# ============================================================================


SUPPORTED_LOCALES = ('en-us', 'pt-mz')  # extend as pilots land
DEFAULT_LOCALE = 'en-us'


def _output_language_block(locale: str) -> str:
    """Return a strong "respond in this language" instruction for the
    LLM output. Without this, our English-default prompts cause the
    LLM to translate PT source content into English when emitting
    titles / objectives / enabling_objectives — which is exactly NOT
    what we want for a Mozambique curriculum where students will read
    these labels.

    Returns "" for en-us so the EN cache key stays byte-stable.
    """
    code = (locale or 'en-us').lower()
    if code in ('en-us', 'en'):
        return ""
    if code == 'pt-mz':
        return (
            "\n\n<output_language>\n"
            "CRITICAL: Generate ALL output fields (title, objective, "
            "description, enabling_objectives, every string) in "
            "MOZAMBIQUE PORTUGUESE. Do NOT translate the document's "
            "Portuguese content into English. The downstream UI shows "
            "these strings verbatim to Mozambican students and teachers; "
            "English leakage will be visible in production.\n"
            "Use 'tu' informal addressing for student-facing strings, "
            "post-1990 Acordo Ortográfico spelling, and keep technical "
            "vocabulary in standard Portuguese (e.g. 'célula', 'núcleo', "
            "'cromossomas', 'ADN', 'ARN').\n"
            "source_evidence stays a VERBATIM snippet from the document "
            "(do not translate it).\n"
            "</output_language>\n"
        )
    # Fallback for unrecognised locales.
    return (
        f"\n\n<output_language>\n"
        f"Generate ALL output fields in the language identified by "
        f"locale '{code}'. Do not switch to English unless the document "
        f"is itself in English.\n"
        f"</output_language>\n"
    )


def detect_subject_and_locale(
    text: str,
    *,
    subject_hint: str = '',
    grade_hint: str = '',
    locale_hint: str = '',
) -> dict:
    """Detect (subject, locale, grade_range) from the first ~3K chars of
    the document using the generation LLM.

    Teacher-supplied hints are treated as SOFT PRIORS — the LLM is told
    "the teacher suggested X" but is free to disagree based on the
    document content. Disagreement is logged for audit.

    Returns a dict with shape:
        {
            'subject': str,
            'locale': str (one of SUPPORTED_LOCALES; defaults to DEFAULT_LOCALE),
            'grade_levels': list[str],   # e.g. ["10ª Classe", "11ª Classe", "12ª Classe"]
            'hint_disagreement': bool,
            'rationale': str,            # 1-2 sentences for debug
        }

    Raises ParseFailure on llm_unavailable / llm_error / unrecognised
    response shape.
    """
    # Sample first 2000 chars + a 500-char tail — enough to see title
    # page + a chunk of the index / first unit. Keeps the call cheap.
    head = text[:2000]
    tail = text[-500:] if len(text) > 2500 else ''
    sample = head + (f"\n\n[...{len(text) - 2500} chars elided...]\n\n" + tail if tail else '')

    hints = []
    if subject_hint:
        hints.append(f"  - teacher_subject_hint: {subject_hint!r}")
    if grade_hint:
        hints.append(f"  - teacher_grade_hint: {grade_hint!r}")
    if locale_hint:
        hints.append(f"  - teacher_locale_hint: {locale_hint!r}")
    hints_block = (
        "\n<teacher_hints>\n"
        + "The teacher uploading this document supplied the following hints. "
        + "Treat them as SOFT PRIORS — useful context, but trust what you "
        + "actually see in the document. If you disagree, set hint_disagreement=true "
        + "and explain in rationale.\n"
        + "\n".join(hints)
        + "\n</teacher_hints>\n"
        if hints else ""
    )

    system_prompt = (
        "You are a curriculum-document classifier. Given a sample from the "
        "first pages of a curriculum / syllabus / teaching-programme document, "
        "identify the academic subject, the document language/locale, and the "
        "grade level(s) it covers. Be precise — read the actual document "
        "content, including in languages other than English. Respond with a "
        "single JSON object — no prose, no markdown fences."
    )

    user_prompt = (
        f"<document_sample>\n{sample}\n</document_sample>\n"
        f"{hints_block}\n"
        f"<task>\n"
        f"Classify this document.\n\n"
        f"Constraints:\n"
        f"- subject: use a specific name from the doc (Mathematics, "
        f"Biology, Geography, Physics, Chemistry, etc.), NOT 'General'.\n"
        f"- locale: BCP-47 lowercase. Supported: {list(SUPPORTED_LOCALES)}. "
        f"If the doc is in a language we don't list, pick the closest "
        f"supported locale.\n"
        f"- grade_levels: list the distinct grade labels EXACTLY as they "
        f"appear in the doc (e.g. ['10ª Classe', '11ª Classe', "
        f"'12ª Classe'], or ['S3'], or ['Form 4', 'Form 5']). Do not "
        f"coerce between national systems.\n"
        f"- hint_disagreement: true iff your detection differs meaningfully "
        f"from the teacher's hints (above).\n"
        f"- rationale: one short sentence — what in the doc made you pick "
        f"that subject/locale/grade.\n"
        f"</task>"
    )

    parsed = _call_llm_structured(
        response_model=_DetectionResult,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=512,
    )

    subject = parsed.subject.strip()
    locale_raw = parsed.locale.strip().lower()
    grade_levels = [str(g).strip() for g in (parsed.grade_levels or []) if str(g).strip()]
    hint_disagreement = bool(parsed.hint_disagreement)
    rationale = (parsed.rationale or '').strip()

    if not subject:
        raise ParseFailure(
            'subject_unclassified',
            f"LLM returned empty subject. rationale={rationale!r}",
        )

    # Coerce locale to a supported value — log if we override.
    if locale_raw not in SUPPORTED_LOCALES:
        logger.warning(
            "[parser_v2] LLM returned unsupported locale %r; defaulting to %r. "
            "Add to SUPPORTED_LOCALES if this is a new pilot language. "
            "rationale=%s",
            locale_raw, DEFAULT_LOCALE, rationale[:200],
        )
        locale = DEFAULT_LOCALE
    else:
        locale = locale_raw

    if hint_disagreement:
        logger.info(
            "[parser_v2] LLM disagreed with teacher hint: detected=(%s, %s, %s) "
            "vs hints=(subject=%r, grade=%r, locale=%r). rationale=%s",
            subject, locale, grade_levels,
            subject_hint, grade_hint, locale_hint,
            rationale[:200],
        )

    return {
        'subject': subject,
        'locale': locale,
        'grade_levels': grade_levels,
        'hint_disagreement': hint_disagreement,
        'rationale': rationale,
    }


# ============================================================================
# v2 — OUTLINE + LESSONS PASSES (stubs — M3 + M4)
# ============================================================================


MAX_OUTLINE_TEXT_CHARS = 180_000
"""Soft cap on the document text we send to the outline-pass LLM.
Sonnet 4.6 has a 200K context window — we cap below that to leave
headroom for system prompt + schema + max_tokens. 180K fits every
curriculum we've seen so far without trimming:
  - Mozambique Biology: 111K
  - Mozambique Geography: ~130K
  - Mozambique Math: ~95K
Lower caps risk chopping the final grade in multi-grade docs
(observed on M4 first-run: 100K cap dropped 12ª Classe → 0 lessons
for that grade's units). If a future doc exceeds 180K we head+tail
to preserve both the index and the trailing units."""


def outline_pass(text: str, *, subject: str, locale: str,
                 existing_units: Optional[list[dict]] = None) -> list[UnitOutlineV2]:
    """Extract just the unit-level shape from the full document text.
    Returns a list of UnitOutlineV2; lessons are filled in by
    ``lessons_pass`` per-unit (M4).

    Approach:
      - Query-last layout: document FIRST, schema/instructions LAST
        (~30 % quality bump on long-context extraction per Anthropic).
      - Locale-aware structural hints from ``locale_parser_hints`` so
        the LLM recognises PT terminology like "Unidade Temática",
        "Conteúdos", etc.
      - Schema-constrained JSON output — robust parse via
        ``_call_llm_structured`` (markdown fences / prose wrappers OK).
      - Anti-hallucination: each returned unit MUST include
        ``source_evidence`` — a verbatim snippet from the document.
        We post-validate that the evidence actually appears in the text
        and drop units whose evidence we can't find.

    Raises ParseFailure('no_units_found') on empty/garbage output.
    """
    from ai_tutor.apps.curriculum.locale_prompts import locale_parser_hints

    # Trim if oversized — keep head + tail so the index/contents page
    # (typically near the front) and the trailing units (which can
    # cover the final grade in a multi-grade doc) are both represented.
    doc_text = text
    if len(doc_text) > MAX_OUTLINE_TEXT_CHARS:
        head = doc_text[: int(MAX_OUTLINE_TEXT_CHARS * 0.7)]
        tail = doc_text[-int(MAX_OUTLINE_TEXT_CHARS * 0.3):]
        doc_text = head + "\n\n[... middle section elided ...]\n\n" + tail
        logger.info(
            "[parser_v2] outline_pass: doc trimmed %d → %d chars",
            len(text), len(doc_text),
        )

    locale_hints = locale_parser_hints(locale)
    output_lang = _output_language_block(locale)

    # Re-parse dedupe context. On a first upload existing_units is empty and
    # BOTH of these render to "" — the prompt is byte-for-byte the original,
    # so first-upload extraction is unchanged. On a re-parse we list the units
    # already in the course (data block in <context>) and add a guideline (in
    # the trailing <task>) telling Claude to reuse the EXACT existing title for
    # already-covered units so the additive writer recognises them instead of
    # appending a reworded duplicate.
    existing_units_block = ""
    dedupe_guideline = ""
    if existing_units:
        _lines = "\n".join(
            f"- {u.get('title','')}  (grade: {u.get('grade_level','')})"
            for u in existing_units if u.get('title')
        )
        existing_units_block = (
            f"\n<existing_units>\n"
            f"These units already exist for this curriculum (created by an "
            f"earlier upload):\n{_lines}\n"
            f"</existing_units>\n"
        )
        dedupe_guideline = (
            f"9. Some units already exist (see <existing_units>). When this "
            f"document covers a unit that is the same as one already listed, "
            f"reuse its EXACT existing title and grade verbatim so we recognise "
            f"it as the same unit rather than a duplicate. Give a new title "
            f"only to a unit that is genuinely not in that list. Still emit "
            f"every unit the document contains — both the ones that already "
            f"exist and the new ones.\n"
        )

    system_prompt = (
        "You are a curriculum-document structure extractor. Given a "
        "curriculum / teaching-programme document, identify ITS NATURAL "
        "UNIT-LEVEL STRUCTURE — typically called 'units', 'strands', "
        "'themes', or 'Unidades Temáticas' depending on the country's "
        "system. Return ONLY the unit-level skeleton (no individual "
        "lessons yet). Use labels as they appear in the source — do "
        "not coerce them into another country's notation. Anchor every "
        "unit to a verbatim snippet from the document so we can verify "
        "the extraction. Respond with a single JSON object."
        + output_lang
    )

    user_prompt = (
        # Document FIRST (query-last layout).
        f"<document>\n{doc_text}\n</document>\n"
        f"{locale_hints}"
        f"\n<context>\n"
        f"- subject: {subject}\n"
        f"- locale: {locale}\n"
        f"</context>\n"
        f"{existing_units_block}"
        f"\n<task>\n"
        f"Extract the unit-level structure of this curriculum document.\n\n"
        f"GUIDELINES:\n"
        f"1. Identify TOP-LEVEL unit divisions only (chapters, units, "
        f"strands, themes, Unidades Temáticas, etc.) AS THEY APPEAR in "
        f"the source's table of contents / index / overview table. "
        f"Do NOT invent units, do NOT merge units the document treats "
        f"as separate.\n"
        f"2. DO NOT split a single unit into sub-units just because the "
        f"source has multiple tables for it. Sub-tables literally marked "
        f"as 'continuação' / '(continuação)' / '(continued)' are "
        f"CONTINUATIONS of one parent unit — emit ONE outline entry for "
        f"the parent, not one per continuation table. (Example: "
        f"\"Sistemática dos Seres Vivos\" with 5 continuation tables for "
        f"5 kingdoms is ONE unit; the kingdom-level structure becomes "
        f"LESSONS within that unit.) BUT: two units that happen to share "
        f"a code or strand prefix (e.g. 'GM9 - Area & Volume' on one "
        f"page and 'GM9 - Angles' on a later page covering different "
        f"topics with different time slots) are SEPARATE units — emit "
        f"both.\n"
        f"3. If a unit recurs across multiple grades (e.g. 'Citologia' "
        f"in both 10ª Classe and 12ª Classe), emit ONE outline entry "
        f"per (unit, grade) pair — they will become separate Course "
        f"rows in our system, since each Course has a single grade.\n"
        f"4. Use grade labels EXACTLY as they appear (10ª Classe / S3 / "
        f"Form 4 / Secondary 3 / etc.). Do not translate or coerce.\n"
        f"5. SKIP non-structural content: title pages, copyright pages "
        f"('Ficha Técnica'), introductions, methodological notes, "
        f"glossaries, bibliographies. These are NOT units.\n"
        f"6. Provide a short (1-line) description per unit.\n"
        f"7. For source_evidence, paste a 30-100 char VERBATIM snippet "
        f"from the document where that unit is FIRST introduced "
        f"(typically the FIRST heading line — not a continuation). "
        f"This anchors the extraction.\n"
        f"8. BE EXHAUSTIVE. Scan the WHOLE document for unit headings — "
        f"don't stop after the first few. Curriculum docs commonly "
        f"have 5-15+ units per grade, sometimes spread across multiple "
        f"terms / trimestres / cycles. If the document has a table of "
        f"contents or overview table, use it as your unit-count target. "
        f"Missing a unit on a later page is a BIGGER problem than "
        f"splitting too finely.\n"
        f"{dedupe_guideline}"
        f"</task>"
    )

    # 8192 max_tokens: typical doc has 5-15 units, each ~400-600 chars
    # of JSON (title + grade + description + 30-100 char source_evidence
    # + structure overhead). 4096 truncated at ~8 units on a 12-unit
    # Seychelles Geography doc; 8192 leaves headroom for 15-20 units.
    parsed = _call_llm_structured(
        response_model=_OutlineResult,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=8192,
    )

    raw_units = parsed.units

    # Validate + anti-hallucination check.
    outlines: list[UnitOutlineV2] = []
    skipped_no_evidence = 0
    for raw in raw_units:
        # raw is an _OutlineUnit pydantic instance (instructor-typed).
        title = (raw.title or '').strip()
        grade = (raw.grade_level or '').strip()
        if not title or not grade:
            continue
        description = (raw.description or '').strip()
        evidence = (raw.source_evidence or '').strip()

        # Anti-hallucination: the verbatim snippet should appear in the
        # source text. Use the same whitespace-tolerant regex search
        # as _find_unit_body — pdftotext often line-breaks headings
        # mid-word ("Geometry-\nshapes\nand\nSpaces\n(GM9)"), which a
        # strict substring check rejects even though the heading IS
        # in the document.
        if evidence:
            if _find_unit_body(text, evidence) == -1 and \
               _find_unit_body(text, evidence[:50]) == -1 and \
               _find_unit_body(text, title) == -1:
                logger.warning(
                    "[parser_v2] outline_pass dropping unit %r — source_evidence "
                    "%r not found in document (likely hallucinated).",
                    title, evidence[:80],
                )
                skipped_no_evidence += 1
                continue
        try:
            outlines.append(UnitOutlineV2(
                title=title,
                grade_level=grade,
                description=description,
                source_evidence=evidence,
            ))
        except Exception:  # pydantic ValidationError — skip malformed
            logger.warning(
                "[parser_v2] outline_pass: malformed unit dropped: %r",
                {'title': title, 'grade': grade},
            )

    if not outlines:
        raise ParseFailure(
            'no_units_found',
            f"outline_pass returned 0 valid units after anti-hallucination "
            f"filter (raw={len(raw_units)}, evidence_misses={skipped_no_evidence})",
        )

    logger.info(
        "[parser_v2] outline_pass: %d valid units (%d raw, %d evidence-misses)",
        len(outlines), len(raw_units), skipped_no_evidence,
    )
    return outlines


LESSONS_PASS_EXCERPT_CHARS = 8000
"""Char window around the unit's source_evidence we send to the
per-unit lessons LLM call. 8K is enough to capture a typical unit's
3-column table (Objectivos | Conteúdos | Resultados) + the
Sugestões metodológicas narrative that follows. Bigger excerpts
don't improve accuracy and increase latency + cost per unit."""


def _find_unit_body(full_text: str, anchor: str) -> int:
    """Locate an anchor at the unit BODY position, not at the table-of-
    contents entry that typically appears earlier in the document.

    Heuristic: PDFs of curriculum docs include the unit heading in two
    places — once in the index (typically followed by dots and a page
    number) and again at the actual body of the unit (followed by
    content like "OBJECTIVOS" / "O aluno" / a numbered subtopic).
    Prefer the LAST occurrence; that's almost always the body. (The
    index/TOC entry, if present, is by definition earlier in the
    document than the unit body it points to.)

    Tolerates whitespace differences between the LLM-supplied anchor
    and the actual document text. pdftotext leaves wonky spacing
    (column-wrap, page breaks, soft hyphens) so we layer four
    progressively-looser searches:
      1. exact rfind
      2. whitespace-tolerant regex of the full anchor
      3. whitespace-tolerant regex of the first 5-6 word tokens
         (handles the case where the LLM produced a longer
         evidence string than what's contiguous in the source —
         e.g. "Number (N9) CORE (SET 3+) EXTENDED (SETS 1 &2)
         Fractions Weeks 3 & 4" where pdftotext put intermediate
         words like ASSESSMENT in between)
      4. first 40 chars as bare substring

    Returns the character index, or -1 if not found.
    """
    if not anchor:
        return -1
    # 1. Exact match (rfind = last occurrence skips the TOC entry).
    idx = full_text.rfind(anchor)
    if idx != -1:
        return idx
    # Normalise "soft hyphen" line breaks — pdftotext extracts a
    # hyphenated word split across lines as "Geometry-\nshapes" which
    # becomes "Geometry- shapes" after whitespace collapse. The LLM
    # likely re-joins it as "Geometry-shapes" (no space). Collapsing
    # "-\s+" → "-" in BOTH sides reconciles them.
    text_norm = re.sub(r'-\s+', '-', full_text)
    probe = re.sub(r'-\s+', '-', anchor).strip()
    # 2. Exact rfind on the soft-hyphen-normalised text.
    if probe:
        idx = text_norm.rfind(probe)
        if idx != -1:
            return idx
    # 3. Whitespace-tolerant search for the FULL anchor.
    if probe:
        tokens = probe.split()
        if tokens:
            pattern = re.compile(
                r'\s+'.join(re.escape(p) for p in tokens),
                re.IGNORECASE,
            )
            matches = list(pattern.finditer(text_norm))
            if matches:
                return matches[-1].start()
            # 4. Try just the first 5-6 word tokens. The LLM may have
            # produced a longer evidence string than what's contiguous
            # in the PDF (pdftotext interleaves columns; words can
            # appear between the "logical" parts of the heading).
            if len(tokens) >= 5:
                short_tokens = tokens[:5]
                pattern = re.compile(
                    r'\s+'.join(re.escape(p) for p in short_tokens),
                    re.IGNORECASE,
                )
                matches = list(pattern.finditer(text_norm))
                if matches:
                    return matches[-1].start()
    # 5. Last resort: first 40 chars of probe as plain substring.
    short = probe[:40] if probe else ''
    if short:
        idx = text_norm.rfind(short)
        if idx != -1:
            return idx
        idx = text_norm.lower().rfind(short.lower())
        if idx != -1:
            return idx
    return -1


def _excerpt_for_unit(
    full_text: str, outline: UnitOutlineV2, all_outlines: list[UnitOutlineV2]
) -> str:
    """Find the text window for one unit. Anchored on
    ``outline.source_evidence`` at its BODY position (skipping the
    table-of-contents entry); bounded by the next unit's body if we
    find it within the excerpt window.

    Falls back to searching for the unit title verbatim if evidence
    is missing. Worst case (no anchor found), returns the first
    LESSONS_PASS_EXCERPT_CHARS chars of the document — the LLM will
    still produce SOMETHING but accuracy degrades.
    """
    anchor = outline.source_evidence or outline.title
    if not anchor:
        return full_text[:LESSONS_PASS_EXCERPT_CHARS]

    idx = _find_unit_body(full_text, anchor)
    if idx == -1:
        logger.warning(
            "[parser_v2] lessons_pass: anchor %r not found in document, "
            "falling back to first %d chars",
            anchor[:60], LESSONS_PASS_EXCERPT_CHARS,
        )
        return full_text[:LESSONS_PASS_EXCERPT_CHARS]

    # Upper boundary: the body position of the NEXT unit's anchor.
    # We search AFTER our current position so we don't pick up the
    # current unit's own TOC entry, and so we don't pick up a sibling
    # that has the same title across grades.
    next_idx = idx + LESSONS_PASS_EXCERPT_CHARS
    my_pos = all_outlines.index(outline) if outline in all_outlines else -1
    for sibling in all_outlines[my_pos + 1:] if my_pos >= 0 else []:
        sib_anchor = sibling.source_evidence or sibling.title
        if not sib_anchor:
            continue
        # Search for sibling body AFTER our current position
        sib_idx = full_text.rfind(sib_anchor)
        if sib_idx <= idx:
            # Sibling's last occurrence is before our position — skip
            # it (probably the same unit name in a different grade).
            continue
        # Use the FIRST sibling occurrence after our position (we want
        # the nearest boundary, not the last).
        sib_idx = full_text.find(sib_anchor, idx + 1)
        if sib_idx == -1:
            sib_idx = full_text.find(sib_anchor[:40].strip(), idx + 1)
        if sib_idx > idx:
            next_idx = min(next_idx, sib_idx)
            break

    return full_text[idx:next_idx]


def lessons_pass(unit: UnitOutlineV2, full_text: str, *, locale: str,
                 all_outlines: Optional[list[UnitOutlineV2]] = None,
                 existing_lessons: Optional[list[str]] = None) -> list[LessonV2]:
    """Extract the lessons for one unit. Called in a bounded thread-
    pool fan-out from ``parse_curriculum``.

    Approach:
      - Slice a focused ~8K char excerpt around the unit's
        source_evidence so the LLM stays grounded on this unit's
        content (and so we don't pay to re-send 100K chars per unit).
      - Single LLM call returning JSON. Schema-constrained.
      - Anti-hallucination: each lesson must include a verbatim
        snippet from the excerpt; lessons whose evidence isn't
        present are dropped.

    Raises exceptions on infra failure (LLM error, bad JSON after
    retry). The orchestrator's ThreadPoolExecutor catches and logs
    per-unit, so one unit failing doesn't kill the whole parse.
    """
    from ai_tutor.apps.curriculum.locale_prompts import locale_parser_hints

    all_outlines = all_outlines or [unit]
    excerpt = _excerpt_for_unit(full_text, unit, all_outlines)
    locale_hints = locale_parser_hints(locale)
    output_lang = _output_language_block(locale)

    # Re-parse dedupe context (empty → renders to "" → original prompt). On a
    # re-parse we list the lessons already in this unit and ask Claude to reuse
    # their EXACT titles for already-covered lessons so the additive writer
    # matches them instead of appending reworded duplicates.
    existing_lessons_block = ""
    dedupe_guideline = ""
    if existing_lessons:
        _lines = "\n".join(f"- {t}" for t in existing_lessons if t)
        existing_lessons_block = (
            f"\n<existing_lessons>\n"
            f"This unit already contains these lessons (from an earlier "
            f"upload):\n{_lines}\n"
            f"</existing_lessons>\n"
        )
        dedupe_guideline = (
            f"8. Some lessons already exist in this unit (see "
            f"<existing_lessons>). Reuse the EXACT existing title for any "
            f"lesson that is the same as one already listed; give a new title "
            f"only to a genuinely new lesson. Emit both the existing lessons "
            f"and any new ones.\n"
        )

    system_prompt = (
        "You are a curriculum-document lesson extractor. Given a single "
        "unit's worth of text from a curriculum / teaching-programme "
        "document, identify the INDIVIDUAL LESSONS that comprise it. "
        "A lesson typically maps to one teaching objective or one "
        "numbered topic in the unit's content list. Return ONLY lessons "
        "you can ANCHOR to a verbatim snippet from the provided text. "
        "Respond with a single JSON object."
        + output_lang
    )

    user_prompt = (
        # Document excerpt FIRST (query-last layout).
        f"<unit_excerpt unit_title=\"{unit.title}\" grade=\"{unit.grade_level}\">\n"
        f"{excerpt}\n"
        f"</unit_excerpt>\n"
        f"{locale_hints}"
        f"{existing_lessons_block}"
        f"\n<task>\n"
        f"Extract the individual lessons in this unit.\n\n"
        f"GUIDELINES:\n"
        f"1. A lesson maps to ONE main concept or skill. Typical signal: "
        f"a numbered subtopic (1.1, 1.2, …) or a discrete teaching "
        f"objective. Look at the document's own structure — don't "
        f"invent lessons.\n"
        f"2. Title should be SHORT and student-friendly — name the "
        f"concept, not the objective. \"The Cell Nucleus\" not "
        f"\"Students will understand the cell nucleus\".\n"
        f"3. Objective: 1-sentence terminal objective for this lesson "
        f"(what the student will be able to do).\n"
        f"4. enabling_objectives: 3-6 granular sub-skills (action-verb "
        f"statements). Pull these from the document's specific-"
        f"objectives column if available.\n"
        f"5. Aim for 3-12 lessons per unit. Fewer is fine if the unit "
        f"is small. Don't pad.\n"
        f"6. order: 1-indexed position within the unit.\n"
        f"7. For source_evidence: a 30-100 char verbatim snippet from "
        f"the excerpt that anchors this lesson (typically the "
        f"sub-heading or first content line). Anti-hallucination check.\n"
        f"{dedupe_guideline}"
        f"</task>"
    )

    parsed = _call_llm_structured(
        response_model=_LessonsResult,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=4096,
    )

    raw_lessons = parsed.lessons

    # Anti-hallucination filter — each lesson's evidence must appear in
    # the excerpt. Use the same whitespace-tolerant search as
    # _find_unit_body so pdftotext line-break artefacts don't drop
    # genuine lessons.
    lessons: list[LessonV2] = []
    skipped_no_evidence = 0
    for i, raw in enumerate(raw_lessons, start=1):
        # raw is a _LessonRaw pydantic instance.
        title = (raw.title or '').strip()
        objective = (raw.objective or '').strip()
        if not title or not objective:
            continue
        evidence = (raw.source_evidence or '').strip()
        if evidence:
            if _find_unit_body(excerpt, evidence) == -1 and \
               _find_unit_body(excerpt, evidence[:50]) == -1 and \
               _find_unit_body(excerpt, title) == -1:
                logger.warning(
                    "[parser_v2] lessons_pass dropping lesson %r for unit %r "
                    "— evidence not in excerpt.", title, unit.title,
                )
                skipped_no_evidence += 1
                continue
        eos = [str(e).strip() for e in (raw.enabling_objectives or []) if str(e).strip()]
        try:
            lessons.append(LessonV2(
                title=title,
                objective=objective,
                enabling_objectives=eos,
                order=int(raw.order or i),
            ))
        except Exception:
            logger.warning(
                "[parser_v2] lessons_pass: malformed lesson dropped for unit %r: %r",
                unit.title, {'title': title}
            )

    logger.info(
        "[parser_v2] lessons_pass(%s, %s): %d lessons "
        "(%d raw, %d evidence-misses)",
        unit.title, unit.grade_level,
        len(lessons), len(raw_lessons), skipped_no_evidence,
    )
    return lessons


# ============================================================================
# v2 — LESSONS FAN-OUT (M4)
# ============================================================================


LESSONS_FANOUT_MAX_WORKERS = 3
"""Max concurrent per-unit lessons_pass calls. 3 is the sweet spot:
high enough to make a 15-unit doc fit in ~5 LLM round-trips of latency,
low enough to stay well under Anthropic's rate limits even on the
slower workload-profile tiers. Mirrors the bounded concurrency in
apps/tutoring/judges/__init__.py::run_all_judges."""

LESSONS_PASS_TIMEOUT_SECONDS = 90
"""Per-unit timeout. Sonnet 4.6 on an 8K excerpt typically returns
in 8-15s; 90s gives 6x headroom for network glitches without letting
one stuck unit block the whole parse forever."""


def _lessons_fanout(
    outlines: list[UnitOutlineV2],
    *,
    full_text: str,
    locale: str,
    progress_cb: Optional[Callable[[int], None]] = None,
    existing_structure: Optional[list[dict]] = None,
) -> tuple[list[UnitV2], int]:
    """Fan-out lessons_pass across all units with bounded concurrency.

    Fail-soft: per-unit exceptions are logged and that unit is returned
    with empty lessons[]. Only the orchestrator's "all failed" check
    raises ParseFailure. Same pattern as
    ``apps/tutoring/judges/__init__.py::run_all_judges``.

    Returns ``(units_with_lessons, failed_count)``.
    """
    import concurrent.futures as _cf

    # Map existing unit title (normalised) → its existing lesson titles, so each
    # per-unit lessons_pass can be told what's already there for dedupe. Empty
    # on first upload → lessons_pass sees existing_lessons=None (no change).
    existing_lessons_by_unit: dict[str, list[str]] = {}
    for u in (existing_structure or []):
        key = (u.get('title') or '').strip().lower()
        if key:
            existing_lessons_by_unit[key] = [
                t for t in (u.get('lessons') or []) if t
            ]

    units: list[Optional[UnitV2]] = [None] * len(outlines)
    failed = 0
    done = 0

    def _run_one(i: int, outline: UnitOutlineV2) -> tuple[int, list[LessonV2], Optional[Exception]]:
        try:
            lessons = lessons_pass(
                outline,
                full_text=full_text,
                locale=locale,
                all_outlines=outlines,
                existing_lessons=existing_lessons_by_unit.get(
                    (outline.title or '').strip().lower()
                ),
            )
            return i, lessons, None
        except Exception as e:
            logger.exception(
                "[parser_v2] lessons_pass FAILED for unit %r (%s)",
                outline.title, outline.grade_level,
            )
            return i, [], e

    with _cf.ThreadPoolExecutor(max_workers=LESSONS_FANOUT_MAX_WORKERS) as ex:
        futures = {
            ex.submit(_run_one, i, o): (i, o)
            for i, o in enumerate(outlines)
        }
        for fut in _cf.as_completed(futures, timeout=LESSONS_PASS_TIMEOUT_SECONDS * len(outlines)):
            try:
                i, lessons, err = fut.result(timeout=LESSONS_PASS_TIMEOUT_SECONDS)
            except _cf.TimeoutError:
                i, outline = futures[fut]
                logger.warning(
                    "[parser_v2] lessons_pass TIMEOUT for unit %r — "
                    "returning empty lessons[].", outline.title,
                )
                lessons, err = [], TimeoutError("per-unit timeout")

            if err is not None:
                failed += 1
            outline = futures[fut][1]
            units[i] = UnitV2(**outline.model_dump(), lessons=lessons)
            done += 1
            if progress_cb:
                try:
                    progress_cb(done)
                except Exception:
                    logger.exception("[parser_v2] lessons fanout progress_cb raised — ignored")

    # All slots filled (we initialised to None; should all be UnitV2 now).
    return [u for u in units if u is not None], failed


# ============================================================================
# v2 — PUBLIC ENTRY POINT
# ============================================================================


def parse_curriculum(
    file_path: str,
    *,
    subject_hint: str = '',
    grade_hint: str = '',
    locale: str = DEFAULT_LOCALE,
    institution_id: Optional[int] = None,
    progress_cb: Optional[Callable[[str, dict], None]] = None,
    first_page: Optional[int] = None,
    last_page: Optional[int] = None,
    existing_structure: Optional[list[dict]] = None,
) -> ParsedCurriculumV2:
    """Parse a curriculum file into a ``ParsedCurriculumV2`` structure.

    Always uses the LLM — never falls back to regex. Raises
    ``ParseFailure`` with a structured reason on any irrecoverable
    step.

    Args:
        file_path: PDF / DOCX / TXT / image path. Vision-OCR fallback
            in extract_text_from_file handles scanned PDFs.
        subject_hint: optional teacher-supplied subject ("Biology",
            "Mathematics"). Soft prior — LLM may override.
        grade_hint: optional teacher-supplied grade ("10ª Classe",
            "S3"). Soft prior.
        locale: BCP-47 locale code. Defaults to en-us. Soft prior —
            LLM may override based on document content.
        institution_id: optional, used by future KB-grounded passes
            (M3+) to align unit names with existing materials.
        progress_cb: optional ``(phase, data)`` callback so the upload
            UI can stream live updates. Phases:
            'extract', 'detect', 'outline', 'lessons', 'done'.
        first_page: optional 1-based first page (inclusive) to scope a
            single grade out of a multi-grade PDF. None = whole document.
        last_page: optional 1-based last page (inclusive). None = to end.
        existing_structure: optional list of the course's current units —
            ``[{'title', 'grade_level', 'lessons': [titles]}]`` — supplied on a
            re-parse so the outline/lessons passes reuse exact existing titles
            for already-covered units/lessons (dedupe). None on first upload.

    Returns:
        ParsedCurriculumV2.

    Raises:
        ParseFailure with one of the REASONS slugs.
    """
    def _emit(phase: str, **data):
        if progress_cb:
            try:
                progress_cb(phase, data)
            except Exception:
                logger.exception("[parser_v2] progress_cb raised — ignored")

    # ── 1. Extract text (delegates to archive — vision OCR + NUL strip etc.)
    _emit('extract', file=file_path, first_page=first_page, last_page=last_page)
    try:
        text, file_type = extract_text_from_file(
            file_path, first_page=first_page, last_page=last_page,
        )
    except OCRFailure as e:
        raise ParseFailure('no_text', f"OCR failed: {e.reason} — {e.detail}")
    if not text or len(text.strip()) < 100:
        raise ParseFailure(
            'no_text',
            f"extracted {len(text) if text else 0} chars from {file_path} "
            f"(file_type={file_type}); minimum 100 to attempt parse.",
        )

    # ── 2. Detect subject + locale + grade range
    _emit('detect', extracted_chars=len(text))
    detection = detect_subject_and_locale(
        text,
        subject_hint=subject_hint,
        grade_hint=grade_hint,
        locale_hint=locale,
    )
    # Override the caller-supplied locale with the LLM-detected one —
    # the teacher's dropdown is a soft prior; the document content wins.
    effective_locale = detection['locale']

    # ── 3. Outline pass (M3)
    _emit('outline', subject=detection['subject'], locale=effective_locale)
    outlines = outline_pass(
        text, subject=detection['subject'], locale=effective_locale,
        existing_units=existing_structure,
    )
    if not outlines:
        raise ParseFailure(
            'no_units_found',
            f"outline_pass returned 0 units for subject={detection['subject']!r}",
        )

    # ── 4. Lessons pass — bounded thread-pool fan-out (M4)
    _emit('lessons', unit_count=len(outlines))
    units, failed = _lessons_fanout(
        outlines, full_text=text, locale=effective_locale,
        progress_cb=lambda done: _emit('lesson_unit_done', done=done, total=len(outlines)),
        existing_structure=existing_structure,
    )
    if failed == len(outlines):
        raise ParseFailure(
            'lesson_pass_failed',
            f"all {len(outlines)} per-unit lessons_pass calls failed — see logs.",
        )

    _emit('done', units=len(units), lessons=sum(len(u.lessons) for u in units))
    return ParsedCurriculumV2(
        subject=detection['subject'],
        locale=effective_locale,
        grade_levels=detection['grade_levels'],
        description='',
        units=units,
        detection_disagreed_with_hint=detection['hint_disagreement'],
    )


# ============================================================================
# v2 — UPLOAD ORCHESTRATOR (M5)
# ----------------------------------------------------------------------------
# Overrides the archive's process_curriculum_upload + complete_curriculum_upload.
# Same call signature so apps/dashboard views + tasks keep working unchanged.
# Wires parse_curriculum() into the upload flow; surfaces ParseFailure as a
# clean upload.status='failed' with a structured reason slug rather than a
# stack trace. No regex fallback is reachable from this path.
# ============================================================================


def _v2_to_review_shape(parsed: ParsedCurriculumV2, target_grade: Optional[str] = None) -> dict:
    """Convert a ``ParsedCurriculumV2`` to the dict shape the upload
    review UI (templates/dashboard/curriculum/process.html) + the
    archive's ``create_curriculum_from_structure`` both expect.

    If ``target_grade`` is given, returns ONLY the units for that
    grade. Otherwise returns all units mixed (used at the review-UI
    step where teachers see the whole parse before the grade fanout
    happens on Approve).
    """
    units_for_payload = []
    for i, u in enumerate(parsed.units):
        if target_grade and u.grade_level != target_grade:
            continue
        # Collect unit-level terminal objectives from the lesson
        # objectives (the review UI expects them at unit level; v2
        # doesn't track them separately but lesson objectives are a
        # reasonable seed teachers can edit).
        terminal_objectives = [l.objective for l in u.lessons if l.objective]
        # Unit-level enabling objectives: dedupe across lessons.
        all_eos = []
        seen = set()
        for l in u.lessons:
            for eo in l.enabling_objectives:
                if eo and eo.lower() not in seen:
                    seen.add(eo.lower())
                    all_eos.append(eo)

        units_for_payload.append({
            'title': u.title,
            'grade_level': u.grade_level,
            'number': i + 1,
            'introduction': u.description,
            'description': u.description,
            'terminal_objectives': terminal_objectives[:10],
            'enabling_objectives': all_eos,
            'source_evidence': u.source_evidence,
            'lessons': [
                {
                    'title': l.title,
                    'objective': l.objective,
                    'enabling_objectives': l.enabling_objectives,
                    'teaching_strategies': [],
                    'resources': [],
                    'assessment_methods': [],
                    'estimated_minutes': 20,
                    'order': l.order,
                }
                for l in u.lessons
            ],
        })

    return {
        'subject': parsed.subject,
        'locale': parsed.locale,
        'grade_level': target_grade or '',  # for legacy single-grade callers
        'grade_levels': parsed.grade_levels,
        'description': parsed.description,
        'detection_disagreed_with_hint': parsed.detection_disagreed_with_hint,
        'units': units_for_payload,
        'parser_version': 'v2',
    }


def _build_existing_structure(upload) -> Optional[list[dict]]:
    """Snapshot the units/lessons already in the course(s) this upload feeds.

    Used on a re-parse so the parser can reuse exact existing titles (dedupe).
    Returns ``[{'title', 'grade_level', 'lessons': [titles]}]`` or ``None`` when
    the upload hasn't produced a course yet (first upload → no dedupe context).
    """
    from ai_tutor.apps.curriculum.models import Course
    from django.db.models import Q

    course_q = Q(curriculum_upload=upload)
    if upload.created_course_id:
        course_q |= Q(id=upload.created_course_id)
    courses = (
        Course.objects.filter(course_q)
        .distinct()
        .prefetch_related('units__lessons')
    )

    structure: list[dict] = []
    for course in courses:
        for unit in course.units.all():
            structure.append({
                'title': unit.title,
                'grade_level': unit.grade_level or course.grade_level or '',
                'lessons': [l.title for l in unit.lessons.all()],
            })
    return structure or None


def process_curriculum_upload(upload_id: int, skip_review: bool = False) -> dict:
    """v2 orchestrator. Replaces the archive's regex-fallback chain
    with the LLM-first ``parse_curriculum()``. Same call signature so
    apps/dashboard call sites keep working unchanged.

    Status transitions:
      pending → processing → review (or → failed)
      review  → (teacher approves via complete_curriculum_upload) → completed
    """
    from ai_tutor.apps.dashboard.models import CurriculumUpload

    upload = CurriculumUpload.objects.get(id=upload_id)

    def _bump(phase: str, data: dict):
        """Forward parser_v2 progress events to the upload log so the
        review UI's live-progress widget stays informative.

        Signature matches the `progress_cb(phase, data_dict)` contract
        in parse_curriculum._emit — note `data` is a dict, not kwargs.
        """
        data = data or {}
        if phase == 'extract':
            upload.add_log(f"📄 Step 1/4: Extracting text from {upload.file_path}…")
        elif phase == 'detect':
            upload.add_log(
                f"   ✓ Extracted {data.get('extracted_chars', 0):,} characters."
            )
            upload.add_log("🔍 Step 2/4: Classifying subject, locale, and grade…")
        elif phase == 'outline':
            upload.add_log(
                f"   ✓ Detected: subject={data.get('subject')!r}, "
                f"locale={data.get('locale')!r}"
            )
            upload.add_log("📚 Step 3/4: Extracting unit-level structure…")
        elif phase == 'lessons':
            upload.add_log(
                f"   ✓ Found {data.get('unit_count', 0)} units."
            )
            upload.add_log(
                f"📖 Step 4/4: Extracting lessons (parallel fan-out, "
                f"max {LESSONS_FANOUT_MAX_WORKERS} concurrent)…"
            )
        elif phase == 'lesson_unit_done':
            done, total = data.get('done', 0), data.get('total', 0)
            if done % 3 == 0 or done == total:
                upload.add_log(f"   • {done}/{total} units processed")
        elif phase == 'done':
            pass  # final summary logged below
        upload.save(update_fields=['processing_log'])

    try:
        upload.status = 'processing'
        upload.current_step = 1
        upload.processing_log = ""
        upload.add_log("🚀 Starting curriculum parsing (v2 — locale-aware LLM)…")
        upload.add_log(f"   File: {upload.file_path}")
        upload.add_log(f"   Teacher hints: subject={upload.subject_name!r}, "
                       f"grade={upload.grade_level!r}, locale={getattr(upload, 'locale', None)!r}")
        first_page = getattr(upload, 'first_page', None)
        last_page = getattr(upload, 'last_page', None)
        if first_page or last_page:
            upload.add_log(
                f"   📑 Page range: {first_page or 'start'}–{last_page or 'end'} "
                f"(scoping this upload to one grade)"
            )
        # Re-parse dedupe context: snapshot existing units/lessons so the parser
        # reuses their exact titles instead of appending reworded duplicates.
        existing_structure = _build_existing_structure(upload)
        if existing_structure:
            upload.add_log(
                f"   ♻️  Re-parse: {len(existing_structure)} existing unit(s) "
                f"supplied to the parser for de-duplication."
            )
        upload.save()

        parsed = parse_curriculum(
            upload.file_path,
            subject_hint=upload.subject_name or '',
            grade_hint=upload.grade_level or '',
            locale=getattr(upload, 'locale', None) or DEFAULT_LOCALE,
            institution_id=upload.institution_id,
            progress_cb=_bump,
            first_page=first_page,
            last_page=last_page,
            existing_structure=existing_structure,
        )

        # Convert to review-UI shape (one payload, all units, all grades).
        structure = _v2_to_review_shape(parsed)
        upload.parsed_data = structure
        upload.extracted_text_length = sum(
            len(l.objective) + len(l.title) for u in parsed.units for l in u.lessons
        )  # rough proxy — original was extract_text_from_file len; not load-bearing

        unit_count = len(parsed.units)
        lesson_count = sum(len(u.lessons) for u in parsed.units)
        upload.add_log(
            f"✅ Parse complete: {unit_count} units, {lesson_count} lessons "
            f"across {len(parsed.grade_levels)} grade(s) "
            f"({', '.join(parsed.grade_levels)})."
        )
        if parsed.detection_disagreed_with_hint:
            upload.add_log(
                "   ⚠ Subject/locale detection disagreed with your hints — "
                "the document's content was trusted. See parsed_data.detection_*."
            )
        if len(parsed.grade_levels) > 1:
            upload.add_log(
                f"   ℹ️ Multi-grade document — on Approve, one Course will be "
                f"created per grade ({len(parsed.grade_levels)} Courses total)."
            )

        if skip_review:
            upload.save()
            return complete_curriculum_upload(upload_id)

        upload.status = 'review'
        upload.add_log("⏸️ Ready for teacher review — confirm and Approve to "
                       "generate the Course / Unit / Lesson rows.")
        upload.save()

        return {
            'success': True,
            'status': 'review',
            'units_count': unit_count,
            'lessons_count': lesson_count,
            'grades': parsed.grade_levels,
            'parser_version': 'v2',
        }

    except ParseFailure as e:
        logger.warning(
            "[parser_v2] process_curriculum_upload: ParseFailure(%s): %s",
            e.reason, e.detail,
        )
        upload.status = 'failed'
        upload.error_message = f"{e.reason}: {e.detail}"
        upload.add_log(f"❌ Parse failed — reason: {e.reason}")
        if e.detail:
            upload.add_log(f"   Detail: {e.detail[:300]}")
        # Reason-specific actionable next steps for the teacher.
        if e.reason == 'no_text':
            upload.add_log(
                "   This usually means the PDF is a scanned image. Try "
                "uploading a text-PDF version, or wait — vision OCR is "
                "active and may have just failed on this specific page set."
            )
        elif e.reason == 'subject_unclassified':
            upload.add_log(
                "   The LLM couldn't identify the subject from the first "
                "page. Pre-fill the Subject field on the upload form and "
                "try again."
            )
        elif e.reason in ('llm_unavailable', 'llm_error'):
            upload.add_log(
                "   The AI parser is unavailable right now. Check ModelConfig "
                "for purpose='generation' in the admin, or retry shortly."
            )
        upload.save()
        return {
            'success': False,
            'status': 'failed',
            'reason': e.reason,
            'detail': e.detail,
        }
    except Exception as e:
        logger.exception("[parser_v2] process_curriculum_upload: unexpected error")
        upload.status = 'failed'
        upload.error_message = str(e)
        upload.add_log(f"❌ Unexpected error: {type(e).__name__}: {e}")
        upload.save()
        raise


def complete_curriculum_upload(upload_id: int, feedback: str = "") -> dict:
    """v2 completion — fans out by grade. One Course row per grade
    detected in the document; each unit lands under its grade's Course.

    For single-grade docs this is equivalent to the archive's one-
    Course-per-upload behaviour. For multi-grade docs (e.g. Mozambique
    Biology 10ª/11ª/12ª) it creates 3 Course rows. The data model
    already supports this (Course.grade_level is a single string);
    we just needed to actually fan out instead of jamming everything
    under one Course.

    Args:
        upload_id: CurriculumUpload row id.
        feedback: optional teacher feedback string (not currently used
            in v2 — review-time edits are written directly to
            upload.parsed_data by the review form).

    Returns:
        dict with course_ids, total units_created, lessons_created.
    """
    from ai_tutor.apps.dashboard.models import CurriculumUpload
    from ai_tutor.apps.accounts.models import Institution

    upload = CurriculumUpload.objects.get(id=upload_id)
    structure = upload.parsed_data or {}
    if not structure or not structure.get('units'):
        upload.add_log("❌ complete_curriculum_upload: no parsed_data — nothing to commit.")
        upload.status = 'failed'
        upload.error_message = 'empty parsed_data'
        upload.save()
        return {'success': False, 'reason': 'empty_parsed_data'}

    # Multi-grade fanout: group units by their grade_level. If units
    # don't carry a grade_level (older data, or — observed during M7
    # E2E — review form POST that scraped units without preserving the
    # per-unit grade_level field), fall back to the LLM-detected
    # grade_levels from parsed_data FIRST, then upload.grade_level as
    # a last resort. The detected grade is the authoritative one;
    # upload.grade_level is the teacher's hint which the parser
    # already explicitly overrode at detect time.
    fallback_grade = (
        (structure.get('grade_levels') or [None])[0]
        or upload.grade_level
        or ''
    )
    units_by_grade: dict[str, list] = {}
    for unit in structure['units']:
        g = (unit.get('grade_level') or fallback_grade or '').strip()
        units_by_grade.setdefault(g, []).append(unit)

    upload.add_log(
        f"📦 Creating Course rows for {len(units_by_grade)} grade(s): "
        f"{', '.join(sorted(units_by_grade.keys()))}"
    )

    inst = Institution.objects.filter(id=upload.institution_id).first() if upload.institution_id else None
    course_ids = []
    total_units = 0
    total_lessons = 0

    # Re-upload / re-parse targeting: if this upload already produced a course,
    # merge straight back into it so a re-parse can never spawn a duplicate
    # course when detection shifts the computed "{subject} {grade}" title. Only
    # engages for the grade matching that course (or when the parse is
    # single-grade); other grades fall back to title matching.
    target_course = upload.created_course if upload.created_course_id else None

    # Detected locale to stamp on each Course row. The archive's
    # create_curriculum_from_structure doesn't read or set locale —
    # we patch it on after the row is created so downstream content
    # generation picks the right register.
    detected_locale = structure.get('locale') or 'en-us'

    from ai_tutor.apps.curriculum.models import Course as _Course

    for grade, units in sorted(units_by_grade.items()):
        # Build a structure dict for this grade and hand off to the
        # archive's create_curriculum_from_structure (it still works
        # fine; we just feed it a single-grade slice). After M5 this
        # is the only place the archive's structure-creation code runs
        # from the runtime path.
        per_grade_struct = {
            'subject': structure.get('subject', 'General'),
            'grade_level': grade,
            'description': structure.get('description', ''),
            'units': units,
        }
        # Engage the target course only for the matching grade (or when the
        # parse yielded a single grade — the re-upload common case).
        _tc = None
        if target_course is not None and (
            len(units_by_grade) == 1
            or (target_course.grade_level or '').strip() == (grade or '').strip()
        ):
            _tc = target_course
        result = create_curriculum_from_structure(
            per_grade_struct, inst, upload=upload, target_course=_tc,
        )
        cid = result.get('course_id')
        course_ids.append(cid)
        total_units += result.get('units_created', 0)
        total_lessons += result.get('lessons_created', 0)

        # Stamp the detected locale on the Course so content
        # generation runs in the correct register. The archive's
        # create_curriculum_from_structure ignores locale.
        if cid:
            _Course.objects.filter(id=cid).update(locale=detected_locale)

        upload.add_log(
            f"   ✓ {grade or '(no grade)'}: Course #{cid} "
            f"({result.get('units_created', 0)} units, "
            f"{result.get('lessons_created', 0)} lessons, "
            f"locale={detected_locale!r})"
        )

    upload.status = 'completed'
    upload.units_created = total_units
    upload.lessons_created = total_lessons
    # Pick the first course as the primary "created_course" link the UI uses.
    if course_ids:
        from ai_tutor.apps.curriculum.models import Course
        upload.created_course = Course.objects.filter(id=course_ids[0]).first()
    upload.add_log(
        f"🎉 Done — {len(course_ids)} course(s) created across "
        f"{len(units_by_grade)} grade(s): {total_units} units, "
        f"{total_lessons} lessons total."
    )
    upload.save()

    return {
        'success': True,
        'course_ids': course_ids,
        'units_created': total_units,
        'lessons_created': total_lessons,
        'parser_version': 'v2',
    }
