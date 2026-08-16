"""Before 2.0 these checks silently passed on every input: the validation flag
was only ever set inside an `isinstance` branch that a wrong type never entered.
"""

import pathlib
import re

from django.core.checks import Error
from django.core.checks import Warning as CheckWarning
from django.test import override_settings

from django_redis_aiogram.checks import CHECKS, check_settings
from django_redis_aiogram.defaults import DEFAULTS


def ids(messages):
    return {message.id for message in messages}


def errors(messages):
    return [message for message in messages if isinstance(message, Error)]


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost'})
def test_valid_settings_produce_no_errors():
    assert errors(check_settings()) == []


@override_settings(TELEGRAM_BOT={'MAX_RETRIES': 'ten', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_wrong_integer_type_is_caught():
    assert 'django_redis_aiogram.E012' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'MAX_RETRIES': True, 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_bool_is_not_accepted_as_integer():
    assert 'django_redis_aiogram.E012' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'MAX_RETRIES': 0, 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_integer_below_minimum_is_caught():
    assert 'django_redis_aiogram.E012' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'ENABLED': 'yes', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_wrong_boolean_type_is_caught():
    assert 'django_redis_aiogram.E001' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'TOKEN': 42, 'REDIS_URL': 'r://x'})
def test_wrong_string_type_is_caught():
    assert 'django_redis_aiogram.E004' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'DELIVERY': 'carrier-pigeon', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_unknown_delivery_is_rejected():
    assert 'django_redis_aiogram.E009' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'SERIALIZER': 'yaml', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_unknown_serializer_is_rejected():
    assert 'django_redis_aiogram.E010' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'DEFAULT_KWARGS': {}, 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_non_callable_default_kwargs_is_caught():
    assert 'django_redis_aiogram.E015' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'DEFAULT_BOT_PROPERTIES': 'HTML', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_non_mapping_bot_properties_is_caught():
    assert 'django_redis_aiogram.E016' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'TOEKN': 'typo', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_typo_in_a_key_is_reported_as_warning():
    messages = check_settings()
    assert errors(messages) == []
    assert 'django_redis_aiogram.W003' in ids(messages)


@override_settings(TELEGRAM_BOT={})
def test_missing_credentials_warn_but_do_not_fail():
    messages = check_settings()
    assert errors(messages) == []
    assert {'django_redis_aiogram.W001', 'django_redis_aiogram.W002'} <= ids(messages)


@override_settings(TELEGRAM_BOT={'ENABLED': False})
def test_disabled_bot_does_not_warn_about_credentials():
    messages = check_settings()
    assert isinstance(messages, list)
    assert not [m for m in messages if isinstance(m, CheckWarning) and m.id.endswith('W001')]


SETTINGS_PAGE = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'wiki' / 'Settings.md'
# the table separates a range with an en dash
DOCUMENTED = re.compile('`([EW]\\d{3})`(?:\\s*[\u2013-]\\s*`([EW]\\d{3})`)?')

# Every id the checks can emit. Three settings dicts are needed: a wrong type
# stops a check before it can reach its value-level complaint, and an alias that
# is not in DATABASES stops the one asking whether that alias has an engine.
# E008 and E013 guarded the keyspace settings 3.0 removed. Their ids are gone
# rather than reused: a project silencing one must not start silencing a new rule
RETIRED_IDS = {'E008', 'E013'}
EXPECTED_IDS = ({f'E{code:03d}' for code in range(1, 43)} - RETIRED_IDS) | {f'W{code:03d}' for code in range(1, 10)}

WRONG_TYPES = {
    'ENABLED': 'yes',
    'AUTODISCOVER': 'no',
    'RAISE_EXCEPTION': 1,
    'ALLOW_PICKLE': 'maybe',
    'TOKEN': 42,
    'REDIS_URL': 42,
    'MODULE_NAME': 42,
    'REDIS_MESSAGES_KEY': 42,
    'WORKER_NAME': 42,
    'DELIVERY': 42,
    'SERIALIZER': 42,
    'FSM_STORAGE': 42,
    'MAX_RETRIES': 'ten',
    'BLPOP_TIMEOUT': 'five',
    'REDIS_TIMEOUT': 'five',
    'HEARTBEAT_INTERVAL': 'ten',
    'HEALTHCHECK_MAX_QUEUE': 'lots',
    'WEBHOOK_URL': 42,
    'WEBHOOK_SECRET': 42,
    'MODE': 42,
    'DEFAULT_KWARGS': 42,
    'DEFAULT_BOT_PROPERTIES': 42,
    'RATE_LIMIT': 42,
    'EVENT_LOG': 'maybe',
    'EVENT_LOG_KINDS': 'outbound.sent',
    'EVENT_LOG_PAYLOAD': 42,
    'EVENT_LOG_MAX_PAYLOAD_BYTES': 'lots',
    'EVENT_LOG_REDACT_KEYS': 'token',
    'EVENT_LOG_BUFFER_SIZE': 'many',
    'EVENT_LOG_BATCH_SIZE': 'some',
    'EVENT_LOG_FLUSH_INTERVAL': 'often',
    'EVENT_LOG_RETENTION_DAYS': 'thirty',
    'EVENT_LOG_DATABASE': 42,
    'EVENT_LOG_SYNC': 'maybe',
    'NOT_A_SETTING': 1,
}

