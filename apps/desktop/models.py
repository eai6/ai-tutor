"""Device-local state for the offline desktop build.

Everything here is meaningful only on one installed machine. It is never
synced up and never exists on the server — the server has no concept of "this
device". Keeping it in its own app rather than bolting fields onto
``accounts`` keeps that boundary visible.

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import uuid

from django.db import models


class DeviceState(models.Model):
    """Single row (pk=1) describing this installation.

    Why a table and not a settings file: the content pack import, the student
    profile, and the sync bookkeeping all have to move together or not at all,
    and the DB already gives us transactions. A JSON file beside the DB can
    disagree with it after a crash.
    """

    SINGLETON_PK = 1

    institution_id = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Institution this device is bound to, set by the first "
                  "content pack import. Packs from other institutions are "
                  "refused — on a device there is no server to enforce the "
                  "multi-tenancy invariant.",
    )
    pack_version = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Version of the imported content pack. NULL = not provisioned.",
    )
    pack_imported_at = models.DateTimeField(null=True, blank=True)
    device_id = models.UUIDField(
        default=uuid.uuid4, editable=False,
        help_text="Stable identity for opt-in sync, so the server can tell "
                  "two devices apart without knowing anything else about them.",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'device state'
        verbose_name_plural = 'device state'

    def __str__(self) -> str:
        if self.pack_version is None:
            return 'unprovisioned device'
        return f'device (institution {self.institution_id}, pack v{self.pack_version})'

    @classmethod
    def load(cls) -> 'DeviceState':
        """Get-or-create the singleton. Safe to call on every request."""
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj

    def save(self, *args, **kwargs):
        # Force the singleton PK rather than trusting callers. A second row
        # would make `load()` non-deterministic and the failure would show up
        # much later as "my lessons disappeared".
        self.pk = self.SINGLETON_PK
        return super().save(*args, **kwargs)

    @property
    def is_provisioned(self) -> bool:
        return self.pack_version is not None
