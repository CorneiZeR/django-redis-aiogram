"""Before 2.0 these checks silently passed on every input: the validation flag
was only ever set inside an `isinstance` branch that a wrong type never entered.
"""

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
