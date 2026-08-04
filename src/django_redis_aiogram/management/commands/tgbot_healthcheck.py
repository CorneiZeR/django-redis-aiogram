"""Answer whether the bot container is doing its job.

`docker ps` says the process is up, which is not the same thing: the consumer
thread can be dead while polling continues, or Redis can be unreachable, and the
container stays "healthy" either way.
"""

import time
from argparse import ArgumentParser
from typing import Any

from django.core.management import BaseCommand, CommandError

from django_redis_aiogram import bot
from django_redis_aiogram.delivery import get_delivery
from django_redis_aiogram.redis import get_redis
from django_redis_aiogram.settings import conf


class Command(BaseCommand):
    help = "Exit 0 when the bot container is healthy, non-zero with a reason otherwise"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--max-queue",
            type=int,
            default=None,
            help=(
                "fail when the queue is longer than this. Defaults to "
                "TELEGRAM_BOT['HEALTHCHECK_MAX_QUEUE']; 0 turns the check off."
            ),
        )
        parser.add_argument(
            "--max-age",
            type=int,
            default=None,
            help=(
                "fail when the consumer's heartbeat is older than this many seconds. "
                "Defaults to three times TELEGRAM_BOT['HEARTBEAT_INTERVAL']."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not bot.enabled:
            # nothing is meant to be running here, so nothing is wrong
            self.stdout.write("disabled in this process; nothing to check")
            return

        delivery = get_delivery(handler=bot.send_raw)
        interval = max(1, int(conf["HEARTBEAT_INTERVAL"]))
        max_age = options["max_age"] if options["max_age"] is not None else interval * 3
        max_queue = options["max_queue"] if options["max_queue"] is not None else int(conf["HEALTHCHECK_MAX_QUEUE"])

        try:
            connection = get_redis()
            connection.ping()
        except Exception as error:
            raise CommandError(f"redis is unreachable: {error}") from error

        try:
            raw = connection.get(delivery.heartbeat_key)
        except Exception as error:
            # ping answering says nothing about the next command: a failover in
            # between, or a key this replica cannot serve
            raise CommandError(f"could not read the heartbeat: {error}") from error

        if raw is None:
            raise CommandError(
                f"no heartbeat at {delivery.heartbeat_key}: the consumer has not written one "
                f"within {interval * 3}s, or it never started"
            )

        try:
            age = int(time.time()) - int(raw)
        except (TypeError, ValueError) as error:
            raise CommandError(f"the heartbeat at {delivery.heartbeat_key} is not a timestamp") from error

        if age > max_age:
            raise CommandError(f"the consumer last reported {age}s ago, over the {max_age}s limit")

        try:
            queued = int(connection.llen(delivery.queue_key) or 0)
        except Exception as error:
            raise CommandError(f"could not read the queue length: {error}") from error

        if max_queue and queued > max_queue:
            raise CommandError(f"{queued} messages are queued, over the limit of {max_queue}")

        self.stdout.write(self.style.SUCCESS(f"healthy: heartbeat {age}s old, {queued} queued"))
