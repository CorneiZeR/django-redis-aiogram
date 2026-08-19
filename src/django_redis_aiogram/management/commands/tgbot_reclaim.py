"""Put a dead worker's in-flight messages back on the queue.

A message being sent lives in ``<queue>:processing:<worker>`` until the send
finishes, and the worker that put it there reclaims it on its next start. That
only works while the name is stable — a container with no ``hostname:`` and no
``WORKER_NAME`` gets a fresh one every time, and its messages are stranded where
nothing will ever look for them again.

This is the way out, and it is deliberately manual: naming the dead worker is a
human saying it is dead. Nothing here probes for liveness, because a worker that
is merely slow looks exactly like one that is gone, and taking a message back
from a live sender is how you send it twice.
"""

import logging
from argparse import ArgumentParser
from typing import Any

from django.core.management import BaseCommand, CommandError
from redis import Redis
from redis.exceptions import ResponseError

from django_redis_aiogram.events import worker_identity
from django_redis_aiogram.redis import get_redis, processing_key, queue_key

logger = logging.getLogger('django_redis_aiogram')


class Command(BaseCommand):
    """Move one worker's in-flight messages back to the queue."""

    help = 'Requeue the messages a dead worker left in flight'

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the worker to reclaim from, and the safety valves."""
        parser.add_argument(
            '--worker',
            required=True,
            help='the WORKER_NAME (or hostname) whose in-flight list to drain. Naming it is you '
            'saying that worker is gone: reclaiming from a live one sends its message twice.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='stop after this many messages, so one run has a bounded blast radius. 0 means no limit',
        )
        parser.add_argument('--dry-run', action='store_true', help='report what is there, and move nothing')

    @staticmethod
    def _move_one(connection: Redis, source: str, destination: str) -> object:
        """Move one message back to the front of the queue, oldest first.

        ``LMOVE ... RIGHT LEFT`` is what ``reclaim()`` uses, so the order a real run
        produces is the order a dry run promises. On a Redis older than 6.2 that command
        does not exist, and this is the one path that can still recover those messages:
        the consumer's own ``reclaim()`` gives up there and runs at-most-once, so a list
        stranded before the downgrade would have nothing else to come back through.
        ``RPOPLPUSH`` is the same move, and has been there since 1.2.
        """
        try:
            return connection.lmove(source, destination, 'RIGHT', 'LEFT')
        except ResponseError as error:
            if 'unknown command' not in str(error).lower():
                raise
            return connection.rpoplpush(source, destination)

    def handle(self, *args: Any, **options: Any) -> None:
        """Walk the named worker's in-flight list back onto the queue."""
        worker = str(options['worker']).strip()
        if not worker:
            msg = '--worker cannot be empty.'
            raise CommandError(msg)
        limit = int(options['limit'])
        if limit < 0:
            # max(0, ...) would have read this as "no limit", which is the
            # opposite of what someone typing a limit is asking for. Judged with
            # the other arguments, so --dry-run reports the mistake rather than
            # returning happily on a run that would have been refused
            msg = f'--limit cannot be negative, got {limit}. Use 0 for no limit.'
            raise CommandError(msg)
        if worker == worker_identity():
            # this process would be reclaiming from whatever is running here now,
            # which on a bot container is the consumer that is mid-send
            msg = (
                f"{worker!r} is this process's own worker name. A running consumer reclaims its own "
                'messages when it starts; taking them from underneath it sends them twice.'
            )
            raise CommandError(msg)

        source, destination = processing_key(worker), queue_key()
        connection = get_redis()
        try:
            waiting = int(connection.llen(source) or 0)
        except Exception as error:
            msg = f'could not read {source}: {error}'
            raise CommandError(msg) from error

        if not waiting:
            self.stdout.write(f'Nothing in flight for {worker!r}.')
            return
        if options['dry_run']:
            # through the same limit the real run applies. A rehearsal that
            # promises to move two and then moves one is worse than none: it is
            # read as the plan, and the difference shows up as messages left
            # behind that nobody went looking for
            would_move = min(waiting, limit) if limit else waiting
            self.stdout.write(
                f'{waiting} message(s) in flight for {worker!r}; would requeue {would_move} of them.'
                if would_move != waiting
                else f'{waiting} message(s) in flight for {worker!r}; would requeue them.'
            )
            return

        moved = 0
        while not limit or moved < limit:
            try:
                if not self._move_one(connection, source, destination):
                    break
            except Exception as error:
                msg = f'moved {moved} message(s), then failed: {error}'
                raise CommandError(msg) from error
            moved += 1

        logger.info('reclaimed a dead worker', extra={'tg_key': source, 'tg_count': moved})
        self.stdout.write(self.style.SUCCESS(f'Requeued {moved} message(s) from {worker!r}.'))
