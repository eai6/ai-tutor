"""Models for the in-app help assistant.

Three append-only tables:
  - HelpAssistantConversation: one per chat session
  - HelpAssistantMessage: each user / assistant turn
  - HelpAssistantToolCall: every tool invocation (proposed → confirmed → executed)

See memory/help_assistant_plan.md.
"""

from django.db import models
from django.contrib.auth.models import User


class HelpAssistantConversation(models.Model):
    """A single help-chat session between a user and the assistant.

    Persisted across page navigations; the support audit dashboard
    reads these (Phase 5). Idempotent — never deleted automatically.
    """

    class Audience(models.TextChoices):
        STUDENT = 'student', 'Student'
        TEACHER = 'teacher', 'Teacher'
        SUPER_ADMIN = 'super_admin', 'Super Admin'

    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='help_conversations',
    )
    audience = models.CharField(
        max_length=15, choices=Audience.choices, default=Audience.STUDENT,
        help_text="Resolved at conversation start; drives tool catalog filtering.",
    )
    page_url_at_start = models.CharField(max_length=500, blank=True, default='')
    user_agent = models.CharField(max_length=500, blank=True, default='')

    started_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_message_at']
        indexes = [
            models.Index(fields=['user', '-last_message_at']),
        ]

    def __str__(self):
        return f"Help chat #{self.id} — @{self.user.username} ({self.audience})"


class HelpAssistantMessage(models.Model):
    """One turn of a help-assistant conversation. Append-only."""

    class Role(models.TextChoices):
        USER = 'user', 'User'
        ASSISTANT = 'assistant', 'Assistant'
        TOOL_RESULT = 'tool_result', 'Tool Result'

    conversation = models.ForeignKey(
        HelpAssistantConversation, on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=15, choices=Role.choices)
    content = models.TextField(blank=True, default='')
    # Citations as a list of {source, section_title, anchor} dicts.
    citations = models.JSONField(default=list, blank=True)
    # When the assistant proposes a tool call, it lives here as
    # {tool_use_id, name, inputs} so the UI can render a confirmation
    # card. Resolved into a HelpAssistantToolCall row when confirmed.
    tool_proposals = models.JSONField(default=list, blank=True)
    # Optional pointer to an escalated FeedbackReport — set when the
    # user clicked "Send to support" from this message.
    escalated_to_feedback = models.ForeignKey(
        'dashboard.FeedbackReport',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['conversation', 'created_at']),
        ]

    def __str__(self):
        snippet = (self.content or '')[:60]
        return f"[{self.role}] {snippet}"


class HelpAssistantToolCall(models.Model):
    """Audit record for every tool the assistant invoked.

    Tracks the full lifecycle: proposed by the LLM → confirmed (or
    cancelled) by the user → executed (or errored) by the handler.
    Read-only tools skip straight from proposed → executed.
    """

    class Status(models.TextChoices):
        PROPOSED = 'proposed', 'Proposed'
        CONFIRMED = 'confirmed', 'Confirmed (pending execution)'
        EXECUTED = 'executed', 'Executed'
        CANCELLED = 'cancelled', 'Cancelled by user'
        ERRORED = 'errored', 'Errored'
        REJECTED = 'rejected', 'Rejected (permission denied)'

    conversation = models.ForeignKey(
        HelpAssistantConversation, on_delete=models.CASCADE,
        related_name='tool_calls',
    )
    message = models.ForeignKey(
        HelpAssistantMessage, on_delete=models.CASCADE,
        related_name='tool_calls',
        help_text="The assistant turn that proposed the call.",
    )
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )
    tool_name = models.CharField(max_length=64)
    inputs = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.PROPOSED,
    )
    requires_confirmation = models.BooleanField(default=False)

    proposed_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-proposed_at']
        indexes = [
            models.Index(fields=['conversation', '-proposed_at']),
            models.Index(fields=['tool_name', '-proposed_at']),
        ]

    def __str__(self):
        return f"{self.tool_name} [{self.status}]"


# ─── Help-assistant KB chunk (pgvector) ────────────────────────────────
# Replaces the ChromaDB-backed HelpKB at apps/support/kb.py. Same
# embedding model (sentence-transformers all-MiniLM-L6-v2, 384 d)
# and same audience metadata, but stored in Postgres so the index
# survives container restarts. Previously HelpKB lived under
# /tmp/vectordb/support which was wiped on every deploy + re-built
# from source by ``python manage.py build_help_index`` in the
# Dockerfile CMD — the rebuild masked the persistence bug, but at
# the cost of a slow cold-start step that's now redundant.
import hashlib  # noqa: E402

from apps.curriculum.vector_field import VectorField  # noqa: E402


class SupportChunk(models.Model):
    """One chunk of help/admin documentation. pgvector-backed."""

    # Stable chunk id — set by the indexer (e.g. 'help_faq:dashboard').
    # The same id is reused across rebuilds so re-indexing updates in
    # place instead of duplicating.
    chunk_id = models.CharField(max_length=200, unique=True)

    content = models.TextField()
    embedding = VectorField(
        dimensions=384,
        help_text="sentence-transformers all-MiniLM-L6-v2 vector. text column on SQLite.",
    )
    content_hash = models.CharField(max_length=64, db_index=True)

    source = models.CharField(
        max_length=64, blank=True, default='',
        help_text="help_faq / admin_guide / dashboard / etc."
    )
    section_title = models.CharField(max_length=200, blank=True, default='')
    audience = models.CharField(
        max_length=32, default='all',
        help_text="'all' (visible to students + teachers) or 'staff' (teachers + super_admins only)."
    )
    anchor = models.CharField(max_length=200, blank=True, default='')
    extra = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['source', 'audience'], name='supportchunk_src_aud_idx'),
        ]

    def __str__(self):
        return f"SupportChunk(id={self.chunk_id!r}, src={self.source!r}, aud={self.audience!r})"

    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
