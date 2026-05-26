"""MediaService — lesson-scoped media catalog injection.

Phase 2 §2.2 / §3.2 inlined thin selector. Phase 3 extracts a richer
version (KB-similarity ranking, dual-coding directives).

Per R8 + the preserved-runtime-surfaces section of the plan:
  - Catalog is scoped at the Lesson level, NOT per step.
  - ``Course.tutoring_images_enabled`` is honoured — when False the
    catalog returns ``[]`` and no MEDIA block is rendered.
  - The ``|||MEDIA:N|||`` signal parser is lifted forward unchanged.
  - Multi-tenancy: ``Q(institution=inst) | Q(institution__isnull=True)``
    pattern when querying ``MediaAsset``.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# Legacy signal — kept verbatim. NEVER use the deprecated
# ``[SHOW_MEDIA:title]`` fuzzy form (deleted from the legacy engine).
_MEDIA_SIGNAL_RE = re.compile(r"\|\|\|MEDIA:(\d+)\|\|\|", re.IGNORECASE)


class MediaService:
    """Thin per-lesson catalog selector + media-signal parser."""

    def build_catalog(
        self,
        *,
        lesson_id: int,
        institution_id: int,
        max_entries: int = 6,
    ) -> list[dict]:
        """Return up to ``max_entries`` lesson-scoped catalog entries.

        Each entry: ``{"id": int, "title": str, "description": str,
        "figure_facts": list[str]}``. The tutor's per-move prompts
        receive this list; the LLM may emit ``|||MEDIA:N|||`` to
        attach the N-th entry (1-based).

        Returns ``[]`` when the course has
        ``tutoring_images_enabled=False`` — the entire MEDIA block is
        suppressed from the prompt by ``StudentTutor``.
        """
        try:
            from django.db.models import Q
            from apps.curriculum.models import Lesson
            from apps.media_library.models import MediaAsset
        except Exception:
            return []

        try:
            lesson = (
                Lesson.objects
                .select_related("unit__course")
                .filter(pk=lesson_id)
                .first()
            )
        except Exception:
            lesson = None
        if lesson is None:
            return []

        course = getattr(getattr(lesson, "unit", None), "course", None)
        if course is not None and not getattr(course, "tutoring_images_enabled", True):
            return []

        # Pull lesson-scoped media references from each LessonStep's
        # legacy ``media`` JSON blob — that's where authoring sites
        # currently land image-asset IDs. Multi-tenancy-safe lookup.
        asset_ids: set[int] = set()
        for step in lesson.steps.all() if hasattr(lesson, "steps") else []:
            media = getattr(step, "media", None) or {}
            images = media.get("images", []) if isinstance(media, dict) else []
            for img in images[:6]:
                if isinstance(img, dict):
                    aid = img.get("asset_id") or img.get("id")
                    try:
                        if aid is not None:
                            asset_ids.add(int(aid))
                    except (TypeError, ValueError):
                        continue

        if not asset_ids:
            return []

        try:
            qs = (
                MediaAsset.objects
                .filter(pk__in=asset_ids)
                .filter(
                    Q(institution_id=institution_id)
                    | Q(institution__isnull=True)
                )[: max_entries]
            )
        except Exception as exc:
            logger.warning(
                "[MediaService] catalog query raised %s — returning empty",
                type(exc).__name__,
            )
            return []

        entries: list[dict] = []
        for asset in qs:
            entries.append({
                "id": asset.pk,
                "title": getattr(asset, "title", "") or "",
                "description": getattr(asset, "scene_description", "") or "",
                "figure_facts": _coerce_figure_facts(
                    getattr(asset, "figure_facts", None),
                ),
            })
        return entries

    def parse_signal(self, text: str) -> tuple[str, list[int]]:
        """Parse and strip the trailing ``|||MEDIA:N|||`` signal.

        Per CLAUDE.md: the tutor appends the signal as the LAST line.
        Returns ``(clean_text, [N, ...])``. Multiple signals are
        tolerated (lists in order of appearance). Never raises.
        """
        if not text:
            return "", []
        indices: list[int] = []
        for m in _MEDIA_SIGNAL_RE.finditer(text):
            try:
                indices.append(int(m.group(1)))
            except (TypeError, ValueError):
                continue
        cleaned = _MEDIA_SIGNAL_RE.sub("", text).strip()
        return cleaned, indices

    def figure_facts_for_indices(
        self,
        *,
        catalog: list[dict],
        indices: list[int],
    ) -> list[str]:
        """Flatten figure_facts strings for the attached catalog entries.

        Indices are 1-based per the MEDIA signal convention. Out-of-
        range indices are silently dropped — the parser is tolerant.
        """
        facts: list[str] = []
        for idx in indices:
            i = idx - 1
            if 0 <= i < len(catalog):
                for fact in catalog[i].get("figure_facts", []) or []:
                    if fact:
                        facts.append(str(fact))
        return facts


def _coerce_figure_facts(raw) -> list[str]:
    """Normalise figure_facts into a flat ``list[str]``."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        # Accept the legacy "scene_description / labelled_features"
        # split — flatten values.
        out: list[str] = []
        for v in raw.values():
            if isinstance(v, list):
                out.extend(str(x) for x in v if x)
            elif v:
                out.append(str(v))
        return out
    return []
