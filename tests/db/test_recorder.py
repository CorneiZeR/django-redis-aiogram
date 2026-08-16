"""The buffered writer: what it guarantees, and what it gives up.

Most of these drive `drain_once()` on the calling thread rather than starting
the writer and racing it — the same trick `TokenBucket`'s injectable clock plays
for the rate limiter.
"""

import asyncio
import os
import threading
import time

import pytest
from django.db import DatabaseError, OperationalError, connection, connections, transaction
from django.db.models import QuerySet
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.eventlog import ROW_BY_ROW, EventLogRefusedError, write_batch
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.recorder import FAILURE_LIMIT, Event, EventRecorder

ON = {'EVENT_LOG': True}


def an_event(kind=EventKind.OUTBOUND_SENT.value, **kwargs):
    return Event(kind=kind, **kwargs)


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_recorded_event_reaches_the_table(paused_writer):
    recorder = EventRecorder()
    recorder.record(an_event(function='send_message', chat_id=7))

    assert recorder.drain_once() == 1
    assert TelegramEvent.objects.filter(function='send_message', chat_id=7).count() == 1


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_BUFFER_SIZE': 1})
def test_a_full_buffer_drops_instead_of_blocking(paused_writer, caplog):
    """A send must never wait on the database. Swapping put_nowait for put
    would make this hang rather than fail, which is the point of the bound."""
    recorder = EventRecorder()
    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        recorder.record(an_event(chat_id=1))
        recorder.record(an_event(chat_id=2))

    assert 'falling behind' in caplog.text
    assert recorder.drain_once() == 1, 'the second event should have been dropped, not queued'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_the_gap_is_recorded_in_the_feed_not_only_in_the_log(paused_writer):
    """An append-only feed has to be honest about its own holes: a silent gap
    reads as 'nothing happened'."""
    recorder = EventRecorder()
    recorder._dropped = 3
    recorder.record(an_event(chat_id=1))
    recorder.drain_once()

    gap = TelegramEvent.objects.get(kind=EventKind.LOG_DROPPED.value)
    assert gap.detail == {'dropped': 3}


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_poison_row_costs_only_itself(paused_writer):
    """One value the database refuses must not take the rest of the batch with
    it, which is what the per-row fallback and its savepoint are for."""
    recorder = EventRecorder()
    for chat_id in (1, 2, 3):
        recorder.record(an_event(chat_id=chat_id))

    # QuerySet, not Manager: write_batch goes through objects.using(alias),
    # and patching the manager leaves the real insert in place
    original = QuerySet.bulk_create
    calls = []

    def refuse_the_batch(self, rows, *args, **kwargs):
        calls.append(len(rows))
        if len(calls) == 1:
            msg = 'no'
            raise DatabaseError(msg)
        return original(self, rows, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(QuerySet, 'bulk_create', refuse_the_batch)
        recorder.drain_once()

    assert calls, 'the refusal never fired, so nothing was tested'
    assert TelegramEvent.objects.count() == 3, 'the whole batch was lost over one refusal'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_the_writer_thread_writes_and_stops():
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=11))
    # this recorder's own thread, not any thread by that name: the module-level
    # singleton has one too, and asserting on the name would test that instead
    writer = recorder._thread
    try:
        recorder.flush(timeout=5)
        assert TelegramEvent.objects.filter(chat_id=11).count() == 1
    finally:
        recorder.stop(timeout=5)

    assert writer is not None
    assert not writer.is_alive()


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_SYNC': True})
def test_sync_mode_writes_on_the_calling_thread():
    """Tests that assert on rows inside a transaction need the write to happen
    on their own connection; the writer thread's would not be rolled back."""
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=99))

    assert TelegramEvent.objects.filter(chat_id=99).count() == 1
    assert recorder._queue is None, 'sync mode started a writer anyway'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_KINDS': (EventKind.OUTBOUND_FAILED.value,)})
