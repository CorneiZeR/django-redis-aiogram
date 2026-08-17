"""Answer whether the bot container is doing its job.

`docker ps` says the process is up, which is not the same thing: the consumer
thread can be dead while polling continues, or Redis can be unreachable, and the
container stays "healthy" either way.
"""

import logging
import time
from argparse import ArgumentParser
from typing import Any

from django.core.management import BaseCommand, CommandError
from redis import Redis
from redis.exceptions import RedisError, ResponseError

from django_redis_aiogram import bot
from django_redis_aiogram.delivery import Delivery, get_delivery
from django_redis_aiogram.redis import get_redis
from django_redis_aiogram.settings import conf

logger = logging.getLogger('django_redis_aiogram')

# round trips, not keys: MATCH filters on the server but SCAN walks the whole
# keyspace either way, and this probe runs on a timer
STRANDED_SCAN_ROUNDS = 20


class Command(BaseCommand):
    """Check Redis, the consumer's heartbeat and the queue length, in that order."""

    help = 'Exit 0 when the bot container is healthy, non-zero with a reason otherwise'

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the two limits, both of which default to a setting."""
        parser.add_argument(
            '--max-queue',
            type=int,
            default=None,
            help=(
                'fail when the queue is longer than this. Defaults to '
                "TELEGRAM_BOT['HEALTHCHECK_MAX_QUEUE']; 0 turns the check off."
            ),
        )
        parser.add_argument(
            '--max-age',
            type=int,
            default=None,
            help=(
                "fail when the consumer's heartbeat is older than this many seconds. "
                "Defaults to three times TELEGRAM_BOT['HEARTBEAT_INTERVAL']."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Report the first thing that is wrong, or that everything is fine."""
        if not bot.enabled:
            # nothing is meant to be running here, so nothing is wrong
            self.stdout.write('disabled in this process; nothing to check')
            return

        delivery = get_delivery(handler=bot.send_raw)
        interval = max(1, int(conf['HEARTBEAT_INTERVAL']))
        max_age = options['max_age'] if options['max_age'] is not None else interval * 3
        max_queue = options['max_queue'] if options['max_queue'] is not None else int(conf['HEALTHCHECK_MAX_QUEUE'])

        try:
            connection = get_redis()
            connection.ping()
        except Exception as error:
            msg = f'redis is unreachable: {error}'
            raise CommandError(msg) from error

        try:
            raw = connection.get(delivery.heartbeat_key)
        except Exception as error:
            # ping answering says nothing about the next command: a failover in
            # between, or a key this replica cannot serve
            msg = f'could not read the heartbeat: {error}'
            raise CommandError(msg) from error

        if raw is None:
            msg = (
                f'no heartbeat at {delivery.heartbeat_key}: the consumer has not written one '
                f'within {interval * 3}s, or it never started'
            )
            raise CommandError(msg)

        try:
            age = int(time.time()) - int(raw)
        except (TypeError, ValueError) as error:
            msg = f'the heartbeat at {delivery.heartbeat_key} is not a timestamp'
            raise CommandError(msg) from error

        if age > max_age:
            msg = f'the consumer last reported {age}s ago, over the {max_age}s limit'
            raise CommandError(msg)

        try:
            queued = int(connection.llen(delivery.queue_key) or 0)
        except Exception as error:
            msg = f'could not read the queue length: {error}'
            raise CommandError(msg) from error

        if max_queue and queued > max_queue:
            msg = f'{queued} messages are queued, over the limit of {max_queue}'
            raise CommandError(msg)

        stranded, swept = self._stranded(connection, delivery)
        guarantee = self._guarantee(connection, delivery)
        self.stdout.write(self.style.SUCCESS(f'healthy: heartbeat {age}s old, {queued} queued, {guarantee}'))
        if stranded:
            # not a failure: another worker may be sending them right now. But an
            # invisible pile is how a stranded list stays stranded
            self.stdout.write(
                self.style.WARNING(
                    f'{stranded if swept else f"at least {stranded}"} message(s) are in flight under '
                    'other worker names. If one of those workers is gone, '
                    '`manage.py tgbot_reclaim --worker <name>` requeues them.'
                )
            )

    @staticmethod
    def _guarantee(connection: Redis, delivery: Delivery) -> str:
        """Which delivery guarantee this Redis can actually give.

        Asked, not assumed. This command builds its own `Delivery`, and a fresh
        one reports `crash_safe` until something proves otherwise — the consumer
        learns the truth from `reclaim()`, which this probe must not call, since
        requeueing a running worker's in-flight list would send those messages
        twice.

        So it asks the same question `reclaim()` does, on a key that does not
        exist: rotating an empty list is a no-op on a server that has `LMOVE`,
        and `unknown command` on one that does not.
        """
        if not delivery.crash_safe:
            return 'at-most-once'
        probe = f'{delivery.queue_key}:lmove-probe'
        try:
            connection.lmove(probe, probe, 'LEFT', 'RIGHT')
        except ResponseError as error:
            if 'unknown command' in str(error).lower():
                return 'at-most-once'
            logger.warning('could not establish which delivery guarantee is in force')
            return 'unknown'
        except RedisError:
            logger.warning('could not establish which delivery guarantee is in force')
            return 'unknown'
        return 'at-least-once'

    @staticmethod
    def _stranded(connection: Redis, delivery: Delivery) -> tuple[int, bool]:
        """Count what is in flight under a worker name that is not this one.

        Read rather than acted on: a message under another name may be one another
        worker is sending this second, and taking it back would send it twice.

        Bounded, and returns whether it finished. ``MATCH`` filters on the server
        but ``SCAN`` still walks the whole keyspace, and the compose recipe runs
        this probe every thirty seconds — on a Redis shared with a cache backend,
        which the settings page suggests is common, an unbounded sweep is a full
        pass over someone else's keys twice a minute. A partial answer is worth
        having; one that pretends to be complete is not.
        """
        pattern = f'{delivery.queue_key}:processing:*'
        mine = delivery.processing_key
        # SCAN may return the same key more than once when the keyspace changes
        # size mid-iteration, and counting one twice would invent a backlog
        seen: set[str] = set()
        total = 0
        cursor = 0
        try:
            for _ in range(STRANDED_SCAN_ROUNDS):
                cursor, keys = connection.scan(cursor=cursor, match=pattern, count=100)
                for key in keys:
                    name = key.decode() if isinstance(key, bytes) else key
                    if name == mine or name in seen:
                        continue
                    seen.add(name)
                    total += int(connection.llen(name) or 0)
                if not cursor:
                    return total, True
        except RedisError:
            # the probe answers about this worker; a scan it could not finish is
            # not a reason to call a healthy container unhealthy
            logger.warning('could not scan for stranded in-flight lists', extra={'tg_key': pattern})
            return 0, False
        return total, False
