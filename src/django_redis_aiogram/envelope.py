"""What a queued call looks like on the wire.

2.x wrote ``{'function': name, **kwargs}`` and the consumer splatted it straight
back into aiogram, so there was nowhere to put an identifier, a timestamp or a
version without it arriving at Telegram as an unexpected argument. 3.0 nests the
arguments instead.

The tagged-JSON serializer needs no change for this. ``encode`` recurses through
mappings unconditionally, and ``decode`` only reacts to one when a codec tag is a
key in it — ``__envelope__`` is not a tag, so the envelope decodes as a plain
mapping and its ``kwargs`` still become real aiogram objects.

**Deployment order matters because of this.** The reader below accepts the old
flat shape, but a 2.x reader handed a new payload calls the Telegram method with
``__envelope__`` as a keyword, raises ``TypeError``, logs it and swallows it —
the message is lost silently. Deploy the bot container before the web tier.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from django_redis_aiogram.exceptions import DjangoRedisAiogramError

#: marks a payload as nested, and says which shape to read it as
ENVELOPE_KEY = '__envelope__'
ENVELOPE_VERSION = 1


class UnknownEnvelopeVersionError(DjangoRedisAiogramError, ValueError):
    """A payload was written by a newer version than this consumer understands."""

    def __init__(self, version: object) -> None:
        """Name the version found and the newest one this consumer can read."""
        super().__init__(
            f'Queued payload declares envelope version {version!r}, but this consumer '
            f'reads up to {ENVELOPE_VERSION}. Upgrade the bot container first.',
        )


@dataclass(frozen=True)
class Envelope:
    """One queued call, as the consumer needs it."""

    function: str
    kwargs: dict[str, Any]
    correlation_id: uuid.UUID | None = None
    #: a float, not a datetime: it survives both serializers without a codec
    queued_at: float = 0.0


def pack(
    function: str,
    kwargs: dict[str, Any],
    correlation_id: uuid.UUID,
    queued_at: float,
) -> dict[str, Any]:
    """Build the payload that goes on the list."""
    return {
        ENVELOPE_KEY: ENVELOPE_VERSION,
        'correlation_id': correlation_id.hex,
        'queued_at': queued_at,
        'function': function,
        'kwargs': kwargs,
    }


def _as_uuid(value: object) -> uuid.UUID | None:
    """Read the identifier back, tolerating a payload that carries nonsense."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def unpack(payload: dict[str, Any]) -> Envelope:
    """Read either shape.

    A rolling upgrade leaves 2.x payloads on the list for as long as the backlog
    lasts, and refusing them would drop real messages.
    """
    version = payload.get(ENVELOPE_KEY)
    if version is None:
        return Envelope(
            function=str(payload.get('function', '')),
            kwargs={key: value for key, value in payload.items() if key != 'function'},
        )
    try:
        found = int(version)
    except (TypeError, ValueError):
        raise UnknownEnvelopeVersionError(version) from None
    if found > ENVELOPE_VERSION:
        raise UnknownEnvelopeVersionError(found)
    arguments = payload.get('kwargs')
    return Envelope(
        function=str(payload.get('function', '')),
        kwargs=dict(arguments) if isinstance(arguments, dict) else {},
        correlation_id=_as_uuid(payload.get('correlation_id')),
        queued_at=float(payload.get('queued_at') or 0.0),
    )
