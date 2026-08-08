"""Named constants for the strings this package treats as data.

Every value here is frozen: queued payloads carry serialization tags and user
settings carry delivery, serializer, storage and mode names, so changing a value
would break in-flight messages and every deployment's ``TELEGRAM_BOT`` block.
The classes subclass ``str`` so that a member is interchangeable with the string
it names, which is what keeps existing settings and payloads readable as-is.
"""

from enum import Enum, unique


@unique
class DeliveryKind(str, Enum):
    """How the consumer learns that a message is waiting."""

    BLPOP = 'blpop'


@unique
class SerializerKind(str, Enum):
    """Which encoding writes and reads the queue."""

    JSON = 'json'
    PICKLE = 'pickle'


@unique
class StorageKind(str, Enum):
    """Built-in aiogram FSM storage backends."""

    REDIS = 'redis'
    MEMORY = 'memory'


@unique
class UpdateMode(str, Enum):
    """Where Telegram updates come from."""

    POLLING = 'polling'
    WEBHOOK = 'webhook'


@unique
class SerializationTag(str, Enum):
    """Keys that mark a decoded JSON object as something richer than a mapping."""

    MODEL = '__model__'
    DEFAULT = '__default__'
    DATETIME = '__datetime__'
    DATE = '__date__'
    DECIMAL = '__decimal__'
    BYTES = '__bytes__'
    INPUT_FILE = '__input_file__'


@unique
class RateLimitKey(str, Enum):
    """Budget names inside the ``RATE_LIMIT`` setting."""

    OVERALL_PER_SECOND = 'overall_per_second'
    PER_CHAT_PER_SECOND = 'per_chat_per_second'
    GROUP_PER_MINUTE = 'group_per_minute'


@unique
class EventKind(str, Enum):
    """What one row of the event log records.

    Namespaced by direction and dotted, so a project registering its own kinds
    has an obvious convention to follow. These land in a database column and in
    saved admin filters, which is why the values are frozen like the rest.
    """

    OUTBOUND_QUEUED = 'outbound.queued'
    OUTBOUND_CONSUMED = 'outbound.consumed'
    OUTBOUND_SENT = 'outbound.sent'
    OUTBOUND_RETRIED = 'outbound.retried'
    OUTBOUND_FAILED = 'outbound.failed'
    OUTBOUND_DROPPED = 'outbound.dropped'
    INBOUND_RECEIVED = 'inbound.received'
    INBOUND_HANDLED = 'inbound.handled'
    INBOUND_FAILED = 'inbound.failed'
    FSM_TRANSITION = 'fsm.transition'
    QUEUE_UNDECODABLE = 'queue.undecodable'
    QUEUE_REJECTED = 'queue.rejected'
    LOG_DROPPED = 'log.dropped'


@unique
class PayloadDetail(str, Enum):
    """How much of a call's arguments the event log keeps."""

    NONE = 'none'
    SUMMARY = 'summary'
    FULL = 'full'


def choices(kind: type[Enum]) -> frozenset[str]:
    """Return the values of ``kind`` as a frozenset, for membership checks."""
    return frozenset(member.value for member in kind)
