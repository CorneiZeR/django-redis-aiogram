"""The helpers around the shared connection, and the str payloads it may hand back.

A URL with `decode_responses=True` makes every read a str, which is why nothing
downstream may call `.decode()` unconditionally.
"""

import fakeredis
import pytest
from django.test import override_settings
from redis import Redis

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.delivery import BlpopDelivery, KeyspaceDelivery
from django_redis_aiogram.redis import as_bytes, get_db_index, get_redis, reset_redis
from django_redis_aiogram.serializers import JsonSerializer, loads


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (b'already bytes', b'already bytes'),
        ('a str', b'a str'),
        ('кириллица', 'кириллица'.encode()),
    ],
)
def test_as_bytes(value, expected):
    assert as_bytes(value) == expected


@pytest.fixture
def decoded_server(monkeypatch):
    """A connection configured the way decode_responses=True behaves."""
    server = fakeredis.FakeRedis(decode_responses=True)
    for target in (
        'django_redis_aiogram.redis.get_redis',
        'django_redis_aiogram.delivery.get_redis',
        'django_redis_aiogram.client.get_redis',
    ):
        monkeypatch.setattr(target, lambda server=server: server)
    return server


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_blpop_handles_str_payloads(decoded_server):
    decoded_server.rpush(
        'TELEGRAM_BOT_MESSAGE',
        JsonSerializer().dumps({'function': 'send_message', 'chat_id': 4}),
    )
    handled = []
    delivery = BlpopDelivery(handler=lambda **kwargs: handled.append(kwargs))
    thread = delivery.start_thread()
    for _ in range(200):
        if handled:
            break
        thread.join(0.01)
    delivery.stop()
    thread.join(timeout=5)
    assert handled == [{'function': 'send_message', 'chat_id': 4}]


@override_settings(TELEGRAM_BOT={'DELIVERY': 'keyspace'})
def test_keyspace_handles_str_payloads(decoded_server):
    decoded_server.rpush(
        'TELEGRAM_BOT_MESSAGE',
        JsonSerializer().dumps({'function': 'send_message', 'chat_id': 6}),
    )
    handled = []
    KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs))._on_expired({'data': b'TELEGRAM_BOT_EXP'})
    assert handled == [{'function': 'send_message', 'chat_id': 6}]


@override_settings(TELEGRAM_BOT={'DELIVERY': 'keyspace', 'WORKER_NAME': 'tests'})
def test_keyspace_pops_atomically(redis_server):
    """Two workers reacting to the same expiry must share the messages, not
    duplicate them: every id arrives exactly once across both.

    This does not reproduce the 1.x race — there the trim landed before any
    dispatch, so a competitor started from a handler always found the list
    already empty. Forcing that race needs two lrange calls to interleave
    across processes, which cannot be made deterministic. What this pins is the
    property the atomic pop guarantees.
    """
    for index in range(3):
        redis_server.rpush(
            'TELEGRAM_BOT_MESSAGE',
            JsonSerializer().dumps({'function': 'send_message', 'chat_id': index}),
        )

    first, second = [], []
    competitor_ran = []

    def rival(**kwargs):
        second.append(kwargs['chat_id'])

    def handler(**kwargs):
        first.append(kwargs['chat_id'])
        if not competitor_ran:
            # a second worker drains while this dispatch is still in flight
            competitor_ran.append(True)
            KeyspaceDelivery(handler=rival)._on_expired({'data': b'TELEGRAM_BOT_EXP'})

    KeyspaceDelivery(handler=handler)._on_expired({'data': b'TELEGRAM_BOT_EXP'})

    assert competitor_ran, 'the competing drain never ran, so nothing was tested'
    # the split is deterministic under an atomic pop: this worker holds 0 while
    # the competitor drains what is left. A non-atomic drain would hand both of
    # them the same list, and the union would carry every id twice
    assert first == [0], first
    assert second == [1, 2], second
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 0


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/7'})
def test_db_index_comes_from_the_url(monkeypatch):
    server = fakeredis.FakeRedis(db=7)
    monkeypatch.setattr('django_redis_aiogram.redis.get_redis', lambda: server)
    assert get_db_index() == 7


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop'})
def test_send_redis_round_trips_through_a_decoded_connection(decoded_server):
    TelegramBot().send_redis(chat_id=1, text='hi')

    queued = loads(as_bytes(decoded_server.lpop('TELEGRAM_BOT_MESSAGE')))
    assert queued == {'function': 'send_message', 'chat_id': 1, 'text': 'hi'}


def test_the_connection_is_built_once_and_reused(monkeypatch):
    """`redis_conn` and every get_redis() caller must land on one client.

    Nothing asserted this: a per-call client would leak a connection pool per
    send and still pass every other test in the suite.
    """
    built = []
    closed = []

    class Stub:
        def close(self):
            closed.append(self)

    def from_url(cls, url):
        # a fresh object each time, so "the same client" cannot pass by accident
        built.append(url)
        return Stub()

    monkeypatch.setattr(Redis, 'from_url', classmethod(from_url))
    reset_redis()

    with override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/7'}):
        first = get_redis()
        assert get_redis() is first, 'a second call built another client'
        assert built == ['redis://localhost:6379/7'], built

        reset_redis()
        assert closed == [first], 'reset_redis left the connection open'

        # and the slot is empty: keeping a closed client would hand it to the
        # next caller
        second = get_redis()

    reset_redis()

    assert second is not first, 'reset_redis kept the closed client'
    assert len(built) == 2, built


def test_get_reads_the_slot_exactly_once_on_the_fast_path():
    """A reset between two reads of the attribute used to hand the caller None.

    Deterministic where a stress test is not: the second read of the slot
    answers None, exactly what a concurrent reset() makes it. Code that keeps
    one local read never performs a second one.
    """
    from django_redis_aiogram.redis import _SharedConnection

    sentinel = object()
    reads = {'count': 0}

    class SecondReadIsReset(_SharedConnection):
        def __getattribute__(self, name: str):
            if name == '_client':
                reads['count'] += 1
                if reads['count'] > 1:
                    return None
            return super().__getattribute__(name)

    holder = SecondReadIsReset()
    object.__setattr__(holder, '_client', sentinel)

    assert holder.get() is sentinel, 'get() re-read the slot and met the reset'
