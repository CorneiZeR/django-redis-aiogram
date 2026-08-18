import asyncio
import threading
import time

import pytest
from aiogram import exceptions
from aiogram.methods import SendMessage
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.client import Outbound
from django_redis_aiogram.delivery import BlpopDelivery, get_delivery
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.redis import processing_key
from django_redis_aiogram.serializers import JsonSerializer, PickleSerializer


def an_outbound(function='send_message', **kwargs):
    """The call identity every scheduling path now carries."""
    return Outbound(new_correlation_id(), function, kwargs or {'chat_id': 1, 'text': 'x'})


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop'})
def test_get_delivery_blpop():
    assert isinstance(get_delivery(handler=lambda **kwargs: None), BlpopDelivery)


@override_settings(TELEGRAM_BOT={'DELIVERY': 'smoke-signals'})
def test_get_delivery_rejects_unknown():
    with pytest.raises(ValueError, match='Unknown delivery'):
        get_delivery(handler=lambda **kwargs: None)


def drain(delivery, expected, timeout=5):
    """Run the consumer until it has handled `expected` messages."""
    thread = delivery.start_thread()
    for _ in range(int(timeout * 100)):
        if len(delivery.handled) >= expected:
            break
        time.sleep(0.01)
    delivery.stop()
    thread.join(timeout=timeout)
    # a consumer still stuck here would otherwise be a passing test with a
    # leaked thread
    assert not thread.is_alive(), 'the consumer did not stop'
    return thread


