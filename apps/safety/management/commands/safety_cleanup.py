"""
Management command for safety data cleanup.

Run periodically (e.g., daily via cron):
    python manage.py safety_cleanup
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = 'Clean up old data per retention policies (GDPR/FERPA compliance)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting',
        )
        parser.add_argument(
            '--conversation-days',
            type=int,
            default=90,
            help='Days to retain conversation data (default: 90)',
        )
        parser.add_argument(
            '--audit-days',
            type=int,
            default=365,
            help='Days to retain audit logs (default: 365)',
        )

    def handle(self, *args, **options):
        from apps.tutoring.models import SessionTurn, TutorSession
        from apps.safety.models import SafetyAuditLog
        
        dry_run = options['dry_run']
        conversation_days = options['conversation_days']
        audit_days = options['audit_days']
        
        now = timezone.now()
        conversation_cutoff = now - timedelta(days=conversation_days)
        audit_cutoff = now - timedelta(days=audit_days)
        
        self.stdout.write(f"Data cleanup started at {now}")
        self.stdout.write(f"  Conversation retention: {conversation_days} days (cutoff: {conversation_cutoff.date()})")
        self.stdout.write(f"  Audit log retention: {audit_days} days (cutoff: {audit_cutoff.date()})")
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - No data will be deleted"))
        
        # Count records to delete
        old_turns = SessionTurn.objects.filter(created_at__lt=conversation_cutoff)
        old_sessions = TutorSession.objects.filter(
            started_at__lt=conversation_cutoff,
            status='completed'
        )
        old_logs = SafetyAuditLog.objects.filter(timestamp__lt=audit_cutoff)
        
        turns_count = old_turns.count()
        sessions_count = old_sessions.count()
        logs_count = old_logs.count()
        
        self.stdout.write(f"\nRecords to delete:")
        self.stdout.write(f"  Session turns: {turns_count}")
        self.stdout.write(f"  Completed sessions: {sessions_count}")
        self.stdout.write(f"  Audit logs: {logs_count}")
        
        if not dry_run:
            # Delete in order (turns first due to foreign key)
            old_turns.delete()
            old_sessions.delete()
            old_logs.delete()
            
            # Log the cleanup
            SafetyAuditLog.objects.create(
                event_type='data_cleanup',
                details={
                    'turns_deleted': turns_count,
                    'sessions_deleted': sessions_count,
                    'logs_deleted': logs_count,
                    'conversation_cutoff': conversation_cutoff.isoformat(),
                    'audit_cutoff': audit_cutoff.isoformat(),
                },
                severity='info',
            )
            
            self.stdout.write(self.style.SUCCESS(f"\nCleanup complete!"))
        else:
            self.stdout.write(self.style.WARNING(f"\nDry run complete. Use without --dry-run to delete."))
