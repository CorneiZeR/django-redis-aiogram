"""The old `telegram_bot` package name keeps working, with a warning."""

import importlib
import subprocess
import sys
import textwrap
import warnings

import pytest

import django_redis_aiogram

SHIM_MODULES = [
    'telegram_bot.telegram_bot',
    'telegram_bot.settings',
    'telegram_bot.redis',
    'telegram_bot.defaults',
    'telegram_bot.checks',
    'telegram_bot.signals',
    'telegram_bot.apps',
    'telegram_bot.management.commands.start_tgbot',
]


def test_importing_the_shim_warns():
    stale = [
        name
        for name in list(sys.modules)
        if name == 'telegram_bot' or name.startswith('telegram_bot.')
    ]
    for name in stale:
        del sys.modules[name]
    with pytest.warns(DeprecationWarning, match='django_redis_aiogram'):
        importlib.import_module('telegram_bot')


def test_the_singleton_is_shared():
    """A second bot instance would silently own a different router."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        import telegram_bot

    assert telegram_bot.bot is django_redis_aiogram.bot
    assert telegram_bot.conf is django_redis_aiogram.conf


@pytest.mark.parametrize('name', SHIM_MODULES)
def test_legacy_module_paths_resolve(name):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        module = importlib.import_module(name)
    assert module is not None


def test_legacy_class_import():
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        from telegram_bot.telegram_bot import TelegramBot

    assert TelegramBot is django_redis_aiogram.TelegramBot


def test_legacy_command_import():
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        from telegram_bot.management.commands.start_tgbot import Command

    from django_redis_aiogram.management.commands.start_tgbot import Command as Original

    assert Command is Original


def test_installed_apps_entry_still_boots():
    """The whole point of the shim: an untouched settings.py keeps working.

    Runs in a subprocess because Django can only be configured once per process.
    """
    script = textwrap.dedent("""
        import django
        from django.conf import settings

        settings.configure(
            INSTALLED_APPS=['telegram_bot'],
            DATABASES={},
            USE_TZ=True,
            TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost:6379/0'},
        )
        django.setup()

        from django.apps import apps
        from django.core.management import get_commands

        assert apps.is_installed('telegram_bot')
        assert get_commands()['start_tgbot'] == 'telegram_bot'

        # Django falls back to the plain AppConfig when apps.py is ambiguous,
        # and its ready() does nothing: no checks, no autodiscover.
        config = apps.get_app_config('telegram_bot')
        assert type(config).__module__ == 'telegram_bot.apps', type(config)

        from django.core.checks import registry
        registered = [getattr(c, '__name__', '') for c in registry.registry.get_checks()]
        assert 'check_settings' in registered, registered
        print('ok')
    """)
    result = subprocess.run(
        [sys.executable, '-c', script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert 'ok' in result.stdout


def test_legacy_app_config_targets_the_old_label():
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        from telegram_bot.apps import TelegramBotAppConfig

    from django_redis_aiogram.apps import TelegramBotAppConfig as Base

    assert issubclass(TelegramBotAppConfig, Base)
    assert TelegramBotAppConfig.name == 'telegram_bot'
