# media_library

File storage for images used by lesson steps.

---

## Models

### MediaAsset

A reusable media file scoped to an institution. Used by `ImageGenerationService` to store generated images and by teachers uploading custom images via the step edit page.

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

---

## How Media Works

All lesson step media is stored in `LessonStep.media` JSONField (a dict with an `images` list). Each image entry has `url`, `alt`, `caption`, `description`, `type`, and `source` fields.

`MediaAsset` provides the underlying file storage. When an image is generated or uploaded, a `MediaAsset` record is created and the resulting file URL is written into the step's media JSON.

### Media sources
- **Content generation pipeline**: `content_generator.py` writes image descriptions into `LessonStep.media`, then `ImageGenerationService` generates the images and updates the URLs.
- **Teacher upload/replace**: Teachers can upload or replace images on the step edit page. Creates a `MediaAsset` and adds/updates the URL in the step's media JSON.
- **Regenerate**: Teachers can regenerate AI images from the step edit page using the image prompt/description.

### Cleanup
When a course is deleted, `_cleanup_orphaned_media_assets()` in `apps/curriculum/signals.py` deletes `MediaAsset` records whose file URLs are not referenced by any `LessonStep.media` JSON.
