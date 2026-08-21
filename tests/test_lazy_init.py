"""The package must be importable without a token or a reachable Redis.

Before 2.0 both were required at import time, which took the whole Django
project down — including its test suite — whenever they were absent.
"""

import os
import re
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

import django_redis_aiogram
from django_redis_aiogram import TelegramBot, bot, conf, redis_conn
from django_redis_aiogram.settings import Settings, parse_bool

#: seconds a nested interpreter gets before the test fails instead of hanging
SUBPROCESS_TIMEOUT = 120


def test_the_suite_still_boots_without_a_database():
    """The invariant tests/settings.py exists to hold.

    A migration container installs the app and runs with whatever it has; the
    package must never make a database a condition of importing it. The
    database-backed tests live in tests/db under their own settings, so this
    module has to keep proving the empty case.

    The engine is what gets asked, not whether DATABASES is empty: Django fills
    an empty setting in with the dummy backend the first time anything touches
    connections, so the dict stops being empty as soon as something looks.
    """
    from django.db import DEFAULT_DB_ALIAS, connections

    engine = connections[DEFAULT_DB_ALIAS].settings_dict.get('ENGINE')
    assert engine == 'django.db.backends.dummy', f'this suite must have no usable database, got {engine!r}'


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
    with pytest.raises(ImproperlyConfigured, match='TOKEN'):
        _ = TelegramBot().bot


def test_redis_requires_url_only_when_used():
    with pytest.raises(ImproperlyConfigured, match='REDIS_URL'):
        redis_conn.ping()


def test_handlers_register_without_a_token():
    instance = TelegramBot()

    @instance.message()
    async def handler(message):  # pragma: no cover - never dispatched
        ...

    assert len(instance.router.observers['message'].handlers) == 1


def test_defaults_are_readable():
    assert conf['ENABLED'] is True
    assert conf['MAX_RETRIES'] == 10
    assert conf['DELIVERY'] == 'blpop'


@override_settings(TELEGRAM_BOT={'MAX_RETRIES': 3})
def test_override_settings_is_picked_up():
    assert conf['MAX_RETRIES'] == 3


def test_settings_win_over_environment(monkeypatch):
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_MAX_RETRIES', '7')
    with override_settings(TELEGRAM_BOT={'MAX_RETRIES': 3}):
        assert conf['MAX_RETRIES'] == 3


def test_environment_fills_unset_keys(monkeypatch):
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_MAX_RETRIES', '7')
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_TOKEN', '42:from-env')
    with override_settings(TELEGRAM_BOT={}):
        assert conf['MAX_RETRIES'] == 7
        assert conf['TOKEN'] == '42:from-env'


def test_environment_ignores_non_scalar_settings(monkeypatch):
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_DEFAULT_KWARGS', 'nonsense')
    with override_settings(TELEGRAM_BOT={}):
        assert callable(conf['DEFAULT_KWARGS'])


def test_unknown_settings_are_preserved():
    with override_settings(TELEGRAM_BOT={'CUSTOM': 'kept'}):
        assert conf['CUSTOM'] == 'kept'


@pytest.mark.parametrize('raw', ['1', 'true', 'TRUE', ' yes ', 'on'])
def test_parse_bool_accepts_truthy(raw):
    assert parse_bool(raw, 'X') is True


@pytest.mark.parametrize('raw', ['0', 'false', 'No', 'off'])
def test_parse_bool_accepts_falsy(raw):
    assert parse_bool(raw, 'X') is False


def test_parse_bool_rejects_ambiguous():
    with pytest.raises(ImproperlyConfigured, match='must be one of'):
        parse_bool('maybe', 'X')


def test_invalid_integer_in_environment_is_reported(monkeypatch):
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_MAX_RETRIES', 'ten')
    with override_settings(TELEGRAM_BOT={}), pytest.raises(ImproperlyConfigured, match='integer'):
        _ = conf['MAX_RETRIES']


def test_settings_mapping_protocol():
    settings = Settings()
    assert 'TOKEN' in settings
    assert settings.get('missing', 'fallback') == 'fallback'
    assert len(settings) == len(dict(settings))


