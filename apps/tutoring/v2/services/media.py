"""MediaService — lesson-scoped media catalog injection.

Phase 1: skeleton. Phase 2 calls a thin inlined version; Phase 3
extracts it as a proper service with the |||MEDIA:N||| parser lifted
forward unchanged and ``Course.tutoring_images_enabled`` honored.
"""

from __future__ import annotations


class MediaService:
    """Skeleton. Phase 2 inlines a thin version; Phase 3 extracts."""

    def build_catalog(self, lesson_id: int, institution_id: int) -> list[dict]:
        """Lesson-scoped figures + KB-similarity top-N entries (R8)."""
        raise NotImplementedError("MediaService.build_catalog — Phase 3")

    def parse_signal(self, text: str) -> tuple[str, list[int]]:
        """Parse and strip the trailing |||MEDIA:N||| signal.

        Lifted forward unchanged in Phase 3. Returns (clean_text,
        media_indices).
        """
        raise NotImplementedError("MediaService.parse_signal — Phase 3")