def test_only_the_named_kinds_are_kept(paused_writer):
    recorder = EventRecorder()
    recorder.record(an_event(EventKind.OUTBOUND_SENT.value))
    recorder.record(an_event(EventKind.OUTBOUND_FAILED.value))
    recorder.drain_once()

    kinds = list(TelegramEvent.objects.values_list('kind', flat=True))
    assert kinds == [EventKind.OUTBOUND_FAILED.value], kinds


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_the_producer_stamps_the_time_not_the_writer(paused_writer):
    """The buffer writes later than the event happened, so auto_now_add would
    record the flush instead."""
    recorder = EventRecorder()
    recorder.record(Event(kind=EventKind.OUTBOUND_SENT.value, created_at=1_700_000_000.0))
    recorder.drain_once()

    stored = TelegramEvent.objects.get()
    assert stored.created_at.timestamp() == pytest.approx(1_700_000_000.0)


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_BUFFER_SIZE': 1})
def test_the_very_first_drop_is_reported(paused_writer, caplog):
    """`_reported_at` starting at 0.0 would swallow it wherever `monotonic()`
    is still below the report interval — which on Linux is time since boot, so
    a freshly started container is exactly where it would go unnoticed."""
    recorder = EventRecorder()

    with caplog.at_level('ERROR', logger='django_redis_aiogram'), pytest.MonkeyPatch.context() as patch:
        # one second since boot: with _reported_at at 0.0 the first drop is
        # inside the report interval and says nothing at all
        patch.setattr('django_redis_aiogram.recorder.time.monotonic', lambda: 1.0)
        recorder.record(an_event(chat_id=1))
        recorder.record(an_event(chat_id=2))

    assert 'falling behind' in caplog.text


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_writer_that_cannot_start_counts_the_event_it_loses(caplog):
    """Otherwise a later, working writer records no gap for the events that
    were dropped while it could not be created."""
    recorder = EventRecorder()

    def refuse_to_start(self):
        msg = "can't start new thread"
        raise RuntimeError(msg)

    with pytest.MonkeyPatch.context() as patch, caplog.at_level('ERROR', logger='django_redis_aiogram'):
        patch.setattr(threading.Thread, 'start', refuse_to_start)
        recorder.record(an_event(chat_id=1))

    assert recorder._dropped == 1, 'the lost event was not counted'
    assert recorder._queue is None, 'a half-built writer was left behind'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_flush_waits_for_the_write_not_for_the_queue():
    """The queue empties when the batch is taken, which is before it is written.
    Polling it would return while the insert was still in flight."""
    started = threading.Event()
    finished = threading.Event()
    original = write_batch

    def slow_write(events):
        started.set()
        time.sleep(0.3)
        original(events)
        finished.set()

    recorder = EventRecorder()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('django_redis_aiogram.eventlog.write_batch', slow_write)
        recorder.record(an_event(chat_id=77))
        try:
            assert started.wait(5), 'the writer never picked the event up'
            recorder.flush(timeout=5)

            assert finished.is_set(), 'flush returned before the write finished'
            assert TelegramEvent.objects.filter(chat_id=77).count() == 1
        finally:
            recorder.stop(timeout=5)


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_BATCH_SIZE': 2})
def test_the_batch_size_is_what_one_insert_carries(paused_writer):
    """Otherwise a batch of one is indistinguishable from a batch of hundreds,
    and the setting is a number nobody has ever exercised.

    Its own recorder, because `_collect` deliberately leaves three events in
    the queue and the shared one would hand them to whatever runs next.
    """
    recorder = EventRecorder()
    for chat_id in range(5):
        recorder.record(an_event(chat_id=chat_id))

    # _collect is what caps a batch; drain_once deliberately takes everything
    batch, _ = recorder._collect(recorder._queue)

    assert len(batch) == 2, f'the batch size was ignored: {len(batch)}'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_batch_the_database_refuses_repeatedly_suspends_rather_than_hammers(paused_writer, caplog):
    """Draining and discarding keeps producers from filling up while the
    database is down, and stops the writer retrying a dead server every second."""
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=1))

    failures = 0
    with pytest.MonkeyPatch.context() as patch, caplog.at_level('ERROR', logger='django_redis_aiogram'):

        def refuse(_batch):
            msg = 'database is down'
            raise RuntimeError(msg)

        patch.setattr(EventRecorder, '_write', staticmethod(refuse))
        for _ in range(FAILURE_LIMIT):
            failures, blocked = recorder._flush([an_event()], failures=failures)

    assert blocked > 0, 'the writer never backed off'
    assert 'suspended after repeated failures' in caplog.text


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_dropped_connection_is_retried_once_on_a_fresh_one(paused_writer):
    """A management command sees none of the request signals that recycle a
    connection, so the first write after a database restart hits a dead handle."""
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=8))

    attempts = []
    original = QuerySet.bulk_create

    def dead_first_time(self, rows, *args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            msg = 'server closed the connection unexpectedly'
            raise OperationalError(msg)
        return original(self, rows, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(QuerySet, 'bulk_create', dead_first_time)
        recorder.drain_once()

    assert len(attempts) == 2, 'the batch was not retried on a fresh connection'
    assert TelegramEvent.objects.filter(chat_id=8).count() == 1


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'WORKER_NAME': 'web-3'})
def test_every_row_says_which_process_recorded_it(paused_writer):
    """The column is documented as "which container recorded it", and before
    this only the consumer filled it — so the rows that say a message actually
    went out named nobody."""
    recorder = EventRecorder()
    recorder.record(an_event())
    recorder.drain_once()

    assert TelegramEvent.objects.get().worker == 'web-3'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'WORKER_NAME': 'web-3'})
