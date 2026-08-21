"""Turning call arguments into something safe to keep in a row.

Deliberately lossy, which is why it is not :func:`~django_redis_aiogram.serializers.encode`.
That one is lossless by contract: it base64s a whole ``BufferedInputFile``, so a
photo send would arrive in the table as megabytes of base64.

Order is fixed and load-bearing: summarize, then redact, then cap. Redaction runs
over the summarized structure so it never walks an aiogram model, and before the
cap so a truncated preview cannot end halfway through a credential.

No aiogram import: unknown values are rendered by class name through duck typing,
which keeps this usable from the delivery thread as well.
"""

import datetime
import json
import logging
import re
from decimal import Decimal
from enum import Enum
from typing import Any

from django_redis_aiogram.enums import PayloadDetail
from django_redis_aiogram.settings import conf

logger = logging.getLogger('django_redis_aiogram')

#: how deep to walk before giving up
MAX_DEPTH = 6
#: how many characters of a string to keep under 'full'
MAX_STRING = 2000
MAX_KEYS = 50
MAX_ITEMS = 50

_OMITTED = '__omitted__'
_REDACTED = '***'
#: <bot id>:<35 base64url characters>, the shape Telegram issues
_TOKEN_RE = re.compile(r'\b\d{5,}:[A-Za-z0-9_-]{30,}\b')


def detail_level() -> PayloadDetail:
    """How much of a call's arguments to keep, defaulting to the safe answer."""
    try:
        return PayloadDetail(str(conf['EVENT_LOG_PAYLOAD']))
    except ValueError:
        # E033 reports this at boot; at runtime the quiet answer is the safe one
        return PayloadDetail.SUMMARY


class _Unhandled:
    """Says a value is not a scalar, without colliding with None as a value."""


_UNHANDLED = _Unhandled()


def _scalar(value: Any, *, bodies: bool) -> Any:  # noqa: ANN401 - a call argument is anything
    """Render the values that need no recursion, or report that this is not one."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_OMITTED: 'bytes', 'size': len(value)}
    if isinstance(value, str):
        if not bodies:
            return {_OMITTED: 'text', 'length': len(value)}
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + '…'
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, Decimal)):
        return str(value)
    return _UNHANDLED


def summarize(value: Any, *, bodies: bool, depth: int = 0) -> Any:  # noqa: ANN401 - as above
    """Render a call argument for the log: readable, bounded, never a file."""
    if depth > MAX_DEPTH:
        return {_OMITTED: 'depth'}
    scalar = _scalar(value, bodies=bodies)
    if scalar is not _UNHANDLED:
        return scalar
    if isinstance(value, Enum):
        return summarize(value.value, bodies=bodies, depth=depth)
    if isinstance(value, dict):
        pairs = list(value.items())[:MAX_KEYS]
        return {str(key): summarize(item, bodies=bodies, depth=depth + 1) for key, item in pairs}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [summarize(item, bodies=bodies, depth=depth + 1) for item in list(value)[:MAX_ITEMS]]
    # aiogram models and input files land here; the class name is what a reader wants
    return {_OMITTED: type(value).__name__}


def secrets() -> tuple[str, ...]:
    """Return the configured credentials, once per walk rather than once per string."""
    found = (str(conf.get(name) or '').strip() for name in ('TOKEN', 'WEBHOOK_SECRET'))
    return tuple(secret for secret in found if secret)


def redact_text(text: str, configured: tuple[str, ...] | None = None) -> str:
    """Strip credentials from anything that came out of an exception.

    This is not paranoia: the token is in the API URL, aiogram and aiohttp put
    that URL in their error messages, and those messages are what an ``error``
    column holds.

    ``configured`` is threaded down by :func:`redact_values` so the settings are
    read once for a whole payload instead of once per string at every depth.
    Resolving them here was half the cost of this function.
    """
    for secret in secrets() if configured is None else configured:
        text = text.replace(secret, _REDACTED)
    # every token has a colon in it, so a string without one cannot match and does
    # not need the regex walked over it. Revisit if the pattern ever widens
    if ':' not in text:
        return text
    # then anything token-shaped: a second bot's token is just as bad in a row
    return _TOKEN_RE.sub(_REDACTED, text)


def redact_values(
    value: Any,  # noqa: ANN401 - walks whatever summarize produced
    keys: frozenset[str],
    configured: tuple[str, ...] | None = None,
) -> Any:  # noqa: ANN401 - as above
    """Blank out values under credential-named keys, at any depth."""
    if configured is None:
        configured = secrets()
    if isinstance(value, dict):
        return {
            key: _REDACTED if str(key).lower() in keys else redact_values(item, keys, configured)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_values(item, keys, configured) for item in value]
    if isinstance(value, str):
        return redact_text(value, configured)
    return value


def redact_keys() -> frozenset[str]:
    """Return the configured key names, lowercased for comparison."""
    configured = conf['EVENT_LOG_REDACT_KEYS'] or ()
    if isinstance(configured, (str, bytes)):
        return frozenset()  # E035 reports the shape; reading it per character would be worse
    return frozenset(str(name).lower() for name in configured)


def bounded(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep a payload under the configured byte cap, whatever it holds."""
    try:
        cap = int(conf['EVENT_LOG_MAX_PAYLOAD_BYTES'])
    except (TypeError, ValueError):
        return {}  # E034 reports it; a row is not the place to argue
    if cap <= 0:
        return {}
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return {_OMITTED: 'unserializable'}
    if len(text.encode('utf-8')) <= cap:
        return payload
    # a preview string, not a truncated object: half a JSON document is not
    # JSON, and Oracle and SQLite both validate the column
    return _overflow(text, cap)


#: below this many characters the preview shrinks one at a time, not by halves
_FINE_TUNE_BELOW = 8


def _overflow(text: str, cap: int) -> dict[str, Any]:
    """Describe what did not fit, in a form that itself fits.

    The marker is not free: its keys, the size and JSON's own quoting all cost
    bytes, and a preview counted in characters can cost four bytes each. The
    cap is a promise about the column, so the preview shrinks until the whole
    object honours it — and the marker is dropped entirely if even that cannot.
    """
    kept = cap // 2
    while kept >= 0:
        marker: dict[str, Any] = {'__truncated__': True, 'size': len(text), 'preview': text[:kept]}
        if len(json.dumps(marker, ensure_ascii=False).encode('utf-8')) <= cap:
            return marker
        kept = kept // 2 if kept > _FINE_TUNE_BELOW else kept - 1
    return {}


def describe(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Summarize, redact and cap one call's arguments. Never raises."""
    try:
        level = detail_level()
        if level is PayloadDetail.NONE:
            return {}
        summary = summarize(kwargs, bodies=level is PayloadDetail.FULL)
        return bounded(redact_values(summary, redact_keys()))
    except Exception:
        logger.exception('could not describe a payload for the event log')
        return {_OMITTED: 'undescribable'}
