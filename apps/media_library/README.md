# media_library

Reusable media asset management with lesson step attachments.

Provides a library of media files (images, audio, video, PDFs) that can be uploaded per institution and attached to lesson steps with placement and ordering controls.

---

## Models

### MediaAsset

A reusable media file scoped to an institution.

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution) | Owner school |
| `title` | CharField(200) | Display title |
| `asset_type` | CharField | `image`, `audio`, `video`, `pdf` |
| `file` | FileField | Uploaded file (path: `media/<institution_slug>/<filename>`) |
| `alt_text` | CharField(300) | Accessibility text for images |
| `caption` | TextField | Descriptive caption |
| `tags` | CharField(200) | Comma-separated tags for search |

**Upload path function:**
```python
def media_upload_path(instance, filename):
    return f"media/{instance.institution.slug}/{filename}"
```

Files are organized by institution slug to prevent collisions and enable per-school media browsing.

### StepMedia

Attachment of a `MediaAsset` to a `LessonStep` with placement and ordering.

| Field | Type | Description |
|-------|------|-------------|
| `lesson_step` | ForeignKey(LessonStep) | The step this media is attached to |
| `media_asset` | ForeignKey(MediaAsset) | The media file |
| `placement` | CharField | `top` (above content), `inline` (within text), `side` (side panel) |
| `order_index` | PositiveIntegerField | Order when multiple media are attached to the same step |

---

## Relationship to LessonStep.media JSON

There are two ways media is associated with lesson steps:

1. **`LessonStep.media` JSONField** -- Used by the content generator to store media descriptions and generated image URLs inline. This is the primary mechanism during content generation.

2. **`StepMedia` model** -- Used by the media library for explicit, reusable attachments with placement control. This is for manually curated media.

The `StepMedia` model via `media_attachments` related name allows querying attached library assets:
```python
step.media_attachments.all()  # StepMedia objects with placement info
```

---

## Architecture Decisions

- **Institution-scoped uploads** -- Files are organized under `media/<institution_slug>/` to prevent cross-school file access.
- **Dual media system** -- JSON-based inline media (for generated content) coexists with the relational `StepMedia` model (for curated library assets). This supports both automated and manual workflows.
- **Placement options** -- `top`, `inline`, `side` allow teachers to control how media appears relative to the step content in the tutoring interface.
