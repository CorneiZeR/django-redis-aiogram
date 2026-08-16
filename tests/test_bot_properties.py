"""Bot-wide defaults and FSM storage.

1.x had no way to reach aiogram's DefaultBotProperties, so projects injected
parse_mode into every single call through DEFAULT_KWARGS. And the dispatcher
was built without a storage, meaning FSM state lived in memory and was lost on
every restart even though Redis was already configured.
"""

import pytest
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.checks import check_settings
from django_redis_aiogram.client import build_default_properties, build_storage


@override_settings(TELEGRAM_BOT={'DEFAULT_BOT_PROPERTIES': {'parse_mode': 'HTML'}})
def test_default_properties_are_built():
    properties = build_default_properties()
    assert isinstance(properties, DefaultBotProperties)
    assert properties.parse_mode == 'HTML'


@override_settings(TELEGRAM_BOT={})
def test_default_properties_default_to_empty():
    assert build_default_properties().parse_mode is None


@override_settings(TELEGRAM_BOT={'DEFAULT_BOT_PROPERTIES': {'parse_moed': 'HTML'}})
def test_unknown_property_is_reported_clearly():
    with pytest.raises(ImproperlyConfigured, match='DEFAULT_BOT_PROPERTIES'):
        build_default_properties()


@override_settings(TELEGRAM_BOT={'TOKEN': '42:test', 'DEFAULT_BOT_PROPERTIES': {'parse_mode': 'MarkdownV2'}})
def test_bot_receives_the_properties():
    instance = TelegramBot()
    assert instance.bot.default.parse_mode == 'MarkdownV2'


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'memory'})
def test_memory_storage():
    assert isinstance(build_storage(), MemoryStorage)


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/2'})
def test_redis_storage_is_the_default_choice():
    """No FSM_STORAGE here on purpose: this must fail if the default changes."""
    assert isinstance(build_storage(), RedisStorage)


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'redis', 'REDIS_URL': ''})
def test_redis_storage_without_a_url_is_reported():
    with pytest.raises(ImproperlyConfigured, match='REDIS_URL'):
        build_storage()


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'aiogram.fsm.storage.memory.MemoryStorage'})
def test_dotted_path_storage():
    assert isinstance(build_storage(), MemoryStorage)


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'django_redis_aiogram.client.TelegramBot'})
def test_dotted_path_must_be_a_storage():
    with pytest.raises(ImproperlyConfigured, match='BaseStorage'):
        build_storage()


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'memory', 'TOKEN': '42:test'})
def test_dispatcher_uses_the_configured_storage():
    assert isinstance(TelegramBot().dispatcher.storage, MemoryStorage)


@override_settings(
    TELEGRAM_BOT={
        'DEFAULT_BOT_PROPERTIES': {'parse_moed': 'HTML'},
        'TOKEN': '42:x',
        'REDIS_URL': 'r://x',
    }
)
def test_check_catches_a_misspelled_property():
    assert 'django_redis_aiogram.E018' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'nonsense', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_check_catches_a_bad_storage_name():
    assert 'django_redis_aiogram.E019' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'redis', 'REDIS_URL': '   '})
def test_whitespace_only_redis_url_is_reported_as_missing():
    """Otherwise it fails later inside RedisStorage with a vaguer message."""
    with pytest.raises(ImproperlyConfigured, match='REDIS_URL'):
        build_storage()


@override_settings(TELEGRAM_BOT={'DEFAULT_BOT_PROPERTIES': {1: 'HTML'}, 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_non_string_property_key_does_not_crash_the_check():
    """manage.py check must report the problem, not raise TypeError from join."""
    assert 'django_redis_aiogram.E018' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'does.not.Exist', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_check_catches_a_dotted_path_that_does_not_import():
    """It used to pass the check and then raise ModuleNotFoundError at runtime."""
    assert 'django_redis_aiogram.E019' in {message.id for message in check_settings()}


@override_settings(
    TELEGRAM_BOT={
        'FSM_STORAGE': 'django_redis_aiogram.client.TelegramBot',
        'TOKEN': '42:x',
        'REDIS_URL': 'r://x',
    }
)
def test_check_catches_a_dotted_path_that_is_not_a_storage():
    assert 'django_redis_aiogram.E019' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'FSM_STORAGE': 'does.not.Exist'})
def test_unimportable_storage_is_reported_as_configuration():
    """A raw ModuleNotFoundError does not tell the operator what to fix."""
    with pytest.raises(ImproperlyConfigured, match='cannot be imported'):
        build_storage()


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'REDIS_URL': 'redis://localhost:6379/0',
        'FSM_STORAGE': 'aiogram.fsm.storage.memory.MemoryStorage',
        'DEFAULT_BOT_PROPERTIES': {'parse_mode': 'HTML', 'protect_content': True},
    }
)
def test_a_valid_configuration_reports_neither_e018_nor_e019():
    """The tests above prove detection; this one guards against false positives."""
    reported = {message.id for message in check_settings()}
    assert 'django_redis_aiogram.E018' not in reported
    assert 'django_redis_aiogram.E019' not in reported


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/2', 'REDIS_TIMEOUT': 7})
def test_the_fsm_storage_is_bounded_by_the_same_deadline(monkeypatch):
    """Every update reads FSM state, so an unbounded client here wedges the bot.

    aiogram builds its own client and this package never touches it again, so the
    deadlines have to arrive at construction or never.
    """
    seen = {}
    # aiogram's storage builds an asyncio pool, which is a different class from the
    # one the shared client uses; patching the sync one would watch nothing
    from aiogram.fsm.storage import redis as storage_module

    original = storage_module.ConnectionPool.from_url

    def record(url, **kwargs):
        seen.update(kwargs)
        return original(url, **kwargs)

    monkeypatch.setattr(storage_module.ConnectionPool, 'from_url', staticmethod(record))
    storage = build_storage()

    assert isinstance(storage, RedisStorage)
    assert seen['socket_timeout'] == 7
    assert seen['socket_connect_timeout'] == 7
