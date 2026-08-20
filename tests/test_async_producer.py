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
from types import SimpleNamespace

import pytest
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.context import correlation_scope
from django_redis_aiogram.envelope import unpack
from django_redis_aiogram.exceptions import UnknownApiMethodError
from django_redis_aiogram.redis import aget_redis, as_bytes
from django_redis_aiogram.serializers import SerializationError, loads

QUEUE = 'TELEGRAM_BOT_MESSAGE'
#: the one line that says there is an awaitable form; asserted, so a reword
#: cannot quietly leave Logging.md describing a message nothing emits
MENTION = 'a synchronous send was called from a running event loop'
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
def test_the_correlation_id_is_resolved_before_the_await(redis_server, monkeypatch):
    """A handler's replies inherit the id of the update that caused them.

    Asserting only that the queued row carries the caller's id proves nothing: the
    scope stays active across the awaits, so a resolution moved after one would
    read the same value. What is actually load-bearing is that `asend` hands a
    concrete id *down* rather than passing `None` for something after the await to
    resolve — because that later reader is on the far side of anything the awaited
    code does to the context, and of `_hand_off`, whose callback runs in the loop's
    own context where the variable is empty.
    """
    given = uuid.UUID('22222222-2222-2222-2222-222222222222')
    handed = []
    real = TelegramBot.asend_redis

    async def spy(self, function='send_message', *, correlation_id=None, **kwargs):
        handed.append(correlation_id)
        return await real(self, function, correlation_id=correlation_id, **kwargs)

    monkeypatch.setattr(TelegramBot, 'asend_redis', spy)

    async def inside_scope():
        with correlation_scope(given):
            return await TelegramBot().asend(chat_id=1, text='hi')

    returned = asyncio.run(inside_scope())

    assert handed == [given], f'asend passed {handed} down instead of the resolved id'
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
@pytest.mark.parametrize('producer', ['send_redis', 'asend_redis', 'send_many', 'asend_many'])
def test_the_producer_writes_the_key_the_depth_reads(redis_server, monkeypatch, producer):
    """One derivation of the queue key, not one per caller.

    The test above writes to the key literally, so it cannot notice a producer
    that resolves it some other way — and every other reader goes through
    `queue_key()`: the consumer, both depth methods, `tgbot_reclaim`. A producer
    reading `REDIS_MESSAGES_KEY` itself is the single writer that would not follow
    the helper anywhere it goes, and 4.0 makes it go somewhere.

    So the helper is made to answer something the setting does not, and both ends
    are asked whether they agree.
    """
    monkeypatch.setattr('django_redis_aiogram.client.queue_key', lambda: f'{QUEUE}:elsewhere')
    bot = TelegramBot()
    call = getattr(bot, producer)
    result = call([1], text='hi') if producer.endswith('_many') else call(chat_id=1, text='hi')
    if producer.startswith('a'):
        asyncio.run(result)

    assert bot.queue_depth() == 1, 'the write and the depth read disagree about the key'
    assert redis_server.llen(QUEUE) == 0, 'the producer resolved the key past the helper'


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
    latch = threading.Event()
    monkeypatch.setattr('django_redis_aiogram.client._asend_mentioned', latch)
    bot = TelegramBot()

    async def three_sends():
        for index in range(3):
            bot.send(chat_id=index, text='hi')

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        asyncio.run(three_sends())

    mentions = [record for record in caplog.records if MENTION in record.getMessage()]
    assert len(mentions) == 1, f'said it {len(mentions)} times'
    # the latch is *why* it is once: without asserting it, a logger that happened to
    # de-duplicate would satisfy the count and leave the mechanism unpinned
    assert latch.is_set(), 'the line was emitted without the latch that makes it once'
    assert mentions[0].tg_alternative == 'asend', 'the line has to name the method to move to'
    assert redis_server.llen(QUEUE) == 3, 'the send itself must be unaffected'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_send_off_a_loop_says_nothing(redis_server, caplog, monkeypatch):
    """Most callers are synchronous — Celery, a management command, a view — and
    there is nothing for them to do about a message aimed at async code."""
    latch = threading.Event()
    monkeypatch.setattr('django_redis_aiogram.client._asend_mentioned', latch)

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        TelegramBot().send(chat_id=1, text='hi')

    assert MENTION not in caplog.text
    # and the latch with it: the line is the symptom, the latch is the state, and a caller
    # who never needed the advice must not be the one who spends it
    assert not latch.is_set(), 'a synchronous send spent the line an async caller needs'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_refused_method_does_not_spend_the_mention(caplog, monkeypatch):
    """The line is latched once per process, so whoever emits it takes it from everyone else.

    `send()` named the twin before delegating, and `send_redis` validates the method after
    that — so a call that was about to raise `UnknownApiMethodError` emitted the advice and
    left the first caller who could have acted on it in silence.

    Asserted on the refusal alone. Counting mentions across a refusal *and* a good send
    gives one either way: without the fix the refusal spends it, with the fix the good
    send does.
    """
    latch = threading.Event()
    monkeypatch.setattr('django_redis_aiogram.client._asend_mentioned', latch)
    instance = TelegramBot()

    async def only_the_refusal():
        with pytest.raises(UnknownApiMethodError):
            instance.send('no_such_method', chat_id=1)

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        asyncio.run(only_the_refusal())

    assert MENTION not in caplog.text
    # the latch itself, not only its output: this is the process-wide state the validation
    # order exists to protect, and a handler that swallowed the record would hide the leak
    assert not latch.is_set(), 'the refusal spent the line a valid call needs'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'ENABLED': False})