def test_a_producer_that_names_itself_keeps_its_name(paused_writer):
    """The consumer records on behalf of the worker it is, so a name already on
    the event is the answer, not something to overwrite."""
    recorder = EventRecorder()
    recorder.record(an_event(worker='bot-1'))
    recorder.drain_once()

    assert TelegramEvent.objects.get().worker == 'bot-1'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_poison_row_on_the_retry_still_costs_only_itself(paused_writer):
    """The retry needs the same net as the first attempt.

    A dropped connection and a row the database refuses can arrive together —
    a restart is exactly when a half-written batch gets retried — and without
    the net the second failure took the whole batch with it.
    """
    recorder = EventRecorder()
    for chat_id in (1, 2, 3):
        recorder.record(an_event(chat_id=chat_id))

    attempts = []
    original = QuerySet.bulk_create

    def dead_then_poisoned(self, rows, *args, **kwargs):
        attempts.append(len(rows))
        if len(attempts) == 1:
            msg = 'server closed the connection unexpectedly'
            raise OperationalError(msg)
        if len(attempts) == 2:
            msg = 'no'
            raise DatabaseError(msg)
        return original(self, rows, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(QuerySet, 'bulk_create', dead_then_poisoned)
        recorder.drain_once()

    assert attempts == [3, 3], f'the retry did not carry the whole batch: {attempts}'
    # three rows is small enough that the fallback saves them individually
    assert TelegramEvent.objects.count() == 3, 'the whole batch was lost over one refusal on the retry'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_SYNC': True})
def test_the_batch_insert_takes_a_savepoint_inside_the_caller_transaction():
    """Synchronous recording runs on the caller's thread, inside whatever
    atomic() block the caller opened. A statement that fails there marks the
    whole transaction for rollback — PostgreSQL does it in the server — so
    without a savepoint the log destroys the data of the request it was only
    supposed to describe.

    Asserted on the savepoint rather than on surviving data: SQLite does not
    abort a transaction on a failed statement, so a test about the damage would
    pass here and fail nowhere until production.
    """
    recorder = EventRecorder()

    with transaction.atomic(), CaptureQueriesContext(connection) as queries:
        recorder.record(an_event(chat_id=123))

    statements = [query['sql'] for query in queries]
    assert any(sql.startswith('SAVEPOINT') for sql in statements), statements
    assert TelegramEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_SYNC': True})
def test_sync_mode_falls_back_to_the_writer_inside_a_loop():
    """The ORM is @async_unsafe on a running loop, so writing on the calling
    thread there raises SynchronousOnlyOperation instead of recording anything.

    Every inbound seam runs inside a loop, so without this the setting would
    turn the update middleware from a recorder into a source of exceptions.
    """
    recorder = EventRecorder()

    async def record_from_a_coroutine():
        recorder.record(an_event(chat_id=55))

    asyncio.run(record_from_a_coroutine())
    try:
        recorder.flush(timeout=5)
        assert TelegramEvent.objects.filter(chat_id=55).count() == 1
    finally:
        recorder.stop(timeout=5)


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_fork_leaves_the_child_a_queue_of_its_own(paused_writer):
    """Under `gunicorn --preload` the master builds the queue and every worker
    inherits the object — but not the thread that drains it.

    Without the pid check each worker would fill a queue nobody reads, and the
    events would sit there until the process ended. The fork itself is not
    performed here: forking a process with live threads inside a test runner is
    its own hazard, and the pid is what the production path actually branches on.
    """
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=1))
    inherited = recorder._queue

    recorder._owner_pid = os.getpid() + 1  # what a child sees
    recorder.record(an_event(chat_id=2))

    assert recorder._queue is not inherited, 'the child kept a queue its thread cannot drain'
    assert recorder._owner_pid == os.getpid()
    assert recorder._thread is not None, 'the child never started its own writer'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_batch_too_big_to_save_one_by_one_is_bisected(paused_writer):
    """A refused batch of two hundred must not become two hundred statements.

    Bisection is what keeps the recovery proportional: halve until the half is
    small enough to be worth saving row by row, so one poison row in a large
    batch costs a handful of extra inserts rather than the whole batch again.
    """
    original = QuerySet.bulk_create
    sizes = []

    def refuse_anything_large(self, rows, *args, **kwargs):
        sizes.append(len(rows))
        if len(rows) > ROW_BY_ROW:
            msg = 'no'
            raise DatabaseError(msg)
        return original(self, rows, *args, **kwargs)

    rows = [an_event(chat_id=index) for index in range(ROW_BY_ROW * 2)]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(QuerySet, 'bulk_create', refuse_anything_large)
        write_batch(rows)

    assert sizes == [ROW_BY_ROW * 2, ROW_BY_ROW, ROW_BY_ROW], sizes
    assert TelegramEvent.objects.count() == ROW_BY_ROW * 2, 'bisection lost rows'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_SYNC': True})
