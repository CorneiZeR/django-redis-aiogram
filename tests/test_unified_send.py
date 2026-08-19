"""`send()` picks the route the current process can actually use."""

import contextlib

import pytest
from aiogram import exceptions
from aiogram.methods import SendMessage
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


class Refusing:
    """A bot whose every send fails, so the retry path runs to its end."""

    def __init__(self):
        self.attempts = []

    async def send_message(self, **kwargs):
        """Refuse with something `_schedule` does not intercept on its way out."""
        self.attempts.append(kwargs)
        # not RuntimeError: `_schedule` catches that one to spot a loop already
        # running under it, so it would never reach the branch under test
        msg = 'chat not found'
        raise ValueError(msg)

    class session:
        @staticmethod
        async def close():
            """aiogram's session, reduced to the one call `close()` makes."""


class RateLimited:
    """A bot Telegram always meters, so the retry loop runs out instead of failing."""

    def __init__(self):
        self.attempts = []

    async def send_message(self, **kwargs):
        """Refuse the way Telegram refuses, which is the other raising path entirely."""
        self.attempts.append(kwargs)
        raise exceptions.TelegramRetryAfter(
            method=SendMessage(chat_id=1, text='x'),
            message='Too Many Requests',
            retry_after=0,
        )

    class session:
        @staticmethod
        async def close():
            """aiogram's session, reduced to the one call `close()` makes."""


def a_failing_send(raise_exception, bot=Refusing):
    """Drive one doomed send on the caller's thread and report what reached them."""
    with override_settings(
        TELEGRAM_BOT={**SETTINGS, 'RAISE_EXCEPTION': raise_exception, 'MAX_RETRIES': 0, 'RATE_LIMIT': None}
    ):
        instance = TelegramBot()
        refusing = bot()
        instance._bot = refusing
        try:
            # no loop runner, so `send_raw` drives the coroutine here and a raise
            # from it lands on this line — the way it reaches a caller's view
            instance.send_raw('send_message', chat_id=1, text='x')
        finally:
            instance._bot = None
            instance.close()
        return refusing.attempts


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_raise_exception_reads_the_word_and_not_its_truthiness():
    """`DJANGO_REDIS_AIOGRAM_RAISE_EXCEPTION=false` arrives as `'false'`, which is truthy.

    Read with a bare `if`, that re-raised into the caller the exception the project had
    just asked to have swallowed — and only after a send had exhausted `MAX_RETRIES`, so a
    project that spelled the flag the way the environment can spell it learned about it
    the day Telegram started refusing them.
    """
    assert a_failing_send('false'), 'the send never ran, so nothing was proved'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_flag_still_reaches_the_caller_when_it_is_asked_to():
    """The other direction, so the fix cannot be "the flag never fires".

    `'true'` is the string form as well: the point is that the word is read, not that
    strings are ignored.
    """
    for asked in (True, 'true'):
        with pytest.raises(ValueError, match='chat not found'):
            a_failing_send(asked)


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_exhausted_retry_path_reads_the_word_too():
    """The second place the flag is read, and the one the tests above cannot reach.

    A refusal Telegram *retries* leaves the loop by exhausting `MAX_RETRIES`, not through
    the generic handler, so it raises `last_error` from a different line. Reverting that
    line alone to raw truthiness left 116 tests passing — half a fix with no failure to
    its name, in the change whose whole subject is reading this flag correctly.
    """
    assert a_failing_send('false', bot=RateLimited), 'the send never ran, so nothing was proved'

    for asked in (True, 'true'):
        with pytest.raises(exceptions.TelegramRetryAfter):
            a_failing_send(asked, bot=RateLimited)
