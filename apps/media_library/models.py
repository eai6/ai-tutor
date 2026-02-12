"""
Media Library app - MediaAsset and StepMedia models.

Simple approach: upload assets to a library, then attach them to lesson steps.
This allows reuse of media across multiple lessons.
"""

from django.db import models
from apps.accounts.models import Institution
from apps.curriculum.models import LessonStep


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


class StepMedia(models.Model):
    """
    Attaches a media asset to a lesson step with placement info.
    """
    class Placement(models.TextChoices):
        TOP = 'top', 'Above content'
        INLINE = 'inline', 'Inline with content'
        SIDE = 'side', 'Side panel'

    lesson_step = models.ForeignKey(
        LessonStep,
        on_delete=models.CASCADE,
        related_name='media_attachments'
    )
    media_asset = models.ForeignKey(
        MediaAsset,
        on_delete=models.CASCADE,
        related_name='step_usages'
    )
    placement = models.CharField(
        max_length=10,
        choices=Placement.choices,
        default=Placement.TOP
    )
    order_index = models.PositiveIntegerField(
        default=0,
        help_text="Order when multiple media attached to same step"
    )

    class Meta:
        ordering = ['order_index']
        verbose_name = "Step Media Attachment"

    def __str__(self):
        return f"{self.media_asset.title} on {self.lesson_step}"
