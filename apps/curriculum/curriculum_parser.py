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


def outline_pass(text: str, *, subject: str, locale: str) -> list[UnitOutlineV2]:
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
    from apps.curriculum.locale_prompts import locale_parser_hints

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
    )

    user_prompt = (
        # Document FIRST (query-last layout).
        f"<document>\n{doc_text}\n</document>\n"
        f"{locale_hints}"
        f"\n<context>\n"
        f"- subject: {subject}\n"
        f"- locale: {locale}\n"
        f"</context>\n"
        f"\n<task>\n"
        f"Extract the unit-level structure of this curriculum document.\n\n"
        f"GUIDELINES:\n"
        f"1. Identify TOP-LEVEL unit divisions only (chapters, units, "
        f"strands, themes, Unidades Temáticas, etc.) AS THEY APPEAR in "
        f"the source's table of contents / index / overview table. "
        f"Do NOT invent units, do NOT merge units the document treats "
        f"as separate.\n"
        f"2. DO NOT split a single unit into sub-units just because the "
        f"source has multiple tables for it. Sub-tables like 'Reino "
        f"Monera (continuação)' / 'Reino Protista (continuação)' / "
        f"'(continuação)' are CONTINUATIONS of one parent unit "
        f"(e.g. 'Sistemática dos Seres Vivos') — emit ONE outline entry "
        f"for the parent, not one per continuation table. The fine-"
        f"grained kingdom/phylum-level structure shows up later as "
        f"LESSONS within that unit.\n"
        f"3. If a unit recurs across multiple grades (e.g. 'Citologia' "
        f"in both 10ª Classe and 12ª Classe), emit ONE outline entry "
        f"per (unit, grade) pair — they will become separate Course "
        f"rows in our system, since each Course has a single grade.\n"
        f"4. Use grade labels EXACTLY as they appear (10ª Classe / S3 / "
        f"Form 4 / etc.). Do not translate or coerce.\n"
        f"5. SKIP non-structural content: title pages, copyright pages "
        f"('Ficha Técnica'), introductions, methodological notes, "
        f"glossaries, bibliographies. These are NOT units.\n"
        f"6. Provide a short (1-line) description per unit.\n"
        f"7. For source_evidence, paste a 30-100 char VERBATIM snippet "
        f"from the document where that unit is FIRST introduced "
        f"(typically the FIRST heading line — not a continuation). "
        f"This anchors the extraction.\n"
        f"8. Expected unit counts: a typical 3-grade secondary school "
        f"curriculum has 5-10 top-level units per grade. If you find "
        f"yourself emitting 20+ units, you are likely splitting too "
        f"finely — re-read guideline 2.\n"
        f"\nReturn JSON with this exact shape:\n"
        f"{{\n"
        f'  "units": [\n'
        f'    {{\n'
        f'      "title": "Unit name as in source",\n'
        f'      "grade_level": "Grade label as in source",\n'
        f'      "description": "One-line summary of what the unit covers",\n'
        f'      "source_evidence": "Verbatim heading text from the doc"\n'
        f'    }},\n'
        f'    ...\n'
        f'  ]\n'
        f"}}\n"
        f"</task>"
    )

    parsed = _call_llm_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=4096,
    )

    raw_units = parsed.get('units') or []
    if not isinstance(raw_units, list):
        raise ParseFailure(
            'no_units_found',
            f"outline_pass: response.units was {type(raw_units).__name__}, expected list",
        )

    # Validate + anti-hallucination check.
    text_lower = text.lower()
    outlines: list[UnitOutlineV2] = []
    skipped_no_evidence = 0
    for raw in raw_units:
        if not isinstance(raw, dict):
            continue
        title = (raw.get('title') or '').strip()
        grade = (raw.get('grade_level') or '').strip()
        if not title or not grade:
            continue
        description = (raw.get('description') or '').strip()
        evidence = (raw.get('source_evidence') or '').strip()

        # Anti-hallucination: the verbatim snippet should appear in the
        # source text (modulo whitespace + casing). If it doesn't,
        # the LLM probably invented this unit — drop it.
        if evidence:
            # Be lenient: collapse whitespace, lowercase, and look for
            # the first 30 chars of the evidence somewhere in the doc.
            ev_norm = ' '.join(evidence.split()).lower()
            ev_probe = ev_norm[:30] if len(ev_norm) > 30 else ev_norm
            text_norm = ' '.join(text.split()).lower()
            if ev_probe and ev_probe not in text_norm:
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
    and the actual document text — pdftotext leaves wonky spacing
    around colons and line breaks that the LLM may normalise.

    Returns the character index, or -1 if not found.
    """
    if not anchor:
        return -1
    # 1. Exact match (rfind = last occurrence skips the TOC entry).
    idx = full_text.rfind(anchor)
    if idx != -1:
        return idx
    # 2. Whitespace-tolerant search: build a regex where each run of
    # whitespace in the anchor matches one-or-more whitespace chars in
    # the text. This handles "II:  Genética" (LLM) vs "II: Genética"
    # (doc body), or vice versa.
    probe = anchor.strip()
    if probe:
        pattern = re.compile(
            r'\s+'.join(re.escape(p) for p in probe.split()),
            re.IGNORECASE,
        )
        matches = list(pattern.finditer(full_text))
        if matches:
            return matches[-1].start()  # last occurrence = body, not TOC
    # 3. Last resort: first 40 chars of probe.
    short = probe[:40] if probe else ''
    if short:
        idx = full_text.rfind(short)
        if idx != -1:
            return idx
        idx = full_text.lower().rfind(short.lower())
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
                 all_outlines: Optional[list[UnitOutlineV2]] = None) -> list[LessonV2]:
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
    from apps.curriculum.locale_prompts import locale_parser_hints

    all_outlines = all_outlines or [unit]
    excerpt = _excerpt_for_unit(full_text, unit, all_outlines)
    locale_hints = locale_parser_hints(locale)

    system_prompt = (
        "You are a curriculum-document lesson extractor. Given a single "
        "unit's worth of text from a curriculum / teaching-programme "
        "document, identify the INDIVIDUAL LESSONS that comprise it. "
        "A lesson typically maps to one teaching objective or one "
        "numbered topic in the unit's content list. Return ONLY lessons "
        "you can ANCHOR to a verbatim snippet from the provided text. "
        "Respond with a single JSON object."
    )

    user_prompt = (
        # Document excerpt FIRST (query-last layout).
        f"<unit_excerpt unit_title=\"{unit.title}\" grade=\"{unit.grade_level}\">\n"
        f"{excerpt}\n"
        f"</unit_excerpt>\n"
        f"{locale_hints}"
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
        f"\nReturn JSON with this exact shape:\n"
        f"{{\n"
        f'  "lessons": [\n'
        f'    {{\n'
        f'      "title": "Short concept name",\n'
        f'      "objective": "Terminal objective in 1 sentence",\n'
        f'      "enabling_objectives": ["Sub-skill 1", "Sub-skill 2", ...],\n'
        f'      "order": 1,\n'
        f'      "source_evidence": "Verbatim snippet from the excerpt"\n'
        f'    }},\n'
        f'    ...\n'
        f'  ]\n'
        f"}}\n"
        f"</task>"
    )

    parsed = _call_llm_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=4096,
    )

    raw_lessons = parsed.get('lessons') or []
    if not isinstance(raw_lessons, list):
        raise ParseFailure(
            'llm_error',
            f"lessons_pass for {unit.title!r}: response.lessons was "
            f"{type(raw_lessons).__name__}, expected list",
        )

    # Anti-hallucination filter — each lesson's evidence must appear in
    # the excerpt (case + whitespace insensitive, 30-char probe).
    excerpt_norm = ' '.join(excerpt.split()).lower()
    lessons: list[LessonV2] = []
    skipped_no_evidence = 0
    for i, raw in enumerate(raw_lessons, start=1):
        if not isinstance(raw, dict):
            continue
        title = (raw.get('title') or '').strip()
        objective = (raw.get('objective') or '').strip()
        if not title or not objective:
            continue
        evidence = (raw.get('source_evidence') or '').strip()
        if evidence:
            ev_norm = ' '.join(evidence.split()).lower()
            ev_probe = ev_norm[:30] if len(ev_norm) > 30 else ev_norm
            if ev_probe and ev_probe not in excerpt_norm:
                logger.warning(
                    "[parser_v2] lessons_pass dropping lesson %r for unit %r "
                    "— evidence not in excerpt.", title, unit.title,
                )
                skipped_no_evidence += 1
                continue
        eos = raw.get('enabling_objectives') or []
        if not isinstance(eos, list):
            eos = [str(eos)]
        eos = [str(e).strip() for e in eos if str(e).strip()]
        try:
            lessons.append(LessonV2(
                title=title,
                objective=objective,
                enabling_objectives=eos,
                order=int(raw.get('order') or i),
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
) -> tuple[list[UnitV2], int]:
    """Fan-out lessons_pass across all units with bounded concurrency.

    Fail-soft: per-unit exceptions are logged and that unit is returned
    with empty lessons[]. Only the orchestrator's "all failed" check
    raises ParseFailure. Same pattern as
    ``apps/tutoring/judges/__init__.py::run_all_judges``.

    Returns ``(units_with_lessons, failed_count)``.
    """
    import concurrent.futures as _cf

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
    units, failed = _lessons_fanout(
        outlines, full_text=text, locale=effective_locale,
        progress_cb=lambda done: _emit('lesson_unit_done', done=done, total=len(outlines)),
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
