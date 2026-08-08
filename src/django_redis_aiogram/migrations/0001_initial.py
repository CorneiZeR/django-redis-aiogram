"""The first migration this package has ever shipped: the event log table."""

from typing import ClassVar

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    """Create the append-only event feed and the four indexes it is read by."""

    initial = True

    dependencies: ClassVar[list[tuple[str, str]]] = []

    operations: ClassVar[list[migrations.operations.base.Operation]] = [
        migrations.CreateModel(
            name='TelegramEvent',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('correlation_id', models.UUIDField()),
                ('kind', models.CharField(max_length=48)),
                ('function', models.CharField(blank=True, max_length=64)),
                ('chat_id', models.BigIntegerField(blank=True, null=True)),
                ('user_id', models.BigIntegerField(blank=True, null=True)),
                ('message_id', models.BigIntegerField(blank=True, null=True)),
                ('update_id', models.BigIntegerField(blank=True, null=True)),
                ('worker', models.CharField(blank=True, max_length=128)),
                ('attempt', models.PositiveSmallIntegerField(default=0)),
                ('duration_ms', models.PositiveIntegerField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, max_length=64)),
                ('error', models.TextField(blank=True)),
                ('detail', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'verbose_name': 'telegram event',
                'verbose_name_plural': 'telegram events',
                'db_table': 'django_redis_aiogram_event',
                'ordering': ('-id',),
                'permissions': (('view_telegramevent_payload', 'Can see event payloads and error text'),),
                'indexes': [
                    models.Index(fields=['correlation_id'], name='drai_event_correlation'),
                    models.Index(fields=['-created_at'], name='drai_event_recent'),
                    models.Index(fields=['kind', '-created_at'], name='drai_event_kind_recent'),
                    models.Index(fields=['chat_id', '-id'], name='drai_event_chat'),
                ],
            },
        ),
    ]
