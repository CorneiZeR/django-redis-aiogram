"""`send()` picks the route the current process can actually use."""

import contextlib

from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.serializers import JsonSerializer

SETTINGS = {'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0'}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_outside_the_worker_it_queues(redis_server):
    instance = TelegramBot()
    assert instance.is_worker is False

    instance.send(chat_id=1, text='hi')

    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 1
    queued = JsonSerializer().loads(redis_server.lindex('TELEGRAM_BOT_MESSAGE', 0))
    assert queued == {'function': 'send_message', 'chat_id': 1, 'text': 'hi'}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_inside_the_worker_it_calls_telegram(redis_server, monkeypatch):
    instance = TelegramBot()
    instance._polling = True
    sent = []
    monkeypatch.setattr(
        instance, 'send_raw', lambda function='send_message', **kw: sent.append((function, kw))
    )

    instance.send(chat_id=1, text='hi')

    assert sent == [('send_message', {'chat_id': 1, 'text': 'hi'})]
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_function_name_is_forwarded(redis_server):
    TelegramBot().send('send_photo', chat_id=1, photo='file_id')

    queued = JsonSerializer().loads(redis_server.lindex('TELEGRAM_BOT_MESSAGE', 0))
    assert queued['function'] == 'send_photo'


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_disabled_send_is_a_noop():
    """Neither route may build a bot or reach for a connection."""
    instance = TelegramBot()
    instance.send(chat_id=1, text='hi')
    assert instance._bot is None


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_polling_clears_the_worker_flag_even_when_it_fails(monkeypatch):
    instance = TelegramBot()

    def failing_polling(coroutine, *args, **kwargs):
        coroutine.close()
        assert instance.is_worker is True
        raise KeyboardInterrupt

    monkeypatch.setattr(instance.loop, 'run_until_complete', failing_polling)

    with contextlib.suppress(KeyboardInterrupt):
        instance.start_polling()

    assert instance.is_worker is False
