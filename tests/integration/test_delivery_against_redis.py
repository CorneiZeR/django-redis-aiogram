"""Delivery against a real server.

Every claim here is one fakeredis cannot settle: `BLMOVE` exists only on Redis
6.2+, and the consumer picks its mode from a live server's error message.
"""

import logging
import threading
import time
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings
from redis import Redis
from redis.exceptions import ResponseError

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.delivery import BlpopDelivery
from django_redis_aiogram.serializers import JsonSerializer, PickleSerializer

pytestmark = pytest.mark.integration

QUEUE = 'TELEGRAM_BOT_MESSAGE'
WORKER = 'integration'
PROCESSING = f'{QUEUE}:processing:{WORKER}'
SETTINGS = {'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1, 'WORKER_NAME': WORKER}


def payload(chat_id, serializer=JsonSerializer):
    return serializer().dumps({'function': 'send_message', 'chat_id': chat_id})


class Recording(BlpopDelivery):
    def __init__(self, handler=None):
        self.handled = []
        super().__init__(handler=handler or (lambda **kwargs: self.handled.append(kwargs)))


def drain(delivery, expected, timeout=10, settle=0.0):
    """Run the consumer until it has handled `expected` messages, then stop it.

    `settle` is the minimum it runs for. Without it, `expected=0` returns before
    the startup reclaim has happened, and a test asserting nothing was reclaimed
    would pass having stopped the consumer first.
    """
    started = time.monotonic()
    thread = delivery.start_thread()
    deadline = started + timeout
    while time.monotonic() < deadline and len(delivery.handled) < expected:
        time.sleep(0.01)
    remaining = settle - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
    delivery.stop()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), 'the consumer did not stop'


def test_blpop_delivers_and_acknowledges(server, redis_url):
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url}):
        server.rpush(QUEUE, payload(1), payload(2))
        delivery = Recording()

        drain(delivery, expected=2)

        assert [item['chat_id'] for item in delivery.handled] == [1, 2]
        assert server.llen(QUEUE) == 0
        assert server.llen(PROCESSING) == 0, 'a delivered message was left in flight'


def test_the_server_supports_the_crash_safe_path(server, redis_url, version):
    """The whole reason the processing list exists: BLMOVE on 6.2+."""
    if version < (6, 2):
        pytest.skip(f'this server is {version}, so the fallback is the only path')

    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url}):
        delivery = Recording()
        assert delivery.reclaim() is True
        assert delivery._reliable is True, 'the consumer downgraded on a server that has LMOVE'


def test_a_message_left_in_flight_is_reclaimed(server, redis_url):
    """What a worker killed mid-send leaves behind, and what the next one does."""
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url}):
        server.rpush(PROCESSING, payload(7))
        server.rpush(QUEUE, payload(8))

        delivery = Recording()
        drain(delivery, expected=2)

        # the reclaimed one goes back to the front, so it is handled first
        assert [item['chat_id'] for item in delivery.handled] == [7, 8]
        assert server.llen(PROCESSING) == 0


def test_a_worker_does_not_reclaim_another_workers_message(server, redis_url):
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url}):
        other = f'{QUEUE}:processing:someone-else'
        server.rpush(other, payload(9))

        delivery = Recording()
        # long enough for its startup reclaim to have run and be visible
        drain(delivery, expected=0, timeout=2, settle=1.0)

        assert server.llen(other) == 1, "another worker's message was taken"


def test_a_mixed_backlog_drains(server, redis_url):
    """A 1.x queue and a 2.x queue in the same list, which is the upgrade."""
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'ALLOW_PICKLE': True, 'REDIS_URL': redis_url}):
        server.rpush(QUEUE, payload(1, PickleSerializer), payload(2), payload(3, PickleSerializer))

        delivery = Recording()
        drain(delivery, expected=3)

        assert sorted(item['chat_id'] for item in delivery.handled) == [1, 2, 3]


def test_two_workers_split_the_queue_without_duplicating(server, redis_url):
    """The pop is atomic, so each message goes to exactly one of them.

    Each worker needs its own name. Sharing one means sharing the in-flight
    list, and a reclaim then takes a message the other worker is still sending:
    written with a shared name, this test delivered twenty-one times for twenty
    messages — and only sometimes, which is what makes the caveat worth
    documenting.
    """

    class Named(Recording):
        def __init__(self, name):
            self._name = name
            super().__init__()

        @property
        def processing_key(self):
            return f'{self.queue_key}:processing:{self._name}'

    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url}):
        for chat_id in range(20):
            server.rpush(QUEUE, payload(chat_id))

        first, second = Named('worker-a'), Named('worker-b')
        threads = [first.start_thread(), second.start_thread()]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and len(first.handled) + len(second.handled) < 20:
            time.sleep(0.01)
        for delivery in (first, second):
            delivery.stop()
        for thread in threads:
            thread.join(timeout=10)

        seen = [item['chat_id'] for item in first.handled + second.handled]
        assert len(seen) == len(set(seen)), f'a message was delivered twice: {sorted(seen)}'
        assert sorted(seen) == list(range(20)), f'lost or duplicated: {sorted(seen)}'


def test_the_consumer_survives_the_server_going_away(server, redis_url):
    """A dropped connection must be retried, not end the thread."""
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url}):
        delivery = Recording()
        thread = delivery.start_thread()
        try:
            time.sleep(0.3)
            server.client_kill_filter(_type='normal', skipme=True)  # cut every other client
            time.sleep(0.5)
            server.rpush(QUEUE, payload(5))

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not delivery.handled:
                time.sleep(0.05)
        finally:
            delivery.stop()
            thread.join(timeout=10)

        assert [item['chat_id'] for item in delivery.handled] == [5]
        assert not thread.is_alive()


