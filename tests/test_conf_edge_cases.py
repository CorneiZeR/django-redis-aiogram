"""Misconfiguration must produce a clear error, not an obscure crash."""

import importlib
import types

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.test import override_settings

from django_redis_aiogram import conf as conf_object
from django_redis_aiogram import settings as settings_module
from django_redis_aiogram.checks import check_settings
from django_redis_aiogram.defaults import no_default_kwargs
from django_redis_aiogram.settings import Settings, conf


@override_settings(TELEGRAM_BOT=['not', 'a', 'mapping'])
def test_non_mapping_settings_are_reported_clearly():
    with pytest.raises(ImproperlyConfigured, match='must be a mapping'):
        _ = conf['TOKEN']


@override_settings(TELEGRAM_BOT='TOKEN=abc')
def test_string_settings_are_reported_clearly():
    with pytest.raises(ImproperlyConfigured, match='must be a mapping'):
        _ = conf['TOKEN']


@pytest.mark.parametrize('value', [[], (), '', 0])
def test_an_empty_non_mapping_is_reported_too(value):
    """The two tests above pick non-empty values, which is how this went unnoticed.

    `_resolve` used to fold every falsy setting into `{}` before the mapping check, so
    `TELEGRAM_BOT = []` reached none of it: the token and every other value came from the
    environment or the defaults, silently, and a project that had configured the bot ran
    as though it had not. An empty mistaken assignment is the likelier one, and it was the
    only shape that got through.
    """
    with override_settings(TELEGRAM_BOT=value), pytest.raises(ImproperlyConfigured, match='must be a mapping'):
        _ = conf['TOKEN']


@pytest.mark.parametrize('value', [None, {}])
def test_an_absent_setting_is_still_absent_rather_than_wrong(value):
    """The other side of that fix: not configured is not the same as misconfigured.

    `TELEGRAM_BOT = None` and an unset one both mean *take everything from the environment
    and the defaults*, which is what the lazy-boot tests and `tests/bare_settings.py` rely
    on. An empty dict is a mapping and means the same.
    """
    with override_settings(TELEGRAM_BOT=value):
        assert conf['TOKEN'] == ''


@pytest.mark.parametrize(
    ('module', 'uid'),
    [
        ('django_redis_aiogram.settings', 'django_redis_aiogram.settings'),
        ('django_redis_aiogram.redis', 'django_redis_aiogram.redis'),
        ('django_redis_aiogram.throttling', 'django_redis_aiogram.throttling'),
    ],
)
def test_reset_receiver_is_deduplicated(module, uid):
    """Without dispatch_uid, autoreload stacks a fresh receiver every import."""
    receiver = importlib.import_module(module)._reset_on_setting_change
    before = len(setting_changed.receivers)
    setting_changed.connect(receiver, dispatch_uid=uid)
    assert len(setting_changed.receivers) == before


def test_settings_module_is_not_shadowed_by_the_conf_object():
    """`conf` is exported by the package, so the module cannot be named conf."""
    assert isinstance(settings_module, types.ModuleType)
    assert isinstance(conf_object, Settings)
    assert settings_module.conf is conf_object


def test_settings_survive_an_empty_override():
    with override_settings(TELEGRAM_BOT=None):
        assert isinstance(Settings()['MAX_RETRIES'], int)


def test_the_default_kwargs_protocol_is_positional():
    """DEFAULT_KWARGS callables are invoked with the function name positionally;
    the shipped default declares exactly that contract and returns nothing."""
    assert no_default_kwargs('send_message') == {}

    with pytest.raises(TypeError):
        # by its own name too: `/` is what makes the contract positional-only
        no_default_kwargs(_function='send_message')  # type: ignore[call-arg]


@pytest.mark.parametrize(('raw', 'expected'), [('0.5', 0.5), ('5', 5.0), ('30', 30.0)])
def test_a_number_setting_reads_a_number_from_the_environment(monkeypatch, raw, expected):
    """`DRAIN_TIMEOUT: 0.5` was valid in settings and fatal from the environment.

    `_from_env` coerced on the default's type and knew only bool, int and str, so a
    fractional value met the integer branch and raised out of `apps.ready()` — which
    stops *every* `manage.py` command, not just the bot. `E044` accepts any finite number,
    `close()` reads one, and the Settings page promises an environment twin for every
    scalar; the environment was the only one of the three that refused.
    """
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_DRAIN_TIMEOUT', raw)
    conf.reset()

    assert conf['DRAIN_TIMEOUT'] == expected


def test_a_number_setting_the_environment_cannot_read_is_refused(monkeypatch):
    """The other direction: silently ignoring it would be worse than either behaviour."""
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_DRAIN_TIMEOUT', 'soon')
    conf.reset()

    with pytest.raises(ImproperlyConfigured, match='must be a number'):
        _ = conf['DRAIN_TIMEOUT']


def test_the_flush_interval_is_read_the_way_its_check_demands():
    """`E038` refuses a fraction and the writer used to honour one.

    Two rules for one setting is how a value passes `manage.py check` and then behaves in
    a way the check said was impossible. Asserted at the writer's own reader, not at the
    helper below it — the first version of this test asked `_number` directly and passed
    with the float read still in place.
    """
    from django_redis_aiogram.recorder import EventRecorder

    with override_settings(TELEGRAM_BOT={'EVENT_LOG_FLUSH_INTERVAL': 0.5}):
        assert 'django_redis_aiogram.E038' in {str(m.id) for m in check_settings()}
        assert EventRecorder.flush_interval() == 1, 'the writer honoured an interval the check refuses'

    with override_settings(TELEGRAM_BOT={'EVENT_LOG_FLUSH_INTERVAL': 3}):
        assert EventRecorder.flush_interval() == 3
