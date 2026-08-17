"""The async producer, and the bulk pair that shares its body.

`asend` exists for one measured case: a Django async view calling `send()` does a
blocking socket write on the thread serving requests. Everything else about a
queued message — the payload, the key, the event rows — is shared with the
synchronous path on purpose, so these tests check the transport and the sharing
rather than re-testing the queueing.
"""

import asyncio
import threading
import uuid

import pytest
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.context import correlation_scope
from django_redis_aiogram.envelope import unpack
from django_redis_aiogram.redis import aget_redis, as_bytes
from django_redis_aiogram.serializers import loads

QUEUE = 'TELEGRAM_BOT_MESSAGE'
SETTINGS = {'REDIS_URL': 'redis://localhost:6379/0', 'RATE_LIMIT': None}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_loop_keeps_running_while_a_message_is_queued(redis_server, monkeypatch):
    """The whole point, and it cannot be shown by a return value.

    A ticker counts turns of the loop while the write is in flight. Against the
    synchronous producer it counts zero, because the socket write happens on the
    thread the loop is running on — which under ASGI is the thread serving every
    other request.
    """
    ticks = []

    async def tick():
        while True:
            ticks.append(1)
            await asyncio.sleep(0)

    async def drive():
        ticker = asyncio.ensure_future(tick())
        client = await aget_redis()
        original = client.rpush

        async def slow_rpush(*args, **kwargs):
            await asyncio.sleep(0.05)
            return await original(*args, **kwargs)

        client.rpush = slow_rpush
        try:
            await TelegramBot().asend_redis(chat_id=1, text='hi')
        finally:
            ticker.cancel()

    asyncio.run(drive())

    assert len(ticks) > 1, f'the loop did not advance while the write was in flight: {len(ticks)} turns'
    assert redis_server.llen(QUEUE) == 1


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_correlation_id_is_taken_before_the_first_await(redis_server):
    """A handler's replies inherit the id of the update that caused them.

    Resolving after an await would read whatever context the loop had moved on
    to, so the queued row would belong to a different conversation than the one
    that asked for it — silently, and only under concurrency.
    """
    given = uuid.UUID('22222222-2222-2222-2222-222222222222')

    async def inside_scope():
        with correlation_scope(given):
            return await TelegramBot().asend(chat_id=1, text='hi')

    returned = asyncio.run(inside_scope())

    assert returned == given
    queued = unpack(loads(as_bytes(redis_server.lrange(QUEUE, 0, -1)[0])))
    assert queued.correlation_id == given


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_both_paths_queue_the_same_shape(redis_server):
    """The consumer knows one payload shape, so the two producers must agree.

    Written as a comparison rather than two assertions about one: a change to the
    envelope that only one path learned about is exactly what this catches.
    """
    bot = TelegramBot()
    bot.send_redis(chat_id=7, text='hi')
    asyncio.run(bot.asend_redis(chat_id=7, text='hi'))

    first, second = (unpack(loads(as_bytes(raw))) for raw in redis_server.lrange(QUEUE, 0, -1))

    assert first.function == second.function
    assert first.kwargs == second.kwargs
    assert first.correlation_id != second.correlation_id, 'two messages shared one id'


