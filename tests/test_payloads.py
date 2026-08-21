"""What reaches a row, and what is kept out of one.

This module is the only thing between a call's arguments and a database column,
so both halves matter: what it keeps has to be readable, and what it drops has
to stay dropped.
"""

import datetime
import json
from decimal import Decimal
from enum import Enum

import pytest
from django.test import override_settings

from django_redis_aiogram.payloads import (
    MAX_DEPTH,
    MAX_ITEMS,
    MAX_KEYS,
    MAX_STRING,
    bounded,
    describe,
    redact_keys,
    redact_text,
    redact_values,
    summarize,
)

TOKEN = '123456789:AAFakeTokenThatLooksExactlyLikeARealOne'
SETTINGS = {'TOKEN': TOKEN, 'WEBHOOK_SECRET': 'hunter2'}


class Colour(str, Enum):
    """A value that has to arrive as its value, not its qualified name."""

    RED = 'red'


class NotSerializable:
    """Stands in for an aiogram model or an input file."""


def test_bytes_become_a_size_not_a_body():
    """A BufferedInputFile would otherwise arrive as megabytes of base64."""
    assert summarize(b'x' * 5000, bodies=True) == {'__omitted__': 'bytes', 'size': 5000}


def test_text_is_a_length_unless_bodies_are_asked_for():
    assert summarize('hello', bodies=False) == {'__omitted__': 'text', 'length': 5}
    assert summarize('hello', bodies=True) == 'hello'


def test_a_long_body_is_cut_rather_than_stored_whole():
    kept = summarize('x' * (MAX_STRING + 100), bodies=True)

    assert len(kept) == MAX_STRING + 1  # the ellipsis
    assert kept.endswith('…')


def test_scalars_survive_unchanged():
    assert summarize({'n': 1, 'f': 1.5, 'b': True, 'none': None}, bodies=True) == {
        'n': 1,
        'f': 1.5,
        'b': True,
        'none': None,
    }


def test_an_enum_arrives_as_its_value():
    """A (str, Enum) member formats as its qualified name on newer Pythons."""
    assert summarize(Colour.RED, bodies=True) == 'red'


def test_dates_and_decimals_are_readable():
    assert summarize(datetime.date(2026, 8, 9), bodies=True) == '2026-08-09'
    assert summarize(Decimal('1.50'), bodies=True) == '1.50'


def test_an_unknown_object_arrives_as_its_class_name():
    """Duck-typed rather than isinstance-checked, so this module never imports
    aiogram and stays usable from the delivery thread."""
    assert summarize(NotSerializable(), bodies=True) == {'__omitted__': 'NotSerializable'}


def test_recursion_stops_at_the_depth_cap():
    deep = current = {}
    for _ in range(MAX_DEPTH + 5):
        current['next'] = {}
        current = current['next']

    rendered = summarize(deep, bodies=True)

    flattened = str(rendered)
    assert "'depth'" in flattened


def test_wide_structures_are_cut():
    wide = {str(index): index for index in range(MAX_KEYS + 20)}
    long_list = list(range(MAX_ITEMS + 20))

    assert len(summarize(wide, bodies=True)) == MAX_KEYS
    assert len(summarize(long_list, bodies=True)) == MAX_ITEMS


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_configured_token_is_removed():
    assert TOKEN not in redact_text(f'POST /bot{TOKEN}/sendMessage')


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_webhook_secret_is_removed():
    assert 'hunter2' not in redact_text('header said hunter2')


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_token_shaped_string_is_removed_even_if_it_is_not_ours():
    other = '987654321:BBSomeOtherBotsTokenEntirelyDifferent'

    assert other not in redact_text(f'refused for {other}')


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_redaction_keeps_the_part_worth_reading():
    cleaned = redact_text(f'ClientError: POST https://api.telegram.org/bot{TOKEN}/sendMessage failed')

    assert 'sendMessage failed' in cleaned


