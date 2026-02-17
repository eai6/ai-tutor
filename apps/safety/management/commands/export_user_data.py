"""
Management command for GDPR data export.

Export all user data for data portability:
    python manage.py export_user_data --user-id 123 --output user_data.json
"""

import json
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = 'Export all data for a user (GDPR data portability)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user-id',
            type=int,
            required=True,
            help='User ID to export data for',
        )
        parser.add_argument(
            '--output',
            type=str,
            default='user_data.json',
            help='Output file path (default: user_data.json)',
        )
        parser.add_argument(
            '--pretty',
            action='store_true',
            help='Pretty print JSON output',
        )

    def handle(self, *args, **options):
        from apps.safety import DataPrivacy
        from apps.safety.models import SafetyAuditLog
        
        User = get_user_model()
        user_id = options['user_id']
        output_path = options['output']
        
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise CommandError(f"User with ID {user_id} not found")
        
        self.stdout.write(f"Exporting data for user: {user.username} (ID: {user_id})")
        
        # Export data
        export_data = DataPrivacy.export_user_data(user)
        
        # Add export metadata
        from django.utils import timezone
        export_data['export_metadata'] = {
            'exported_at': timezone.now().isoformat(),
            'user_id': user_id,
            'data_types': list(export_data.keys()),
        }
        
        # Write to file
        indent = 2 if options['pretty'] else None
        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=indent, default=str)
        
        # Log the export
        SafetyAuditLog.objects.create(
            event_type='data_export',
            user_id=user_id,
            user_hash=DataPrivacy.anonymize_user_id(user_id),
            details={
                'output_file': output_path,
                'sessions_count': len(export_data.get('sessions', [])),
                'progress_count': len(export_data.get('progress', [])),
            },
            severity='info',
        )
        
        self.stdout.write(self.style.SUCCESS(f"Data exported to: {output_path}"))
        self.stdout.write(f"  Sessions: {len(export_data.get('sessions', []))}")
        self.stdout.write(f"  Progress records: {len(export_data.get('progress', []))}")
