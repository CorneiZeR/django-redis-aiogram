"""Credentials must not reach a row, and the boundary is where that is enforced.

The realistic leak is not someone passing the token as an argument. It is that
the token is in the API URL, aiogram and aiohttp put that URL into their
exception messages, and those messages are exactly what an `error` column holds.
"""

import pytest
from django.test import override_settings

from django_redis_aiogram.eventlog import to_row
from django_redis_aiogram.recorder import Event

TOKEN = '123456789:AAFakeTokenThatLooksExactlyLikeARealOne'
SETTINGS = {'EVENT_LOG': True, 'TOKEN': TOKEN, 'WEBHOOK_SECRET': 'hunter2'}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_token_never_reaches_the_error_column():
    """A real aiogram failure carries the request URL, and the URL carries the token."""
    message = f'aiohttp.ClientError: POST https://api.telegram.org/bot{TOKEN}/sendMessage failed'

    row = to_row(Event(kind='outbound.failed', error=message))

    assert TOKEN not in row.error
    assert '***' in row.error
    assert 'sendMessage failed' in row.error, 'redaction ate the part worth reading'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_second_bots_token_is_redacted_too():
    """Shape-matching, not just the configured value: another bot's token in an
    error message is exactly as bad in a row."""
    other = '987654321:BBSomeOtherBotsTokenEntirelyDifferent'

    row = to_row(Event(kind='outbound.failed', error=f'refused for {other}'))

    assert other not in row.error


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_webhook_secret_is_redacted():
    row = to_row(Event(kind='inbound.failed', error='header mismatch: hunter2'))

    assert 'hunter2' not in row.error


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_sensitive_keys_are_blanked_at_any_depth():
    """An Event built by hand, or a seam that forgets to describe() its payload,
    must still not put a credential in the JSON column."""
    row = to_row(
        Event(
            kind='outbound.queued',
            detail={'outer': {'token': TOKEN, 'password': 'letmein', 'text': 'fine'}},
        )
    )

    assert row.detail == {'outer': {'token': '***', 'password': '***', 'text': 'fine'}}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_token_inside_a_payload_string_is_redacted():
    row = to_row(Event(kind='outbound.queued', detail={'note': f'url is /bot{TOKEN}/x'}))

    assert TOKEN not in row.detail['note']


@pytest.mark.parametrize('text', ['', None])
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_an_empty_error_stays_empty(text):
    row = to_row(Event(kind='outbound.sent', error=text or ''))

    assert row.error == ''