WRONG_VALUES = {
    'TOKEN': '',
    'REDIS_URL': '',
    'DELIVERY': 'carrier-pigeon',
    'SERIALIZER': 'pickle',
    'ALLOW_PICKLE': False,
    'FSM_STORAGE': 'no.such.Storage',
    'DEFAULT_BOT_PROPERTIES': {'not_a_property': 1},
    'RATE_LIMIT': {'overall_per_second': 'fast'},
    # a URL with no secret, and not https either
    'WEBHOOK_URL': 'http://example.test/tg/',
    'WEBHOOK_SECRET': '',
    'MODE': 'sideways',
    # a pop asked to wait longer than a read may take, which the consumer caps
    'BLPOP_TIMEOUT': 30,
    'REDIS_TIMEOUT': 5,
    'WEBHOOK_ALLOWED_UPDATES': 'message',
    # the log on with nowhere to write it, nothing to prune it, a batch the
    # buffer can never fill, an alias that is not in DATABASES, and a typo
    'EVENT_LOG': True,
    'EVENT_LOG_RETENTION_DAYS': 0,
    'EVENT_LOG_BATCH_SIZE': 5000,
    'EVENT_LOG_DATABASE': 'nope',
    'EVENT_LOG_KINDS': ('outbound.snet',),
}

# the log on and pointed at a real alias, which under tests.settings is the
# dummy backend Django fills an empty DATABASES in with
LOG_WITHOUT_A_DATABASE = {'EVENT_LOG': True, 'EVENT_LOG_SYNC': True}


def documented_ids():
    """The table lists ranges, so a documented E004-E011 covers each id between."""
    found = set()
    for first, last in DOCUMENTED.findall(SETTINGS_PAGE.read_text(encoding='utf-8')):
        if not last:
            found.add(first)
            continue
        found.update(f'{first[0]}{number:03d}' for number in range(int(first[1:]), int(last[1:]) + 1))
    return found


def emitted_ids():
    """What the checks actually emit — running them, not reading their source.

    Scraping the registrations was worse: reformatting one dropped it from the
    scan, and the documentation check below stayed green without it.
    """
    found = set()
    for settings in (WRONG_TYPES, WRONG_VALUES, LOG_WITHOUT_A_DATABASE):
        with override_settings(TELEGRAM_BOT=settings):
            found |= {str(message.id).removeprefix('django_redis_aiogram.') for message in check_settings()}
    return found


def test_the_expected_ids_are_the_ones_the_checks_emit():
    """A new check has to be added here, and therefore to the docs, to pass."""
    emitted = emitted_ids()
    assert emitted - EXPECTED_IDS == set(), f'undeclared check ids: {sorted(emitted - EXPECTED_IDS)}'
    assert EXPECTED_IDS - emitted == set(), f'ids nothing emitted: {sorted(EXPECTED_IDS - emitted)}'


def test_every_check_id_is_documented():
    """An operator meeting E021 has to be able to look it up."""
    missing = sorted(EXPECTED_IDS - documented_ids())
    assert not missing, f'check ids missing from docs/wiki/Settings.md: {missing}'


def test_every_registry_row_reports_under_its_own_id():
    """Two rows sharing an id would make the docs entry ambiguous."""
    codes = [check.code for check in CHECKS]
    assert sorted(codes) == sorted(set(codes))


def test_every_registry_row_guards_a_real_setting():
    """A typo in the key would validate a setting nothing ever reads."""
    # the unknown-keys row is about the settings dict as a whole, so it has no key
    unknown = sorted({check.key for check in CHECKS if check.key} - set(DEFAULTS))
    assert unknown == []


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://x', 'WORKER_NAME': 7})
def test_a_non_string_worker_name_is_reported():
    """It names the in-flight list, so a wrong type breaks reclaim at startup."""
    assert 'django_redis_aiogram.E021' in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://x', 42: 'numeric'})
