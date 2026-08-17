"""The consumer thread must not touch the loop before the loop is running."""

import asyncio
import signal
import threading
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from django_redis_aiogram import bot
from django_redis_aiogram.management.commands.start_tgbot import Command


class RecordingDelivery:
    # the two the command asks about before it starts anything
    crash_safe = True
    # named in the log line when a crash-safety probe could not reach Redis
    queue_key = 'TELEGRAM_BOT_MESSAGE'

    def __init__(self, events):
        self.events = events

    def reclaim(self):
        return True

    def start_thread(self):
        self.events.append('consumer-started')
        # is_alive too: the command warns about a consumer that outlived its join,
        # and a double that only answers join() hides that call from every test here
        return SimpleNamespace(join=lambda timeout=None: None, is_alive=lambda: False)

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


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0', 'MODE': 'webhook'})
def test_webhook_mode_consumes_without_calling_telegram(monkeypatch):
    """Webhook mode: updates arrive over HTTP, but the queue still needs a worker."""
    events = []

    class Delivery:
        def start_thread(self):
            # the real one starts it, and handle() joins what it is given
            events.append('consumer-started')
            thread = threading.Thread(target=lambda: None)
            thread.start()
            return thread

        def stop(self):
            events.append('stopped')

    handlers = []
    monkeypatch.setattr(
        'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
        lambda handler: handlers.append(handler) or Delivery(),
    )
    monkeypatch.setattr(bot, 'close', lambda: events.append('closed'))
    monkeypatch.setattr(bot, 'start_polling', lambda: events.append('POLLED'))
    release = threading.Event()
    monkeypatch.setattr(Command, 'idle_event', release)

    out = StringIO()
    finished = threading.Event()

    def run():
        call_command('start_tgbot', stdout=out)
        finished.set()

    threading.Thread(target=run, daemon=True).start()
    assert not finished.wait(0.4), 'it returned instead of consuming'
    release.set()
    assert finished.wait(5)

    assert handlers == [bot.send_raw], 'the consumer was given the wrong handler'
    assert 'POLLED' not in events, 'it polled Telegram in webhook mode'
    assert events == ['consumer-started', 'stopped', 'closed'], events
    assert 'Updates arrive by webhook.' in out.getvalue()
    assert 'Consuming the queue' in out.getvalue()


