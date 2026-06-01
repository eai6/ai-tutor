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
