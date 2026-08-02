"""Autodiscover has to actually import routers and register their handlers.

`tests.fake_app` is in INSTALLED_APPS and defines handlers in tg_router.py, so
a plain `django.setup()` is enough to exercise the whole path.
"""

import sys

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import module_loading

from django_redis_aiogram import bot
from django_redis_aiogram.routers import autodiscover_tg_routers


def test_router_module_was_imported():
    assert 'tests.fake_app.tg_router' in sys.modules


def test_handlers_landed_on_the_shared_bot():
    """They must register on the exported singleton, not some other instance.

    Read the module out of sys.modules rather than importing it: importing it
    here would satisfy test_router_module_was_imported even if autodiscover
    during django.setup() never ran.
    """
    module = sys.modules['tests.fake_app.tg_router']

    assert module.bot is bot
    assert len(bot.router.observers['message'].handlers) >= 1
    assert len(bot.router.observers['callback_query'].handlers) >= 1


def test_a_missing_router_module_is_not_an_error():
    autodiscover_tg_routers()


@override_settings(TELEGRAM_BOT={'MODULE_NAME': 'definitely_not_here'})
def test_unknown_module_name_finds_nothing(monkeypatch):
    """Watch the import seam, so this cannot pass by never looking at all."""
    requested = []
    real_import = module_loading.import_module

    def spy(name, *args, **kwargs):
        requested.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(module_loading, 'import_module', spy)
    before = {name: len(observer.handlers) for name, observer in bot.router.observers.items()}

    autodiscover_tg_routers()

    assert 'tests.fake_app.definitely_not_here' in requested, requested
    assert not [name for name in sys.modules if name.endswith('.definitely_not_here')]
    after = {name: len(observer.handlers) for name, observer in bot.router.observers.items()}
    assert after == before


@override_settings(TELEGRAM_BOT={'MODULE_NAME': 'broken_router'})
def test_a_broken_router_surfaces_instead_of_being_swallowed():
    """1.x caught bare ImportError, so a typo inside a router silently
    disabled the whole file."""
    with pytest.raises(ImproperlyConfigured, match='intentionally broken'):
        autodiscover_tg_routers()