def run_start_command(**options):
    """Run the command with a consumer that records and an idle release."""
    events = []

    class Delivery:
        def start_thread(self):
            events.append('consumer-started')
            thread = threading.Thread(target=lambda: None)
            thread.start()
            return thread

        def stop(self):
            events.append('stopped')

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
            lambda handler: Delivery(),
        )
        patch.setattr(bot, 'close', lambda: None)
        patch.setattr(bot, 'start_polling', lambda: events.append('polled'))
        release = threading.Event()
        patch.setattr(Command, 'idle_event', release)

        out = StringIO()
        finished = threading.Event()

        def run():
            call_command('start_tgbot', stdout=out, **options)
            finished.set()

        threading.Thread(target=run, daemon=True).start()
        if options.get('mode') == 'webhook':
            assert not finished.wait(0.3)
            release.set()
        assert finished.wait(5)

    return out.getvalue(), events


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0', 'MODE': 'polling'})
def test_asking_for_webhook_mode_against_a_polling_setting_warns():
    """The view reads the setting, so this process would consume updates nobody
    is serving."""
    printed, events = run_start_command(mode='webhook')

    assert 'Updates arrive by webhook.' in printed
    assert 'disagrees' in printed
    assert 'refuses updates' in printed
    assert 'polled' not in events


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'REDIS_URL': 'redis://localhost:6379/0',
        'MODE': 'webhook',
        'WEBHOOK_URL': 'https://example.test/tg/',
        'WEBHOOK_SECRET': 'x' * 16,
    }
)
def test_asking_for_polling_against_a_webhook_setting_warns():
    printed, events = run_start_command(mode='polling')

    assert 'Updates arrive by polling.' in printed
    assert 'disagrees' in printed
    assert 'getUpdates fails' in printed
    assert 'polled' in events, 'it did not poll despite being asked to'


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0', 'MODE': 'polling'})
def test_no_warning_when_the_flag_agrees_with_the_setting():
    printed, events = run_start_command(mode='polling')

    assert 'disagrees' not in printed
    assert 'polled' in events


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0', 'REDIS_TIMEOUT': 10})
def test_the_consumer_join_is_derived_from_the_read_deadline(monkeypatch):
    """`BLPOP_TIMEOUT + 1` was six seconds against a worst case of ten.

    Every call the consumer makes is bounded by `REDIS_TIMEOUT`, and its blocking
    pop by one less than that — so the old deadline could expire while the thread
    was still inside a legitimate call, and the consumer that came back would
    acknowledge a message `close()` had already refused.
    """
    joined = []

    class SlowDelivery(RecordingDelivery):
        def start_thread(self):
            self.events.append('consumer-started')
            return SimpleNamespace(join=lambda timeout=None: joined.append(timeout), is_alive=lambda: False)

    monkeypatch.setattr(
        'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
        lambda handler: SlowDelivery([]),
    )
    monkeypatch.setattr(bot, 'start_polling', lambda: bot.loop.run_until_complete(asyncio.sleep(0)))
    monkeypatch.setattr(bot, 'close', lambda: None)

    call_command('start_tgbot')

    assert joined == [11], f'joined with {joined}, expected the read deadline plus one'


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0'})
def test_a_consumer_that_outlives_its_join_is_reported(monkeypatch, caplog):
    """Silence there reads as a clean shutdown, and it is the opposite."""

    class StuckDelivery(RecordingDelivery):
        def start_thread(self):
            self.events.append('consumer-started')
            return SimpleNamespace(join=lambda timeout=None: None, is_alive=lambda: True)

    monkeypatch.setattr(
        'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
        lambda handler: StuckDelivery([]),
    )
    monkeypatch.setattr(bot, 'start_polling', lambda: bot.loop.run_until_complete(asyncio.sleep(0)))
    monkeypatch.setattr(bot, 'close', lambda: None)

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        call_command('start_tgbot')

    assert 'the delivery consumer did not stop in time' in caplog.text
    # the field, not just the sentence: Logging.md documents it, and the message
    # alone passes with `extra` deleted
    warning = next(r for r in caplog.records if 'did not stop in time' in r.message)
    assert warning.tg_timeout == 11


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'REDIS_URL': 'redis://localhost:6379/0',
        'REQUIRE_CRASH_SAFE': True,
    }
)
def test_a_server_without_lmove_is_refused_when_crash_safety_is_required(monkeypatch):
    """Probed before the thread starts on purpose: `run()` is a daemon thread, so
    a SystemExit raised there kills only that thread and leaves the process
    polling updates with a dead consumer."""

    class OldServer(RecordingDelivery):
        def reclaim(self):
            self.crash_safe = False
            return True

    started = []
    monkeypatch.setattr(
        'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
        lambda handler: OldServer(started),
    )
    # recorded too: asserting only on the consumer would let the probe move after
    # start_polling and still pass, and a process polling updates with nothing
    # draining the queue is the shape this refusal exists to prevent
    monkeypatch.setattr(bot, 'start_polling', lambda: started.append('polling-started'))
    monkeypatch.setattr(bot, 'close', lambda: None)

    with pytest.raises(CommandError, match='LMOVE'):
        call_command('start_tgbot')

    assert started == [], f'the refusal came too late: {started}'


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'REDIS_URL': 'redis://localhost:6379/0',
        'REQUIRE_CRASH_SAFE': True,
    }
)
def test_an_unreachable_redis_does_not_read_as_an_old_server(monkeypatch, caplog):
    """`reclaim()` returns False when it could not talk to Redis at all, with
    crash safety still intact. Refusing to start over that turns a blip into an
    outage."""

    events = []

    class Unreachable(RecordingDelivery):
        def reclaim(self):
            return False

    monkeypatch.setattr(
        'django_redis_aiogram.management.commands.start_tgbot.get_delivery',
        lambda handler: Unreachable(events),
    )

    def polled():
        events.append('polling-started')
        bot.loop.run_until_complete(asyncio.sleep(0))

    monkeypatch.setattr(bot, 'start_polling', polled)
    monkeypatch.setattr(bot, 'close', lambda: None)

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        call_command('start_tgbot')

    # not merely "it did not raise": a command that returned early over the failed
    # probe would satisfy that while starting neither the consumer nor polling
    assert 'consumer-started' in events, events
    assert 'polling-started' in events, events
    # and the operator is told the guarantee went unproven rather than passed
    assert 'could not verify crash-safe delivery' in caplog.text
