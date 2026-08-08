"""The append-only feed of what the bot did.

Django imports this on every ``django.setup()`` — before ``AppConfig.ready()``
and regardless of ``ENABLED`` — so it may not reach aiogram, directly or
otherwise, and may not read settings at import time.

Rows are inserted and never updated. The stages of one outbound message are
three rows sharing a ``correlation_id``: the web process writes the queued row
and the bot container writes the delivered one, with no coordination between
them and no foreign key either way. That only works because the feed is
insert-only, which is also what keeps pruning cheap and the table shardable.
"""

from django.db import models
from django.utils import timezone

from django_redis_aiogram.events import MAX_KIND_LENGTH


class TelegramEvent(models.Model):
    """One thing that happened to one message, update or handler."""

    id = models.BigAutoField(primary_key=True)
    # stamped by whoever recorded it: the writer batches, so auto_now_add would
    # record when the batch was flushed rather than when the thing happened
    created_at = models.DateTimeField(default=timezone.now)
    # time-ordered (UUIDv7), so this index appends rather than scattering
    correlation_id = models.UUIDField()
    # no choices: see events.kind_choices for why the registry stays in Python
    kind = models.CharField(max_length=MAX_KIND_LENGTH)

    function = models.CharField(max_length=64, blank=True)
    chat_id = models.BigIntegerField(null=True, blank=True)
    user_id = models.BigIntegerField(null=True, blank=True)
    message_id = models.BigIntegerField(null=True, blank=True)
    update_id = models.BigIntegerField(null=True, blank=True)
    # the same name the in-flight list uses, so a row points at a container
    worker = models.CharField(max_length=128, blank=True)

    attempt = models.PositiveSmallIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        """Portable everywhere: no constraints, no relations, four indexes."""

        db_table = 'django_redis_aiogram_event'
        # by id, not created_at: id is the insert order and unique, so the admin
        # paginator gets a total order without a tie-breaker column
        ordering = ('-id',)
        verbose_name = 'telegram event'
        verbose_name_plural = 'telegram events'
        # Django's four stock permissions, plus the one it has no equivalent for:
        # there are no field-level permissions, and seeing that a message went
        # out is a different question from reading what it said
        permissions = (('view_telegramevent_payload', 'Can see event payloads and error text'),)
        # named explicitly and kept short: Oracle rejects an identifier over 30
        indexes = (
            models.Index(fields=('correlation_id',), name='drai_event_correlation'),
            models.Index(fields=('-created_at',), name='drai_event_recent'),
            models.Index(fields=('kind', '-created_at'), name='drai_event_kind_recent'),
            models.Index(fields=('chat_id', '-id'), name='drai_event_chat'),
        )

    def __str__(self) -> str:
        """Name the event the way an admin row reads."""
        return f'{self.kind} {self.function}'.strip()