@override_settings(TELEGRAM_BOT={'EVENT_LOG_REDACT_KEYS': ('mine',)})
def test_the_key_list_is_configurable():
    """Naming your own keys replaces the defaults rather than adding to them,
    which is what the setting means and what the docs say."""
    keys = redact_keys()

    assert keys == frozenset({'mine'})
    assert redact_values({'mine': 'x', 'token': 'y'}, keys) == {'mine': '***', 'token': 'y'}


@override_settings(TELEGRAM_BOT={'EVENT_LOG_REDACT_KEYS': 'token'})
def test_a_string_key_list_is_refused_rather_than_read_per_character():
    """'token' as a string would otherwise redact keys named t, o, k, e and n."""
    assert redact_keys() == frozenset()


def test_redaction_reaches_every_depth():
    keys = frozenset({'secret'})
    payload = {'a': [{'secret': 'x'}], 'b': {'c': {'secret': 'y'}}}

    assert redact_values(payload, keys) == {'a': [{'secret': '***'}], 'b': {'c': {'secret': '***'}}}


@override_settings(TELEGRAM_BOT={'EVENT_LOG_MAX_PAYLOAD_BYTES': 0})
def test_a_zero_cap_stores_no_payload_at_all():
    assert bounded({'text': 'anything'}) == {}


@override_settings(TELEGRAM_BOT={'EVENT_LOG_MAX_PAYLOAD_BYTES': 60})
def test_an_oversized_payload_becomes_a_preview_not_half_a_document():
    """Half a JSON document is not JSON, and Oracle and SQLite validate the
    column, so the overflow has to be a string rather than a truncated object."""
    capped = bounded({'text': 'x' * 500})

    assert capped['__truncated__'] is True
    assert capped['size'] > 60
    assert isinstance(capped['preview'], str)


@pytest.mark.parametrize('cap', [40, 60, 120, 8192])
def test_the_overflow_marker_obeys_the_cap_it_reports(cap):
    """The marker is not free: its own keys, the size and JSON's quoting cost
    bytes, and a preview counted in characters can cost four each. A cap the
    overflow ignores is a column the operator sized wrong."""
    with override_settings(TELEGRAM_BOT={'EVENT_LOG_MAX_PAYLOAD_BYTES': cap}):
        capped = bounded({'text': 'ю' * 4000})

    assert len(json.dumps(capped, ensure_ascii=False).encode('utf-8')) <= cap


@override_settings(TELEGRAM_BOT={'EVENT_LOG_MAX_PAYLOAD_BYTES': 8192})
def test_a_payload_that_cannot_be_serialized_says_so():
    """The net under everything else.

    `default=str` renders almost anything, so this branch needs a structure
    json refuses outright — a cycle. `describe` cannot produce one, because
    `summarize` cuts at MAX_DEPTH first; `bounded` is reachable on its own.
    """
    circular: dict[str, object] = {}
    circular['self'] = circular

    assert bounded(circular) == {'__omitted__': 'unserializable'}


@override_settings(TELEGRAM_BOT={'EVENT_LOG_MAX_PAYLOAD_BYTES': 8192})
def test_an_unknown_object_never_reaches_the_serializer_through_describe():
    """summarize renders it by class name first, so a row holds a readable
    marker rather than an object's repr with a memory address in it."""
    described = describe({'value': NotSerializable()})

    assert described == {'value': {'__omitted__': 'NotSerializable'}}


@override_settings(TELEGRAM_BOT={'EVENT_LOG_PAYLOAD': 'none'})
def test_the_none_level_stores_nothing():
    assert describe({'chat_id': 1, 'text': 'hello'}) == {}


@override_settings(TELEGRAM_BOT={'EVENT_LOG_PAYLOAD': 'full', 'TOKEN': TOKEN})
def test_describe_summarizes_then_redacts_then_caps():
    """The order is load-bearing: redaction runs over the summarized structure
    so it never walks an aiogram model, and before the cap so a truncated
    preview cannot end halfway through a credential."""
    described = describe({'text': f'url is /bot{TOKEN}/x', 'token': 'anything'})

    assert TOKEN not in described['text']
    assert described['token'] == '***'


