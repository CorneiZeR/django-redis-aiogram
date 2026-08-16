"""Turning recorded events into rows, on the one thread allowed to do it.

This is the only module in the package that touches the ORM, and it is imported
from inside the writer thread rather than at module scope — that is what keeps
``recorder`` free of ``django.db``, and therefore keeps ``record()`` legal from
a coroutine and free in a process with the log switched off.
"""

import datetime
import logging
from collections.abc import Sequence

from django.conf import settings as django_settings
from django.db import (
    DEFAULT_DB_ALIAS,
    DatabaseError,
    InterfaceError,
    OperationalError,
    connections,
    transaction,
)
from django.utils import timezone

from django_redis_aiogram.dbrouter import event_log_database
from django_redis_aiogram.exceptions import DjangoRedisAiogramError
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.payloads import redact_keys, redact_text, redact_values, secrets
from django_redis_aiogram.recorder import Event

logger = logging.getLogger('django_redis_aiogram')

#: below this a failed batch is retried row by row rather than bisected
ROW_BY_ROW = 32


class EventLogRefusedError(DjangoRedisAiogramError):
    """Every row of a batch was refused.

    Not a `DatabaseError`: it is this module's verdict on one, and the difference
    matters because the recorder treats it as a failed flush rather than as
    something to bisect further. It never escapes that flush.
    """

    def __init__(self, count: int) -> None:
        """Name how many rows were lost, which is what the log line reports."""
        super().__init__(f'the database refused all {count} rows of this batch')


def log_alias() -> str:
    """Return the alias rows are written to and read from."""
    return event_log_database() or DEFAULT_DB_ALIAS


def _moment(stamp: float) -> datetime.datetime:
    """Return the recorded instant, in whichever flavour of datetime this project stores."""
    moment = datetime.datetime.fromtimestamp(stamp, tz=datetime.timezone.utc)
    return moment if django_settings.USE_TZ else timezone.make_naive(moment)


def _text(value: object, length: int) -> str:
    """Cut a value to its column's width, without the NULs PostgreSQL refuses."""
    if value is None:
        return ''
    return str(value).replace('\x00', '')[:length]


def to_row(
    event: Event,
    keys: frozenset[str] | None = None,
    configured: tuple[str, ...] | None = None,
) -> TelegramEvent:
    """Build the unsaved row for one event, sanitised so it cannot poison a batch.

    Redaction happens here as well as at the producer. This is the boundary rows
    cross, and the rule is that the token must not reach one: a caller that
    builds an Event by hand, or a new seam that forgets, would otherwise put an
    aiogram error message — which carries the API URL, which carries the token —
    straight into a column.
    """
    if keys is None:
        keys = redact_keys()
    if configured is None:
        configured = secrets()
    return TelegramEvent(
        created_at=_moment(event.created_at),
        correlation_id=event.correlation_id,
        kind=_text(event.kind, 48),
        function=_text(event.function, 64),
        chat_id=event.chat_id,
        user_id=event.user_id,
        message_id=event.message_id,
        update_id=event.update_id,
        worker=_text(event.worker, 128),
        attempt=max(0, event.attempt),
        duration_ms=event.duration_ms,
        error_code=_text(event.error_code, 64),
        error=_text(redact_text(str(event.error or ''), configured), 20000),
        detail=redact_values(event.detail or {}, keys, configured),
    )


def write_batch(events: Sequence[Event]) -> None:
    """Insert one batch, recycling a connection the database has since dropped."""
    alias = log_alias()
    _recycle(alias)
    # resolved once for the batch: both walk the settings, and a batch is 200 rows
    keys, configured = redact_keys(), secrets()
    rows = [to_row(event, keys, configured) for event in events]
    manager = TelegramEvent.objects.using(alias)
    try:
        # the savepoint is what keeps a failed log write from taking the
        # caller's data with it: under EVENT_LOG_SYNC this runs on the caller's
        # thread, inside whatever atomic() block the caller opened
        with transaction.atomic(using=alias):
            manager.bulk_create(rows)
    except (OperationalError, InterfaceError) as error:
        # the connection died between the check above and the insert; one retry
        # on a fresh one is the difference between losing a batch and not
        _recycle(alias, force=True)
        # the retry needs the same net as the first attempt: a fresh connection
        # rejecting one poison row must not cost the whole batch
        _refused(rows, _write_half(rows, alias), error)
    except DatabaseError as error:
        _refused(rows, _write_one_by_one(rows, alias), error)


def _recycle(alias: str, *, force: bool = False) -> None:
    """Discard a connection the database has since dropped, unless we are inside a transaction.

    Before the work, not after: this is what discards a connection whose
    CONN_MAX_AGE expired, that a restart killed, or that a previous error marked
    unusable. Closing afterwards would leave a broken one in place.

    Never while the caller holds a transaction open, though. Under EVENT_LOG_SYNC
    this runs on the caller's thread inside their ``atomic()`` block — and on
    PostgreSQL and MySQL, closing a connection there marks the whole transaction
    for rollback, so recording an event would destroy the writes the caller made
    alongside it. A connection Django is already using is not stale anyway.

    One alias, not all of them. ``close_old_connections()`` walks every initialized
    connection, so with ``EVENT_LOG_DATABASE`` pointing somewhere of its own it
    reaches past the log's connection — which is not in a transaction — and closes
    the caller's ``default`` one, which is. The guard above would then be reading
    the wrong connection's state. The log has no business touching one it never
    writes to.
    """
    connection = connections[alias]
    if connection.in_atomic_block:
        return
    if force:
        connection.close()
        return
    connection.close_if_unusable_or_obsolete()


def _refused(rows: list[TelegramEvent], written: int, error: Exception) -> None:
    """Raise when a batch reached the database and none of it landed.

    The bisecting ladder below catches ``DatabaseError`` at every rung, so a
    database that refuses everything — no table, no permission, no disk — used to
    end in a normal return. The recorder read that as success: its failure counter
    never moved, so the backoff never engaged and no ``log.dropped`` row was ever
    written. It hammered the database once per flush interval, for ever.
    """
    if rows and not written:
        raise EventLogRefusedError(len(rows)) from error


def _write_half(rows: list[TelegramEvent], alias: str) -> int:
    """Insert one half of a bisected batch, splitting it again if it still fails."""
    try:
        with transaction.atomic(using=alias):
            TelegramEvent.objects.using(alias).bulk_create(rows)
    except DatabaseError:
        return _write_one_by_one(rows, alias)
    return len(rows)


def _write_row(row: TelegramEvent, alias: str) -> bool:
    """Insert one row, dropping it if the database refuses it."""
    try:
        # the savepoint is not optional: on PostgreSQL a failed statement aborts
        # the transaction, so one bad row would take every later one with it
        with transaction.atomic(using=alias):
            row.save(force_insert=True, using=alias)
    except DatabaseError:
        logger.exception('dropping an event the database refused', extra={'tg_kind': row.kind})
        return False
    return True


def _write_one_by_one(rows: list[TelegramEvent], alias: str) -> int:
    """Save rows individually, dropping only the ones the database refuses."""
    if len(rows) > ROW_BY_ROW:
        # bisect first, so a 200-row batch does not become 200 statements
        middle = len(rows) // 2
        return _write_half(rows[:middle], alias) + _write_half(rows[middle:], alias)
    return sum(_write_row(row, alias) for row in rows)


def close_connections() -> None:
    """Release the calling thread's connections; nothing else ever will."""
    try:
        connections.close_all()
    except Exception:
        logger.exception('could not close the event writer connection')
