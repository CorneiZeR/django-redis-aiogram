"""The package must be importable without a token or a reachable Redis.

Before 2.0 both were required at import time, which took the whole Django
project down — including its test suite — whenever they were absent.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_redis_aiogram import TelegramBot, bot, conf, redis_conn
from django_redis_aiogram.settings import Settings, parse_bool


def test_package_exposes_public_api():
    assert isinstance(bot, TelegramBot)
    assert bot.router is not None


def test_bot_name_is_not_shadowed_by_a_module():
    """The singleton is exported as `bot`, so the class must not live in bot.py.

    Otherwise `django_redis_aiogram.bot` resolves to the module or the instance
    depending on import order.
    """
    import django_redis_aiogram
    import django_redis_aiogram.client

    assert isinstance(django_redis_aiogram.bot, TelegramBot)
    assert django_redis_aiogram.client.TelegramBot is TelegramBot


def test_building_a_bot_is_cheap():
    instance = TelegramBot()
    assert instance._bot is None
    assert instance._dispatcher is None
    assert instance._loop is None


def test_bot_requires_token_only_when_used():
    with pytest.raises(ImproperlyConfigured, match="TOKEN"):
        _ = TelegramBot().bot


def test_redis_requires_url_only_when_used():
    with pytest.raises(ImproperlyConfigured, match="REDIS_URL"):
        redis_conn.ping()


def test_handlers_register_without_a_token():
    instance = TelegramBot()

    @instance.message()
    async def handler(message):  # pragma: no cover - never dispatched
        ...

    assert len(instance.router.observers["message"].handlers) == 1


def test_defaults_are_readable():
    assert conf["ENABLED"] is True
    assert conf["MAX_RETRIES"] == 10
    assert conf["DELIVERY"] == "blpop"


@override_settings(TELEGRAM_BOT={"MAX_RETRIES": 3})
def test_override_settings_is_picked_up():
    assert conf["MAX_RETRIES"] == 3


def test_settings_win_over_environment(monkeypatch):
    monkeypatch.setenv("DJANGO_REDIS_AIOGRAM_MAX_RETRIES", "7")
    with override_settings(TELEGRAM_BOT={"MAX_RETRIES": 3}):
        assert conf["MAX_RETRIES"] == 3


def test_environment_fills_unset_keys(monkeypatch):
    monkeypatch.setenv("DJANGO_REDIS_AIOGRAM_MAX_RETRIES", "7")
    monkeypatch.setenv("DJANGO_REDIS_AIOGRAM_TOKEN", "42:from-env")
    with override_settings(TELEGRAM_BOT={}):
        assert conf["MAX_RETRIES"] == 7
        assert conf["TOKEN"] == "42:from-env"


def test_environment_ignores_non_scalar_settings(monkeypatch):
    monkeypatch.setenv("DJANGO_REDIS_AIOGRAM_DEFAULT_KWARGS", "nonsense")
    with override_settings(TELEGRAM_BOT={}):
        assert callable(conf["DEFAULT_KWARGS"])


def test_unknown_settings_are_preserved():
    with override_settings(TELEGRAM_BOT={"CUSTOM": "kept"}):
        assert conf["CUSTOM"] == "kept"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_parse_bool_accepts_truthy(raw):
    assert parse_bool(raw, "X") is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "off"])
def test_parse_bool_accepts_falsy(raw):
    assert parse_bool(raw, "X") is False


def test_parse_bool_rejects_ambiguous():
    with pytest.raises(ImproperlyConfigured, match="must be one of"):
        parse_bool("maybe", "X")


def test_invalid_integer_in_environment_is_reported(monkeypatch):
    monkeypatch.setenv("DJANGO_REDIS_AIOGRAM_MAX_RETRIES", "ten")
    with override_settings(TELEGRAM_BOT={}), pytest.raises(ImproperlyConfigured, match="integer"):
        _ = conf["MAX_RETRIES"]


def test_settings_mapping_protocol():
    settings = Settings()
    assert "TOKEN" in settings
    assert settings.get("missing", "fallback") == "fallback"
    assert len(settings) == len(dict(settings))