@override_settings(TELEGRAM_BOT=SETTINGS)
@pytest.mark.parametrize('bulk', ['send_many', 'asend_many'])
def test_a_chunk_is_one_round_trip_not_one_per_message(redis_server, bulk):
    """The reason this exists at all.

    A loop over `send()` is one round trip per chat; the whole gain here is the
    variadic `RPUSH`, so counting calls is the test and counting messages is not.
    """
    writes = []
    bot = TelegramBot()
    with pytest.MonkeyPatch.context() as patch:
        _count_writes(patch, writes)
        call = getattr(bot, bulk)
        result = call(range(250), chunk_size=100)
        identifiers = asyncio.run(result) if bulk.startswith('a') else result

    assert len(identifiers) == 250
    assert len(set(identifiers)) == 250, 'ids repeated across the batch'
    assert writes == [100, 100, 50], f'wrote {writes} instead of three chunks'
    assert redis_server.llen(QUEUE) == 250


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True, 'EVENT_LOG_SYNC': True})
@pytest.mark.parametrize('bulk', ['send_many', 'asend_many'])
def test_a_failed_chunk_records_its_own_messages_and_raises(redis_server, bulk, monkeypatch):
    """A variadic `RPUSH` fails for its whole chunk, and the ids go with the
    exception — so the drops have to be recorded here or nothing will ever know
    which messages were lost. Earlier chunks are already queued, which is why
    this raises rather than returning a partial list."""
    recorded = []
    monkeypatch.setattr('django_redis_aiogram.client.recorder.record', recorded.append)

    calls = []
    bot = TelegramBot()

    def broadcast():
        result = getattr(bot, bulk)(range(20), chunk_size=10)
        return asyncio.run(result) if bulk.startswith('a') else result

    with pytest.MonkeyPatch.context() as patch:
        _count_writes(patch, calls, fail_on_call=2)
        with pytest.raises(ConnectionError):
            broadcast()

    dropped = [event for event in recorded if event.kind == 'outbound.dropped']
    queued = [event for event in recorded if event.kind == 'outbound.queued']
    assert len(queued) == 10, f'the first chunk should be recorded as queued, got {len(queued)}'
    assert len(dropped) == 10, f'the failed chunk should be recorded as dropped, got {len(dropped)}'
    assert {event.detail['stage'] for event in dropped} == {'queueing'}


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_the_depths_read_the_keys_this_package_owns(redis_server):
    """An exporter should not have to reproduce the `:processing:<worker>` scheme
    by hand, which is what the Troubleshooting page used to leave it doing."""
    bot = TelegramBot()
    redis_server.rpush(QUEUE, b'{}', b'{}')
    redis_server.rpush(f'{QUEUE}:processing:mine', b'{}')
    redis_server.rpush(f'{QUEUE}:processing:gone', b'{}', b'{}', b'{}')

    assert bot.queue_depth() == 2
    assert bot.inflight_depth() == 1
    assert bot.inflight_depth('gone') == 3, 'naming another worker is how a stranded list is read'
    assert asyncio.run(bot.aqueue_depth()) == 2
    assert asyncio.run(bot.ainflight_depth('gone')) == 3


@override_settings(TELEGRAM_BOT=SETTINGS)
@pytest.mark.parametrize('bulk', ['send_many', 'asend_many'])
def test_the_ids_come_back_in_the_order_the_chats_were_given(redis_server, bulk):
    """Two pages say so, so something has to fail when it stops being true.

    The order is what lets a caller zip the ids back onto its own rows: without
    it the return value is a bag of ids and the caller has to re-derive which
    belongs to which chat, which is the work `send_many` was meant to save.
    """
    chats = [7, 8, 9, 10, 11]
    bot = TelegramBot()
    result = getattr(bot, bulk)(chats, chunk_size=2)
    identifiers = asyncio.run(result) if bulk.startswith('a') else result

    queued = [unpack(loads(as_bytes(raw))) for raw in redis_server.lrange(QUEUE, 0, -1)]

    assert [envelope.kwargs['chat_id'] for envelope in queued] == chats
    assert [envelope.correlation_id for envelope in queued] == identifiers, 'the ids do not line up with the chats'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_send_from_a_loop_mentions_asend_once(redis_server, caplog, monkeypatch):
    """Said once, and not a `DeprecationWarning`.

    `send()` from async code is correct and will keep working — it just writes on
    the thread the loop is running on. So this is a line someone reads once, not
    an exception and not a line per message: a warning on a working path, repeated,
    is how people learn to filter our logger out.
    """
    monkeypatch.setattr('django_redis_aiogram.client._asend_mentioned', threading.Event())
    bot = TelegramBot()

    async def three_sends():
        for index in range(3):
            bot.send(chat_id=index, text='hi')

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        asyncio.run(three_sends())

    mentions = [record for record in caplog.records if 'await asend()' in record.getMessage()]
    assert len(mentions) == 1, f'said it {len(mentions)} times'
    assert redis_server.llen(QUEUE) == 3, 'the send itself must be unaffected'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_send_off_a_loop_says_nothing(redis_server, caplog, monkeypatch):
    """Most callers are synchronous — Celery, a management command, a view — and
    there is nothing for them to do about a message aimed at async code."""
    monkeypatch.setattr('django_redis_aiogram.client._asend_mentioned', threading.Event())

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        TelegramBot().send(chat_id=1, text='hi')

    assert 'await asend()' not in caplog.text