def test_a_disabled_send_from_a_loop_says_nothing(caplog, monkeypatch):
    """Nothing was written, so there is no better way to have written it.

    `send()` named the async twin before delegating, and `send_redis` refuses a disabled
    bot after that — so a process with the bot off was advised about a call that did
    nothing. Worse than noise: the mention is latched once per process, so the disabled
    path spent the one line the first real caller should have got.
    """
    latch = threading.Event()
    monkeypatch.setattr('django_redis_aiogram.client._asend_mentioned', latch)

    def refuse():
        raise AssertionError('a disabled send reached Redis')

    # the silence is only worth having if nothing was written: a regression that wrote and
    # suppressed the line would satisfy every other assertion here
    monkeypatch.setattr('django_redis_aiogram.client.get_redis', refuse)
    instance = TelegramBot()

    async def one_send():
        assert instance.send(chat_id=1, text='hi') is not None, 'the id is still returned'

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        asyncio.run(one_send())

    assert MENTION not in caplog.text
    assert not latch.is_set(), 'a disabled send spent the line a real send needs'


@override_settings(TELEGRAM_BOT=SETTINGS)
@pytest.mark.parametrize(
    ('producer', 'alternative'),
    # each names its *own* twin: the test used to expect `send_redis` to name `asend`,
    # which is the twin of the method the caller did not call
    [('send', 'asend'), ('send_redis', 'asend_redis'), ('send_many', 'asend_many')],
)
def test_every_synchronous_route_that_writes_names_its_own_twin(
    redis_server, caplog, monkeypatch, producer, alternative
):
    """The mention was on `send` alone, and that is two thirds of nothing.

    A web tier that wants to be explicit calls `send_redis`; a fan-out calls
    `send_many`, which holds the loop longest of the three because it serialises
    every payload between round trips. Both were silent, so the async methods this
    release adds went unmentioned to exactly the callers who needed them.
    """
    latch = threading.Event()
    monkeypatch.setattr('django_redis_aiogram.client._asend_mentioned', latch)
    bot = TelegramBot()

    async def once():
        if producer == 'send_many':
            bot.send_many([1], text='hi')
        else:
            getattr(bot, producer)(chat_id=1, text='hi')

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        asyncio.run(once())

    mentions = [record for record in caplog.records if MENTION in record.getMessage()]
    assert len(mentions) == 1, f'{producer} said it {len(mentions)} times'
    assert latch.is_set(), f'{producer} emitted the line without spending the latch'
    assert mentions[0].tg_alternative == alternative, f'{producer} pointed at the wrong method'


