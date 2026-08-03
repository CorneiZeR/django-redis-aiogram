"""Crash-safe consuming: a worker killed mid-send must not lose the message.

The consumer moves each message to a processing list before sending and
removes it afterwards; a new worker reclaims whatever a crashed one left
behind. On servers without LMOVE it falls back to plain pops.
"""

import threading

import pytest
from django.test import override_settings
from redis.exceptions import ResponseError

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.delivery import BlpopDelivery, KeyspaceDelivery
from django_redis_aiogram.serializers import JsonSerializer

QUEUE = 'TELEGRAM_BOT_MESSAGE'
PROCESSING = f'{QUEUE}:processing'
SETTINGS = {'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1}


def payload(chat_id):
    return JsonSerializer().dumps({'function': 'send_message', 'chat_id': chat_id})


def drain(delivery, expected_handled, timeout=5):
    thread = delivery.start_thread()
    waiter = threading.Event()
    for _ in range(int(timeout * 100)):
        if len(delivery.handled) >= expected_handled:
            break
        waiter.wait(0.01)
    delivery.stop()
    thread.join(timeout=timeout)


class Recording(BlpopDelivery):
    def __init__(self, handler=None):
        self.handled = []
        super().__init__(handler=handler or (lambda **kwargs: self.handled.append(kwargs)))


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_delivered_message_is_acknowledged(redis_server):
    redis_server.rpush(QUEUE, payload(1))
    delivery = Recording()
    drain(delivery, expected_handled=1)

    # both lists are also empty when the payload was dropped before the handler
    assert [item['chat_id'] for item in delivery.handled] == [1]
    assert redis_server.llen(QUEUE) == 0
    assert redis_server.llen(PROCESSING) == 0, 'delivered message left in processing'


@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_message_survives_a_worker_killed_mid_send(redis_server):
    redis_server.rpush(QUEUE, payload(7))

    class Killed(BaseException):
        """Bypasses dispatch()'s except Exception, like a real kill would."""

    dying = Recording(handler=lambda **kwargs: (_ for _ in ()).throw(Killed()))
    thread = dying.start_thread()
    thread.join(timeout=5)
    assert not thread.is_alive()

    # the message is stranded in processing, not lost
    assert redis_server.llen(PROCESSING) == 1
    assert redis_server.llen(QUEUE) == 0

    survivor = Recording()
    drain(survivor, expected_handled=1)

    assert [item['chat_id'] for item in survivor.handled] == [7]
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_reclaim_preserves_the_original_order(redis_server):
    for chat_id in (1, 2):
        redis_server.rpush(PROCESSING, payload(chat_id))
    redis_server.rpush(QUEUE, payload(3))

    survivor = Recording()
    drain(survivor, expected_handled=3)

    assert [item['chat_id'] for item in survivor.handled] == [1, 2, 3]


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_failing_handler_is_not_redelivered_forever(redis_server):
    """Handler errors are logged and acknowledged — only a crash redelivers."""
    calls = []

    def exploding(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('boom')

    delivery = Recording(handler=exploding)
    delivery.handled = calls
    redis_server.rpush(QUEUE, payload(1))
    drain(delivery, expected_handled=1)

    assert len(calls) == 1
    assert redis_server.llen(PROCESSING) == 0


class OldRedis:
    """A server from before 6.2: LMOVE does not exist."""

    def __init__(self, inner):
        self._inner = inner

    def lmove(self, *args, **kwargs):
        raise ResponseError("unknown command 'LMOVE'")

    def blmove(self, *args, **kwargs):
        raise ResponseError("unknown command 'BLMOVE'")

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def old_redis_server(redis_server, monkeypatch):
    wrapped = OldRedis(redis_server)
    for target in (
        'django_redis_aiogram.redis.get_redis',
        'django_redis_aiogram.delivery.get_redis',
        'django_redis_aiogram.client.get_redis',
    ):
        monkeypatch.setattr(target, lambda wrapped=wrapped: wrapped)
    return redis_server


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_falls_back_to_plain_pops_on_an_old_server(old_redis_server):
    old_redis_server.rpush(QUEUE, payload(5))

    delivery = Recording()
    drain(delivery, expected_handled=1)

    assert [item['chat_id'] for item in delivery.handled] == [5]
    assert delivery._reliable is False


@override_settings(TELEGRAM_BOT={'DELIVERY': 'keyspace'})
def test_keyspace_acknowledges_too(redis_server):
    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs))
    redis_server.rpush(QUEUE, payload(3))

    delivery._on_expired({'data': b'TELEGRAM_BOT_EXP'})

    assert [item['chat_id'] for item in handled] == [3]
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT={'DELIVERY': 'keyspace'})
def test_keyspace_accepts_str_event_data(redis_server):
    """pubsub hands back str when the URL enables decode_responses; 1.x-style
    unconditional .decode() crashed the consumer thread on it."""
    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs))
    redis_server.rpush(QUEUE, payload(9))

    delivery._on_expired({'data': 'TELEGRAM_BOT_EXP'})

    assert [item['chat_id'] for item in handled] == [9]


@override_settings(TELEGRAM_BOT={**SETTINGS, 'DELIVERY': 'keyspace'})
def test_keyspace_drains_a_backlog_left_while_the_worker_was_down(redis_server):
    """Expiry events are not replayed, so a backlog would otherwise sit in the
    list until some later message happened to trigger one."""
    for chat_id in (1, 2):
        redis_server.rpush(QUEUE, payload(chat_id))

    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs['chat_id']))
    thread = delivery.start_thread()
    waiter = threading.Event()
    for _ in range(500):
        if len(handled) >= 2:
            break
        waiter.wait(0.01)
    delivery.stop()
    thread.join(timeout=5)

    assert sorted(handled) == [1, 2], handled
    assert redis_server.llen(QUEUE) == 0


def test_only_telegram_api_methods_may_be_named():
    """A queued payload picks the method, so `getattr` must not be open season."""
    from django_redis_aiogram.api import API_METHODS, check_function

    assert 'send_message' in API_METHODS
    assert check_function('send_photo') == 'send_photo'

    for forbidden in ('download_file', 'token', 'session', 'me', '__init__'):
        with pytest.raises(ValueError, match='not a Telegram API method'):
            check_function(forbidden)


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'RATE_LIMIT': None})
def test_send_raw_refuses_a_non_api_method():
    with pytest.raises(ValueError, match='not a Telegram API method'):
        TelegramBot().send_raw('download_file', file_path='x', destination='/tmp/y')


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x'})
def test_send_redis_refuses_a_non_api_method(redis_server):
    with pytest.raises(ValueError, match='not a Telegram API method'):
        TelegramBot().send_redis('download_file', file_path='x')
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 0


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_a_queued_non_api_method_is_dropped_not_executed(redis_server):
    """A payload written by something malicious must not kill the worker either."""
    redis_server.rpush(
        QUEUE, JsonSerializer().dumps({'function': 'download_file', 'file_path': 'x'})
    )
    redis_server.rpush(QUEUE, payload(5))

    delivery = Recording()
    drain(delivery, expected_handled=1)

    assert [item['chat_id'] for item in delivery.handled] == [5]
