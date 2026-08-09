"""What the event log costs when it is off, and the parts that need no database.

The whole design rests on two claims: recording is free while the flag is off,
and `models.py` never drags aiogram into a boot that wants nothing to do with
it. Both are asserted here rather than assumed.
"""

import os
import subprocess
import sys
import textwrap
import threading
import uuid

import pytest
from django.test import override_settings

from django_redis_aiogram.dbrouter import TelegramEventLogRouter, event_log_database
from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.events import (
    MAX_KIND_LENGTH,
    failure_kinds,
    kind_choices,
    known_kinds,
    new_correlation_id,
    register_kind,
)
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.recorder import WRITER_THREAD, Event, EventRecorder


def test_recording_is_free_while_the_flag_is_off():
    """Asserted, not inferred: the writer is booby-trapped so any attempt to
    reach it would raise rather than quietly work."""
    recorder = EventRecorder()

    def explode(_batch):
        msg = 'the disabled recorder tried to write'
        raise AssertionError(msg)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(EventRecorder, '_write', staticmethod(explode))
        recorder.record(Event(kind=EventKind.OUTBOUND_SENT.value))

    assert recorder._queue is None, 'a disabled recorder built a buffer'
    assert recorder._thread is None, 'a disabled recorder started a thread'


def test_no_writer_thread_exists_when_the_flag_is_off():
    recorder = EventRecorder()
    recorder.record(Event(kind=EventKind.OUTBOUND_SENT.value))

    assert not [thread for thread in threading.enumerate() if thread.name == WRITER_THREAD]


@override_settings(TELEGRAM_BOT={'EVENT_LOG': 'maybe'})
def test_an_unreadable_flag_turns_recording_off_rather_than_raising(caplog):
    """A misconfigured flag is E031's finding at boot. At runtime it must not
    become the reason a message was not sent."""
    recorder = EventRecorder()
    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        recorder.record(Event(kind=EventKind.OUTBOUND_SENT.value))

    assert recorder.enabled is False
    assert 'could not read the event log flag' in caplog.text


def test_recording_never_raises():
    """A log that can break delivery is worse than no log."""
    recorder = EventRecorder()
    recorder._enabled = True

    def explode(_batch):
        msg = 'database on fire'
        raise RuntimeError(msg)

    with pytest.MonkeyPatch.context() as patch, override_settings(TELEGRAM_BOT={'EVENT_LOG_SYNC': True}):
        patch.setattr(EventRecorder, '_write', staticmethod(explode))
        recorder._enabled = True
        recorder.record(Event(kind=EventKind.OUTBOUND_SENT.value))  # must not raise


def test_the_model_module_never_imports_aiogram():
    """Django imports models.py on every setup(), before ready() and regardless
    of ENABLED, so an import here is paid by every migration container."""
    script = textwrap.dedent("""
        import sys

        import django

        django.setup()

        from django_redis_aiogram.recorder import Event, recorder

        recorder.record(Event(kind='outbound.sent'))

        assert 'django_redis_aiogram.models' in sys.modules, 'models did not load with the registry'
        assert 'aiogram' not in sys.modules, 'the model or the recorder pulled aiogram'
        assert 'django_redis_aiogram.eventlog' not in sys.modules, 'a disabled recorder imported the ORM layer'
        print('models stay cheap')
    """)
    result = subprocess.run(  # noqa: S603 - our own interpreter, and a script written right above
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            'DJANGO_SETTINGS_MODULE': 'tests.settings',
            'DJANGO_REDIS_AIOGRAM_ENABLED': '0',
        },
    )
    assert result.returncode == 0, result.stderr
    assert 'models stay cheap' in result.stdout


def test_every_shipped_kind_is_registered():
    assert {kind.value for kind in EventKind} <= known_kinds()


def test_a_kind_can_be_registered_without_a_migration():
    code = register_kind('tests.custom.kind', 'Custom')

    assert code in known_kinds()
    assert ('tests.custom.kind', 'Custom') in kind_choices()
    # the column is a plain CharField, so the registry is the only gate
    assert TelegramEvent._meta.get_field('kind').choices is None


def test_registering_the_same_kind_differently_is_refused():
    register_kind('tests.stable.kind', 'Stable')

    with pytest.raises(ValueError, match='already registered'):
        register_kind('tests.stable.kind', 'Something else')


def test_a_kind_too_long_for_the_column_is_refused():
    """MySQL truncates in non-strict mode and rejects in strict mode, so a
    silently shortened kind would be two different codes on two deployments."""
    with pytest.raises(ValueError, match='longer than'):
        register_kind('x' * (MAX_KIND_LENGTH + 1), 'Too long')


def test_failure_kinds_are_the_ones_worth_alerting_on():
    failures = failure_kinds()

    assert EventKind.OUTBOUND_FAILED.value in failures
    assert EventKind.OUTBOUND_SENT.value not in failures


def test_the_correlation_id_is_a_version_7_uuid():
    identifier = new_correlation_id()

    assert isinstance(identifier, uuid.UUID)
    assert identifier.version == 7


def test_the_router_stays_quiet_without_an_alias():
    """With no alias configured the feature still works, because the writer and
    the admin name the database explicitly; the router only moves migrate."""
    router = TelegramEventLogRouter()

    assert event_log_database() is None
    assert router.db_for_write(TelegramEvent) is None
    assert router.allow_migrate('default', 'django_redis_aiogram') is None


@override_settings(TELEGRAM_BOT={'EVENT_LOG_DATABASE': 'logs'})
def test_the_router_has_no_opinion_on_relations():
    """None, not True: this app owns no relation in either direction, and
    claiming otherwise would let Django allow one across databases."""
    assert TelegramEventLogRouter().allow_relation(TelegramEvent(), TelegramEvent()) is None


@override_settings(TELEGRAM_BOT={'EVENT_LOG_DATABASE': 'logs'})
def test_the_router_moves_only_this_app():
    router = TelegramEventLogRouter()

    assert router.db_for_read(TelegramEvent) == 'logs'
    assert router.db_for_write(TelegramEvent) == 'logs'
    assert router.allow_migrate('logs', 'django_redis_aiogram') is True
    assert router.allow_migrate('default', 'django_redis_aiogram') is False
    # None, not False: where somebody else's table belongs is not ours to say
    assert router.allow_migrate('default', 'auth') is None


def test_every_setting_is_documented():
    """A setting nobody can look up is a setting nobody configures on purpose.

    The check-id table has its own test; this is the other half, and it fails
    when a setting lands without a row on the page.
    """
    import pathlib

    from django_redis_aiogram.defaults import DEFAULTS

    page = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'wiki' / 'Settings.md'
    text = page.read_text(encoding='utf-8')
    missing = sorted(name for name in DEFAULTS if f'`{name}`' not in text)

    assert missing == [], f'settings missing from Settings.md: {missing}'


def test_every_shipped_kind_is_documented():
    """The kinds table is what an operator reads to know what a row means."""
    import pathlib

    page = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'wiki' / 'Event-log.md'
    text = page.read_text(encoding='utf-8')
    missing = sorted(kind.value for kind in EventKind if kind.value not in text)

    assert missing == [], f'kinds missing from Event-log.md: {missing}'


def test_every_kind_the_enum_ships_is_registered():
    """A kind the recorder would refuse, because nothing registered it, is a
    seam that silently records nothing.

    A subset, not an equality: other tests register kinds of their own, and
    this must not depend on which of them ran first.
    """
    missing = sorted({kind.value for kind in EventKind} - known_kinds())

    assert missing == [], f'shipped but unregistered: {missing}'
