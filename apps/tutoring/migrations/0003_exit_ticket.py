# Generated migration for exit ticket models

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('tutoring', '0002_tutorsession_engine_state'),
        ('curriculum', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExitTicket',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('passing_score', models.PositiveIntegerField(default=8, help_text='Minimum correct answers to pass (out of 10)')),
                ('time_limit_minutes', models.PositiveIntegerField(default=10, help_text='Time limit for exit ticket (0 = no limit)')),
                ('instructions', models.TextField(blank=True, default='Answer all 10 questions. You need 8 correct to pass.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('lesson', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='exit_ticket', to='curriculum.lesson')),
            ],
            options={
                'verbose_name': 'Exit Ticket',
                'verbose_name_plural': 'Exit Tickets',
            },
        ),
        migrations.CreateModel(
            name='ExitTicketQuestion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('question_text', models.TextField(help_text='The question stem')),
                ('option_a', models.CharField(max_length=500)),
                ('option_b', models.CharField(max_length=500)),
                ('option_c', models.CharField(max_length=500)),
                ('option_d', models.CharField(max_length=500)),
                ('correct_answer', models.CharField(choices=[('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')], help_text='The correct option (A, B, C, or D)', max_length=1)),
                ('explanation', models.TextField(blank=True, help_text='Explanation of the correct answer (shown after submission)')),
                ('difficulty', models.CharField(choices=[('easy', 'Easy (recall)'), ('medium', 'Medium (apply)'), ('hard', 'Hard (analyze)')], default='medium', max_length=10)),
                ('order_index', models.PositiveIntegerField(default=0, help_text='Order in the exit ticket (0-9)')),
                ('image', models.ImageField(blank=True, help_text='Optional image for the question', null=True, upload_to='exit_tickets/')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('exit_ticket', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='questions', to='tutoring.exitticket')),
            ],
            options={
                'verbose_name': 'Exit Ticket Question',
                'ordering': ['order_index'],
            },
        ),
        migrations.CreateModel(
            name='ExitTicketAttempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('score', models.PositiveIntegerField(default=0)),
                ('passed', models.BooleanField(default=False)),
                ('answers', models.JSONField(default=dict)),
                ('started_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('exit_ticket', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attempts', to='tutoring.exitticket')),
                ('session', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='exit_ticket_attempts', to='tutoring.tutorsession')),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='exit_ticket_attempts', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-started_at'],
            },
        ),
    ]