@override_settings(TELEGRAM_BOT=SETTINGS)
@pytest.mark.parametrize('bulk', ['send_many', 'asend_many'])
@pytest.mark.parametrize('chat_ids', [[1, 2], []], ids=['with chats', 'no chats'])
def test_the_bulk_pair_refuses_an_unknown_method_before_writing(redis_server, bulk, chat_ids):
    """The promise that an unknown method raises before the queue now covers four.

    The check lives in `_chunks`, which is a generator — its body does not run
    until the first `next()`. That makes the empty-chat case worth asserting
    rather than assuming: a caller broadcasting to a queryset that turned out
    empty would otherwise get silence for a typo, and learn about it on the day
    the queryset is not empty.
    """
    bot = TelegramBot()

    def broadcast():
        result = getattr(bot, bulk)(chat_ids, 'not_a_telegram_method', text='hi')
        return asyncio.run(result) if bulk.startswith('a') else result

    with pytest.raises(UnknownApiMethodError):
        broadcast()

    assert redis_server.llen(QUEUE) == 0, 'a refused method still reached the queue'


@override_settings(TELEGRAM_BOT={'RATE_LIMIT': None, 'ENABLED': False})
@pytest.mark.parametrize('bulk', ['send_many', 'asend_many'])
def test_a_disabled_process_queues_nothing_and_still_names_the_messages(redis_server, bulk, monkeypatch):
    """`ENABLED=0` means this process reaches neither Telegram nor Redis.

    The single-message path has always honoured that and still returned the id, so
    a caller can store ids beside its own rows whether or not this deployment
    sends. The bulk pair wrote anyway when it was first written — and the async one
    built its client before deciding, which turns "do nothing" into a crash on a
    disabled process with no `REDIS_URL` at all.

    So the settings here carry no `REDIS_URL`, and both accessors are made to
    refuse: the fixture hands out a fake whatever the configuration says, so
    leaving them in place would let a client be built and prove nothing about the
    deployment this is named after. The queue is read through the fixture's own
    handle, which is not the accessor either path calls.
    """

    def refuse(*args: object, **kwargs: object) -> None:
        message = 'a disabled process asked for a Redis client'
        raise AssertionError(message)

    monkeypatch.setattr('django_redis_aiogram.client.get_redis', refuse)
    monkeypatch.setattr('django_redis_aiogram.redis.build_async_client', refuse)

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


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True, 'EVENT_LOG_SYNC': True})
@pytest.mark.parametrize('bulk', ['send_many', 'asend_many'])
def test_a_payload_that_cannot_be_serialised_is_recorded_as_lost(redis_server, bulk, monkeypatch):
    """A message can be lost before the write as well as by it.

    `send_many` promises a chunk that fails records a drop for its own messages,
    and for a bulk call those rows are the only record of which ones: the ids go
    with the exception. Serialising outside the guard made that promise false for
    the one failure that happens before the socket is touched at all.

    The `stage` matters as much as the row. This failure means the payload never
    left the process, so re-sending it is safe; a failed `RPUSH` means the write
    may have been applied and only the reply lost, so re-sending may duplicate.
    Recording both as `queueing` would have made the safe case indistinguishable
    from the one that is not.
    """
    recorded = []
    monkeypatch.setattr('django_redis_aiogram.client.recorder.record', recorded.append)

    def refuse(payload):
        msg = 'nothing here can be encoded'
        raise SerializationError(msg)

    monkeypatch.setattr('django_redis_aiogram.client.get_serializer', lambda: SimpleNamespace(dumps=refuse))
    bot = TelegramBot()

    def broadcast():
        result = getattr(bot, bulk)(range(4), text='hi')
        return asyncio.run(result) if bulk.startswith('a') else result

    with pytest.raises(SerializationError):
        broadcast()

    dropped = [event for event in recorded if event.kind == 'outbound.dropped']
    assert len(dropped) == 4, f'{len(dropped)} rows for four messages nothing could encode'
    assert {event.detail['stage'] for event in dropped} == {'serialising'}
    assert redis_server.llen(QUEUE) == 0


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
