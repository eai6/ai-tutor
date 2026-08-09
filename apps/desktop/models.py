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

    # Set once at enrolment, in exchange for a one-time code. Stored in the
    # clear because it lives in the device's own SQLite alongside the student
    # data it protects — hashing it here would guard nothing an attacker with
    # this file does not already have.
    sync_token = models.CharField(max_length=255, blank=True, default='')
    enrolled_at = models.DateTimeField(null=True, blank=True)

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


class RosterEntry(models.Model):
    """A student from the institution's roster, shipped in the content pack.

    This is NOT an auth_user row. It is the list a student picks their name
    from at first launch, and the carrier of the one field that makes sync
    possible: ``server_user_id``.

    The problem it solves: every device has its own auth_user table with
    device-local integer PKs, so the same real student registering on two
    laptops becomes two unrelated users, and nothing the device pushes can be
    attributed to a person the server knows. Pairing a local account with a
    roster entry stamps that student's work with an identity the cloud already
    has — decided offline, on a machine that may never have been online.

    Deliberately carries no password hash and no email. A pack travels on a USB
    stick between schools; it holds the minimum needed to show someone their
    own name. Authentication stays local.
    """

    server_user_id = models.PositiveIntegerField(
        unique=True,
        help_text="The student's primary key on the SERVER. Never a local id.",
    )
    username = models.CharField(max_length=150)
    display_name = models.CharField(max_length=300)
    grade_level = models.CharField(max_length=50, blank=True, default='')

    # Null until someone claims this entry on this device. Claiming is what
    # binds a local login to a server identity; SET_NULL rather than CASCADE so
    # deleting a local account releases the entry instead of erasing the
    # roster row the next sync needs.
    local_user = models.OneToOneField(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='roster_entry',
    )
    claimed_at = models.DateTimeField(null=True, blank=True)

    pack_version = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Pack version this entry last arrived in.',
    )

    class Meta:
        ordering = ['display_name']
        verbose_name_plural = 'roster entries'

    def __str__(self):
        return f'{self.display_name} (server #{self.server_user_id})'


class SyncOutbox(models.Model):
    """Work waiting to be pushed to the cloud.

    A table, not an in-memory queue. The device is a classroom laptop that gets
    closed mid-lesson and reopened days later; anything held in memory is lost
    exactly when it matters. Rows are enqueued as tutoring writes happen, so a
    lesson taught with no internet is still queued when the machine next sees a
    network.

    Payloads are self-contained JSON rather than foreign keys to local rows.
    The server has never heard of this device's integer PKs — what it can
    resolve is ``server_user_id`` from the roster and the ``client_uuid`` that
    makes a re-push idempotent. Freezing the payload at enqueue time also means
    a later edit cannot silently change what gets sent.
    """

    class Kind(models.TextChoices):
        SESSION = 'session', 'Tutoring session + turns'
        EXIT_TICKET = 'exit_ticket', 'Exit ticket attempt'
        PROGRESS = 'progress', 'Lesson progress'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Waiting to send'
        SENT = 'sent', 'Delivered'
        FAILED = 'failed', 'Given up'

    # Client-generated so the row can be referenced, and deduped by the server,
    # before the server has ever seen it.
    client_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    payload = models.JSONField(default=dict)

    # Which student this belongs to, in the SERVER's terms. Null means the
    # student self-registered and has no roster entry; those rows still sync,
    # flagged for a teacher to reconcile, rather than being dropped.
    server_user_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text='Backoff gate. The worker skips rows until this passes.',
    )
    last_error = models.TextField(blank=True, default='')
    sent_at = models.DateTimeField(null=True, blank=True)

    # Five attempts then park it. Retrying forever against a server that will
    # never accept the row burns battery and buries the real failures; a parked
    # row stays readable so a teacher can be told something needs attention.
    MAX_ATTEMPTS = 5

    class Meta:
        ordering = ['created_at']
        verbose_name_plural = 'sync outbox'
        indexes = [
            models.Index(fields=['status', 'next_attempt_at']),
        ]

    def __str__(self):
        return f'{self.kind} {self.client_uuid} ({self.status})'
