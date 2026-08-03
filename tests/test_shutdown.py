"""Shutdown paths: --idle, SIGTERM and releasing what the bot holds."""

import asyncio
import contextlib
import os
import signal
import threading
import time
from io import StringIO

from django.core.management import call_command
from django.test import override_settings

from django_redis_aiogram import TelegramBot, bot
from django_redis_aiogram.management.commands.start_tgbot import Command as StartCommand

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

    assert events == ['polling', 'stopped', 'closed']


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

    instance._bot = stub_bot()
    instance.close(drain_timeout=0.1)

    assert not foreign[0].cancelled(), 'shutdown cancelled a task belonging to someone else'