def test_importing_the_package_pulls_nothing_third_party():
    """aiogram costs most of a second; only using the bot may pay it.

    A delta rather than an absolute set: a `.pth` file in site-packages can import
    anything it likes before this script runs, so what was already loaded says nothing
    about what the package is responsible for.

    The stdlib is subtracted from that delta, which is right for third parties and
    blind to two modules that mattered: `typing` and `threading` were 1.270 ms of the
    1.420 ms this import used to cost, and re-adding either would have passed here
    unnoticed. They are named explicitly below, out of the raw delta.
    """
    script = textwrap.dedent("""
        import sys

        before = set(sys.modules)
        import django_redis_aiogram
        delta = set(sys.modules) - before
        pulled = {name.split('.')[0] for name in delta}
        pulled -= sys.stdlib_module_names | {'django_redis_aiogram'}

        assert not pulled, f'importing the package pulled {sorted(pulled)}'
        # out of the raw delta, so the stdlib subtraction above cannot hide them
        assert 'typing' not in delta, 'the package imported typing again'
        assert 'threading' not in delta, 'the package imported threading again'
        assert django_redis_aiogram.__version__

        _ = django_redis_aiogram.bot
        assert 'aiogram' in sys.modules, 'using the bot did not resolve it'
        assert django_redis_aiogram.bot is _, 'a second access built a second bot'
        print('lazy ok')
    """)
    result = subprocess.run(  # noqa: S603 - our own interpreter, and a script written right above
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
        env={**os.environ, 'DJANGO_SETTINGS_MODULE': 'tests.settings'},
    )
    assert result.returncode == 0, result.stderr
    assert 'lazy ok' in result.stdout


def test_a_disabled_django_boot_never_pays_for_aiogram():
    """The migration container's whole point: INSTALLED_APPS, no bot, no cost."""
    script = textwrap.dedent("""
        import sys

        import django

        django.setup()

        FORBIDDEN = {'aiogram', 'pydantic'}
        loaded = {name.split('.')[0] for name in sys.modules} & FORBIDDEN
        assert not loaded, f'a disabled boot imported {sorted(loaded)}'
        print('cheap boot ok')
    """)
    result = subprocess.run(  # noqa: S603 - our own interpreter, and a script written right above
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
        env={
            **os.environ,
            'DJANGO_SETTINGS_MODULE': 'tests.settings',
            'DJANGO_REDIS_AIOGRAM_ENABLED': '0',
        },
    )
    assert result.returncode == 0, result.stderr
    assert 'cheap boot ok' in result.stdout


def test_running_the_system_checks_does_not_import_aiogram():
    """`manage.py check` runs inside every migrate, runserver and shell.

    Under tests.bare_settings, because tests/settings.py installs an app whose router
    imports aiogram at django.setup() and would answer this question for the checks.

    The absence is asserted only after something proves the checks ran. On its own this
    is a test that passes when nothing happens: replacing `register(check_settings)` with
    `_ = check_settings` makes `check` print `no issues`, import no aiogram because it
    imports nothing, and satisfy every line below. So the subprocess first drives a
    configuration one of our rules must refuse, and requires our id in the failure.
    """
    script = textwrap.dedent("""
        import sys

        import django

        django.setup()
        from django.core.management import call_command
        from django.core.management.base import SystemCheckError
        from django.test import override_settings

        call_command('check')

        # the positive control: our rules reached this run through Django's registry
        try:
            with override_settings(TELEGRAM_BOT={'TOKEN': 42}):
                call_command('check')
        except SystemCheckError as refused:
            assert 'django_redis_aiogram.E004' in str(refused), f'someone else refused it: {refused}'
        else:
            raise AssertionError('manage.py check accepted a TOKEN of the wrong type')

        FORBIDDEN = {'aiogram', 'pydantic'}
        loaded = {name.split('.')[0] for name in sys.modules} & FORBIDDEN
        assert not loaded, f'the system checks imported {sorted(loaded)}'
        print('cheap checks ok')
    """)
    result = subprocess.run(  # noqa: S603 - our own interpreter, and a script written right above
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
        env={**os.environ, 'DJANGO_SETTINGS_MODULE': 'tests.bare_settings'},
    )
    assert result.returncode == 0, result.stderr
    assert 'cheap checks ok' in result.stdout


def test_dir_lists_the_lazy_exports():
    import django_redis_aiogram

    assert set(django_redis_aiogram.__all__) <= set(dir(django_redis_aiogram))


