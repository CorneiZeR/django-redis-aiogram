"""The registry of event kinds, and the identifier that ties a message together.

``kind`` is an unconstrained ``CharField``: the set of legal values lives here,
in Python, so registering one is not a schema change. The cost is that nothing
at the database level rejects a typo, which is why the recorder validates
against this registry before a row is built.

Imported by :mod:`django_redis_aiogram.models`, so it must stay free of aiogram
and of anything that reads Django settings at import time.
"""

import os
import socket
import time
import uuid
from dataclasses import dataclass

from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.settings import conf

#: the width of the model's ``kind`` column; a longer code would be truncated by
#: MySQL in non-strict mode and rejected in strict mode, so it is refused here
MAX_KIND_LENGTH = 48


@dataclass(frozen=True)
class EventKindSpec:
    """One registered kind: the stored code, its label, and whether it is bad news."""

    code: str
    label: str
    failure: bool = False


_KINDS: dict[str, EventKindSpec] = {}


def register_kind(code: str, label: str, *, failure: bool = False) -> str:
    """Register an event kind, and return its code.

    Adding a kind is not a schema change. Namespace your own as
    ``<app>.<noun>.<verb>`` and keep the total in the tens: ``kind`` is the
    leading column of an index, and it stays worth having only while its
    cardinality is low.
    """
    if len(code) > MAX_KIND_LENGTH:
        msg = f'Event kind {code!r} is longer than {MAX_KIND_LENGTH} characters.'
        raise ValueError(msg)
    spec = EventKindSpec(code, label, failure=failure)
    existing = _KINDS.get(code)
    if existing is not None and existing != spec:
        msg = f'Event kind {code!r} is already registered as {existing.label!r}.'
        raise ValueError(msg)
    _KINDS[code] = spec
    return code


def known_kinds() -> frozenset[str]:
    """Every registered code, for membership checks."""
    return frozenset(_KINDS)


def kind_choices() -> list[tuple[str, str]]:
    """Every registered kind as Django choices, for the admin filter.

    Deliberately not passed to the model field: a callable there would have to
    survive ``deconstruct()`` unexpanded, and if it ever did not, registering a
    kind would become a migration every consumer has to run.
    """
    return [(spec.code, spec.label) for spec in _KINDS.values()]


def failure_kinds() -> tuple[str, ...]:
    """Return the kinds that mean something went wrong."""
    return tuple(spec.code for spec in _KINDS.values() if spec.failure)


def new_correlation_id() -> uuid.UUID:
    """Build a time-ordered identifier, so its index appends instead of scattering.

    uuid4 is uniformly random, and inserting random keys into a large B-tree
    touches a different leaf page every time. RFC 9562's version 7 puts a 48-bit
    millisecond prefix first, which sorts the same way on every backend —
    PostgreSQL compares the raw bytes, and the others store lowercase hex.
    """
    generate = getattr(uuid, 'uuid7', None)
    if generate is not None:  # pragma: no cover - 3.14 and newer
        return generate()  # type: ignore[no-any-return]
    raw = bytearray(int(time.time() * 1000).to_bytes(6, 'big') + os.urandom(10))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(raw))


def worker_identity() -> str:
    """Name this process, for the in-flight list and for the rows it records.

    Defaults to the hostname, which a container keeps across restarts — that is
    what lets a restarted worker find its own interrupted messages. Set
    WORKER_NAME when several workers share a host.
    """
    configured = conf.get('WORKER_NAME')
    if configured:
        return str(configured)
    return os.environ.get('HOSTNAME') or socket.gethostname()


register_kind(EventKind.OUTBOUND_QUEUED.value, 'Queued')
register_kind(EventKind.OUTBOUND_CONSUMED.value, 'Taken off the queue')
register_kind(EventKind.OUTBOUND_SENT.value, 'Sent')
register_kind(EventKind.OUTBOUND_RETRIED.value, 'Rate limited, retrying', failure=True)
register_kind(EventKind.OUTBOUND_FAILED.value, 'Send failed', failure=True)
register_kind(EventKind.OUTBOUND_DROPPED.value, 'Dropped', failure=True)
register_kind(EventKind.INBOUND_RECEIVED.value, 'Update received')
register_kind(EventKind.INBOUND_HANDLED.value, 'Update handled')
register_kind(EventKind.INBOUND_FAILED.value, 'Handler raised', failure=True)
register_kind(EventKind.FSM_TRANSITION.value, 'FSM transition')
register_kind(EventKind.QUEUE_UNDECODABLE.value, 'Undecodable payload', failure=True)
register_kind(EventKind.QUEUE_REJECTED.value, 'Rejected payload', failure=True)
register_kind(EventKind.LOG_DROPPED.value, 'Events dropped', failure=True)
