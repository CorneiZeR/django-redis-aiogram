"""The surface predating 2.0 is API, not implementation.

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
import pathlib

import pytest
from django.test import override_settings

import django_redis_aiogram
from django_redis_aiogram import TelegramBot, bot
from django_redis_aiogram.checks import check_settings
from django_redis_aiogram.enums import DeliveryKind, SerializerKind, StorageKind, UpdateMode

#: attributes that predate 2.0 and are reached for directly
INHERITED_ATTRIBUTES = (
    'bot',  # the aiogram Bot
    'dispatcher',
    'loop',
    'max_retries',
    'redis_conn',
)

#: methods that predate 2.0
INHERITED_METHODS = ('start_polling', 'send_raw', 'send_redis')

#: every observer aiogram has a decorator for
OBSERVER_DECORATORS = (
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

#: what 2.0 added on top, and must keep
TWO_X_ADDITIONS = ('send', 'router', 'enabled', 'is_worker', 'rate_limiter', 'close')

MODULE_EXPORTS = ('TelegramBot', 'bot', 'conf', 'redis_conn', 'get_redis', '__version__')

SETTINGS = {'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0', 'FSM_STORAGE': 'memory'}


@pytest.mark.parametrize('name', INHERITED_ATTRIBUTES + TWO_X_ADDITIONS)
def test_the_attribute_is_still_there(name):
    assert hasattr(TelegramBot, name), f'{name} disappeared from the public surface'


@pytest.mark.parametrize('name', INHERITED_METHODS + OBSERVER_DECORATORS)
def test_the_method_is_still_callable(name):
    member = getattr(TelegramBot, name, None)
    assert callable(member), f'{name} is no longer a callable member'


@pytest.mark.parametrize('name', MODULE_EXPORTS)
def test_the_package_still_exports_it(name):
    assert name in django_redis_aiogram.__all__, f'{name} left __all__'
    assert getattr(django_redis_aiogram, name, None) is not None


@pytest.mark.parametrize('name', OBSERVER_DECORATORS)
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
def test_the_pre_2_0_shape_still_works_end_to_end():
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


def test_the_installed_redis_can_run_the_fsm_storage():
    """Catches a bad floor in the unit suite, where fakeredis cannot hide it.

    aiogram's RedisStorage calls aclose() on the async client. redis-py added it
    in 5.0.1, so the 2.1.x floor of >=5.0 promised support that raised
    AttributeError — and nothing noticed, because the unit suite talks to
    fakeredis, which has aclose(). This asserts the real client the floor allows.
    """
    from redis.asyncio import Redis

    assert hasattr(Redis, 'aclose'), 'the installed redis-py cannot back FSM_STORAGE = redis'


def test_the_declared_redis_floor_is_not_below_what_aiogram_asks_for():
    """We use aiogram's storage, so its requirement is the real lower bound."""
    import re
    from importlib.metadata import requires

    pyproject = (pathlib.Path(__file__).resolve().parent.parent / 'pyproject.toml').read_text(encoding='utf-8')
    declared = re.search(r'"redis>=([\d.]+)"', pyproject)
    assert declared, 'the redis requirement is no longer spelled the way this test reads it'

    wanted = [re.search(r'>=([\d.]+)', line) for line in requires('aiogram') or [] if line.startswith('redis')]
    asked = [match.group(1) for match in wanted if match]
    assert asked, 'aiogram no longer declares a redis lower bound'

    def parts(version):
        # padded: '6.2' and '6.2.0' are the same floor, and a short tuple sorts low
        numbers = [int(number) for number in version.split('.')]
        return tuple(numbers + [0] * (3 - len(numbers)))

    assert parts(declared.group(1)) >= max(parts(version) for version in asked), (
        f'declared redis>={declared.group(1)}, aiogram asks for >={max(asked)}'
    )


#: the module-level strings 2.0 shipped and 3.0 removed, by the module that held
#: them. Restoring one would quietly undo a documented breaking change and give
#: the package two spellings of every constant again
REMOVED_ALIASES = {
    'django_redis_aiogram.delivery': ('BLPOP_DELIVERY', 'KEYSPACE_DELIVERY'),
    'django_redis_aiogram.client': ('MEMORY_STORAGE', 'REDIS_STORAGE'),
    'django_redis_aiogram.throttling': ('OVERALL_PER_SECOND', 'PER_CHAT_PER_SECOND', 'GROUP_PER_MINUTE'),
    'django_redis_aiogram.webhook': ('POLLING', 'WEBHOOK'),
    'django_redis_aiogram.serializers': (
        'JSON_SERIALIZER',
        'PICKLE_SERIALIZER',
        'TAG_MODEL',
        'TAG_DEFAULT',
        'TAG_DATETIME',
        'TAG_DATE',
        'TAG_DECIMAL',
        'TAG_BYTES',
        'TAG_INPUT_FILE',
    ),
}


@pytest.mark.parametrize(
    ('module_name', 'alias'),
    [(module_name, alias) for module_name, aliases in REMOVED_ALIASES.items() for alias in aliases],
)
def test_the_2_0_string_alias_stays_gone(module_name, alias):
    """Import the enum member instead; the values are unchanged."""
    import importlib

    module = importlib.import_module(module_name)

    assert not hasattr(module, alias), f'{module_name}.{alias} is back'
    assert alias not in getattr(module, '__all__', ()), f'{alias} is back in {module_name}.__all__'
