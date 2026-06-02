"""Locale-aware prompt block helper for content generation.

Shared by the lesson content generator and the exit-ticket generator
so that uploading a Portuguese curriculum on the platform produces
Portuguese lessons + MCQs without manual prompt tweaking. The helper
mirrors the per-turn locale injection in
``apps/tutoring/simple_tutor/prompts.py::_build_locale_rule`` so the
content gen and the runtime tutor stay in sync on register conventions.

Usage:

    from apps.curriculum.locale_prompts import locale_instruction_block

    sys_prompt = "...base instructions..." + locale_instruction_block(course.locale)

Returns an empty string for the English baseline so the existing
Seychelles cache key stays byte-identical — no cache churn for the
prod baseline.

Part of M5-prep of memory/portuguese_mozambique_pilot_plan.md.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def locale_instruction_block(locale: str) -> str:
    """Return an XML-tagged locale instruction for content generation
    prompts, or empty string for en-us.

    Inserted at the END of the system prompt (recency bias works in
    our favour — the model reads the language constraint just before
    generation starts).

    Register decisions match the tutor engine's
    ``simple_tutor/prompts.py::_build_locale_rule``:
      - 'tu' informal addressing (13-14 year-old students)
      - post-1990 Acordo Ortográfico spelling
      - Mozambique-specific vocabulary where it differs from European PT
      - Technical terms kept in standard Portuguese
      - Brand / product names stay in original Latin form

    Unknown locales fall back to a generic instruction so a
    misconfigured course doesn't silently produce English content.
    """
    code = (locale or "en-us").lower()
    if code in ("en-us", "en"):
        return ""
    if code == "pt-mz":
        return (
            "\n\n<locale>\n"
            "Generate ALL content (lesson text, teaching scripts, MCQ "
            "stems, options, explanations, vocabulary) in Mozambique "
            "Portuguese (pt-mz register).\n"
            "- Use 'tu' informal addressing throughout. These are 13-14 "
            "year-old secondary-school students.\n"
            "- Use post-1990 Acordo Ortográfico spelling. Examples: "
            "\"atividade\" not \"actividade\"; \"ótimo\" not \"óptimo\".\n"
            "- Keep technical vocabulary in standard Portuguese: "
            "\"ângulo\", \"escala\", \"fotossíntese\", \"ecossistema\", "
            "\"célula\", \"mitose\", \"ADN\".\n"
            "- Brand and product names stay in original Latin form "
            "(\"AI Tutor\" stays \"AI Tutor\"; do not translate it).\n"
            "- Localise place-based examples for a Mozambique context "
            "(Maputo, Beira, Nampula, Inhambane; metical / MZN for "
            "currency; familiar Mozambican geography and ecology where "
            "natural — but do not force it where the lesson topic is "
            "purely abstract).\n"
            "- Do not switch to English mid-content. Section headings, "
            "affirmations, error messages — all in Portuguese.\n"
            "</locale>\n"
        )
    # Defensive fallback for unrecognised locales.
    logger.warning(
        "locale_instruction_block: unknown locale '%s' — emitting generic "
        "language instruction. Add an explicit branch in "
        "apps/curriculum/locale_prompts.py to tailor the register.",
        code,
    )
    return (
        f"\n\n<locale>\n"
        f"Generate ALL content in the language identified by the locale "
        f"tag '{code}'. Do not switch to English mid-content.\n"
        f"</locale>\n"
    )


def locale_parser_hints(locale: str) -> str:
    """Locale-aware hints for the curriculum PARSER (distinct from
    ``locale_instruction_block``, which guides content GENERATION).

    Returns an XML-tagged block that tells the LLM what section
    terminology to recognise as structural cues in the source
    document. Without this, an English-prompted LLM tends to coerce
    PT structural labels into Seychelles equivalents (e.g. "Unidade
    Temática" → "Strand", "10ª Classe" → "S3"), which we don't want
    — the parser should produce labels as they appear in the source.

    Returns an empty string for ``en-us`` so the EN cache key stays
    byte-stable.

    Part of M3 in ``memory/curriculum_parser_v2_plan.md``.
    """
    code = (locale or "en-us").lower()
    if code in ("en-us", "en"):
        return ""
    if code == "pt-mz":
        return (
            "\n\n<locale_parser_hints locale=\"pt-mz\">\n"
            "This document is in Mozambique Portuguese. Recognise these "
            "section labels as STRUCTURAL CUES (not content):\n"
            "- \"Unidade Temática\" / \"Unidade\" = a unit / strand. The "
            "  Roman numeral or Arabic number that follows is its ordinal.\n"
            "- \"Conteúdos\" = the topic/content list within a unit. "
            "  Numbered entries (1.1, 1.2, …) are usually individual "
            "  lessons or lesson topics.\n"
            "- \"Objectivos Específicos\" = enabling objectives (the "
            "  granular sub-skills a lesson teaches).\n"
            "- \"Resultados de Aprendizagem\" = learning outcomes — what "
            "  the student should be able to do, typically phrased "
            "  \"O aluno: identifica…, explica…, aplica…\".\n"
            "- \"Sugestões metodológicas\" = teaching methodology notes "
            "  (narrative). NOT a unit or lesson.\n"
            "- \"Tema Transversal\" = cross-cutting theme. Treat as a "
            "  topic suggestion, not a separate unit.\n"
            "- \"Trimestre\" (1º / 2º / 3º) = academic term divisions. "
            "  Useful for ordering but do NOT create units from them.\n"
            "- \"10ª Classe\" / \"11ª Classe\" / \"12ª Classe\" = grade "
            "  levels. Use these labels VERBATIM — do not coerce to \"S1\", "
            "  \"S3\", \"Form 1\", or any other system's notation.\n"
            "- \"CH\" column = carga horária (hours). Ignore for structure "
            "  extraction.\n"
            "- \"Ficha Técnica\" = publication colophon (title, publisher, "
            "  copyright). NOT curriculum content — skip.\n"
            "- \"Índice\" = table of contents. Use it as ground truth for "
            "  unit + grade structure if available.\n"
            "Objective verbs in Portuguese to recognise as enabling-"
            "objective cues: interpretar, descrever, identificar, "
            "mencionar, explicar, aplicar, relacionar, comparar, "
            "distinguir, analisar, utilizar, reconhecer, demonstrar, "
            "definir, resolver, classificar, representar.\n"
            "</locale_parser_hints>\n"
        )
    # Defensive fallback: empty (no hints) so the LLM falls back to its
    # general reasoning. The detect_subject_and_locale step warns when
    # an unsupported locale is detected, so this is just safety net.
    logger.warning(
        "locale_parser_hints: no parser hints defined for locale '%s'. "
        "Add an explicit branch in apps/curriculum/locale_prompts.py.",
        code,
    )
    return ""
