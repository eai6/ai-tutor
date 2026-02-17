"""
Management command for GDPR right to erasure.

Delete all user data:
    python manage.py delete_user_data --user-id 123 --confirm
"""

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = 'Delete all data for a user (GDPR right to erasure)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user-id',
            type=int,
            required=True,
            help='User ID to delete data for',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirm deletion (required)',
        )
        parser.add_argument(
            '--keep-anonymized',
            action='store_true',
            help='Keep anonymized records for analytics',
        )
        parser.add_argument(
            '--delete-account',
            action='store_true',
            help='Also delete the user account',
        )

    def handle(self, *args, **options):
        from apps.safety import DataPrivacy
        from apps.safety.models import SafetyAuditLog
        
        User = get_user_model()
        user_id = options['user_id']
        
        if not options['confirm']:
            raise CommandError("You must use --confirm to delete data. This action is irreversible!")
        
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise CommandError(f"User with ID {user_id} not found")
        
        self.stdout.write(self.style.WARNING(f"Deleting data for user: {user.username} (ID: {user_id})"))
        
        keep_anonymized = options['keep_anonymized']
        
        if keep_anonymized:
            self.stdout.write("  Mode: Anonymize (keeping anonymized records)")
        else:
            self.stdout.write("  Mode: Full deletion")
        
        # Count before deletion
        from apps.tutoring.models import SessionTurn, TutorSession, StudentLessonProgress
        
        turns_count = SessionTurn.objects.filter(session__student=user).count()
        sessions_count = TutorSession.objects.filter(student=user).count()
        progress_count = StudentLessonProgress.objects.filter(student=user).count()
        
        self.stdout.write(f"\nData to be {'anonymized' if keep_anonymized else 'deleted'}:")
        self.stdout.write(f"  Session turns: {turns_count}")
        self.stdout.write(f"  Sessions: {sessions_count}")
        self.stdout.write(f"  Progress records: {progress_count}")
        
        # Delete/anonymize data
        DataPrivacy.delete_user_data(user, keep_anonymized=keep_anonymized)
        
        # Log the deletion
        SafetyAuditLog.objects.create(
            event_type='data_delete',
            user_id=user_id,
            user_hash=DataPrivacy.anonymize_user_id(user_id),
            details={
                'turns_affected': turns_count,
                'sessions_affected': sessions_count,
                'progress_deleted': progress_count,
                'mode': 'anonymize' if keep_anonymized else 'delete',
            },
            severity='warning',
        )
        
        # Optionally delete the account
        if options['delete_account']:
            username = user.username
            user.delete()
            self.stdout.write(self.style.SUCCESS(f"\nUser account '{username}' deleted"))
        
        self.stdout.write(self.style.SUCCESS(f"\nData {'anonymized' if keep_anonymized else 'deleted'} successfully!"))
