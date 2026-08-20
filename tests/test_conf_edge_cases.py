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


@pytest.mark.parametrize('raw', ['nan', 'inf', '-inf', 'Infinity', 'NaN'])
def test_a_number_the_environment_cannot_bound_a_wait_with_is_refused(monkeypatch, raw):
    """`float()` accepts these, and every setting read through this branch is a deadline.

    `nan` compares false against everything, so a wait bounded by it never expires and a
    graceful stop hangs where it promised five seconds; `sleep(nan)` raises instead, from
    inside a thread nobody is watching. `E044` reports the value, but only where
    `manage.py check` runs — and the environment reaches every process, the ones that
    never run checks included, which is the case this branch exists for.
    """
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_DRAIN_TIMEOUT', raw)
    conf.reset()

    with pytest.raises(ImproperlyConfigured, match='must be a finite number'):
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


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan')])
def test_a_writer_dial_that_cannot_be_read_falls_back_instead_of_ending_the_thread(value):
    """`_number` runs on the writer thread, in a loop, past the net `_flush` provides.

    `int(float('inf'))` raises `OverflowError`, which is not a cast error the reader was
    catching — so a settings dict holding `inf` ended the writer and took the buffer with
    it. The environment cannot deliver these; it refuses them at resolution. A settings
    dict can, and `E038` — the check that owns this setting — only reports it where
    `manage.py check` runs.
    """
    from django_redis_aiogram.defaults import DEFAULTS
    from django_redis_aiogram.recorder import EventRecorder

    with override_settings(TELEGRAM_BOT={'EVENT_LOG_FLUSH_INTERVAL': value}):
        assert EventRecorder.flush_interval() == DEFAULTS['EVENT_LOG_FLUSH_INTERVAL']


def test_the_writer_waits_the_interval_its_reader_returns():
    """The accessor above is only worth having if `_collect` is what calls it.

    Asserting `flush_interval()` alone leaves the call site free: a `_collect` that went
    back to reading the setting as a float would restore the fractional wait `E038`
    refuses and keep that test green. So this one measures the wait itself, through a
    buffer that records the timeout it is asked for.
    """
    import queue

    from django_redis_aiogram.recorder import EventRecorder

    class RecordingBuffer:
        """Answers `_collect` the way an empty queue does, and remembers the deadline."""

        def __init__(self):
            self.timeouts = []

        def get(self, timeout=None):
            self.timeouts.append(timeout)
            raise queue.Empty

    buffer = RecordingBuffer()
    with override_settings(TELEGRAM_BOT={'EVENT_LOG_FLUSH_INTERVAL': 0.5}):
        batch, wakes = EventRecorder()._collect(buffer)  # type: ignore[arg-type]  # a stand-in for the queue

    assert (batch, wakes) == ([], [])
    assert buffer.timeouts, 'the writer never waited on the queue'
    assert buffer.timeouts[0] == pytest.approx(1, abs=0.05), 'the fractional interval reached the wait'


@pytest.mark.parametrize('key', ['RATE_LIMIT', 'EVENT_LOG_KINDS', 'DEFAULT_KWARGS'])
def test_an_environment_variable_for_a_settings_only_key_says_it_is_ignored(monkeypatch, caplog, key):
    """Silence was the worst of the three possible answers.

    A container or a callable has no textual form, so the variable cannot be honoured —
    but the Settings page promises an environment twin for every scalar, and an operator
    throttling the bot with `DJANGO_REDIS_AIOGRAM_RATE_LIMIT` got the default rate and no
    word about it. Honoured, refused or reported: this is the third.
    """
    monkeypatch.setenv(f'DJANGO_REDIS_AIOGRAM_{key}', 'anything')
    conf.reset()

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        _ = conf[key]

    assert 'ignoring an environment variable' in caplog.text
    # in `extra`, not interpolated into the message: the rule this package logs by, and
    # the reason the first version of this assertion looked for it in the text and failed
    reported = [record for record in caplog.records if getattr(record, 'tg_setting', None) == key]
    assert reported, f'the warning did not name {key}'
    assert reported[0].tg_variable == f'DJANGO_REDIS_AIOGRAM_{key}'
