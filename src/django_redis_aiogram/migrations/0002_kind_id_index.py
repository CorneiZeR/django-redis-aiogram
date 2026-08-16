"""Index the kind filter by the column the changelist actually orders on.

Added before the old one is removed, so the table is never without an index on
``kind``. A new name rather than a rename, because the columns differ: the old
one was ``(kind, -created_at)`` while the changelist orders by ``-id``.

``AddIndex`` issues a plain ``CREATE INDEX``, with no ``IF NOT EXISTS`` and no
adoption of one that is already there — so creating it by hand ahead of this
migration makes the migration fail rather than saving it work. On a table large
enough for the lock to matter, run the migration in a window.
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