def test_recording_does_not_doom_the_transaction_it_runs_inside(monkeypatch):
    """The one bug here that destroyed the caller's own data.

    `close_old_connections()` ran before every batch. Inside an `atomic()` block
    the autocommit setting no longer matches the one in `DATABASES`, so
    `close_if_unusable_or_obsolete()` takes its very first branch and closes —
    and `BaseDatabaseWrapper.close()` sets `needs_rollback` when it closes inside
    a transaction. Under `EVENT_LOG_SYNC`, with `ATOMIC_REQUESTS` or a plain
    `atomic()`, recording an event therefore rolled back the writes the caller
    made alongside it.

    The suite could not see it: `tests/db_settings.py` is sqlite `:memory:`, and
    the SQLite backend refuses to close an in-memory database at all. So this
    reports itself as a file-backed one, and stubs the real close so nothing is
    actually torn down.
    """
    monkeypatch.setattr(connection, 'is_in_memory_db', lambda: False)
    monkeypatch.setattr(connection, '_close', lambda: None)
    recorder = EventRecorder()

    try:
        with transaction.atomic():
            recorder.record(an_event(chat_id=321))
            doomed = connection.needs_rollback
    finally:
        connection.needs_rollback = False
        connection.closed_in_transaction = False

    assert doomed is False


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_batch_the_database_refuses_entirely_is_reported(monkeypatch):
    """The ladder caught `DatabaseError` at every rung and returned normally.

    The recorder read that as a written batch: its failure counter never moved,
    so `FAILURE_LIMIT` and `FAILURE_BACKOFF` were unreachable and no
    `log.dropped` row was ever written. A forgotten `migrate` meant a full batch
    of statements and a full batch of tracebacks per flush interval, for ever.
    """

    def refuse(*args, **kwargs):
        msg = 'no such table: django_redis_aiogram_event'
        raise DatabaseError(msg)

    monkeypatch.setattr(QuerySet, 'bulk_create', refuse)
    monkeypatch.setattr(TelegramEvent, 'save', refuse)

    with pytest.raises(EventLogRefusedError):
        write_batch([an_event(chat_id=1), an_event(chat_id=2)])


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_partly_refused_batch_is_not_reported(monkeypatch):
    """Only a wholesale refusal is a failed flush. One poison row is not."""
    refused = []
    # captured before the patch: Model.save inserts through _do_insert, not through
    # bulk_create, so the good row still reaches the table
    original_save = TelegramEvent.save

    def one_bad_row(self, *args, **kwargs):
        if self.chat_id == 2:
            refused.append(self.chat_id)
            msg = 'value too long'
            raise DatabaseError(msg)
        return original_save(self, *args, **kwargs)

    def refuse_the_batch(*args, **kwargs):
        msg = 'batch refused'
        raise DatabaseError(msg)

    monkeypatch.setattr(QuerySet, 'bulk_create', refuse_the_batch)
    monkeypatch.setattr(TelegramEvent, 'save', one_bad_row)

    write_batch([an_event(chat_id=1), an_event(chat_id=2)])

    assert refused == [2]
    # the point of bisecting: one poison row costs itself and nothing else
    assert TelegramEvent.objects.filter(chat_id=1).count() == 1
    assert TelegramEvent.objects.filter(chat_id=2).count() == 0


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_the_writer_suspends_itself_after_repeated_refusals(paused_writer, monkeypatch):
    """What the changelog and Troubleshooting both promise, and what could not
    happen while `write_batch` returned normally on a total failure."""

    def refuse(*args, **kwargs):
        msg = 'no such table: django_redis_aiogram_event'
        raise DatabaseError(msg)

    monkeypatch.setattr(QuerySet, 'bulk_create', refuse)
    monkeypatch.setattr(TelegramEvent, 'save', refuse)
    recorder = EventRecorder()

    failures = 0
    blocked_until = 0.0
    for _ in range(FAILURE_LIMIT):
        failures, blocked_until = recorder._flush([an_event(chat_id=5)], failures=failures)

    assert blocked_until > time.monotonic()


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_events_left_by_a_dying_writer_are_written_not_lost(monkeypatch):
    """A thread target that raises used to clear the slot and walk away, and
    everything still queued went with it — no row, no counter, no gap marker.

    The writer is killed for real rather than `_abandon` being called by hand:
    what is under test is that `_run`'s finally reaches for it at all, and a test
    that calls it directly passes with that line deleted.
    """

    def die(*args, **kwargs):
        msg = 'the writer fell over'
        raise RuntimeError(msg)

    monkeypatch.setattr(EventRecorder, '_collect', die)
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=11))
    writer = recorder._thread

    writer.join(timeout=5)

    assert not writer.is_alive()
    assert TelegramEvent.objects.filter(chat_id=11).count() == 1


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_events_racing_stop_are_still_written(paused_writer):
    """stop() detaches the queue under the guard, but a record() that already
    read it puts into the detached one, which nothing else will ever look at."""
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=12))
    buffer = recorder._queue

    # exactly what a producer that lost the race leaves behind
    recorder.stop(timeout=0.1)
    buffer.put_nowait(an_event(chat_id=13))
    recorder._abandon(buffer)

    assert TelegramEvent.objects.filter(chat_id=13).count() == 1


