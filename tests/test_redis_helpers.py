"""The helpers around the shared connection, and the str payloads it may hand back.

A URL with `decode_responses=True` makes every read a str, which is why nothing
downstream may call `.decode()` unconditionally.
"""

import fakeredis
import pytest
from django.test import override_settings
from redis import Redis

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.delivery import BlpopDelivery
from django_redis_aiogram.envelope import unpack
from django_redis_aiogram.redis import (
    as_bytes,
    connection_kwargs,
    get_redis,
    read_timeout,
    reset_redis,
    url_decodes_responses,
)
from django_redis_aiogram.serializers import JsonSerializer, loads
from django_redis_aiogram.settings import conf


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
    delivery = BlpopDelivery(
        handler=lambda function=None, correlation_id=None, queued_at=0.0, **kwargs: handled.append(kwargs)
    )
    thread = delivery.start_thread()
    for _ in range(200):
        if handled:
            break
        thread.join(0.01)
    delivery.stop()
    thread.join(timeout=5)
    assert handled == [{'chat_id': 4}]


@override_settings(TELEGRAM_BOT={'WORKER_NAME': 'tests'})
def test_draining_handles_str_payloads(decoded_server):
    decoded_server.rpush(
        'TELEGRAM_BOT_MESSAGE',
        JsonSerializer().dumps({'function': 'send_message', 'chat_id': 6}),
    )
    handled = []
    BlpopDelivery(
        handler=lambda function=None, correlation_id=None, queued_at=0.0, **kwargs: handled.append(kwargs)
    ).consume_pending()
    assert handled == [{'chat_id': 6}]


@override_settings(TELEGRAM_BOT={'WORKER_NAME': 'tests'})
def test_draining_pops_atomically(redis_server):
    """Two workers draining the same list must share the messages, not
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
            BlpopDelivery(handler=rival).consume_pending()

    BlpopDelivery(handler=handler).consume_pending()

    assert competitor_ran, 'the competing drain never ran, so nothing was tested'
    # the split is deterministic under an atomic pop: this worker holds 0 while
    # the competitor drains what is left. A non-atomic drain would hand both of
    # them the same list, and the union would carry every id twice
    assert first == [0], first
    assert second == [1, 2], second
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 0


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop'})
def test_send_redis_round_trips_through_a_decoded_connection(decoded_server):
    TelegramBot().send_redis(chat_id=1, text='hi')

    queued = unpack(loads(as_bytes(decoded_server.lpop('TELEGRAM_BOT_MESSAGE'))))
    assert queued.function == 'send_message'
    assert queued.kwargs == {'chat_id': 1, 'text': 'hi'}


def test_the_connection_is_built_once_and_reused(monkeypatch):
    """`redis_conn` and every get_redis() caller must land on one client.

    Nothing asserted this: a per-call client would leak a connection pool per
    send and still pass every other test in the suite.
    """
    built = []
    closed = []
    deadlines = []

    class Stub:
        def close(self):
            closed.append(self)

    def from_url(cls, url, **kwargs):
        # a fresh object each time, so "the same client" cannot pass by accident
        built.append(url)
        deadlines.append(kwargs)
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
    # a client with no deadline hangs for ever on a server that stopped
    # answering, which is what redis-py 5.0 does by default
    assert all(kwargs['socket_timeout'] and kwargs['socket_connect_timeout'] for kwargs in deadlines), deadlines


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


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/0', 'REDIS_TIMEOUT': 7})
def test_the_shared_client_is_bounded_in_time(monkeypatch):
    """redis-py only started defaulting to a read deadline in 8.0; on the 5.0
    floor a server that stops answering blocks the caller until it is killed."""
    seen = {}

    def from_url(cls, url, **kwargs):
        seen.update(kwargs)
        return fakeredis.FakeRedis()

    monkeypatch.setattr(Redis, 'from_url', classmethod(from_url))
    reset_redis()
    get_redis()
    reset_redis()

    assert seen['socket_timeout'] == 7
    assert seen['socket_connect_timeout'] == 7


@override_settings(TELEGRAM_BOT={'REDIS_TIMEOUT': 'seven'})
def test_an_unreadable_deadline_does_not_reach_the_socket():
    """E030 reports it; until then the call must not build a broken client."""
    with pytest.raises((TypeError, ValueError)):
        read_timeout()


@pytest.mark.parametrize(
    ('blpop', 'deadline', 'expected'),
    [
        (5, 5, 4),  # the defaults: the pop yields a second to the deadline
        (30, 5, 4),  # asked for more than a read may take, so capped (W004 warns)
        (2, 60, 2),  # comfortably inside, left alone
        (1, 1, 1),  # never below one, which Redis reads as "block for ever"
    ],
)
def test_the_pop_never_outlasts_the_read_deadline(blpop, deadline, expected):
    """A pop asked to wait longer than the socket will wait for an answer turns
    an idle round into an exception, once per round, for ever."""
    settings = {'BLPOP_TIMEOUT': blpop, 'REDIS_TIMEOUT': deadline, 'HEARTBEAT_INTERVAL': 3600}
    with override_settings(TELEGRAM_BOT=settings):
        interval = max(1, int(conf['HEARTBEAT_INTERVAL']))
        assert max(1, min(int(conf['BLPOP_TIMEOUT']), interval, read_timeout() - 1)) == expected


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/0', 'REDIS_TIMEOUT': 7})
def test_the_shared_client_carries_the_read_deadline():
    """`connection_kwargs` is the one place the deadlines are decided."""
    assert connection_kwargs() == {'socket_connect_timeout': 7, 'socket_timeout': 7}


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/0'})
def test_the_shared_client_does_not_retry_commands():
    """Pinned because it is a decision, not an accident.

    `Redis.from_url` builds the pool before the client, so redis-py's client-level
    retry default never reaches the connection. Neither `RPUSH` nor `BLMOVE` is
    idempotent, so a retry after the server applied one would duplicate a message
    a real person receives.
    """
    reset_redis()
    try:
        pool = get_redis().connection_pool
        # built, never connected: the retry is decided by the pool's kwargs, and
        # this test must not need a server
        connection = pool.connection_class(**pool.connection_kwargs)
        assert connection.retry.get_retries() == 0
    finally:
        reset_redis()


@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        ('redis://localhost:6379/0', False),
        ('redis://localhost:6379/0?decode_responses=true', True),
        ('redis://localhost:6379/0?decode_responses=1', True),
        # redis-py has no boolean parser for this key, so the raw string reaches
        # the connection and any non-empty value enables decoding
        ('redis://localhost:6379/0?decode_responses=false', True),
        ('redis://localhost:6379/0?decode_responses=0', True),
        ('redis://localhost:6379/0?decode_responses=no', True),
        # blank values are dropped by the query parser before redis-py sees them
        ('redis://localhost:6379/0?decode_responses=', False),
        ('not a url at all', False),
        ('redis://localhost:6379/0?db=2&decode_responses=True', True),
        ('redis://localhost:6379/0?db=2', False),
        ('', False),
    ],
)
def test_url_decodes_responses(url, expected):
    assert url_decodes_responses(url) is expected
