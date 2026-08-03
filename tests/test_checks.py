"""Before 2.0 these checks silently passed on every input: the validation flag
was only ever set inside an `isinstance` branch that a wrong type never entered.
"""

import pathlib
import re

from django.core.checks import Error, Warning
from django.test import override_settings

from django_redis_aiogram.checks import check_settings


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


@override_settings(
    TELEGRAM_BOT={'DELIVERY': 'carrier-pigeon', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'}
)
def test_unknown_delivery_is_rejected():
    assert 'django_redis_aiogram.E009' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'SERIALIZER': 'yaml', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_unknown_serializer_is_rejected():
    assert 'django_redis_aiogram.E010' in ids(errors(check_settings()))


@override_settings(TELEGRAM_BOT={'DEFAULT_KWARGS': {}, 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_non_callable_default_kwargs_is_caught():
    assert 'django_redis_aiogram.E015' in ids(errors(check_settings()))


@override_settings(
    TELEGRAM_BOT={'DEFAULT_BOT_PROPERTIES': 'HTML', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'}
)
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
    assert not [m for m in messages if isinstance(m, Warning) and m.id.endswith('W001')]


SETTINGS_PAGE = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'wiki' / 'Settings.md'
# the table separates a range with an en dash
DOCUMENTED = re.compile('`([EW]\\d{3})`(?:\\s*[\u2013-]\\s*`([EW]\\d{3})`)?')

# Every id the checks can emit. Two settings dicts are needed: a wrong type
# stops a check before it can reach its value-level complaint.
EXPECTED_IDS = {f'E{code:03d}' for code in range(1, 25)} | {'W001', 'W002', 'W003'}

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
    'REDIS_EXP_KEY': 42,
    'DELIVERY': 42,
    'SERIALIZER': 42,
    'FSM_STORAGE': 42,
    'MAX_RETRIES': 'ten',
    'REDIS_EXP_TIME': 'five',
    'BLPOP_TIMEOUT': 'five',
    'HEARTBEAT_INTERVAL': 'ten',
    'HEALTHCHECK_MAX_QUEUE': 'lots',
    'DEFAULT_KWARGS': 42,
    'DEFAULT_BOT_PROPERTIES': 42,
    'RATE_LIMIT': 42,
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
}


def documented_ids():
    """The table lists ranges, so a documented E004-E011 covers each id between."""
    found = set()
    for first, last in DOCUMENTED.findall(SETTINGS_PAGE.read_text(encoding='utf-8')):
        if not last:
            found.add(first)
            continue
        found.update(
            f'{first[0]}{number:03d}' for number in range(int(first[1:]), int(last[1:]) + 1)
        )
    return found


def emitted_ids():
    """What the checks actually emit — running them, not reading their source.

    Scraping the registrations was worse: reformatting one dropped it from the
    scan, and the documentation check below stayed green without it.
    """
    found = set()
    for settings in (WRONG_TYPES, WRONG_VALUES):
        with override_settings(TELEGRAM_BOT=settings):
            found |= {
                str(message.id).removeprefix('django_redis_aiogram.')
                for message in check_settings()
            }
    return found


def test_the_expected_ids_are_the_ones_the_checks_emit():
    """A new check has to be added here, and therefore to the docs, to pass."""
    emitted = emitted_ids()
    assert emitted - EXPECTED_IDS == set(), (
        f'undeclared check ids: {sorted(emitted - EXPECTED_IDS)}'
    )
    assert EXPECTED_IDS - emitted == set(), f'ids nothing emitted: {sorted(EXPECTED_IDS - emitted)}'


def test_every_check_id_is_documented():
    """An operator meeting E021 has to be able to look it up."""
    missing = sorted(EXPECTED_IDS - documented_ids())
    assert not missing, f'check ids missing from docs/wiki/Settings.md: {missing}'


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://x', 'WORKER_NAME': 7})
def test_a_non_string_worker_name_is_reported():
    """It names the in-flight list, so a wrong type breaks reclaim at startup."""
    assert 'django_redis_aiogram.E021' in ids(check_settings())
