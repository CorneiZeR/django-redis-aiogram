"""Backends that move queued messages from Redis to Telegram.

``blpop`` is the default: a blocking pop needs no server configuration, works
on any database index, delivers immediately and leaves messages in the list
while the worker is down.

``keyspace`` reproduces the 1.x mechanism — write a key with a TTL and react to
its expiry event. It needs ``CONFIG SET notify-keyspace-events``, which managed
Redis providers usually refuse, and it only delivers once the TTL elapses.
"""

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, cast

from redis.exceptions import ResponseError

from django_redis_aiogram.redis import as_bytes, get_db_index, get_redis
from django_redis_aiogram.serializers import SerializationError, loads
from django_redis_aiogram.settings import conf

logger = logging.getLogger('django_redis_aiogram')

Handler = Callable[..., Any]

BLPOP_DELIVERY = 'blpop'
KEYSPACE_DELIVERY = 'keyspace'


class Delivery(ABC):
    """Consumes the Redis queue until stopped."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self._stop = threading.Event()

    @abstractmethod
    def run(self) -> None:
        """Block, consuming messages, until :meth:`stop` is called."""

    def stop(self) -> None:
        self._stop.set()

    def start_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name='tgbot-delivery', daemon=True)
        thread.start()
        return thread

    def dispatch(self, raw: bytes) -> None:
        try:
            payload = loads(raw)
        except SerializationError:
            logger.exception('dropping undecodable queued message')
            return
        except Exception:
            logger.exception('dropping queued message that failed to decode')
            return
        try:
            self.handler(**payload)
        except Exception:
            logger.exception(
                'handler failed for queued message',
                extra={'tg_function': payload.get('function')},
            )


class BlpopDelivery(Delivery):
    def run(self) -> None:
        connection = get_redis()
        key = conf['REDIS_MESSAGES_KEY']
        timeout = int(conf['BLPOP_TIMEOUT'])
        logger.info(
            'delivery started',
            extra={'tg_delivery': BLPOP_DELIVERY, 'tg_key': key, 'tg_timeout': timeout},
        )
        while not self._stop.is_set():
            try:
                item = connection.blpop([key], timeout=timeout)
            except Exception:
                # a dropped connection must not kill the worker thread
                logger.exception('blocking pop failed, retrying', extra={'tg_key': key})
                self._stop.wait(timeout)
                continue
            if item is None:
                continue
            self.dispatch(as_bytes(item[1]))


class KeyspaceDelivery(Delivery):
    def run(self) -> None:
        connection = get_redis()
        self._enable_notifications()
        channel = f'__keyevent@{get_db_index()}__:expired'
        pubsub = connection.pubsub(ignore_subscribe_messages=True)  # type: ignore[no-untyped-call]
        pubsub.subscribe(**{channel: self._on_expired})
        logger.info(
            'delivery started',
            extra={'tg_delivery': KEYSPACE_DELIVERY, 'tg_channel': channel},
        )
        try:
            while not self._stop.is_set():
                pubsub.get_message(timeout=1.0)
        finally:
            pubsub.close()

    def _enable_notifications(self) -> None:
        connection = get_redis()
        try:
            current = connection.config_get('notify-keyspace-events')
            flags = str(current.get('notify-keyspace-events', ''))
            if 'E' in flags and ('x' in flags or 'A' in flags):
                return
            connection.config_set('notify-keyspace-events', f'{flags}Ex')
        except ResponseError as error:
            logger.warning(
                'cannot enable keyspace notifications; enable them server-side or '
                "switch TELEGRAM_BOT['DELIVERY'] to 'blpop'",
                extra={'tg_error': str(error)},
            )

    def _on_expired(self, message: dict[str, Any]) -> None:
        if message['data'].decode('utf-8') != conf['REDIS_EXP_KEY']:
            return
        connection = get_redis()
        key = conf['REDIS_MESSAGES_KEY']
        # LPOP is atomic, unlike the 1.x lrange+ltrim pair, which let a second
        # worker read the same messages before the trim landed. The cast is
        # because lpop only widens to a list when given a count.
        while (raw := cast('bytes | str | None', connection.lpop(key))) is not None:
            self.dispatch(as_bytes(raw))


DELIVERIES: dict[str, type[Delivery]] = {
    BLPOP_DELIVERY: BlpopDelivery,
    KEYSPACE_DELIVERY: KeyspaceDelivery,
}


def get_delivery(handler: Handler) -> Delivery:
    name = conf['DELIVERY']
    try:
        return DELIVERIES[name](handler)
    except KeyError:
        raise ValueError(
            f'Unknown delivery {name!r}, expected one of {sorted(DELIVERIES)}.'
        ) from None
