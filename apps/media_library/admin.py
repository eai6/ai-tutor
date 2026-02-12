from django.contrib import admin
from .models import MediaAsset, StepMedia


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ['title', 'institution', 'asset_type', 'created_at']
    list_filter = ['asset_type', 'institution']
    search_fields = ['title', 'tags', 'caption']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(StepMedia)
class StepMediaAdmin(admin.ModelAdmin):
    list_display = ['media_asset', 'lesson_step', 'placement', 'order_index']
    list_filter = ['placement']
    raw_id_fields = ['lesson_step', 'media_asset']
