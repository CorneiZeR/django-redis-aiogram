import contextlib
import logging
import pickle
import threading
from argparse import ArgumentParser
from typing import Any

from django.core.management import BaseCommand

from django_redis_aiogram import bot
from django_redis_aiogram.redis import as_bytes, get_redis
from django_redis_aiogram.settings import conf

logger = logging.getLogger('django_redis_aiogram')


class Command(BaseCommand):
    help = 'Start telegram bot'

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
                    threading.Event().wait()
            return

        connection = get_redis()

        def event_handler(message: dict[str, Any]) -> None:
            if message['data'].decode('utf-8') != conf['REDIS_EXP_KEY']:
                return

            if not (length := connection.llen(conf['REDIS_MESSAGES_KEY'])):
                return

            queued = connection.lrange(conf['REDIS_MESSAGES_KEY'], 0, length - 1)
            connection.ltrim(conf['REDIS_MESSAGES_KEY'], length, -1)

            for payload in queued:
                bot.send_raw(**pickle.loads(as_bytes(payload)))

            connection.delete(conf['REDIS_EXP_KEY'])

        pubsub = connection.pubsub()  # type: ignore[no-untyped-call]
        connection.config_set('notify-keyspace-events', 'Ex')
        pubsub.psubscribe(**{'__keyevent@0__:expired': event_handler})
        pubsub.run_in_thread(sleep_time=conf['REDIS_EXP_TIME'])
        logger.info('Running worker redis subscriber')

        with contextlib.suppress(KeyboardInterrupt, SystemExit):
            bot.start_polling()
