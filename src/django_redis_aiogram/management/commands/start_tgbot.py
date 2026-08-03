import contextlib
import logging
import signal
import threading
from argparse import ArgumentParser
from collections.abc import Callable
from types import FrameType
from typing import Any

from django.core.management import BaseCommand

from django_redis_aiogram import bot
from django_redis_aiogram.delivery import get_delivery
from django_redis_aiogram.settings import conf

logger = logging.getLogger('django_redis_aiogram')

#: what signal.signal returns: a handler, one of the SIG_* constants, or None
Handler = Callable[[int, FrameType | None], Any] | int | None


class Command(BaseCommand):
    help = 'Start telegram bot'

    #: what --idle waits on; tests replace it so they can end the wait
    idle_event: threading.Event | None = None

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            '--idle',
            action='store_true',
            help=(
                'When the bot is disabled, block instead of exiting. Useful under '
                'restart policies that treat a clean exit as a crash loop.'
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not bot.enabled:
            self.stdout.write(
                self.style.WARNING(
                    'django-redis-aiogram is disabled '
                    "(TELEGRAM_BOT['ENABLED'] or DJANGO_REDIS_AIOGRAM_ENABLED); "
                    'not starting the bot.'
                )
            )
            if options['idle']:
                self.stdout.write('Idling. Send SIGINT or SIGTERM to stop.')
                with contextlib.suppress(KeyboardInterrupt):
                    (self.idle_event or threading.Event()).wait()
            return

        delivery = get_delivery(handler=bot.send_raw)
        threads: list[threading.Thread] = []

        # Starting the consumer before the loop runs would let a backlog reach
        # send_raw while loop.is_running() is still False, so the coroutine
        # would be driven from the consumer thread. Deferring the start until
        # the loop picks up this callback keeps the loop single-threaded.
        bot.loop.call_soon(lambda: threads.append(delivery.start_thread()))
        previous = self._install_sigterm_handler()

        try:
            with contextlib.suppress(KeyboardInterrupt, SystemExit):
                bot.start_polling()
        finally:
            logger.info('shutting down')
            delivery.stop()
            for thread in threads:
                thread.join(timeout=float(conf['BLPOP_TIMEOUT']) + 1)
            bot.close()
            if previous is not None:
                # the command may be called in-process; leaving our handler
                # installed would turn a later SIGTERM into a stray interrupt
                with contextlib.suppress(ValueError):
                    signal.signal(signal.SIGTERM, previous)

    @staticmethod
    def _install_sigterm_handler() -> Handler:
        """Turn SIGTERM into KeyboardInterrupt so `docker stop` unwinds cleanly.

        Returns the handler it replaced, or None when it could not install one —
        signal.signal only works on the main thread.
        """

        def raise_interrupt(signum: int, frame: FrameType | None) -> None:
            raise KeyboardInterrupt

        try:
            return signal.signal(signal.SIGTERM, raise_interrupt)
        except ValueError:
            return None
