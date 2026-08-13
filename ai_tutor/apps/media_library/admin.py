from django.contrib import admin
from .models import MediaAsset


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ['title', 'institution', 'asset_type', 'created_at']
    list_filter = ['asset_type', 'institution']
    search_fields = ['title', 'tags', 'caption']
    readonly_fields = ['created_at', 'updated_at']