@pytest.mark.django_db(transaction=True, databases=['default', 'logs'])
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_DATABASE': 'logs'})
def test_recording_to_another_alias_leaves_the_callers_connection_alone(monkeypatch):
    """The guard has to be about the connection actually being closed.

    `close_old_connections()` walks *every* initialized connection. Checking that
    the log's own alias is not in a transaction says nothing about `default`,
    which is exactly where the caller's transaction is — so with
    EVENT_LOG_DATABASE pointing somewhere of its own, the log reached past its own
    connection and doomed one it never writes to.
    """
    caller = connections['default']
    monkeypatch.setattr(caller, 'is_in_memory_db', lambda: False)
    monkeypatch.setattr(caller, '_close', lambda: None)

    try:
        with transaction.atomic(using='default'):
            write_batch([an_event(chat_id=77)])
            doomed = caller.needs_rollback
    finally:
        caller.needs_rollback = False
        caller.closed_in_transaction = False

    assert doomed is False
    assert TelegramEvent.objects.using('logs').filter(chat_id=77).count() == 1


@pytest.mark.django_db(transaction=True)
@override_settings(
    TELEGRAM_BOT={
        **ON,
        'EVENT_LOG_BUFFER_SIZE': 'not a number',
        'EVENT_LOG_BATCH_SIZE': None,
        'EVENT_LOG_FLUSH_INTERVAL': 'soon',
    }
)
def test_unreadable_writer_dials_fall_back_to_their_defaults():
    """These are read on the writer thread, in a loop, outside `_flush`'s net.

    A raise there ends the writer and takes the whole buffer with it, which is a
    steep price for a typo. Checks E036-E038 still report the value at boot.

    The real writer, not `drain_once()`: the batch size and the flush interval are
    read in `_collect`, which only the writer loop runs, so a paused one would
    leave two of the three dials untested.
    """
    recorder = EventRecorder()
    try:
        recorder.record(an_event(chat_id=8))
        assert recorder._queue.maxsize == DEFAULTS['EVENT_LOG_BUFFER_SIZE']
        recorder.flush(timeout=5)

        assert TelegramEvent.objects.filter(chat_id=8).count() == 1
    finally:
        recorder.stop(timeout=5)


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_gap_reported_keeps_what_was_dropped_while_it_was_reported(paused_writer):
    """`_record_gap` used to assign zero, so anything dropped during the write it
    was reporting disappeared with it — and no later flush ever mentioned it."""
    recorder = EventRecorder()
    recorder._dropped = 5

    recorder._record_gap(3)

    assert recorder._dropped == 2


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_an_event_put_into_a_detached_queue_is_moved_to_the_live_one(paused_writer, monkeypatch):
    """stop() detaches the queue under the guard; a record() that already read it
    puts into the detached one, and nothing else will ever look at that queue.

    Driven through `record()` rather than by calling `_rehome`: the wiring under
    test is the queue-identity check, and calling the helper directly passes with
    that check deleted. `_buffer` hands back the detached queue exactly once,
    which is what a producer that read it before the swap is holding.
    """
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=21))
    orphan = recorder._queue
    recorder.stop(timeout=0.1)

    stale = [orphan]
    live = recorder._buffer
    monkeypatch.setattr(recorder, '_buffer', lambda: stale.pop() if stale else live())

    recorder.record(an_event(chat_id=22))

    assert orphan.empty()
    assert recorder.drain_once() == 1
    assert TelegramEvent.objects.filter(chat_id=22).count() == 1
