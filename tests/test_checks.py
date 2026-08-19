"""Before 2.0 these checks silently passed on every input: the validation flag
was only ever set inside an `isinstance` branch that a wrong type never entered.
"""

import pathlib
import re

import pytest
from django.core.checks import Error
from django.core.checks import Warning as CheckWarning
from django.test import override_settings

from django_redis_aiogram.checks import CHECKS, check_settings
from django_redis_aiogram.dbrouter import TelegramEventLogRouter
from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.events import worker_identity


@pytest.fixture(autouse=True)
def _stable_hostname(monkeypatch):
    """Pin the hostname, because W010 reads it.

    Without this the whole module's results depend on where it runs: a container
    started without `hostname:` gets a twelve-character hex name, which is exactly
    what W010 exists to report, so `test_the_defaults_report_nothing` would pass
    on a laptop and fail in Docker. The tests that are *about* W010 patch it back.
    """
    monkeypatch.setenv('HOSTNAME', 'bot-worker-1')


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


@override_settings(
    TELEGRAM_BOT={
        'ENABLED': 'true',
        'AUTODISCOVER': '1',
        'ALLOW_PICKLE': 'no',
        'EVENT_LOG': 'on',
        'EVENT_LOG_SYNC': 'off',
        'REQUIRE_CRASH_SAFE': 0,
        'TOKEN': '42:x',
        'REDIS_URL': 'r://x',
    }
)
def test_a_configuration_that_boots_and_sends_does_not_fail_the_checks():
    """Every boolean here comes from the environment, which has only strings.

    This exact settings dict is documented, loads, and sends — and used to fail
    `manage.py check` on five separate ids, because the rule demanded a real `bool`
    while every one of these is coerced at the point of use. It was inverted twice
    over: the values `coerce_bool` genuinely refuses raise `ImproperlyConfigured`
    out of `apps.ready()` before a check ever runs, so the errors could not fire on
    the case they were written for.
    """
    reported = {str(message.id) for message in check_settings()}
    boolean_ids = {f'django_redis_aiogram.{code}' for code in ('E001', 'E002', 'E017', 'E031', 'E042', 'E046')}

    assert reported & boolean_ids == set(), f'a working configuration was refused: {sorted(reported & boolean_ids)}'


