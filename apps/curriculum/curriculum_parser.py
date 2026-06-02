"""Curriculum parser — public interface.

This module is the canonical import path for curriculum parsing
(`from apps.curriculum.curriculum_parser import …`). During the v2
rewrite (`memory/curriculum_parser_v2_plan.md`) it is a thin re-export
shim over the legacy implementation in ``curriculum_parser_archive``.

After M2-M5 of the plan, the new locale-aware LLM-based parser will
be implemented IN THIS FILE, and this shim becomes a real module.
The archive stays in tree as a safety net until v2 is validated
across multiple country/language pilots; see the deletion follow-up
in §8 of the plan.

DO NOT import from ``curriculum_parser_archive`` directly — use this
file. The shim contract:

  Text extraction layer (stays in archive, will be moved out / shared
  in M5+):
    - extract_text_from_file, extract_from_pdf, extract_from_docx,
      extract_from_image, extract_figures_from_pdf,
      extract_curriculum_with_vision, _strip_nul
    - OCRFailure exception
    - _classify_llm_error, _render_page_within_b64_limit
      (internals used by vision_ocr / material_tasks)

  Structure-extraction layer (TO BE REPLACED by v2):
    - parse_curriculum_file, parse_curriculum_with_llm,
      parse_mathematics_curriculum, parse_geography_curriculum,
      parse_generic_curriculum, detect_subject
    - create_lessons_from_objectives, create_curriculum_from_structure
    - process_curriculum_upload, complete_curriculum_upload
    - ParsedCurriculum dataclass
    - FigureDescription, FigureExtractionResult pydantic models
"""
from __future__ import annotations

# ---------------------------------------------------------------------
# Text extraction layer — kept verbatim, not slated for replacement.
# ---------------------------------------------------------------------
from apps.curriculum.curriculum_parser_archive import (  # noqa: F401
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
)

# ---------------------------------------------------------------------
# Structure extraction — these are the functions v2 will replace.
# Kept as transitive re-exports so existing call sites keep working
# while v2 is built out in M2-M4. After M5, only `parse_curriculum`
# (new) and `process_curriculum_upload` (rewired) should be in use;
# the per-subject parsers become unreachable from runtime code.
# ---------------------------------------------------------------------
from apps.curriculum.curriculum_parser_archive import (  # noqa: F401
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
