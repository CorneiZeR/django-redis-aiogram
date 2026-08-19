"""Shutdown paths: --idle, SIGTERM and releasing what the bot holds."""

import asyncio
import contextlib
import os
import signal
import threading
import time
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.test import override_settings

from django_redis_aiogram import TelegramBot, bot
from django_redis_aiogram.client import Outbound, loop_lock
from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.management.commands.start_tgbot import Command as StartCommand


async def stub_close():
    """Enough of an aiogram session for close() to release it."""


def an_outbound(function='send_message', **kwargs):
    """The call identity every scheduling path now carries."""
    return Outbound(new_correlation_id(), function, kwargs or {'chat_id': 1, 'text': 'x'})


SETTINGS = {'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0', 'FSM_STORAGE': 'memory'}


def stub_bot(sent=None, first_send=None):
    """A bot that records sends and signals the first one.

    Waiting on that signal is what lets the assertions below be about the rate
    limiter rather than about how fast this machine happens to be.
    """

    class StubBot:
        async def send_message(self, **kwargs):
            if sent is not None:
                sent.append(kwargs)
            if first_send is not None:
                first_send.set()

        class session:
            @staticmethod
            async def close():
                pass

    return StubBot()


@contextlib.contextmanager
def running_loop(instance):
    """Run the bot's loop in a thread, and wait until it is actually running.

    Sleeping instead would schedule sends against a loop that may not have
    started, which is a timing assumption rather than a test.
    """
    loop = instance.loop
    ready = threading.Event()
    loop.call_soon_threadsafe(ready.set)
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    assert ready.wait(5), 'the event loop never started'
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_idle_blocks_until_interrupted(monkeypatch):
    """A clean exit is a restart loop under `restart: always`, hence --idle."""
    out = StringIO()
    finished = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(StartCommand, 'idle_event', release)

    def run():
        call_command('start_tgbot', '--idle', stdout=out)
        finished.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()

    # still parked a moment later, unlike the plain disabled path
    assert not finished.wait(0.4)
    assert 'Idling' in out.getvalue()

    # end the wait rather than leaving the thread parked for the whole session
    release.set()
    assert finished.wait(5)
    worker.join(timeout=5)
    assert not worker.is_alive()


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_without_idle_the_command_returns_immediately():
    out = StringIO()
    finished = threading.Event()

    def run():
        call_command('start_tgbot', stdout=out)
        finished.set()

    threading.Thread(target=run, daemon=True).start()
    assert finished.wait(5)
    assert 'Idling' not in out.getvalue()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_sigterm_unwinds_polling(monkeypatch):
    """`docker stop` sends SIGTERM; it has to reach the shutdown path."""
    events = []

    class Delivery:
        def start_thread(self):
            return threading.Thread(target=lambda: None)

        def stop(self):
            events.append('stopped')

        def collect(self):
            # the teardown settles the sends close() drained; a double that omits it
            # only proves the command still runs, not that it finishes the job
            events.append('collected')

    monkeypatch.setattr(
        'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
        lambda handler: Delivery(),
    )
    monkeypatch.setattr(bot, 'close', lambda: events.append('closed'))

    def polling_that_waits_for_the_signal():
        events.append('polling')
        os.kill(os.getpid(), signal.SIGTERM)
        # the handler raises KeyboardInterrupt into this thread
        time.sleep(2)

    monkeypatch.setattr(bot, 'start_polling', polling_that_waits_for_the_signal)

    previous = signal.getsignal(signal.SIGTERM)
    try:
        call_command('start_tgbot')
    finally:
        signal.signal(signal.SIGTERM, previous)

    # `collected` after `closed`, because close() is what drains the sends being settled
    assert events == ['polling', 'stopped', 'closed', 'collected']


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_close_releases_the_fsm_storage():
    """RedisStorage owns a second async client that nothing else closes."""
    instance = TelegramBot()
    storage = instance.dispatcher.storage
    closed = []
    original = storage.close

    async def tracking_close():
        closed.append(True)
        await original()

    storage.close = tracking_close
    instance.close()

    assert closed == [True]
    assert instance._dispatcher is None
    assert instance._bot is None


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_close_is_safe_when_nothing_was_built():
    TelegramBot().close()


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'FSM_STORAGE': 'memory',
        'RATE_LIMIT': {'overall_per_second': 1, 'per_chat_per_second': 0, 'group_per_minute': 0},
    }
)
def test_shutdown_waits_for_sends_blocked_in_the_rate_limiter():
    """Pacing means waiting, so closing without draining loses those messages."""
    instance = TelegramBot()
    sent = []
    first_send = threading.Event()

    instance._bot = stub_bot(sent, first_send)
    with running_loop(instance):
        for index in range(4):  # one per second, so three have to wait
            instance.send_raw(chat_id=index, text='x')
        assert first_send.wait(5), 'nothing was sent at all, so nothing is being tested'
        assert len(sent) < 4, 'the limiter did not actually block anything'

    instance._bot = stub_bot(sent)
    instance.close(drain_timeout=10)

    assert len(sent) == 4, f'shutdown dropped {4 - len(sent)} paced sends'


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'FSM_STORAGE': 'memory',
        # one message every 100s: the second send cannot finish within the drain
        'RATE_LIMIT': {'overall_per_second': 0.01, 'per_chat_per_second': 0, 'group_per_minute': 0},
    }
)
def test_shutdown_cancels_a_send_that_outlasts_the_drain(caplog):
    """A send stuck behind the limiter must not hang the container forever."""
    instance = TelegramBot()
    sent = []
    first_send = threading.Event()

    instance._bot = stub_bot(sent, first_send)
    with running_loop(instance):
        instance.send_raw(chat_id=1, text='first')
        instance.send_raw(chat_id=2, text='stuck behind the limiter')
        assert first_send.wait(5), 'the first send never happened'

    assert len(sent) == 1, sent
    assert [task for task in instance._sends if not task.done()], 'nothing was left tracked'

    instance._bot = stub_bot(sent)
    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        instance.close(drain_timeout=0.1)

    assert 'dropped in-flight sends at shutdown' in caplog.text
    assert len(sent) == 1, 'the cancelled send should not have gone out'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_shutdown_leaves_tasks_it_does_not_own_alone():
    """aiogram keeps its own tasks on this loop; cancelling them is not ours."""
    instance = TelegramBot()
    foreign = []
    created = threading.Event()

    def create_foreign_task(loop):
        foreign.append(loop.create_task(asyncio.sleep(3600)))
        created.set()

    instance._bot = stub_bot()
    with running_loop(instance) as loop:
        loop.call_soon_threadsafe(create_foreign_task, loop)
        assert created.wait(5), 'the foreign task was never created'

    task = foreign[0]
    cancels = []
    original_cancel = task.cancel
    # spied rather than inferred: a stopped loop reports nothing as cancelled
    task.cancel = lambda *args, **kwargs: cancels.append(True) or original_cancel(*args, **kwargs)  # type: ignore[method-assign]

    instance._bot = stub_bot()
    instance.close(drain_timeout=0.1)

    assert cancels == [], 'shutdown cancelled a task belonging to someone else'
    assert not task.cancelled()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_send_started_during_shutdown_is_refused_loudly(caplog):
    """Scheduling onto a loop that is being torn down loses the message."""
    instance = TelegramBot()
    instance._bot = stub_bot()
    instance._closing = True

    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        instance.send_raw(chat_id=1, text='x')

    assert 'send refused: the bot is shutting down' in caplog.text
    assert not instance._sends, 'the send was scheduled anyway'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_handoff_queued_before_shutdown_is_dropped_loudly(caplog):
    """close() can start after call_soon_threadsafe and before the callback."""
    instance = TelegramBot()
    sent = []
    instance._bot = stub_bot(sent)

    with running_loop(instance) as loop:
        instance._closing = True
        instance._hand_off(instance.bot.send_message(chat_id=1, text='x'), loop, an_outbound())
        with caplog.at_level('ERROR', logger='django_redis_aiogram'):
            done = threading.Event()
            loop.call_soon_threadsafe(done.set)
            assert done.wait(5), 'the loop never ran the queued callback'

    assert 'send dropped: the bot started shutting down' in caplog.text
    assert sent == []


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_close_waits_for_a_send_driving_the_same_loop():
    """Tearing the loop down under run_until_complete corrupts both."""
    instance = TelegramBot()
    instance._bot = stub_bot()
    lock = loop_lock(instance.loop)
    finished = threading.Event()

    lock.acquire()
    threading.Thread(target=lambda: (instance.close(drain_timeout=0.1), finished.set()), daemon=True).start()
    try:
        assert not finished.wait(0.3), 'close tore the loop down while it was in use'
    finally:
        lock.release()
    assert finished.wait(5), 'close never finished after the loop was released'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_send_waiting_on_the_lock_finds_the_loop_closed(caplog):
    """close() holds the same lock, so it can finish while a send waits for it."""
    instance = TelegramBot()
    instance._bot = stub_bot()
    loop = instance.loop
    entered = threading.Event()
    released = threading.Event()

    def hold_the_lock():
        with loop_lock(loop):
            entered.set()
            released.wait(5)

    threading.Thread(target=hold_the_lock, daemon=True).start()
    assert entered.wait(5), 'the lock was never taken'

    sender = threading.Thread(target=lambda: instance.send_raw(chat_id=1, text='x'), daemon=True)
    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        sender.start()
        # close the loop underneath the send, and leave _closing alone: it is
        # read before the lock, so setting it races the sender's start-up and
        # decides which of the two refusals wins
        loop.close()
        released.set()
        sender.join(timeout=5)

    assert not sender.is_alive(), 'the send never returned'
    assert 'send refused: the event loop was closed' in caplog.text


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_close_refuses_to_tear_down_a_running_loop(caplog):
    """run_until_complete and loop.close() both raise on a running loop."""
    instance = TelegramBot()
    instance._bot = stub_bot()
    assert instance.dispatcher is not None  # so there is something to tear down

    with running_loop(instance), caplog.at_level('WARNING', logger='django_redis_aiogram'):
        instance.close(drain_timeout=0.1)

    assert 'skipping close: stop polling, or the loop thread, before closing the bot' in caplog.text
    # nothing was half-released, so closing again after polling stops still works
    assert instance._loop is not None
    assert instance._bot is not None
    instance.close(drain_timeout=0.1)
    assert instance._loop is None
    assert instance._bot is None


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True})
def test_the_recorder_is_stopped_even_when_close_raises(monkeypatch, redis_server):
    """A close() that raises must not also lose the rows the shutdown produced."""
    from django_redis_aiogram.recorder import recorder

    stopped = []
    monkeypatch.setattr(recorder, 'stop', lambda *args, **kwargs: stopped.append(True))

    instance = TelegramBot()

    def explode(*args, **kwargs):
        msg = 'close blew up'
        raise RuntimeError(msg)

    monkeypatch.setattr(instance, 'close', explode)
    monkeypatch.setattr('django_redis_aiogram.management.commands.start_tgbot.bot', instance)

    command = StartCommand()
    command.idle_event = threading.Event()
    command.idle_event.set()

    with pytest.raises(RuntimeError, match='close blew up'):
        command.handle(mode='webhook', idle=False)

    assert stopped, 'the recorder was not stopped when close() raised'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_handoff_the_loop_never_stepped_is_drained_not_destroyed():
    """The one `_register`'s docstring says shutdown must not lose.

    A hand-off is a `call_soon_threadsafe` callback until the loop steps, and a
    callback is not a task, so `_drain` could not see it. `close()` set `_closing`
    first, `start()` refused on that flag, and the message — already gone from the
    queue's in-flight list — died with the loop.

    The runner is stopped *and joined* before the hand-off. The obvious recipe —
    hand off, then `call_soon_threadsafe(loop.stop)` — passes on the old code,
    because one `_run_once` batch runs both callbacks.
    """
    instance = TelegramBot()
    sent = []
    instance._bot = stub_bot(sent)

    loop = instance.loop
    ready = threading.Event()
    loop.call_soon_threadsafe(ready.set)
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    assert ready.wait(5), 'the event loop never started'
    loop.call_soon_threadsafe(loop.stop)
    runner.join(timeout=5)
    assert not runner.is_alive(), 'the loop is still running'

    instance._hand_off(instance.bot.send_message(chat_id=1, text='drained'), loop, an_outbound())
    instance.close()

    assert [call['chat_id'] for call in sent] == [1]