@override_settings(TELEGRAM_BOT={'ENABLED': 'maybe', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_a_boolean_nothing_can_read_is_still_caught():
    """The other direction, which is what keeps the rule a rule.

    `'maybe'` is what `coerce_bool` refuses, so this is the value that would have
    taken `apps.ready()` down — and the check has to name it rather than staying
    quiet because it stopped demanding a `bool`.
    """
    reported = errors(check_settings())

    assert 'django_redis_aiogram.E001' in ids(reported)
    # the message is the one the runtime would have raised, not a paraphrase
    assert any("must be one of ['0', '1', 'false'" in str(message) for message in reported), reported


@override_settings(TELEGRAM_BOT={'RAISE_EXCEPTION': 'false', 'TOKEN': '42:x', 'REDIS_URL': 'r://x'})
def test_raise_exception_accepts_what_the_environment_can_express():
    """It was the last setting demanding a real bool, and only because of a defect.

    `client.py` read it with a bare `if`, so `'false'` — truthy, and what
    `DJANGO_REDIS_AIOGRAM_RAISE_EXCEPTION=false` arrives as — re-raised the exception the
    project had asked to have swallowed. The check was strict to say so at boot rather
    than at the first failed send. The read goes through `coerce_bool` now, so the
    documented spelling is a working value here too.
    """
    assert 'django_redis_aiogram.E003' not in ids(errors(check_settings()))


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
# W010 is left out of the emitted set on purpose: it fires only when this machine's
# hostname looks Docker-generated, so whether the fixtures below produce it differs
# between a laptop and CI. It has its own tests, and
# `test_every_check_id_is_documented` reads the registry rather than this set, so it
# is still held to the documentation
HOSTNAME_DEPENDENT_IDS = {'W010'}
EXPECTED_IDS = ({f'E{code:03d}' for code in range(1, 47)} - RETIRED_IDS) | (
    {f'W{code:03d}' for code in range(1, 12)} - HOSTNAME_DEPENDENT_IDS
)

WRONG_TYPES = {
    # non-coercible on purpose: 'yes' and 'no' are documented, working values, and
    # E001/E002 asking for a real bool was the defect. `[]` and 4.2 take the other
    # refusal path in `coerce_bool` — wrong type rather than unrecognised word
    'ENABLED': 'maybe',
    'AUTODISCOVER': [],
    'RAISE_EXCEPTION': 'maybe',
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
    'EVENT_LOG': 4.2,
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
    'DRAIN_TIMEOUT': 'soon',
    'MAX_IN_FLIGHT': 'two',
    'REQUIRE_CRASH_SAFE': 'maybe',
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
    # readable numbers, but not ones a bound can be made of: a drain that
    # finishes before it starts and a limit of minus one message
    'DRAIN_TIMEOUT': -1,
    'MAX_IN_FLIGHT': -1,
}

# the log on and pointed at a real alias, which under tests.settings is the
# dummy backend Django fills an empty DATABASES in with
LOG_WITHOUT_A_DATABASE = {'EVENT_LOG': True, 'EVENT_LOG_SYNC': True}

# E043 is about a pair, not a value: neither half is wrong alone, so it needs
# its own settings rather than a slot in the maps above
PICKLE_ON_A_DECODING_URL = {
    'ALLOW_PICKLE': True,
    'REDIS_URL': 'redis://localhost:6379/0?decode_responses=1',
}


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
    for settings in (WRONG_TYPES, WRONG_VALUES, LOG_WITHOUT_A_DATABASE, PICKLE_ON_A_DECODING_URL):
        with override_settings(TELEGRAM_BOT=settings):
            found |= {str(message.id).removeprefix('django_redis_aiogram.') for message in check_settings()}
    return found


def test_the_expected_ids_are_the_ones_the_checks_emit():
    """A new check has to be added here, and therefore to the docs, to pass."""
    emitted = emitted_ids()
    assert emitted - EXPECTED_IDS == set(), f'undeclared check ids: {sorted(emitted - EXPECTED_IDS)}'
    assert EXPECTED_IDS - emitted == set(), f'ids nothing emitted: {sorted(EXPECTED_IDS - emitted)}'


def test_every_check_id_is_documented():
    """An operator meeting E021 has to be able to look it up.

    Read from the registry rather than from `EXPECTED_IDS`, which is the set of ids
    the fixtures below *emit*. Some rows cannot be emitted by a `TELEGRAM_BOT` dict at
    all — W010 needs an ephemeral hostname, W011 needs `DATABASE_ROUTERS` — so an id
    added without touching that set was documented only by whoever remembered to.
    """
    missing = sorted({check.code for check in CHECKS} - documented_ids())
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


@override_settings(
    TELEGRAM_BOT={
        'BLPOP_TIMEOUT': 30,
        'HEARTBEAT_INTERVAL': 10,
        'REDIS_TIMEOUT': 60,
        'TOKEN': '1:x',
        'REDIS_URL': 'redis://x',
    }
)
def test_a_pop_capped_by_the_heartbeat_is_reported_and_names_it():
    """The configuration W004 used to be silent on.

    The consumer caps the pop at `min(BLPOP_TIMEOUT, HEARTBEAT_INTERVAL, REDIS_TIMEOUT
    - 1)`, so this waits ten seconds while the setting says thirty. Comparing against
    the read deadline alone — 60 here — said nothing, and the operator went on
    believing the thirty took.
    """
    reported = [message for message in check_settings() if str(message.id).endswith('W004')]

    assert reported, 'a pop capped at a third of its setting was not reported'
    assert 'caps at 10' in reported[0].msg, reported[0].msg
    assert 'HEARTBEAT_INTERVAL' in (reported[0].hint or ''), reported[0].hint


@override_settings(
    TELEGRAM_BOT={
        'BLPOP_TIMEOUT': 30,
        'HEARTBEAT_INTERVAL': 60,
        'REDIS_TIMEOUT': 10,
        'TOKEN': '1:x',
        'REDIS_URL': 'redis://x',
    }
)
def test_a_pop_capped_by_the_read_deadline_names_that_instead():
    """The other binding term, because a hint that always says the same thing is a
    hint that sends half its readers to the wrong setting."""
    reported = [message for message in check_settings() if str(message.id).endswith('W004')]

    assert reported, 'a pop outside the read deadline was not reported'
    assert 'caps at 9' in reported[0].msg, reported[0].msg
    assert 'REDIS_TIMEOUT' in (reported[0].hint or ''), reported[0].hint


@override_settings(
    TELEGRAM_BOT={
        'BLPOP_TIMEOUT': 30,
        'HEARTBEAT_INTERVAL': 9,
        'REDIS_TIMEOUT': 10,
        'TOKEN': '1:x',
        'REDIS_URL': 'redis://x',
    }
)
def test_a_tie_between_the_two_limits_names_both():
    """A hint that names one of two tied limits sends the operator on a round trip.

    `HEARTBEAT_INTERVAL` at 9 and `REDIS_TIMEOUT` at 10 both produce a ceiling of 9.
    Raise the heartbeat alone and `REDIS_TIMEOUT - 1` still caps at 9, so the warning
    comes back unchanged — which is the same defect this whole rule was fixed for, one
    level down.
    """
    reported = [message for message in check_settings() if str(message.id).endswith('W004')]

    assert reported, 'a tied cap was not reported at all'
    assert 'caps at 9' in reported[0].msg, reported[0].msg
    hint = reported[0].hint or ''
    assert 'HEARTBEAT_INTERVAL' in hint, hint
    assert 'REDIS_TIMEOUT' in hint, hint
    assert 'both have to move' in hint, hint


@override_settings(
    TELEGRAM_BOT={
        'BLPOP_TIMEOUT': 10,
        'HEARTBEAT_INTERVAL': 10,
        'REDIS_TIMEOUT': 60,
        'TOKEN': '1:x',
        'REDIS_URL': 'redis://x',
    }
)
def test_a_pop_exactly_at_the_cap_is_not_reported():
    """Equal is not over. The consumer runs it at ten, which is what was asked for, so
    warning here would be the "fires on a working install" defect in miniature."""
    assert [message for message in check_settings() if str(message.id).endswith('W004')] == []


ROUTED_LOG = {'EVENT_LOG': True, 'EVENT_LOG_DATABASE': 'events', 'TOKEN': '1:x', 'REDIS_URL': 'redis://x'}


def routing_warnings():
    """The W011 messages the current settings produce."""
    return [message for message in check_settings() if str(message.id).endswith('W011')]


@override_settings(TELEGRAM_BOT=ROUTED_LOG, DATABASE_ROUTERS=[])
def test_a_log_database_nothing_routes_to_is_reported():
    """E040, E041 and W005 all pass on this, and `migrate` still never creates the table.

    The alias names where the rows belong; the router is what puts them there. Set the
    first and forget the second and every existing check is satisfied while the writer
    logs `no such table` once per batch for ever.
    """
    reported = routing_warnings()

    assert reported, 'a log pointed at an unrouted alias was not reported'
    assert 'nothing routes this app there' in reported[0].msg
    assert 'TelegramEventLogRouter' in (reported[0].hint or '')


@override_settings(
    TELEGRAM_BOT=ROUTED_LOG,
    DATABASE_ROUTERS=['django_redis_aiogram.dbrouter.TelegramEventLogRouter'],
)
def test_a_dotted_path_router_satisfies_it():
    """The spelling Django's own documentation uses."""
    assert routing_warnings() == []


@override_settings(TELEGRAM_BOT=ROUTED_LOG, DATABASE_ROUTERS=[TelegramEventLogRouter()])
def test_an_instance_router_satisfies_it_too():
    """`DATABASE_ROUTERS` takes instances as well, and a project mixing the two — a path
    for ours, an instance for its own — is exactly what a string comparison gets wrong."""
    assert routing_warnings() == []


@override_settings(TELEGRAM_BOT={'EVENT_LOG': True, 'TOKEN': '1:x', 'REDIS_URL': 'redis://x'}, DATABASE_ROUTERS=[])
def test_a_log_on_the_default_database_needs_no_router():
    """Nothing was pointed anywhere, so nothing needs routing — and warning here would
    be the "fires on a working install" defect this whole issue is about."""
    assert routing_warnings() == []


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


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost'})
def test_a_container_that_forgot_its_hostname_is_warned_about(monkeypatch):
    """The in-flight list is keyed on the worker's name.

    Docker invents a twelve-character hex hostname when a container is started
    without `hostname:`, so replacing one strands whatever the last one was
    sending, somewhere nothing will look for it again.
    """
    monkeypatch.setenv('HOSTNAME', 'ba333cb79e00')

    assert 'django_redis_aiogram.W010' in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost'})
def test_a_fixed_hostname_is_not_warned_about(monkeypatch):
    """An unset WORKER_NAME is the documented default and correct almost
    everywhere; warning about it as such would fire on every install."""
    monkeypatch.setenv('HOSTNAME', 'bot-worker-1')

    assert 'django_redis_aiogram.W010' not in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost', 'WORKER_NAME': '   '})
def test_a_padded_name_is_judged_the_way_the_worker_judges_it(monkeypatch):
    """`worker_identity()` takes any truthy value, so a padded name *is* the name.

    A check that stripped first would call this empty, look at the hostname, and
    warn about a name the worker never uses. Poor as that name is, it is stable,
    and stability is the only thing W010 is about.
    """
    monkeypatch.setenv('HOSTNAME', 'ba333cb79e00')

    assert worker_identity() == '   ', 'the runtime stopped taking a padded name'
    assert 'django_redis_aiogram.W010' not in ids(check_settings())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://localhost', 'WORKER_NAME': 'bot-1'})
def test_a_named_worker_is_not_warned_about(monkeypatch):
    monkeypatch.setenv('HOSTNAME', 'ba333cb79e00')

    assert 'django_redis_aiogram.W010' not in ids(check_settings())