@override_settings(TELEGRAM_BOT={**SETTINGS, 'ENABLED': False})
@pytest.mark.parametrize('bulk', ['send_many', 'asend_many'])
def test_a_disabled_process_queues_nothing_and_still_names_the_messages(redis_server, bulk):
    """`ENABLED=0` means this process reaches neither Telegram nor Redis.

    The single-message path has always honoured that and still returned the id, so
    a caller can store ids beside its own rows whether or not this deployment
    sends. The bulk pair wrote anyway when it was first written — and the async one
    built its client before deciding, which would raise on a disabled process that
    has no `REDIS_URL` at all, turning "do nothing" into a crash.
    """
    bot = TelegramBot()
    result = getattr(bot, bulk)([1, 2, 3], text='hi')
    identifiers = asyncio.run(result) if bulk.startswith('a') else result

    assert len(identifiers) == 3, 'a disabled process still has to name the messages'
    assert len(set(identifiers)) == 3
    assert redis_server.llen(QUEUE) == 0, 'a disabled process wrote to Redis'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True, 'EVENT_LOG_SYNC': True})
@pytest.mark.parametrize('bulk', ['send_many', 'asend_many'])
def test_a_broadcast_records_one_row_per_message(redis_server, bulk, monkeypatch):
    """The event log defines `outbound.queued` as one message, and the consumer
    writes `consumed` and `sent` rows per message against the same id.

    A summary row for the batch would orphan those: they would have nothing to
    join to. It is also why a broadcast is where the writer's buffer gets tested —
    see the Event log page.
    """
    recorded = []
    monkeypatch.setattr('django_redis_aiogram.client.recorder.record', recorded.append)

    bot = TelegramBot()
    result = getattr(bot, bulk)(range(25), chunk_size=10, text='hi')
    identifiers = asyncio.run(result) if bulk.startswith('a') else result

    queued = [event for event in recorded if event.kind == 'outbound.queued']
    assert len(queued) == 25, f'{len(queued)} rows for 25 messages'
    assert [event.correlation_id for event in queued] == identifiers, 'the rows do not carry the returned ids'


def _count_writes(patch, writes, fail_on_call=None):
    """Count `rpush` calls on both transports.

    The two paths use different client classes — `fakeredis.FakeRedis` and
    `fakeredis.aioredis.FakeRedis` — so patching one leaves the other unmeasured,
    and a test that counts nothing reads exactly like a test that passed.
    """
    import fakeredis
    import fakeredis.aioredis

    def wrap(cls, is_async):
        original = cls.rpush

        if is_async:

            async def counting(self, key, *payloads):
                writes.append(len(payloads))
                if fail_on_call is not None and len(writes) == fail_on_call:
                    msg = 'connection reset'
                    raise ConnectionError(msg)
                return await original(self, key, *payloads)
        else:

            def counting(self, key, *payloads):
                writes.append(len(payloads))
                if fail_on_call is not None and len(writes) == fail_on_call:
                    msg = 'connection reset'
                    raise ConnectionError(msg)
                return original(self, key, *payloads)

        patch.setattr(cls, 'rpush', counting)

    wrap(fakeredis.FakeRedis, is_async=False)
    wrap(fakeredis.aioredis.FakeRedis, is_async=True)


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_closing_releases_this_loops_client(redis_server):
    """Django has no hook that closes it, so a caller with a lifespan needs one.

    Deliberately not the mirror of `close()`: that tears the bot down, this closes
    the one thing an ASGI process opens lazily. Skipping it costs a possible
    `ResourceWarning` when the loop goes, not a leak — which is why the docs say
    so rather than warning about it at runtime.
    """
    closed = []

    async def open_then_close():
        bot = TelegramBot()
        client = await aget_redis()
        original = client.aclose

        async def recording(*args, **kwargs):
            closed.append(client)
            return await original(*args, **kwargs)

        client.aclose = recording
        await bot.asend_redis(chat_id=1, text='hi')
        await bot.aclose()
        # and the next caller on this loop gets a fresh one, not the closed one
        assert await aget_redis() is not client
        await bot.aclose()

    asyncio.run(open_then_close())

    assert closed, 'aclose() did not reach the client'