@override_settings(TELEGRAM_BOT={**SETTINGS, 'DRAIN_TIMEOUT': 9})
def test_close_takes_its_drain_budget_from_the_setting():
    """`start_tgbot` calls `bot.close()` bare, so the hardcoded five seconds was
    the only budget a deployment could ever get — however long its
    `stop_grace_period` said."""
    instance = TelegramBot()
    instance._bot = stub_bot()
    seen = []
    instance._drain = seen.append

    instance.close()

    assert seen == [9.0]


@override_settings(TELEGRAM_BOT={**SETTINGS, 'DRAIN_TIMEOUT': 'soon'})
def test_an_unreadable_drain_budget_does_not_break_shutdown():
    """E044 reports it at boot. Here the safe answer is the default: the drain sits
    between stopping the consumer and flushing the event log, so raising costs the
    rows that describe what the drain just did."""
    instance = TelegramBot()
    instance._bot = stub_bot()
    seen = []
    instance._drain = seen.append

    instance.close()

    assert seen == [float(DEFAULTS['DRAIN_TIMEOUT'])]


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_send_cancelled_at_shutdown_is_not_acknowledged():
    """Cancellation is not completion.

    A send drained away never reached Telegram, so acknowledging it would destroy
    it — leaving it in the in-flight list is exactly what lets the next start
    pick it up, and is what makes at-least-once true rather than documented.
    """
    instance = TelegramBot()
    started = threading.Event()

    async def never_returns(**kwargs):
        started.set()
        await asyncio.sleep(60)

    instance._bot = SimpleNamespace(send_message=never_returns, session=SimpleNamespace(close=stub_close))
    acknowledged = []

    with running_loop(instance):
        instance.send_raw(chat_id=1, text='x', on_complete=lambda: acknowledged.append(True))
        assert started.wait(5), 'the send never started'

    instance.close(drain_timeout=0.05)

    assert acknowledged == [], 'a cancelled send was acknowledged'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_finished_send_is_acknowledged_once():
    """Sent, refused or given up on — all three are finished, and redelivering
    any of them would only repeat itself."""
    instance = TelegramBot()
    instance._bot = stub_bot()
    acknowledged = []

    with running_loop(instance):
        instance.send_raw(chat_id=1, text='x', on_complete=lambda: acknowledged.append(True))
        for _ in range(500):
            if acknowledged:
                break
            threading.Event().wait(0.01)

    instance.close()

    assert acknowledged == [True]