def test_a_non_string_settings_key_is_reported_not_raised():
    """`", ".join` over mixed key types used to raise out of manage.py check."""
    reported = {message.id for message in check_settings()}

    assert 'django_redis_aiogram.W003' in reported


@override_settings(TELEGRAM_BOT={'ENABLED': 'false', 'TOKEN': '', 'REDIS_URL': ''})
def test_a_textually_disabled_bot_does_not_warn_about_credentials():
    """'false' from the environment disables startup and sending, so the
    credential warnings have to agree rather than nag a disabled process."""
    reported = {message.id for message in check_settings()}

    assert 'django_redis_aiogram.W001' not in reported
    assert 'django_redis_aiogram.W002' not in reported


@override_settings(TELEGRAM_BOT={'ENABLED': 'maybe', 'TOKEN': '', 'REDIS_URL': ''})
def test_an_unreadable_enabled_still_warns_and_reports_its_own_problem():
    """E001 owns the type complaint; the warnings assume the bot is on."""
    reported = {message.id for message in check_settings()}

    assert 'django_redis_aiogram.E001' in reported
    assert 'django_redis_aiogram.W001' in reported


@override_settings(TELEGRAM_BOT={'TOKEN': '1:x', 'REDIS_URL': 'redis://localhost:6379/0'})
def test_the_defaults_report_nothing():
    """A warning on an untouched install teaches people to ignore the checks.

    2.1.0 shipped `REDIS_TIMEOUT` at 5 next to `BLPOP_TIMEOUT` at 5, so W004
    fired on every default configuration.
    """
    reported = [f'{message.id}: {message.msg}' for message in check_settings()]

    assert reported == [], reported


@override_settings(TELEGRAM_BOT={'EVENT_LOG': False, 'EVENT_LOG_SYNC': True})
def test_the_synchronous_writer_warning_is_silent_while_the_log_is_off():
    """`record()` returns before it ever reads EVENT_LOG_SYNC, so warning here
    would describe a cost nobody is paying — and a warning that is wrong is one
    people learn to scroll past."""
    emitted = {str(message.id).removeprefix('django_redis_aiogram.') for message in check_settings()}

    assert 'W009' not in emitted, emitted


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'REDIS_URL': 'redis://localhost:6379/0?decode_responses=true',
        'ALLOW_PICKLE': True,
        'SERIALIZER': 'pickle',
    }
)
def test_a_decoding_url_with_pickle_is_refused():
    """The one pairing nothing can recover from at runtime.

    redis-py decodes inside its own parser, so a pickled payload raises after the
    server has already moved the message to the in-flight list, and every later
    reclaim trips over the same message for ever.
    """
    assert 'django_redis_aiogram.E043' in ids(check_settings())


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        'REDIS_URL': 'redis://localhost:6379/0?decode_responses=true',
        'ALLOW_PICKLE': False,
    }
)
def test_a_decoding_url_without_pickle_is_fine():
    """Decoding is supported: one REDIS_URL is often shared with a cache backend."""
    assert 'django_redis_aiogram.E043' not in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost', 'ALLOW_PICKLE': True})
def test_a_plain_url_with_pickle_is_fine():
    assert 'django_redis_aiogram.E043' not in ids(check_settings())


@override_settings(
    TELEGRAM_BOT={
        'TOKEN': '42:x',
        # reads as off and is not: redis-py has no boolean parser for this key, so
        # the string 'false' reaches the connection and enables decoding
        'REDIS_URL': 'redis://localhost:6379/0?decode_responses=false',
        'ALLOW_PICKLE': True,
    }
)
def test_a_url_that_only_looks_like_it_disables_decoding_is_refused():
    assert 'django_redis_aiogram.E043' in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost', 'DRAIN_TIMEOUT': 'soon'})
def test_an_unreadable_drain_timeout_is_reported():
    """`close()` reads this while shutting down, which is the worst place to raise."""
    assert 'django_redis_aiogram.E044' in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost', 'DRAIN_TIMEOUT': -1})
def test_a_negative_drain_timeout_is_reported():
    """A negative budget makes the drain expire before it starts."""
    assert 'django_redis_aiogram.E044' in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost', 'DRAIN_TIMEOUT': float('nan')})
def test_a_drain_timeout_that_is_not_a_number_is_reported():
    """Every comparison against nan is false, so it slips past a plain bound."""
    assert 'django_redis_aiogram.E044' in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost', 'DRAIN_TIMEOUT': 2.5})
def test_a_fractional_drain_timeout_is_fine():
    assert 'django_redis_aiogram.E044' not in ids(check_settings())
