"""Index the kind filter by the column the changelist actually orders on.

Added before the old one is removed, so the table is never without an index on
``kind``. The new name is deliberate rather than a rename: it lets a large
deployment run ``CREATE INDEX CONCURRENTLY drai_event_kind_id ...`` by hand
ahead of ``migrate``, after which this migration finds it already there.
"""

from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    """Swap ``(kind, -created_at)`` for ``(kind, -id)``."""

    dependencies: ClassVar[list[tuple[str, str]]] = [('django_redis_aiogram', '0001_initial')]

    operations: ClassVar[list[migrations.operations.base.Operation]] = [
        migrations.AddIndex(
            model_name='telegramevent',
            index=models.Index(fields=['kind', '-id'], name='drai_event_kind_id'),
        ),
        migrations.RemoveIndex(
            model_name='telegramevent',
            name='drai_event_kind_recent',
        ),
    ]