def test_the_version_is_the_one_the_changelog_announces():
    """`pyproject` reads the version from here, so this string is what a user
    installs — and nothing pinned it, so a revert of the release bump would
    have passed every test.

    Asserted against the changelog rather than a literal, so a release edits one
    place and this keeps checking that the other one followed.
    """
    changelog = (Path(__file__).resolve().parent.parent / 'CHANGELOG.md').read_text(encoding='utf-8')
    # the first heading of any shape, not the first that looks like a version:
    # searching for the latter would walk past a broken top entry to an older
    # one and call the release good
    heading = re.search(r'^## (.+)$', changelog, re.MULTILINE)

    assert heading is not None, 'the changelog has no release headings'
    announced = heading.group(1).split(' - ')[0].strip()
    assert re.fullmatch(r'\d+\.\d+\.\d+', announced), f'the top changelog heading is not a version: {heading.group(1)}'
    assert django_redis_aiogram.__version__ == announced


def test_connecting_a_metrics_receiver_pulls_neither_aiogram_nor_the_orm():
    """`django_redis_aiogram.signals` must be importable from settings-time code.

    A metrics module is imported early, and importing this package's client half
    loads aiogram — the ~900ms this whole file exists about. So the seam is its own
    module: connect a receiver without paying for a bot you may never build, and
    without the ORM, so it can be done before `django.setup()` has finished.

    Measured on top of a process that has already imported Django, which every
    Django process has by the time settings are read: **0.15 ms**. From a *bare*
    interpreter it is 16 ms, almost all of it `django.dispatch` pulling `asgiref`
    rather than anything of ours — which is the reason this asserts the shape of what
    gets pulled rather than a number: the figure moves with Django's version and the
    machine, and the shape does not. `django` and `asgiref` are allowed
    through: `django.dispatch` imports `asgiref.sync` for async receivers, and both
    are already loaded in the process this runs in.
    """
    script = textwrap.dedent("""
        import sys

        before = set(sys.modules)
        from django_redis_aiogram.signals import events_recorded
        pulled = {name.split('.')[0] for name in set(sys.modules) - before}
        pulled -= sys.stdlib_module_names | {'django_redis_aiogram', 'django', 'asgiref'}

        assert not pulled, f'importing the signal pulled {sorted(pulled)}'
        assert 'aiogram' not in sys.modules, 'the metrics seam loaded aiogram'
        assert 'django.db.models' not in sys.modules, 'the metrics seam loaded the ORM'
        assert 'redis' not in sys.modules, 'the metrics seam loaded the Redis client'

        events_recorded.connect(lambda sender, **kwargs: None, weak=False)
        print('cheap seam ok')
    """)
    result = subprocess.run(  # noqa: S603 - our own interpreter, and a script written right above
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
        env={**os.environ, 'DJANGO_SETTINGS_MODULE': 'tests.settings'},
    )
    assert result.returncode == 0, result.stderr
    assert 'cheap seam ok' in result.stdout


def test_threads_racing_for_the_bot_all_get_the_same_one(monkeypatch):
    """The guarantee that replaced an explicit lock, so it needs holding down.

    Two instances mean two event loops and two HTTP sessions, and `loop_lock` — which
    exists to stop `run_until_complete` being re-entered — would be guarding one of
    them while the other was entered. The lock this package used to hold is gone
    because `_singleton`'s module body runs once per process and Python makes
    concurrent importers wait on that module's own import lock.

    A `Barrier` rather than luck: every thread is held until all of them are ready, so
    they reach the import at the same moment rather than in sequence.
    """
    import django_redis_aiogram

    # forget both the cached attribute and the module whose body builds it, so this
    # really is a first access rather than a read of what an earlier test left — and
    # through monkeypatch, so the next test does not inherit the instance built here
    # while every test before it holds the original
    monkeypatch.delattr(django_redis_aiogram, 'bot', raising=False)
    monkeypatch.delitem(sys.modules, 'django_redis_aiogram._singleton', raising=False)

    gate = threading.Barrier(8)
    seen: list[object] = []
    errors: list[BaseException] = []

    def grab():
        """Wait at the barrier with the others, then take the shared bot."""
        try:
            gate.wait(timeout=10)
            seen.append(django_redis_aiogram.bot)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=grab, name=f'racer-{index}') for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, errors
    assert len(seen) == 8, f'only {len(seen)} of 8 threads got there'
    assert len(set(map(id, seen))) == 1, f'{len(set(map(id, seen)))} different bots were built'


