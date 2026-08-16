"""Run the bot: receive updates, and consume the queue Django writes to.

This is the long-running process a bot container is built around. It owns two
things at once — whatever brings updates in, and the consumer that drains the
Redis queue — and has to shut both down cleanly when the container stops.
"""

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
from django_redis_aiogram.enums import UpdateMode
from django_redis_aiogram.recorder import recorder
from django_redis_aiogram.redis import read_timeout
from django_redis_aiogram.webhook import MODES, current_mode

logger = logging.getLogger('django_redis_aiogram')

#: what signal.signal returns: a handler, one of the SIG_* constants, or None
Handler = Callable[[int, FrameType | None], Any] | int | None


class Command(BaseCommand):
    """Start the bot and the queue consumer, and stop them together."""

    help = 'Start telegram bot'

    #: what --idle waits on; tests replace it so they can end the wait
    idle_event: threading.Event | None = None

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare --mode and --idle."""
        parser.add_argument(
            '--mode',
            choices=sorted(MODES),
            default=None,
            help=(
                "how updates reach the bot for this run. Defaults to TELEGRAM_BOT['MODE'] "
                "(env: DJANGO_REDIS_AIOGRAM_MODE), itself 'polling'. In webhook mode this "
                'process consumes the queue and never calls getUpdates, because the updates '
                'arrive over HTTP instead.'
            ),
        )
        parser.add_argument(
            '--idle',
            action='store_true',
            help=(
                'When the bot is disabled, block instead of exiting. Useful under '
                'restart policies that treat a clean exit as a crash loop.'
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Receive updates, drain the queue, and unwind both on a signal."""
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

        configured = current_mode()
        mode = options['mode'] or configured
        self.stdout.write(f'Updates arrive by {mode}.')
        if mode != configured:
            # the webhook view reads the setting, not this flag, so it would
            # refuse the updates this process is no longer polling for
            self.stdout.write(
                self.style.WARNING(
                    f"--mode {mode} disagrees with TELEGRAM_BOT['MODE'] ({configured}), and it "
                    'changes this process only: '
                    + (
                        'the webhook view still refuses updates while the setting says polling'
                        if mode == UpdateMode.WEBHOOK
                        else 'getUpdates fails while a webhook is registered'
                    )
                )
            )

        delivery = get_delivery(handler=bot.send_raw)
        threads: list[threading.Thread] = []

        if mode == UpdateMode.WEBHOOK:
            # nothing will run the loop here, so the callback below would never
            # fire. The consumer drives the loop itself for each send instead,
            # under the same lock a web thread uses.
            threads.append(delivery.start_thread())
        else:
            # Starting the consumer before the loop runs would let a backlog reach
            # send_raw while loop.is_running() is still False, so the coroutine
            # would be driven from the consumer thread. Deferring the start until
            # the loop picks up this callback keeps the loop single-threaded.
            bot.loop.call_soon(lambda: threads.append(delivery.start_thread()))
        previous = self._install_sigterm_handler()

        try:
            with contextlib.suppress(KeyboardInterrupt, SystemExit):
                if mode == UpdateMode.WEBHOOK:
                    self.stdout.write('Consuming the queue; updates are expected over HTTP.')
                    (self.idle_event or threading.Event()).wait()
                else:
                    bot.start_polling()
        finally:
            logger.info('shutting down')
            delivery.stop()
            for thread in threads:
                # derived from the bound that actually governs the thread: every
                # call it makes is capped by REDIS_TIMEOUT, and its blocking pop
                # by one less than that. BLPOP_TIMEOUT + 1 was six seconds against
                # a worst case of ten, so a consumer that outlived the join went on
                # to acknowledge a message close() had already refused
                thread.join(timeout=read_timeout() + 1)
                if thread.is_alive():
                    logger.warning(
                        'the delivery consumer did not stop in time',
                        extra={'tg_timeout': read_timeout() + 1},
                    )
            try:
                bot.close()
            finally:
                # after close(), never before: closing drains in-flight sends,
                # and those are what produce the final rows. In its own finally
                # because a close() that raises must not also lose the rows
                recorder.stop()
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

        def raise_interrupt(_signum: int, _frame: FrameType | None) -> None:
            raise KeyboardInterrupt

        try:
            return signal.signal(signal.SIGTERM, raise_interrupt)
        except ValueError:
            return None
