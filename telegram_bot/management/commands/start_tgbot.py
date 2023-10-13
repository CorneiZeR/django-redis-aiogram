import contextlib
import pickle
import logging

from django.core.management import BaseCommand

from telegram_bot import bot
from telegram_bot import conf, redis_conn


class Command(BaseCommand):
    help = 'Start telegram bot'

    def handle(self, *args, **options):
        """entrypoint"""

        def event_handler(message: dict) -> None:
            """message handler"""

            # not that key exp
            if message['data'].decode('utf-8') != conf.REDIS_EXP_KEY:
                return

            # no messages to send
            if not (length := redis_conn.llen(conf.REDIS_MESSAGES_KEY)):
                return

            messages = redis_conn.lrange(conf.REDIS_MESSAGES_KEY, 0, length-1)
            redis_conn.ltrim(conf.REDIS_MESSAGES_KEY, length, -1)

            for message in messages:
                message = pickle.loads(message)
                bot.send_message(**message)

            redis_conn.delete(conf.REDIS_EXP_KEY)

        # add redis psubscribe and run in thread
        pubsub = redis_conn.pubsub()
        redis_conn.config_set('notify-keyspace-events', 'Ex')
        pubsub.psubscribe(**{"__keyevent@0__:expired": event_handler})
        pubsub.run_in_thread(sleep_time=conf.REDIS_EXP_TIME)
        logging.info('Running worker redis subscriber')

        # start bot
        with contextlib.suppress(KeyboardInterrupt, SystemExit):
            bot.start_polling()
