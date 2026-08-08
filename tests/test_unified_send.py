"""`send()` picks the route the current process can actually use."""

import contextlib

from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.envelope import unpack
from django_redis_aiogram.serializers import JsonSerializer

SETTINGS = {'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0'}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_outside_the_worker_it_queues(redis_server):
    instance = TelegramBot()
    assert instance.is_worker is False

    instance.send(chat_id=1, text='hi')

    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 1
    queued = unpack(JsonSerializer().loads(redis_server.lindex('TELEGRAM_BOT_MESSAGE', 0)))
    assert queued.function == 'send_message'
    assert queued.kwargs == {'chat_id': 1, 'text': 'hi'}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_inside_the_worker_it_calls_telegram(redis_server, monkeypatch):
    instance = TelegramBot()
    instance._polling = True
    sent = []
    monkeypatch.setattr(
        instance,
        'send_raw',
        lambda function='send_message', correlation_id=None, **kw: sent.append((function, kw)),
    )

    instance.send(chat_id=1, text='hi')

    assert sent == [('send_message', {'chat_id': 1, 'text': 'hi'})]
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_function_name_is_forwarded(redis_server):
    TelegramBot().send('send_photo', chat_id=1, photo='file_id')

    queued = unpack(JsonSerializer().loads(redis_server.lindex('TELEGRAM_BOT_MESSAGE', 0)))
    assert queued.function == 'send_photo'


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_disabled_send_is_a_noop(monkeypatch):
    """Neither route may build a bot or reach for a connection."""

    def forbidden():
        msg = 'a disabled send reached for Redis'
        raise AssertionError(msg)

    monkeypatch.setattr('django_redis_aiogram.client.get_redis', forbidden)

    instance = TelegramBot()
    instance.send(chat_id=1, text='hi')

    assert instance._bot is None


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_polling_clears_the_worker_flag_even_when_it_fails(monkeypatch):
    instance = TelegramBot()
    seen = []

    async def failing_polling(*args, **kwargs):
        # observed from inside the loop, which is the only place it may be true
        seen.append(instance.is_worker)
        raise KeyboardInterrupt

    monkeypatch.setattr(instance.dispatcher, 'start_polling', failing_polling)

    with contextlib.suppress(KeyboardInterrupt):
        instance.start_polling()

    assert seen == [True], 'the flag was never set while polling was running'
    assert instance.is_worker is False


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_not_a_worker_until_the_loop_is_actually_running(redis_server, monkeypatch):
    """The flag used to be set before run_until_complete, so during startup
    send() chose send_raw against a loop that was not running yet."""
    instance = TelegramBot()
    observed = []

    def spy(coroutine):
        # the exact moment the loop is handed the polling coroutine
        observed.append(instance.is_worker)
        coroutine.close()

    monkeypatch.setattr(instance.loop, 'run_until_complete', spy)
    instance.start_polling()

    assert observed == [False], 'is_worker was already true before the loop ran'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_sends_during_startup_are_queued_not_sent_directly(redis_server, monkeypatch):
    """Deterministic stand-in for the startup interval: a send issued while the
    loop has not started must go to Redis, not to a loop-bound send_raw."""
    instance = TelegramBot()
    direct = []
    monkeypatch.setattr(instance, 'send_raw', lambda *a, **kw: direct.append(kw))

    def send_during_startup(coroutine):
        instance.send(chat_id=7, text='during startup')
        coroutine.close()

    monkeypatch.setattr(instance.loop, 'run_until_complete', send_during_startup)
    instance.start_polling()

    assert direct == [], 'a startup-time send was driven through send_raw'
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 1