@override_settings(TELEGRAM_BOT={'EVENT_LOG_PAYLOAD': 'nonsense'})
def test_an_unreadable_level_falls_back_to_the_safe_one():
    """E033 reports it at boot; at runtime the quiet answer must not be the one
    that starts storing message bodies."""
    described = describe({'text': 'a secret plan'})

    assert described['text'] == {'__omitted__': 'text', 'length': len('a secret plan')}


def test_describe_never_raises(monkeypatch):
    """A log that can break a send is worse than no log.

    The failure is injected rather than staged: `summarize` reads a class name
    and never calls `__repr__`, so a hostile object walks straight through and
    a test built on one passes with the whole try/except deleted.
    """

    def explode(*_args, **_kwargs):
        msg = 'the summarizer itself broke'
        raise RuntimeError(msg)

    monkeypatch.setattr('django_redis_aiogram.payloads.summarize', explode)

    assert describe({'value': 'anything'}) == {'__omitted__': 'undescribable'}


@pytest.mark.parametrize('level', ['none', 'summary', 'full'])
def test_every_documented_level_is_accepted(level):
    with override_settings(TELEGRAM_BOT={'EVENT_LOG_PAYLOAD': level}):
        assert isinstance(describe({'chat_id': 1}), dict)


@override_settings(TELEGRAM_BOT={'TOKEN': '424242:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'WEBHOOK_SECRET': 'hunter2'})
@pytest.mark.parametrize(
    'text',
    [
        'failed for https://api.telegram.org/bot424242:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/sendMessage',
        '424242:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'a second bot: 999999:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'hunter2',
        'the secret is hunter2 and the token is 424242:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    ],
)
def test_the_colon_prefilter_never_lets_a_credential_through(text):
    """`redact_text` skips the regex for a string with no colon.

    Every token Telegram issues has one, so the shortcut is exact — but it is the
    kind of shortcut that is only safe until someone widens the pattern, and the
    thing it guards is the token reaching a database row.
    """
    cleaned = redact_text(text)

    assert '424242:' not in cleaned
    assert 'hunter2' not in cleaned
    assert 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' not in cleaned


@override_settings(TELEGRAM_BOT={'TOKEN': '424242:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'})
def test_the_settings_are_read_once_for_a_whole_payload(monkeypatch):
    """They were read for every string, at every depth, of every event."""
    from django_redis_aiogram import payloads

    reads = []
    original = payloads.conf.get

    def counting(key, *args):
        reads.append(key)
        return original(key, *args)

    monkeypatch.setattr(payloads.conf, 'get', counting)
    nested = {'a': ['one', 'two', {'b': 'three', 'c': ['four', 'five']}], 'd': 'six'}

    payloads.redact_values(nested, frozenset())

    assert reads.count('TOKEN') == 1, reads


@override_settings(TELEGRAM_BOT={'TOKEN': '424242:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'})
def test_a_string_without_a_colon_never_reaches_the_token_regex(monkeypatch):
    """The prefilter is the optimisation; the test above is only its safety net.

    Those assertions pass with the prefilter deleted, because deleting it makes
    the regex run on everything and the output is identical. This is the one that
    fails when the optimisation is reverted.
    """
    from django_redis_aiogram import payloads

    scanned = []
    real = payloads._TOKEN_RE

    class Spy:
        def sub(self, replacement, text):
            scanned.append(text)
            return real.sub(replacement, text)

    monkeypatch.setattr(payloads, '_TOKEN_RE', Spy())

    payloads.redact_text('an ordinary message with no colon in it')
    assert scanned == [], scanned

    # and it still reaches the regex when it could possibly match
    payloads.redact_text('a second bot: 999999:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')
    assert len(scanned) == 1, scanned
