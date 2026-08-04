"""The surface 1.x exposed is API, not implementation.

1.x `TelegramBot` was a dataclass, so its internals were part of how people used
it: driving `loop` by hand, feeding `dispatcher`, reusing `redis_conn`. The lazy
rewrite turned every one of those into a property. They all still exist — this
file is what keeps them existing, because a refactor that drops one would
otherwise be caught only indirectly, if at all.

The names are written out rather than derived from the code: a list generated
from the class under test would agree with any change to it.
"""

import asyncio
import inspect

import pytest
from django.test import override_settings

import django_redis_aiogram
from django_redis_aiogram import TelegramBot, bot
from django_redis_aiogram.checks import check_settings
from django_redis_aiogram.enums import DeliveryKind, SerializerKind, StorageKind, UpdateMode

#: attributes 1.x code reaches for directly
ONE_X_ATTRIBUTES = (
    'bot',  # the aiogram Bot
    'dispatcher',
    'loop',
    'max_retries',
    'redis_conn',
)

#: methods 1.x code calls
ONE_X_METHODS = ('start_polling', 'send_raw', 'send_redis')

#: every observer aiogram had a decorator for
ONE_X_DECORATORS = (
    'message',
    'edited_message',
    'channel_post',
    'edited_channel_post',
    'inline_query',
    'chosen_inline_result',
    'callback_query',
    'shipping_query',
    'pre_checkout_query',
    'poll',
    'poll_answer',
    'my_chat_member',
    'chat_member',
    'chat_join_request',
    'error',
)

#: what 2.x added on top, and must keep
TWO_X_ADDITIONS = ('send', 'router', 'enabled', 'is_worker', 'rate_limiter', 'close')

MODULE_EXPORTS = ('TelegramBot', 'bot', 'conf', 'redis_conn', 'get_redis', '__version__')

SETTINGS = {'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0', 'FSM_STORAGE': 'memory'}


@pytest.mark.parametrize('name', ONE_X_ATTRIBUTES + TWO_X_ADDITIONS)
def test_the_attribute_is_still_there(name):
    assert hasattr(TelegramBot, name), f'{name} disappeared from the public surface'


@pytest.mark.parametrize('name', ONE_X_METHODS + ONE_X_DECORATORS)
def test_the_method_is_still_callable(name):
    member = getattr(TelegramBot, name, None)
    assert callable(member), f'{name} is no longer a callable member'


@pytest.mark.parametrize('name', MODULE_EXPORTS)
def test_the_package_still_exports_it(name):
    assert name in django_redis_aiogram.__all__, f'{name} left __all__'
    assert getattr(django_redis_aiogram, name, None) is not None


@pytest.mark.parametrize('name', ONE_X_DECORATORS)
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_every_decorator_registers_on_the_router(name):
    """A decorator that silently stops registering is worse than a missing one."""
    instance = TelegramBot()
    observer = instance.router.observers[name]
    before = len(observer.handlers)

    @getattr(instance, name)()
    async def handler(event):  # pragma: no cover - registration is the point
        ...

    assert len(observer.handlers) == before + 1, f'{name} registered nothing'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_1_x_shape_still_works_end_to_end():
    """What 1.x code does: build it, reach inside, drive the loop yourself."""
    instance = TelegramBot()

    assert instance.max_retries == 10
    assert instance.loop is instance.loop, 'the loop must be the same object twice'
    assert instance.bot.token == '42:x'
    assert instance.dispatcher.storage is not None

    async def ask() -> str:
        return 'driven by hand'

    assert instance.loop.run_until_complete(ask()) == 'driven by hand'
    instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_construction_arguments_1_x_accepted():
    """`TelegramBot(max_retries=..., loop=...)` is how 1.x code built it."""
    signature = inspect.signature(TelegramBot)
    assert set(signature.parameters) == {'max_retries', 'loop'}

    supplied = asyncio.new_event_loop()
    instance = TelegramBot(max_retries=3, loop=supplied)
    try:
        assert instance.max_retries == 3
        # accepting the argument and then ignoring it would pass a signature check
        assert instance.loop is supplied
    finally:
        instance.close()
        if not supplied.is_closed():
            supplied.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_redis_conn_is_the_shared_connection(redis_server):
    """1.x reached through the bot for the connection; it must still be one."""
    assert bot.redis_conn is redis_server

    another = TelegramBot()
    try:
        assert another.redis_conn is bot.redis_conn, 'a second instance opened its own'
    finally:
        another.close()


ENUM_CLASSES = ('DeliveryKind', 'SerializerKind', 'StorageKind', 'UpdateMode', 'RateLimitKey', 'SerializationTag')
ERROR_CLASSES = ('DjangoRedisAiogramError', 'SerializationError', 'UnknownApiMethodError')


@pytest.mark.parametrize('name', ENUM_CLASSES)
def test_the_enums_page_documents_a_real_class(name):
    """The API page tells a project to import these; they have to exist."""
    from django_redis_aiogram import enums

    assert hasattr(enums, name), f'{name} is documented but missing'


@pytest.mark.parametrize('name', ERROR_CLASSES)
def test_the_errors_page_documents_a_real_class(name):
    from django_redis_aiogram import exceptions

    assert hasattr(exceptions, name)


def test_one_family_catches_everything_the_package_raises():
    """The page promises DjangoRedisAiogramError catches all of them."""
    from django_redis_aiogram.exceptions import (
        DjangoRedisAiogramError,
        SerializationError,
        UnknownApiMethodError,
    )

    assert issubclass(SerializationError, DjangoRedisAiogramError)
    assert issubclass(UnknownApiMethodError, DjangoRedisAiogramError)
    # and the bases they had before the family existed, so old excepts still work
    assert issubclass(UnknownApiMethodError, ValueError)


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'REDIS_URL': 'redis://localhost:6379/0',
        'DELIVERY': DeliveryKind.BLPOP,
        'SERIALIZER': SerializerKind.JSON,
        'FSM_STORAGE': StorageKind.REDIS,
        'MODE': UpdateMode.POLLING,
    }
)
def test_enum_members_are_accepted_wherever_the_string_is():
    """The page's settings example, executed: members must satisfy the checks."""
    serious = [message.id for message in check_settings() if message.is_serious()]

    assert serious == [], serious
