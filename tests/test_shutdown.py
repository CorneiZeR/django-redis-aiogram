"""Shutdown paths: --idle, SIGTERM and releasing what the bot holds."""

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
