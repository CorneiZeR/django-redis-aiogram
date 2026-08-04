"""ENABLED lets a process opt out of the bot entirely.

Most processes in a deployment — web, celery, beat — never talk to Telegram,
but they all load the app. Disabling them must cost nothing and must not
require credentials.
"""

from io import StringIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.apps import TelegramBotAppConfig


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_send_raw_is_a_noop_when_disabled():
    instance = TelegramBot()
    instance.send_raw(chat_id=1, text='hi')
    assert instance._bot is None
    assert instance._loop is None


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_send_redis_is_a_noop_when_disabled():
    instance = TelegramBot()
    # would raise ImproperlyConfigured if it tried to reach Redis
    instance.send_redis(chat_id=1, text='hi')


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_enabled_property_reflects_settings():
    assert TelegramBot().enabled is False


@override_settings(TELEGRAM_BOT={})
def test_enabled_defaults_to_true():
    assert TelegramBot().enabled is True


def test_env_can_disable_the_bot(monkeypatch):
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_ENABLED', 'false')
    with override_settings(TELEGRAM_BOT={}):
        assert TelegramBot().enabled is False


def test_settings_beat_env_for_enabled(monkeypatch):
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_ENABLED', 'false')
    with override_settings(TELEGRAM_BOT={'ENABLED': True}):
        assert TelegramBot().enabled is True


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_command_refuses_to_start_when_disabled():
    out = StringIO()
    call_command('start_tgbot', stdout=out)
    assert 'disabled' in out.getvalue()


@override_settings(TELEGRAM_BOT={'ENABLED': False, 'AUTODISCOVER': True})
def test_ready_skips_autodiscover_when_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(
        'django_redis_aiogram.routers.autodiscover_tg_routers',
        lambda: called.append(True),
    )
    config = TelegramBotAppConfig('django_redis_aiogram', __import__('django_redis_aiogram'))
    config.ready()
    assert called == []


@override_settings(TELEGRAM_BOT={'ENABLED': True, 'AUTODISCOVER': False})
def test_autodiscover_can_be_disabled_on_its_own(monkeypatch):
    called = []
    monkeypatch.setattr(
        'django_redis_aiogram.routers.autodiscover_tg_routers',
        lambda: called.append(True),
    )
    config = TelegramBotAppConfig('django_redis_aiogram', __import__('django_redis_aiogram'))
    config.ready()
    assert called == []


@override_settings(TELEGRAM_BOT={'ENABLED': True, 'AUTODISCOVER': True})
def test_ready_runs_autodiscover_when_enabled(monkeypatch):
    """Guards the two tests above against being vacuously green."""
    called = []
    monkeypatch.setattr(
        'django_redis_aiogram.routers.autodiscover_tg_routers',
        lambda: called.append(True),
    )
    config = TelegramBotAppConfig('django_redis_aiogram', __import__('django_redis_aiogram'))
    config.ready()
    assert called == [True]


@pytest.mark.parametrize(('raw', 'expected'), [('1', True), ('0', False), ('on', True), ('off', False)])
def test_env_boolean_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_ENABLED', raw)
    with override_settings(TELEGRAM_BOT={}):
        assert TelegramBot().enabled is expected


@override_settings(TELEGRAM_BOT={'ENABLED': 'false'})
def test_the_string_false_disables_the_bot():
    """bool('false') is True, which would silently enable a bot meant to be off."""
    assert TelegramBot().enabled is False


@override_settings(TELEGRAM_BOT={'ENABLED': 'no'})
def test_other_falsy_spellings_in_settings():
    assert TelegramBot().enabled is False


@override_settings(TELEGRAM_BOT={'ENABLED': 1})
def test_integers_are_accepted():
    assert TelegramBot().enabled is True


@override_settings(TELEGRAM_BOT={'ENABLED': 'perhaps'})
def test_an_unparseable_value_is_reported():
    with pytest.raises(ImproperlyConfigured, match='must be one of'):
        _ = TelegramBot().enabled


@override_settings(TELEGRAM_BOT={'ENABLED': []})
def test_a_nonsense_type_is_reported():
    with pytest.raises(ImproperlyConfigured, match='must be a boolean'):
        _ = TelegramBot().enabled


@override_settings(TELEGRAM_BOT={'ENABLED': 'false'})
def test_command_refuses_to_start_for_the_string_false():
    out = StringIO()
    call_command('start_tgbot', stdout=out)
    assert 'disabled' in out.getvalue()


@override_settings(TELEGRAM_BOT={'ENABLED': 'false', 'AUTODISCOVER': True})
def test_ready_agrees_with_the_bot_about_the_string_false(monkeypatch):
    """ready() used to read the raw value, so 'false' started the app anyway."""
    called = []
    monkeypatch.setattr(
        'django_redis_aiogram.routers.autodiscover_tg_routers',
        lambda: called.append(True),
    )
    config = TelegramBotAppConfig('django_redis_aiogram', __import__('django_redis_aiogram'))
    config.ready()

    assert called == []
    assert TelegramBot().enabled is False


@override_settings(TELEGRAM_BOT={'ENABLED': True, 'AUTODISCOVER': 'off'})
def test_autodiscover_accepts_the_same_spellings(monkeypatch):
    called = []
    monkeypatch.setattr(
        'django_redis_aiogram.routers.autodiscover_tg_routers',
        lambda: called.append(True),
    )
    config = TelegramBotAppConfig('django_redis_aiogram', __import__('django_redis_aiogram'))
    config.ready()

    assert called == []
