"""Delete event log rows older than the retention window.

Nothing on the write path deletes anything, so the table grows until this runs.
Schedule it; check ``W006`` says so while the retention is unset.

The deletion walks **primary key ranges**, not ``pk__in``. Two reasons, both
practical: MySQL rejects ``DELETE ... WHERE id IN (SELECT ... LIMIT n)`` against
the same table, which is exactly what the ORM generates for the obvious
formulation; and a bounded range at the cold end of the table cannot conflict
with the inserts still arriving at the hot end, which is what keeps InnoDB's
next-key locks out of the picture.
"""

import datetime
import logging
import time
from argparse import ArgumentParser
from typing import Any, NamedTuple

from django.core.management import BaseCommand
from django.db import models, transaction
from django.utils import timezone

from django_redis_aiogram.eventlog import log_alias
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.settings import conf

logger = logging.getLogger('django_redis_aiogram')


class Window(NamedTuple):
    """The range one run walks: the rows, its ends, the cutoff and the alias."""

    rows: models.QuerySet[TelegramEvent]
    low: int
    watermark: int
    cutoff: datetime.datetime
    alias: str


class Command(BaseCommand):
    """Prune the event log in bounded chunks, one transaction each."""

    help = 'Delete bot event rows older than the retention window'

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the window, the chunking and the two safety valves."""
        parser.add_argument(
            '--days',
            type=int,
            default=None,
            help="delete rows older than this. Defaults to TELEGRAM_BOT['EVENT_LOG_RETENTION_DAYS'].",
        )
        parser.add_argument(
            '--chunk',
            type=int,
            default=1000,
            help='width of the id range each transaction covers. Rows inside it that are still '
            'within the window are left alone, so a chunk deletes at most this many (default 1000)',
        )
        parser.add_argument(
            '--sleep',
            type=float,
            default=0.1,
            help='seconds between chunks. The valve for replica lag: with row-based binlogs '
            'every deleted row is an event (default 0.1)',
        )
        parser.add_argument(
            '--max-chunks',
            type=int,
            default=0,
            help='stop after this many chunks, so a nightly run has a bounded blast radius. 0 means no limit',
        )
        parser.add_argument('--database', default=None, help='the alias to prune; defaults to the configured one')
        parser.add_argument('--dry-run', action='store_true', help='report what would be deleted, and delete nothing')

    def handle(self, *args: Any, **options: Any) -> None:
        """Walk the table by primary key, deleting one bounded range per commit."""
        days = options['days'] if options['days'] is not None else int(conf['EVENT_LOG_RETENTION_DAYS'])
        if days <= 0:
            self.stdout.write('Retention is not set, so nothing is pruned. See EVENT_LOG_RETENTION_DAYS.')
            return

        alias = options['database'] or log_alias()
        cutoff = timezone.now() - datetime.timedelta(days=days)
        rows = TelegramEvent.objects.using(alias)

        # where the walk stops: nothing older than the cutoff lives above this
        # id. `ORDER BY id DESC LIMIT 1` rather than an aggregate — the same
        # answer without a heap pass over exactly the set about to be deleted
        expired = rows.filter(created_at__lt=cutoff)
        watermark = expired.order_by('-id').values_list('id', flat=True).first()
        if watermark is None:
            self.stdout.write(f'Nothing older than {cutoff.isoformat()}.')
            return
        # filtered by the cutoff like the watermark is. Taking the table's lowest
        # id instead meant one surviving row down there pinned the walk to
        # restart from it every night, and a --max-chunks run never got past it
        low = expired.order_by('id').values_list('id', flat=True).first() or 1

        deleted = self._walk(Window(rows, low, watermark, cutoff, alias), options)
        verb = 'would delete' if options['dry_run'] else 'deleted'
        self.stdout.write(f'{verb} {deleted} events older than {cutoff.isoformat()} from {alias!r}.')

    def _walk(self, window: 'Window', options: dict[str, Any]) -> int:
        """Delete each id range in turn, pausing between them."""
        rows, low, watermark, cutoff, alias = window
        chunk = max(1, int(options['chunk']))
        limit = max(0, int(options['max_chunks']))
        pause = max(0.0, float(options['sleep']))
        deleted = 0
        rounds = 0

        while low <= watermark:
            high = min(low + chunk - 1, watermark)
            # the id range is the access path; created_at is the correctness
            # condition, because id order only approximates time order once
            # several processes and a buffered writer are involved
            batch = rows.filter(id__gte=low, id__lte=high, created_at__lt=cutoff)
            if options['dry_run']:
                removed_here = batch.count()
            else:
                with transaction.atomic(using=alias):
                    removed_here, _ = batch.delete()
            deleted += removed_here
            low = high + 1
            rounds += 1
            if limit and rounds >= limit:
                self.stdout.write(f'Stopped after {rounds} chunks; rerun to continue.')
                break
            # nothing was deleted, so there is nothing for a replica to catch up
            # on and nothing to vacuum; a dry run deletes nothing at all
            if pause and removed_here and low <= watermark and not options['dry_run']:
                # replicas and autovacuum both need the gaps
                time.sleep(pause)
        return deleted
