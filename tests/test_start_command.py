"""The consumer thread must not touch the loop before the loop is running."""

import asyncio
import signal
import threading
from types import SimpleNamespace

from django.core.management import call_command
from django.test import override_settings

from django_redis_aiogram import bot


class RecordingDelivery:
    def __init__(self, events):
        self.events = events

    def start_thread(self):
        self.events.append('consumer-started')
        return SimpleNamespace(join=lambda timeout=None: None)

    def stop(self):
        self.events.append('stopped')


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0'})
def test_consumer_starts_only_after_the_loop_is_running(monkeypatch):
    events = []
    monkeypatch.setattr(
        'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
        lambda handler: RecordingDelivery(events),
    )

    def fake_polling():
        events.append('loop-running')
        # draining one iteration runs whatever call_soon queued
        bot.loop.run_until_complete(asyncio.sleep(0))

    monkeypatch.setattr(bot, 'start_polling', fake_polling)
    monkeypatch.setattr(bot, 'close', lambda: events.append('closed'))

    call_command('start_tgbot')

    assert events.index('loop-running') < events.index('consumer-started'), (
        f'consumer started before the loop was running: {events}'
    )
    assert 'stopped' in events


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0'})
def test_shutdown_is_safe_when_the_consumer_never_started(monkeypatch):
    """Polling can fail before the loop runs the deferred start."""
    events = []
    monkeypatch.setattr(
        'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
        lambda handler: RecordingDelivery(events),
    )

    def failing_polling():
        raise KeyboardInterrupt

    monkeypatch.setattr(bot, 'start_polling', failing_polling)
    monkeypatch.setattr(bot, 'close', lambda: events.append('closed'))

    call_command('start_tgbot')

    assert 'consumer-started' not in events
    assert events == ['stopped', 'closed']


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0'})
def test_the_previous_sigterm_handler_is_restored(monkeypatch):
    """The command may run in-process; a left-behind handler turns a later
    SIGTERM into a stray KeyboardInterrupt somewhere else entirely."""

    def sentinel(signum, frame):
        pass

    previous = signal.signal(signal.SIGTERM, sentinel)
    try:
        monkeypatch.setattr(
            'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
            lambda handler: _NoDelivery(),
        )
        monkeypatch.setattr(bot, 'close', lambda: None)
        monkeypatch.setattr(bot, 'start_polling', lambda: None)

        call_command('start_tgbot')

        assert signal.getsignal(signal.SIGTERM) is sentinel
    finally:
        signal.signal(signal.SIGTERM, previous)


class _NoDelivery:
    def start_thread(self):
        return threading.Thread(target=lambda: None)

    def stop(self):
        pass