def test_the_healthcheck_probe_does_not_import_aiogram():
    """A container healthcheck runs on a timer, and paid ~900 ms every time.

    It imported the shared `bot` for one flag and built a `Delivery` for a branch that
    could not fire, and both pull aiogram. Measured in a process with
    `AUTODISCOVER=0`: 902 ms against 16 ms.

    The saving needs `AUTODISCOVER=0` to materialise, because the documented
    `<app>/tg_router.py` layout imports aiogram during `django.setup()` anyway — so
    this is decoupling first and speed second. What it buys unconditionally is that
    the probe no longer depends on which class `DELIVERY` names.
    """
    script = textwrap.dedent("""
        import sys

        from django_redis_aiogram.management.commands import tgbot_healthcheck

        assert 'aiogram' not in sys.modules, 'the healthcheck pulled aiogram'
        assert 'django_redis_aiogram.client' not in sys.modules, 'it pulled the client half'
        assert hasattr(tgbot_healthcheck, 'Command')
        print('cheap probe ok')
    """)
    result = subprocess.run(  # noqa: S603 - our own interpreter, and a script written right above
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
        env={**os.environ, 'DJANGO_SETTINGS_MODULE': 'tests.settings', 'DJANGO_REDIS_AIOGRAM_AUTODISCOVER': '0'},
    )
    assert result.returncode == 0, result.stderr
    assert 'cheap probe ok' in result.stdout


def test_the_probe_with_no_settings_module_refuses_in_one_line():
    """The mistake the new recipe invites, exercised for real rather than mocked.

    `manage.py` sets `DJANGO_SETTINGS_MODULE` with `os.environ.setdefault` *inside its own
    process*, so a container that runs it need never export the variable — and a
    healthcheck is a different process. The unit test for this covers `main()`'s handler
    with a raise of its own; only a subprocess can show that the real path arrives there,
    which matters because anything that made the settings layer fall back to defaults
    would answer `redis is unreachable` instead and never name the variable at fault.
    """
    environment = {key: value for key, value in os.environ.items() if key != 'DJANGO_SETTINGS_MODULE'}
    probe = subprocess.run(
        [sys.executable, '-m', 'django_redis_aiogram.healthcheck'],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
        env=environment,
    )

    assert probe.returncode == 1, probe.stdout
    assert probe.stdout == '', probe.stdout
    assert probe.stderr.startswith('cannot read the settings: '), probe.stderr
    assert 'DJANGO_SETTINGS_MODULE' in probe.stderr, probe.stderr
    assert 'Traceback' not in probe.stderr, probe.stderr
    # one line, which is the actual claim: a traceback-free multi-line dump would pass
    # the check above and still be the thing a healthcheck cannot show anybody
    assert len(probe.stderr.splitlines()) == 1, probe.stderr


def test_the_healthcheck_probe_does_not_populate_the_app_registry(tmp_path):
    """It runs on a timer in a container, and `django.setup()` costs whatever the host
    project costs.

    Measured in one consumer — Django 5.2, twenty apps, one registering adapters in
    `AppConfig.ready()` — 2.45s for the settings module against 17.89s more for
    `apps.populate()`, which is what made Docker kill the probe at any timeout the wiki
    could honestly publish, while the probe's own last line said `healthy`.

    Asserted on evidence rather than on timing, which would flake on CI: the settings
    module used here installs an app whose `ready()` writes a file. Absent means the
    registry was never populated. The control below proves the marker fires at all.
    """
    marker = tmp_path / 'registry-marker'
    environment = {
        **os.environ,
        'DJANGO_SETTINGS_MODULE': 'tests.marker_settings',
        'DJANGO_REDIS_AIOGRAM_TEST_MARKER': str(marker),
        'DJANGO_REDIS_AIOGRAM_REDIS_URL': 'redis://127.0.0.1:1/0',
    }
    probe = subprocess.run(
        [sys.executable, '-m', 'django_redis_aiogram.healthcheck'],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
        env=environment,
    )

    # a refusal is expected: nothing is listening on port 1. What matters is that it
    # got far enough to try, and that it never booted the app registry to do so
    assert probe.returncode == 1, f'the probe did not run at all: {probe.stderr}'
    assert 'redis is unreachable' in probe.stderr, probe.stderr
    assert not marker.exists(), 'the probe populated the app registry'

    control = subprocess.run(
        [sys.executable, '-c', 'import django; django.setup()'],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
        env=environment,
    )

    assert control.returncode == 0, control.stderr
    assert marker.exists(), 'the marker never fires, so its absence above proved nothing'
