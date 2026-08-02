import asyncio
import threading

import pytest
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.delivery import (
    BlpopDelivery,
    KeyspaceDelivery,
    get_delivery,
)
from django_redis_aiogram.serializers import JsonSerializer, PickleSerializer


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop'})
def test_get_delivery_blpop():
    assert isinstance(get_delivery(handler=lambda **kwargs: None), BlpopDelivery)


@override_settings(TELEGRAM_BOT={'DELIVERY': 'keyspace'})
def test_get_delivery_keyspace():
    assert isinstance(get_delivery(handler=lambda **kwargs: None), KeyspaceDelivery)


@override_settings(TELEGRAM_BOT={'DELIVERY': 'smoke-signals'})
def test_get_delivery_rejects_unknown():
    with pytest.raises(ValueError, match='Unknown delivery'):
        get_delivery(handler=lambda **kwargs: None)


def drain(delivery, expected, timeout=5):
    """Run the consumer until it has handled `expected` messages."""
    thread = delivery.start_thread()
    deadline = threading.Event()
    for _ in range(int(timeout * 100)):
        if len(delivery.handled) >= expected:
            break
        deadline.wait(0.01)
    delivery.stop()
    thread.join(timeout=timeout)
    return thread


class RecordingBlpop(BlpopDelivery):
    def __init__(self):
        self.handled = []
        super().__init__(handler=lambda **kwargs: self.handled.append(kwargs))


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_blpop_delivers_queued_messages(redis_server):
    redis_server.rpush(
        'TELEGRAM_BOT_MESSAGE', JsonSerializer().dumps({'function': 'send_message', 'chat_id': 7})
    )
    delivery = RecordingBlpop()
    drain(delivery, expected=1)
    assert delivery.handled == [{'function': 'send_message', 'chat_id': 7}]


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_blpop_drains_a_backlog(redis_server):
    """A worker that was down must still find its messages waiting."""
    for index in range(3):
        redis_server.rpush(
            'TELEGRAM_BOT_MESSAGE',
            JsonSerializer().dumps({'function': 'send_message', 'chat_id': index}),
        )
    delivery = RecordingBlpop()
    drain(delivery, expected=3)
    assert [item['chat_id'] for item in delivery.handled] == [0, 1, 2]


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_blpop_accepts_legacy_pickle(redis_server):
    redis_server.rpush(
        'TELEGRAM_BOT_MESSAGE', PickleSerializer().dumps({'function': 'send_message', 'chat_id': 9})
    )
    delivery = RecordingBlpop()
    drain(delivery, expected=1)
    assert delivery.handled[0]['chat_id'] == 9


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_undecodable_message_is_dropped_not_fatal(redis_server):
    redis_server.rpush('TELEGRAM_BOT_MESSAGE', b'{"__model__": "os", "data": {}}')
    redis_server.rpush(
        'TELEGRAM_BOT_MESSAGE', JsonSerializer().dumps({'function': 'send_message', 'chat_id': 1})
    )
    delivery = RecordingBlpop()
    drain(delivery, expected=1)
    assert [item['chat_id'] for item in delivery.handled] == [1]


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_failing_handler_does_not_kill_the_consumer(redis_server):
    calls = []

    class Exploding(BlpopDelivery):
        def __init__(self):
            self.handled = calls
            super().__init__(handler=self._handle)

        def _handle(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError('boom')

    for index in range(2):
        redis_server.rpush(
            'TELEGRAM_BOT_MESSAGE',
            JsonSerializer().dumps({'function': 'send_message', 'chat_id': index}),
        )
    delivery = Exploding()
    drain(delivery, expected=2)
    assert len(calls) == 2


@override_settings(TELEGRAM_BOT={'DELIVERY': 'keyspace'})
def test_keyspace_handler_drains_the_list(redis_server):
    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs))
    redis_server.rpush(
        'TELEGRAM_BOT_MESSAGE', JsonSerializer().dumps({'function': 'send_message', 'chat_id': 3})
    )
    delivery._on_expired({'data': b'TELEGRAM_BOT_EXP'})
    assert handled == [{'function': 'send_message', 'chat_id': 3}]
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 0


@override_settings(TELEGRAM_BOT={'DELIVERY': 'keyspace'})
def test_keyspace_ignores_other_keys(redis_server):
    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs))
    redis_server.rpush(
        'TELEGRAM_BOT_MESSAGE', JsonSerializer().dumps({'function': 'send_message', 'chat_id': 3})
    )
    delivery._on_expired({'data': b'SOMETHING_ELSE'})
    assert handled == []


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop'})
def test_send_redis_does_not_write_an_expiry_key(redis_server):
    TelegramBot().send_redis(chat_id=1, text='hi')
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 1
    assert redis_server.get('TELEGRAM_BOT_EXP') is None


@override_settings(TELEGRAM_BOT={'DELIVERY': 'keyspace', 'REDIS_EXP_TIME': 5})
def test_send_redis_sets_an_expiring_key_for_keyspace(redis_server):
    TelegramBot().send_redis(chat_id=1, text='hi')
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 1
    # 1.x passed 'EX' as the value and the TTL positionally, so the key never expired properly
    assert 0 < redis_server.ttl('TELEGRAM_BOT_EXP') <= 5


def test_schedule_hops_to_the_loop_thread():
    """send_raw runs on the delivery thread while the loop lives elsewhere.

    create_task across that boundary is not thread safe; this pins the hop.
    """
    instance = TelegramBot()
    loop = asyncio.new_event_loop()
    instance._loop = loop

    started = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.call_soon(started.set)
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    assert started.wait(5)

    ran_on = []
    done = threading.Event()

    async def coroutine():
        ran_on.append(threading.get_ident())
        done.set()

    instance._schedule(coroutine())
    assert done.wait(5), 'coroutine never ran on the loop thread'
    assert ran_on == [thread.ident]

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
    instance._loop = None


def test_schedule_runs_inline_when_no_loop_is_running():
    instance = TelegramBot()
    ran = []

    async def coroutine():
        ran.append(True)

    instance._schedule(coroutine())
    assert ran == [True]
    instance.close()