def test_threading_is_not_needed_to_drain(server, redis_url):
    """What the Testing page tells a reader to use: no thread, no timeout."""
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url}):
        server.rpush(QUEUE, payload(4), payload(5))
        delivery = Recording()
        before = threading.active_count()

        delivery.consume_pending()

        assert threading.active_count() == before, 'it started a thread after all'
        assert [item['chat_id'] for item in delivery.handled] == [4, 5]
        assert server.llen(QUEUE) == 0, 'consume_pending left messages behind'
        assert server.llen(PROCESSING) == 0


def test_the_heartbeat_expires_on_its_own(server, redis_url):
    """A worker that dies must stop looking alive, and only the server can do that."""
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url, 'HEARTBEAT_INTERVAL': 1}):
        delivery = Recording()
        delivery.heartbeat()

        key = delivery.heartbeat_key
        assert server.get(key) is not None
        ttl = server.ttl(key)
        assert 0 < ttl <= 3, ttl  # three times the interval

        # a value old enough to be stale must fail the command, not merely look old
        server.set(key, str(int(time.time()) - 600))
        with pytest.raises(CommandError, match='last reported'):
            call_command('tgbot_healthcheck', stdout=StringIO())


def test_a_read_longer_than_the_heartbeat_interval_keeps_it_fresh(server, redis_url):
    """BLPOP_TIMEOUT above the interval would let the key expire mid-read."""
    settings = {
        **SETTINGS,
        'REDIS_URL': redis_url,
        'BLPOP_TIMEOUT': 30,  # ten times the interval
        'HEARTBEAT_INTERVAL': 3,
    }
    with override_settings(TELEGRAM_BOT=settings):
        delivery = Recording()
        key = delivery.heartbeat_key
        thread = delivery.start_thread()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and server.get(key) is None:
                time.sleep(0.05)
            assert server.get(key) is not None, 'it never reported in'

            # past the whole TTL of the first beat (3 x 3s): if the read were
            # not clamped, the consumer would still be blocked and the key gone
            time.sleep(10)
            still_there = server.get(key)
        finally:
            delivery.stop()
            thread.join(timeout=40)

        assert still_there is not None, 'the heartbeat expired while the read was blocked'
        assert int(time.time()) - int(still_there) <= 3 * 3


def test_the_running_consumer_keeps_its_heartbeat_fresh(server, redis_url):
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': redis_url, 'HEARTBEAT_INTERVAL': 1}):
        delivery = Recording()
        key = delivery.heartbeat_key
        thread = delivery.start_thread()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and server.get(key) is None:
                time.sleep(0.05)
            first = server.get(key)

            server.delete(key)  # it has to come back without any traffic
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and server.get(key) is None:
                time.sleep(0.05)
        finally:
            delivery.stop()
            thread.join(timeout=10)

        assert first is not None, 'the consumer never reported in'
        assert server.get(key) is not None, 'it stopped reporting while still running'


def test_an_idle_consumer_survives_more_rounds_than_the_deadline(server, redis_url, caplog):
    """The reason a read deadline cannot simply be handed to BLPOP.

    The pop is meant to sit there doing nothing. If it is asked to wait longer
    than the socket will wait for an answer, every idle round raises instead of
    returning empty-handed — a working consumer logging an error every few
    seconds, for ever. Here the queue stays empty for several rounds and the log
    has to stay clean.
    """
    settings = {
        'DELIVERY': 'blpop',
        'WORKER_NAME': WORKER,
        'REDIS_URL': redis_url,
        'BLPOP_TIMEOUT': 2,
        'REDIS_TIMEOUT': 2,  # the pop gets capped to 1, and must not exceed it
    }
    with override_settings(TELEGRAM_BOT=settings), caplog.at_level(logging.WARNING):
        server.delete(QUEUE)
        handled = []
        # the consumer also passes correlation_id and queued_at; what this test
        # is about is that a message still arrives after the idle rounds
        delivery = BlpopDelivery(
            handler=lambda function, correlation_id=None, queued_at=0.0, **kwargs: handled.append((function, kwargs))
        )
        thread = delivery.start_thread()
        try:
            time.sleep(4.5)  # at least four empty rounds at the capped timeout
            TelegramBot().send_redis(chat_id=99, text='still alive')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not handled:
                time.sleep(0.05)
        finally:
            delivery.stop()
            thread.join(timeout=10)

        assert handled == [('send_message', {'chat_id': 99, 'text': 'still alive'})], (
            f'the consumer stopped reading while idling: {handled}'
        )
        failures = [record for record in caplog.records if 'blocking pop failed' in record.message]
        assert not failures, f'an idle round raised: {[record.message for record in failures]}'


def test_an_unknown_command_says_so_in_the_words_the_downgrade_looks_for(redis_url):
    """The whole crash-safety downgrade rests on one substring.

    `Delivery._downgrade_without_lmove` decides a server predates 6.2 by finding
    `unknown command` in the error text, and falls back to at-most-once pops when
    it does. Nothing else in the suite can check that: fakeredis has every
    command, so the classifier is otherwise only ever run against a double
    written to produce the string it looks for.
    """
    connection = Redis.from_url(redis_url)
    try:
        with pytest.raises(ResponseError) as raised:
            connection.execute_command('DEFINITELYNOTACOMMAND', 'x')
    finally:
        connection.close()

    assert 'unknown command' in str(raised.value).lower(), str(raised.value)
