"""The async producer against a real server.

Everything here is a question fakeredis cannot answer, and one of them hid a
defect through a whole review round. fakeredis's async client keeps its sockets in
a module-level structure, so a loop it touched never dies whatever this package
does — which makes the registry's own accounting invisible. A real
``redis.asyncio`` client holds its loop through a real transport, and only against
one can you see whether letting go of a loop lets go of its client.
"""

import asyncio
import gc
import weakref

import pytest
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram import redis as redis_module
from django_redis_aiogram.envelope import unpack
from django_redis_aiogram.redis import as_bytes
from django_redis_aiogram.serializers import loads

pytestmark = pytest.mark.integration

QUEUE = 'TELEGRAM_BOT_MESSAGE'


def settings(redis_url):
    """Queue-only settings: these tests are about the write, not about delivery."""
    return {'REDIS_URL': redis_url, 'RATE_LIMIT': None}


def test_a_message_queued_asynchronously_is_readable_synchronously(server, redis_url):
    """The two producers have to agree byte for byte, and only a real server can
    say so: fakeredis is one store behind both halves of the fixture, so a shape
    that differed would still round trip."""
    with override_settings(TELEGRAM_BOT=settings(redis_url)):
        bot = TelegramBot()
        identifier = asyncio.run(bot.asend_redis(chat_id=7, text='hi'))
        asyncio.run(bot.aclose())

    queued = unpack(loads(as_bytes(server.lrange(QUEUE, 0, -1)[0])))
    assert queued.correlation_id == identifier
    assert queued.function == 'send_message'
    assert queued.kwargs == {'chat_id': 7, 'text': 'hi'}


def test_a_client_does_not_outlive_the_loop_it_belongs_to(server, redis_url):
    """A loop-affine client held strongly keeps its own loop alive.

    Through the connection, its writer and its transport — so the registry's weak
    key never fires, and a process that runs a loop per unit of work accumulated a
    client and its sockets for each one. Measured before the sweep existed: three
    loops used and abandoned left three live loops and three live clients, against
    none for the same clients dropped on the floor.

    Asserted on the clients rather than on the registry's size, which the unit
    suite covers: what matters to a deployment is the connection, and the object
    holding it being unreachable is what lets it go.
    """
    references = []

    async def use_it():
        client = await redis_module.aget_redis()
        await client.rpush('drai:integration:probe', b'x')
        references.append(weakref.ref(client))

    with override_settings(TELEGRAM_BOT=settings(redis_url)):
        for _ in range(3):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(use_it())
            finally:
                loop.close()
            del loop
            gc.collect()

        # the last one is still held: only a later call sweeps it, which is the
        # bound this makes explicit rather than a leak it tolerates
        alive = [reference for reference in references if reference() is not None]
        assert len(alive) <= 1, f'{len(alive)} clients outlived the loops that owned them'


def test_closing_releases_the_connection_on_the_loop_that_owns_it(server, redis_url):
    """`aclose()` is the only path that closes a connection on its own loop.

    Checked against the server's own client list rather than the client object:
    the point is the socket, and a client that has let go of its pool still looks
    the same from here.
    """
    with override_settings(TELEGRAM_BOT=settings(redis_url)):

        async def queue_then_close():
            await TelegramBot().asend_redis(chat_id=1, text='hi')
            during = len(server.client_list())
            await TelegramBot().aclose()
            return during

        during = asyncio.run(queue_then_close())

    after = len(server.client_list())
    assert after < during, f'{after} connections after closing against {during} while queueing'


def test_the_async_client_survives_the_loop_being_recreated(server, redis_url):
    """A second loop must get a client of its own rather than the dead one's.

    The failure this rules out is not hypothetical: a client from a closed loop
    answers `Event loop is closed` on its first command, and the registry would
    hand it out for ever if it keyed on anything but the running loop.
    """
    with override_settings(TELEGRAM_BOT=settings(redis_url)):
        bot = TelegramBot()
        first = asyncio.run(bot.asend_redis(chat_id=1, text='one'))
        second = asyncio.run(bot.asend_redis(chat_id=2, text='two'))
        asyncio.run(bot.aclose())

    assert first != second
    queued = [unpack(loads(as_bytes(raw))) for raw in server.lrange(QUEUE, 0, -1)]
    assert [message.kwargs['chat_id'] for message in queued] == [1, 2]


def rpush_calls(server):
    """How many ``RPUSH`` calls this server has served, or ``None`` if it will not say.

    A delta from ``INFO commandstats`` rather than ``CONFIG RESETSTAT`` and an
    absolute count. Managed Redis providers commonly disable ``CONFIG`` entirely,
    and the rest of this suite needs only ``FLUSHDB`` — a test that fails, or skips,
    on the kind of server people are most likely to point it at is a test that does
    not run where it matters.

    An empty section means the server does not report per-command statistics at
    all; a section without ``RPUSH`` in it means none have been served yet, which
    is a count of zero and not an absence of information.
    """
    stats = server.info('commandstats')
    if not stats:
        return None
    return int(stats.get('cmdstat_rpush', {}).get('calls', 0))


def test_a_broadcast_writes_one_round_trip_per_chunk(server, redis_url):
    """The whole argument for `asend_many` is the round trips, and a real server
    is the only thing that can count them."""
    before = rpush_calls(server)
    if before is None:
        pytest.skip('this server does not report INFO commandstats, so round trips cannot be counted')

    with override_settings(TELEGRAM_BOT=settings(redis_url)):
        identifiers = asyncio.run(TelegramBot().asend_many(range(10), chunk_size=4, text='hi'))
        asyncio.run(TelegramBot().aclose())

    assert len(identifiers) == 10
    assert server.llen(QUEUE) == 10
    # ten chats, chunks of four: three pushes, not ten
    pushes = rpush_calls(server) - before
    assert pushes == 3, f'{pushes} RPUSH calls for three chunks'
