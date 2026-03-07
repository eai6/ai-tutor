"""
Media Library app - MediaAsset model for storing generated/uploaded images.

MediaAsset provides file storage for images used by lesson steps.
Lesson steps reference images via their LessonStep.media JSONField.
"""

from django.db import models
from apps.accounts.models import Institution


def media_upload_path(instance, filename):
    """Organize uploads by institution: media/<institution_slug>/<filename>"""
    return f"media/{instance.institution.slug}/{filename}"


class MediaAsset(models.Model):
    """
    A reusable media file (image, audio, video, PDF).
    """
    class AssetType(models.TextChoices):
        IMAGE = 'image', 'Image'
        AUDIO = 'audio', 'Audio'
        VIDEO = 'video', 'Video'
        PDF = 'pdf', 'PDF Document'

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name='media_assets'
    )
    title = models.CharField(max_length=200)
    asset_type = models.CharField(
        max_length=10,
        choices=AssetType.choices
    )
    file = models.FileField(upload_to=media_upload_path)
    alt_text = models.CharField(
        max_length=300,
        blank=True,
        help_text="Accessibility text for images"
    )
    caption = models.TextField(blank=True)
    tags = models.CharField(
        max_length=200,
        blank=True,
        help_text="Comma-separated tags for search"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Media Asset"

    def __str__(self):
        return f"{self.title} ({self.asset_type})"
