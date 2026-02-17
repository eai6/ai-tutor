# Generated migration for safety app

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='SafetyAuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('event_type', models.CharField(choices=[
                    ('content_flagged', 'Content Flagged'),
                    ('rate_limited', 'Rate Limited'),
                    ('age_check', 'Age Verification'),
                    ('data_export', 'Data Export'),
                    ('data_delete', 'Data Deletion'),
                    ('login_attempt', 'Login Attempt'),
                    ('consent_given', 'Consent Given'),
                    ('consent_withdrawn', 'Consent Withdrawn'),
                    ('data_cleanup', 'Data Cleanup'),
                ], max_length=30)),
                ('user_id', models.IntegerField(blank=True, null=True)),
                ('user_hash', models.CharField(blank=True, max_length=16)),
                ('session_id', models.IntegerField(blank=True, null=True)),
                ('details', models.JSONField(default=dict)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.TextField(blank=True)),
                ('severity', models.CharField(choices=[
                    ('info', 'Info'),
                    ('warning', 'Warning'),
                    ('critical', 'Critical'),
                ], default='info', max_length=10)),
            ],
            options={
                'ordering': ['-timestamp'],
            },
        ),
        migrations.CreateModel(
            name='ConsentRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('consent_type', models.CharField(choices=[
                    ('data_processing', 'Data Processing'),
                    ('ai_tutoring', 'AI Tutoring'),
                    ('analytics', 'Analytics'),
                    ('parental', 'Parental Consent'),
                ], max_length=20)),
                ('given', models.BooleanField(default=False)),
                ('given_at', models.DateTimeField(blank=True, null=True)),
                ('withdrawn_at', models.DateTimeField(blank=True, null=True)),
                ('parent_email', models.EmailField(blank=True, max_length=254)),
                ('parent_name', models.CharField(blank=True, max_length=100)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consent_records', to='auth.user')),
            ],
            options={
                'unique_together': {('user', 'consent_type')},
            },
        ),
        migrations.AddIndex(
            model_name='safetyauditlog',
            index=models.Index(fields=['event_type', 'timestamp'], name='safety_safe_event_t_idx'),
        ),
        migrations.AddIndex(
            model_name='safetyauditlog',
            index=models.Index(fields=['user_hash', 'timestamp'], name='safety_safe_user_ha_idx'),
        ),
        migrations.AddIndex(
            model_name='safetyauditlog',
            index=models.Index(fields=['severity', 'timestamp'], name='safety_safe_severit_idx'),
        ),
    ]
