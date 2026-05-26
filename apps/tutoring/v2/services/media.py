"""MediaService — lesson-scoped media catalog injection.

Phase 3 §3.2 — extracted form (KB-similarity ranking + dual-coding
directives). Lifts the figure_facts block injection here too; the
extended figure_ref conformance check reads from
``MediaAsset.figure_facts`` populated at authoring time.

Per R8 + the preserved-runtime-surfaces section of the plan:
  - Catalog is scoped at the Lesson level, NOT per step.
  - ``Course.tutoring_images_enabled`` is honoured — when False the
    catalog returns ``[]`` and no MEDIA block is rendered.
  - The ``|||MEDIA:N|||`` signal parser is lifted forward unchanged.
  - Multi-tenancy: ``Q(institution=inst) | Q(institution__isnull=True)``
    pattern when querying ``MediaAsset``.

KB-similarity ranking uses cheap lexical-overlap scoring against the
(objective + recent_text) signal. This stays inside the request path
budget — no embedding query per turn. The full ChromaDB embedding
path remains reserved for the authoring-time figure-description
pipeline (``CurriculumKnowledgeBase.query_for_figure_descriptions``).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# Legacy signal — kept verbatim. NEVER use the deprecated
# ``[SHOW_MEDIA:title]`` fuzzy form (deleted from the legacy engine).
_MEDIA_SIGNAL_RE = re.compile(r"\|\|\|MEDIA:(\d+)\|\|\|", re.IGNORECASE)

# Dual-coding directive language — Ch. 14 of science-principles.md
# ("verbal + visual throughout"). Surfaced into per-move prompts via
# ``dual_coding_directive()`` so the tutor knows *when* and *how* to
# reach for the MEDIA signal.
_DUAL_CODING_DIRECTIVE = (
    "When a figure in the catalog clarifies a concrete step, attach it "
    "with `|||MEDIA:N|||` and pair it with one sentence of verbal "
    "explanation — not a replacement for words. Skip the figure when "
    "the verbal explanation alone is sufficient; do NOT attach a "
    "figure just because one exists."
)


# Tokens shorter than this are dropped before scoring. Keeps the
# overlap-Jaccard from matching on stopwords / one-letter variables.
_MIN_TOKEN_LEN = 2

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "for", "on", "and", "or",
    "is", "are", "was", "were", "be", "been", "being", "this", "that",
    "these", "those", "with", "as", "at", "by", "it", "its", "from",
    "we", "you", "i", "he", "she", "they", "them", "his", "her",
}


class MediaService:
    """Lesson-scoped catalog selector + signal parser + dual-coding hook."""

    def build_catalog(
        self,
        *,
        lesson_id: int,
        institution_id: int,
        max_entries: int = 6,
        topic_hint: str = "",
        recent_text: str = "",
    ) -> list[dict]:
        """Return up to ``max_entries`` lesson-scoped catalog entries.

        Each entry: ``{"id": int, "title": str, "description": str,
        "figure_facts": list[str], "score": float}``. When
        ``topic_hint`` or ``recent_text`` is non-empty, entries are
        ranked by lexical overlap against that signal — most-relevant
        first. With no signal, ordering falls back to the lesson's
        natural step order. The tutor's per-move prompts receive this
        list; the LLM may emit ``|||MEDIA:N|||`` to attach the N-th
        entry (1-based).

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
                )
            )
        except Exception as exc:
            logger.warning(
                "[MediaService] catalog query raised %s — returning empty",
                type(exc).__name__,
            )
            return []

        signal_tokens = _tokenize(f"{topic_hint} {recent_text}")
        entries: list[dict] = []
        for asset in qs:
            title = getattr(asset, "title", "") or ""
            desc = getattr(asset, "scene_description", "") or ""
            facts = _coerce_figure_facts(getattr(asset, "figure_facts", None))
            score = (
                _relevance_score(
                    signal_tokens,
                    _tokenize(" ".join([title, desc, *facts])),
                )
                if signal_tokens
                else 0.0
            )
            entries.append({
                "id": asset.pk,
                "title": title,
                "description": desc,
                "figure_facts": facts,
                "score": score,
            })

        # Rank by overlap when we had a signal; otherwise preserve
        # the lesson's authoring order (stable for unscored entries).
        if signal_tokens:
            entries.sort(key=lambda e: e.get("score", 0.0), reverse=True)
        return entries[:max_entries]

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

    # ------------------------------------------------------------------
    # Dual-coding directive (Ch. 14 — verbal + visual throughout)
    # ------------------------------------------------------------------

    @staticmethod
    def dual_coding_directive() -> str:
        """Return the dual-coding directive sentence for move prompts.

        Surfaced inside the media catalog block whenever the catalog
        is non-empty — guides the model on *when* to attach a figure
        vs. when verbal explanation alone is sufficient.
        """
        return _DUAL_CODING_DIRECTIVE


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


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


def _tokenize(text: str) -> set[str]:
    """Lower-case word tokens, dropping stopwords and tiny tokens."""
    if not text:
        return set()
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_]*", text.lower())
    return {
        t for t in raw
        if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    }


def _relevance_score(signal: set[str], candidate: set[str]) -> float:
    """Jaccard-style overlap, bias toward signal coverage.

    Returns 0.0 when either side is empty. Symmetric Jaccard would
    underweight short titles; we use ``|A ∩ B| / max(1, |A|)`` so a
    figure whose tokens fully cover the topic ranks above one with
    incidental overlap.
    """
    if not signal or not candidate:
        return 0.0
    inter = len(signal & candidate)
    if inter == 0:
        return 0.0
    return inter / max(1, len(signal))
