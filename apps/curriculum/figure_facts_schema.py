"""Pydantic schema for `MediaAsset.figure_facts` (F1 of
memory/figure_facts_plan.md).

Each figure ships with structured visual facts the tutor uses at
runtime to anchor scaffolding in real labelled features — instead of
asking the student to "imagine two parallel lines". Five fields:

  - type — short LLM-friendly category tag
  - scene_description — concrete prose, what the student is looking at
  - labelled_features — every labelled point/line/region with position
  - angle_relationships — for geometry: structured equalities/sums
  - extra_facts — free-form facts not fitting the structured fields
  - anchor_prompts — pre-authored visualization questions usable verbatim

Validated at storage time (extractor + manual edits both go through
Pydantic). Lesson `is_published` gates approval — no separate flag.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class LabelledFeature(BaseModel):
    """A single labelled element in the figure (a point, line, region)."""
    label: str = Field(
        ...,
        description=(
            "The label text exactly as it appears in the figure. For "
            "diagrams this is typically a short identifier (e.g. '1', "
            "'A', 'l', 'x'). For textbook pages indexed as figures, "
            "this may be a full caption, callout, or short text passage."
        ),
        # Was 40 — bumped 2026-05-15 because textbook-page assets pushed
        # full captions into this field, triggering instructor-style
        # validation-retry loops that burned 2-3 Anthropic round-trips
        # per page in the production material-processor. 200 covers
        # legitimate diagram labels AND realistic caption text without
        # accepting paragraph-length content (use `extra_facts` for those).
        max_length=200,
    )
    location: str = Field(
        ...,
        description=(
            "Where this label appears in the figure. Must be specific "
            "enough that the tutor can direct the student's eye "
            "(e.g. 'top-left of the upper intersection', 'the diagonal "
            "line cutting through both parallel lines')."
        ),
        max_length=200,
    )
    color: Optional[str] = Field(
        default=None,
        description="Color of the labelled feature, when present (e.g. 'blue', 'pink').",
        max_length=40,
    )


class AngleRelationship(BaseModel):
    """A single verified angle relationship asserted by the figure."""
    pair: List[int] = Field(
        ...,
        description="The two angle labels (integers) involved in the relationship.",
        min_length=2,
        max_length=2,
    )
    relationship: Literal[
        "corresponding",
        "alternate_interior",
        "alternate_exterior",
        "co_interior",
        "vertically_opposite",
        "supplementary",
        "complementary",
    ] = Field(..., description="The named relationship between the pair.")
    equal: Optional[bool] = Field(
        default=None,
        description="True when the two angles are equal under this relationship.",
    )
    sum: Optional[int] = Field(
        default=None,
        description="When the relationship asserts a sum (e.g. co-interior = 180), the sum.",
    )

    @field_validator("pair")
    @classmethod
    def _no_self_pair(cls, v: List[int]) -> List[int]:
        if v[0] == v[1]:
            raise ValueError("pair must reference two DIFFERENT angle labels")
        return v


class FigureFacts(BaseModel):
    """Top-level schema for `MediaAsset.figure_facts`.

    `type` describes the figure category for the tutor's awareness.
    `scene_description` and `labelled_features` are required — the
    other fields are optional but typically populated for math figures.

    Non-geometry figures (maps, charts, photos) leave
    `angle_relationships` empty and lean on `scene_description` +
    `labelled_features` + `extra_facts` + `anchor_prompts`.
    """
    type: str = Field(
        ...,
        description=(
            "Short category tag for the figure "
            "(e.g. 'parallel_lines_with_transversal', 'bar_chart', "
            "'map_of_seychelles', 'unstructured')."
        ),
        max_length=80,
    )
    scene_description: str = Field(
        ...,
        description=(
            "1-3 sentences describing what the student sees. Must be "
            "concrete enough to anchor scaffolding in (the tutor reads "
            "this verbatim into its prose)."
        ),
        max_length=800,
    )
    labelled_features: List[LabelledFeature] = Field(
        default_factory=list,
        description="Every labelled point/line/region in the figure.",
    )
    angle_relationships: List[AngleRelationship] = Field(
        default_factory=list,
        description="For geometry figures: structured equalities and sums.",
    )
    extra_facts: List[str] = Field(
        default_factory=list,
        description="Free-form facts the figure asserts (axis labels, panel callouts, captions).",
    )
    anchor_prompts: List[str] = Field(
        default_factory=list,
        description=(
            "Pre-authored visualization questions a tutor can use VERBATIM "
            "to direct the student's attention to specific labelled "
            "features. These bypass the no-authoring rule because they "
            "were verified at content-gen / backfill time."
        ),
    )
    generation_prompt: Optional[str] = Field(
        default=None,
        description=(
            "Original LLM prompt used to generate this figure. Stored so "
            "the runtime tutor can see what the figure was MEANT to "
            "depict — extra context for scaffolding when a student asks "
            "about something the extracted facts don't cover. Only set "
            "for newly-generated figures; backfilled figures don't have "
            "the original prompt available."
        ),
    )

    @field_validator("scene_description")
    @classmethod
    def _scene_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("scene_description is required and must not be empty")
        return v.strip()


def validate_figure_facts(data: dict) -> tuple[Optional[FigureFacts], Optional[str]]:
    """Parse `data` into a FigureFacts instance.

    Returns (FigureFacts, None) on success, or (None, error_message) on
    failure. Used by the extractor and by any callers that want to
    reject malformed `figure_facts` before persisting them.
    """
    try:
        return FigureFacts.model_validate(data), None
    except Exception as e:
        return None, str(e)