class RecordingBlpop(BlpopDelivery):
    def __init__(self):
        self.handled = []
        # the consumer also passes correlation_id and queued_at; the call this
        # records is what a handler cares about
        super().__init__(handler=lambda correlation_id=None, queued_at=0.0, **kwargs: self.handled.append(kwargs))


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_blpop_delivers_queued_messages(redis_server):
    redis_server.rpush('TELEGRAM_BOT_MESSAGE', JsonSerializer().dumps({'function': 'send_message', 'chat_id': 7}))
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


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1, 'ALLOW_PICKLE': True})
def test_blpop_accepts_legacy_pickle(redis_server):
    redis_server.rpush('TELEGRAM_BOT_MESSAGE', PickleSerializer().dumps({'function': 'send_message', 'chat_id': 9}))
    delivery = RecordingBlpop()
    drain(delivery, expected=1)
    assert delivery.handled[0]['chat_id'] == 9


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_undecodable_message_is_dropped_not_fatal(redis_server):
    redis_server.rpush('TELEGRAM_BOT_MESSAGE', b'{"__model__": "os", "data": {}}')
    redis_server.rpush('TELEGRAM_BOT_MESSAGE', JsonSerializer().dumps({'function': 'send_message', 'chat_id': 1}))
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
            msg = 'boom'
            raise RuntimeError(msg)

    for index in range(2):
        redis_server.rpush(
            'TELEGRAM_BOT_MESSAGE',
            JsonSerializer().dumps({'function': 'send_message', 'chat_id': index}),
        )
    delivery = Exploding()
    drain(delivery, expected=2)
    assert len(calls) == 2


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop'})
def test_send_redis_does_not_write_an_expiry_key(redis_server):
    TelegramBot().send_redis(chat_id=1, text='hi')
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 1
    assert redis_server.get('TELEGRAM_BOT_EXP') is None


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

    instance._schedule(coroutine(), an_outbound())
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

    instance._schedule(coroutine(), an_outbound())
    assert ran == [True]
    instance.close()


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'RATE_LIMIT': None})
def test_concurrent_send_raw_from_web_threads(monkeypatch):
    """gunicorn gthread runs several request threads; run_until_complete on a
    shared loop is not reentrant, so unsynchronised sends crash with
    'this event loop is already running'."""
    instance = TelegramBot()
    sent = []

    expected = 8
    all_sent = threading.Event()

    class StubBot:
        async def send_message(self, **kwargs):
            await asyncio.sleep(0.01)
            sent.append(kwargs)
            if len(sent) >= expected:
                all_sent.set()

        class session:
            @staticmethod
            async def close():
                pass

    instance._bot = StubBot()

    errors = []
    # without this the threads may run one after another, and the test would
    # pass on a serial execution it is meant to rule out
    ready = threading.Barrier(expected, timeout=30)

    def send(index):
        try:
            ready.wait()
            instance.send_raw(chat_id=index, text='hi')
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=send, args=(index,)) for index in range(expected)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # wait on the sends themselves: a join timeout is a scheduling guess, and a
    # busy CI runner turned that guess into a flake
    assert all_sent.wait(30), f'only {len(sent)} of {expected} sends completed'
    assert [thread for thread in threads if thread.is_alive()] == []
    assert errors == []
    assert sorted(item['chat_id'] for item in sent) == list(range(expected))
    instance._bot = None
    instance.close()


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'FSM_STORAGE': 'memory', 'RATE_LIMIT': None})
def test_sends_that_all_see_a_stopped_loop_are_serialised():
    """The window the lock exists for.

    The test above cannot reach it: while one thread drives the loop the others
    see `is_running()` and hand off instead. Here every thread passes that check
    and goes for `run_until_complete` on the same loop, which is exactly the
    case that raises 'this event loop is already running'.
    """
    instance = TelegramBot()
    expected = 8
    sent = []

    class StubBot:
        async def send_message(self, **kwargs):
            # long enough that the other threads arrive while this one holds it
            await asyncio.sleep(0.01)
            sent.append(kwargs)

        class session:
            @staticmethod
            async def close():
                pass

    class LooksStopped:
        """Reports a stopped loop to the scheduler; the loop itself still knows."""

        def __init__(self, loop):
            self._loop = loop

        def is_running(self):
            return False

        def __getattr__(self, name):
            return getattr(self._loop, name)

    instance._bot = StubBot()
    instance._loop = LooksStopped(instance.loop)

    errors = []
    ready = threading.Barrier(expected, timeout=30)

    def send(index):
        try:
            ready.wait()
            instance.send_raw(chat_id=index, text='hi')
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=send, args=(index,)) for index in range(expected)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == [], errors
    assert sorted(item['chat_id'] for item in sent) == list(range(expected))

    instance._loop = instance._loop._loop  # unwrap, so close() drives the real one
    instance._bot = None
    instance.close()


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 0})
def test_a_zero_blpop_timeout_is_clamped(redis_server, monkeypatch):
    """0 means "block for ever" to a real Redis, and stop() would never be seen.

    fakeredis returns immediately instead, so this watches the argument rather
    than the wait — a passing end-to-end test would prove nothing here.
    """
    timeouts = []

    class Spy:
        def blmove(self, source, destination, timeout, *args, **kwargs):
            timeouts.append(timeout)
            return redis_server.lmove(source, destination, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    monkeypatch.setattr('django_redis_aiogram.delivery.get_redis', Spy)
    # one message, so the consumer actually reaches the blocking call
    redis_server.rpush('TELEGRAM_BOT_MESSAGE', JsonSerializer().dumps({'function': 'send_message', 'chat_id': 1}))
    drain(RecordingBlpop(), expected=1, timeout=2)

    assert timeouts, 'the consumer never blocked on the queue'
    assert min(timeouts) >= 1, timeouts


def rate_limited_bot(attempts):
    """A bot that always answers 'retry later', so the retries run out."""

    class AlwaysRetryAfter:
        async def send_message(self, **kwargs):
            attempts.append(kwargs)
            raise exceptions.TelegramRetryAfter(
                method=SendMessage(chat_id=1, text='x'),
                message='Too Many Requests',
                retry_after=0,
            )

        class session:
            @staticmethod
            async def close():
                pass

    return AlwaysRetryAfter()


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'FSM_STORAGE': 'memory', 'MAX_RETRIES': 2, 'RATE_LIMIT': None})
def test_exhausting_the_retries_is_logged(caplog):
    """1.x gave up silently: no log, no exception, the message just vanished."""
    instance = TelegramBot()
    attempts = []
    instance._bot = rate_limited_bot(attempts)

    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        instance.send_raw(chat_id=1, text='x')

    assert len(attempts) == 3, 'MAX_RETRIES=2 means the first try plus two retries'
    assert 'giving up on message' in caplog.text
    instance._bot = None
    instance.close()


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'FSM_STORAGE': 'memory',
        'MAX_RETRIES': 1,
        'RAISE_EXCEPTION': True,
        'RATE_LIMIT': None,
    }
)
def test_exhausting_the_retries_raises_when_asked_to():
    """RAISE_EXCEPTION existed in 1.x but never fired on this path."""
    instance = TelegramBot()
    instance._bot = rate_limited_bot([])

    with pytest.raises(exceptions.TelegramRetryAfter):
        instance.send_raw(chat_id=1, text='x')

    instance._bot = None
    instance.close()


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'FSM_STORAGE': 'memory'})
def test_concurrent_first_sends_share_one_event_loop(monkeypatch):
    """Two loops means loop_lock hands the senders locks for different loops,
    and one of the loops is leaked."""
    instance = TelegramBot()
    created = []
    real_new_event_loop = asyncio.new_event_loop

    def slow_new_event_loop():
        time.sleep(0.05)  # widen the window both threads race through
        loop = real_new_event_loop()
        created.append(loop)
        return loop

    monkeypatch.setattr(asyncio, 'new_event_loop', slow_new_event_loop)

    seen = []
    ready = threading.Barrier(4, timeout=10)

    def touch():
        ready.wait()
        seen.append(instance.loop)

    threads = [threading.Thread(target=touch) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(created) == 1, f'{len(created)} event loops were built'
    assert len({id(loop) for loop in seen}) == 1
    for loop in created:
        loop.close()


@override_settings(
    TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 30, 'HEARTBEAT_INTERVAL': 4, 'REDIS_TIMEOUT': 60}
)
def test_the_consumer_pops_for_what_the_shared_ceiling_says(redis_server, monkeypatch):
    """W004 describes a cap; this is what makes the description true.

    The check tests prove `check_settings()` reports the right number. They cannot
    prove `run()` uses it — reverted to arithmetic of its own, every one of them still
    passes while the warning and the consumer disagree, which is the exact defect the
    shared helper was introduced to remove.

    Asked of the call: `blmove` records the timeout it was given. Four here rather
    than thirty, because the heartbeat binds.
    """
    asked: list[int] = []

    def record_and_stop(source, destination, timeout, *args, **kwargs):
        asked.append(timeout)
        delivery.stop()

    # on the instance, not the type: patching the class leaves `self` as the first
    # positional, and the timeout would be read out of the wrong argument
    monkeypatch.setattr(redis_server, 'blmove', record_and_stop, raising=False)
    delivery = BlpopDelivery(handler=lambda **kwargs: None)
    thread = delivery.start_thread()
    thread.join(timeout=5)

    assert asked, 'the consumer never popped, so nothing is being tested'
    assert asked[0] == 4, f'popped for {asked[0]}s while the ceiling says 4'


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1, 'WORKER_NAME': 'mine'})
def test_a_subclass_can_still_name_its_own_in_flight_list(redis_server, monkeypatch):
    """The keys are module-level functions, and the properties over them stay overridable.

    Both halves matter and only one of them was pinned. `queue_key`, `processing_key`
    and `heartbeat_key` live in `redis.py` so a producer, both depth reads and
    `tgbot_reclaim` derive them from one place — but `Delivery` keeps properties over
    those functions precisely so a subclass can answer differently, which
    `tests/integration/test_delivery_against_redis.py` relies on to run two consumers
    against one queue under different names. That reliance only ran with a real Redis.

    Asserted on the key the consumer handed to Redis, not on the property. Reading the
    property back proves only that the subclass defines it, and asserting the message
    arrived proves only that *some* key worked: the first version of this test did both
    and passed with `self.processing_key` replaced by the module function at all eight
    call sites — which is exactly the regression it was written for.
    """

    class Named(BlpopDelivery):
        """A consumer that keeps its in-flight list under a name of its own."""

        @property
        def processing_key(self) -> str:
            """Answer with a name this class chose rather than the worker identity."""
            return f'{self.queue_key}:processing:borrowed'

    destinations: list[str] = []
    original = redis_server.lmove

    def recording_lmove(source, destination, *args, **kwargs):
        destinations.append(destination)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(redis_server, 'lmove', recording_lmove)

    handled: list[int] = []
    delivery = Named(handler=lambda **kwargs: handled.append(kwargs['chat_id']))
    redis_server.rpush(
        'TELEGRAM_BOT_MESSAGE',
        JsonSerializer().dumps({'function': 'send_message', 'chat_id': 7}),
    )

    delivery.consume_pending()

    assert handled == [7], f'the override stopped the consumer working: {handled}'
    assert destinations, 'nothing was moved, so nothing is being tested'
    assert destinations[0] == 'TELEGRAM_BOT_MESSAGE:processing:borrowed', destinations[0]
    # and the module function is untouched by the override, which is what makes the
    # producer and `tgbot_reclaim` agree with each other rather than with a subclass
    assert processing_key() == 'TELEGRAM_BOT_MESSAGE:processing:mine'
