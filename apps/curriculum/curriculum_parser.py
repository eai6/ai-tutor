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

import json
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

from apps.curriculum.curriculum_parser_archive import (  # noqa: F401
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
    # legacy structure layer (re-exported until M5)
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
    process_curriculum_upload,
    complete_curriculum_upload,
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


# ============================================================================
# v2 — LLM CLIENT WRAPPER
# ============================================================================


def _get_llm_client():
    """Return (client, model_name) for the 'generation' purpose, or raise
    ParseFailure('llm_unavailable'). Centralised so every v2 LLM call
    surfaces the same structured error when ModelConfig isn't configured.
    """
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client

    cfg = ModelConfig.get_for('generation')
    if cfg is None:
        raise ParseFailure(
            'llm_unavailable',
            "No ModelConfig found for purpose='generation'. Configure one in the LLM admin.",
        )
    return get_llm_client(cfg), getattr(cfg, 'model_name', 'unknown')


def _call_llm_structured(
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
    temperature: float = 0.1,
) -> dict:
    """Call the generation LLM and parse a JSON object out of the
    response. Mirrors the robust JSON extraction in
    ``content_generator._try_fix_json`` — tolerates markdown fences,
    leading prose, and single-quoted-dict outputs from some providers.

    Raises ParseFailure('llm_error') on JSON parse failure after one
    retry. Raises ParseFailure('llm_unavailable') if no client.
    """
    client, model_name = _get_llm_client()
    logger.info("[parser_v2] LLM call: model=%s max_tokens=%d", model_name, max_tokens)

    try:
        response = client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as e:
        raise ParseFailure('llm_error', f"provider call failed: {type(e).__name__}: {e}")

    text = (getattr(response, 'content', None) or '').strip()
    if not text:
        raise ParseFailure('llm_error', f"empty response from {model_name}")

    parsed = _extract_json_object(text)
    if parsed is None:
        # One retry with a sharper "JSON only, no prose, no markdown" reminder.
        logger.warning(
            "[parser_v2] First-pass JSON parse failed (response was %d chars). Retrying.",
            len(text),
        )
        retry_user = (
            user_prompt
            + "\n\n<output_format>Return ONLY a single JSON object. No prose, "
            "no markdown fences, no explanation. The first character of your "
            "response must be `{`.</output_format>"
        )
        try:
            response = client.generate(
                messages=[{"role": "user", "content": retry_user}],
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=0.0,  # tighter on retry
            )
        except Exception as e:
            raise ParseFailure('llm_error', f"retry call failed: {type(e).__name__}: {e}")
        text = (getattr(response, 'content', None) or '').strip()
        parsed = _extract_json_object(text)
        if parsed is None:
            raise ParseFailure(
                'llm_error',
                f"could not parse JSON from response after retry. preview: {text[:300]!r}",
            )
    return parsed


def _extract_json_object(text: str) -> Optional[dict]:
    """Best-effort JSON-object extraction. Returns None on hard failure
    rather than raising, so the caller can decide retry vs. surface.
    Handles:
      - bare JSON
      - ```json ... ``` fenced
      - prose before/after the object
      - single-quoted Python-dict-style output (a known Gemini failure)
    """
    if not text:
        return None
    s = text.strip()
    # Strip markdown fences if present.
    if s.startswith('```'):
        lines = s.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].startswith('```'):
            lines = lines[:-1]
        s = '\n'.join(lines).strip()
    # Find the outermost {...}
    start = s.find('{')
    end = s.rfind('}')
    if start == -1 or end == -1 or end < start:
        return None
    candidate = s[start:end + 1]
    # Try strict JSON.
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Try fixing single-quoted dicts → double quotes. Same trick as
    # content_generator handles for Gemini outputs.
    try:
        fixed = re.sub(r"'", '"', candidate)
        # Restore apostrophes inside words: "don't" was wrecked above —
        # but for parser-level metadata this is rare; accept the risk
        # since the field values are mostly programmatic.
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


# ============================================================================
# v2 — DETECTION (M2 deliverable)
# ============================================================================


SUPPORTED_LOCALES = ('en-us', 'pt-mz')  # extend as pilots land
DEFAULT_LOCALE = 'en-us'


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
        f"Classify this document. Return JSON with this exact shape:\n"
        f"{{\n"
        f'  "subject": "Biology",          // one of: Mathematics, Geography, '
        f'Biology, Physics, Chemistry, History, Science, English, '
        f'Portuguese, French, Other (use specific names from the doc, not '
        f'"General")\n'
        f'  "locale": "pt-mz",             // BCP-47 lowercase. Supported: '
        f'{list(SUPPORTED_LOCALES)}. If the doc is in a language we don\'t '
        f'list, pick the closest supported locale.\n'
        f'  "grade_levels": ["10ª Classe", "11ª Classe", "12ª Classe"],  '
        f'// distinct grade labels EXACTLY AS THEY APPEAR in the doc. Do not '
        f'coerce "10ª Classe" to "S3" or vice versa.\n'
        f'  "hint_disagreement": false,    // true iff your detection differs '
        f'meaningfully from the teacher_hints\n'
        f'  "rationale": "Title page reads `Programa de Ensino da Disciplina '
        f'de Biologia ... 2º Ciclo`, with grade-overview table covering 10ª, '
        f'11ª and 12ª Classe. Document is in Portuguese."  // 1-2 sentences.\n'
        f"}}\n"
        f"</task>"
    )

    parsed = _call_llm_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=512,
    )

    subject = (parsed.get('subject') or '').strip()
    locale_raw = (parsed.get('locale') or '').strip().lower()
    grade_levels = parsed.get('grade_levels') or []
    if not isinstance(grade_levels, list):
        grade_levels = [str(grade_levels)]
    grade_levels = [str(g).strip() for g in grade_levels if str(g).strip()]
    hint_disagreement = bool(parsed.get('hint_disagreement'))
    rationale = (parsed.get('rationale') or '').strip()

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


def outline_pass(text: str, *, subject: str, locale: str) -> list[UnitOutlineV2]:
    """Extract just the unit-level shape from the full document text.
    Returns a list of UnitOutlineV2; lessons are filled in by
    ``lessons_pass`` per-unit. M3 deliverable.
    """
    raise NotImplementedError("M3 deliverable — see memory/curriculum_parser_v2_plan.md §M3")


def lessons_pass(unit: UnitOutlineV2, full_text: str, *, locale: str) -> list[LessonV2]:
    """Extract the lessons for one unit. Called in a bounded thread-pool
    fan-out from ``parse_curriculum``. M4 deliverable.
    """
    raise NotImplementedError("M4 deliverable — see memory/curriculum_parser_v2_plan.md §M4")


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
    _emit('extract', file=file_path)
    try:
        text, file_type = extract_text_from_file(file_path)
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
    outlines = outline_pass(text, subject=detection['subject'], locale=effective_locale)
    if not outlines:
        raise ParseFailure(
            'no_units_found',
            f"outline_pass returned 0 units for subject={detection['subject']!r}",
        )

    # ── 4. Lessons pass — bounded thread-pool fan-out (M4)
    _emit('lessons', unit_count=len(outlines))
    units = []
    failed = 0
    for outline in outlines:
        try:
            lessons = lessons_pass(outline, full_text=text, locale=effective_locale)
        except Exception:
            logger.exception("[parser_v2] lessons_pass failed for unit %r", outline.title)
            lessons = []
            failed += 1
        units.append(UnitV2(**outline.model_dump(), lessons=lessons))
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
