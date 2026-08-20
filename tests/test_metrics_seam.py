"""The signal a project gets metrics out of, and the three gates behind it.

Every test here has the event log **off**. That is the whole point: the table and
the metrics are separate decisions, and a gate that reads the table flag where it
should read "is anyone listening" produces an advertised metric that is silently
empty. Each of these fails against a gate left on `recorder.enabled`.
"""

import asyncio
import queue
import threading
import time
import uuid

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User
from django.db import OperationalError
from django.test import override_settings
from redis.exceptions import (
    ConnectionError,  # noqa: A004 - shadowing the builtin is the point: this is what redis-py raises
)

from django_redis_aiogram import TelegramBot
from django_redis_aiogram import recorder as recorder_module
from django_redis_aiogram.instrumentation import install_instrumentation, instrumented
from django_redis_aiogram.recorder import (
    DROP_REPORT_INTERVAL,
    WRITER_THREAD,
    Event,
    EventRecorder,
    recorder,
)
from django_redis_aiogram.signals import events_recorded


def a_bot():
    """A stand-in for the aiogram Bot, in the shape `tests/db/test_outbound.py` uses.

    Reached through `_bot` because `TelegramBot.bot` is a read-only property, and
    building the real one would need a token and a network.
    """

    class Fake:
        """Accepts every send and closes its session without doing anything."""

        async def send_message(self, **kwargs):
            """Accept the call the way Telegram would, and answer with nothing."""

        class session:
            """The one attribute `TelegramBot.close` reaches for."""

            @staticmethod
            async def close():
                """Close nothing; there is no session to release."""

    return Fake()


QUEUE = 'TELEGRAM_BOT_MESSAGE'
#: no EVENT_LOG key at all, so the flag is at its default of off
SETTINGS = {'REDIS_URL': 'redis://localhost:6379/0', 'RATE_LIMIT': None}


@pytest.fixture
def collected():
    """Connect a receiver for the duration of one test, and hand back what it saw.

    Connected strongly, with the list as the collector: a bound method of a local
    object would be weakly referenced, and a receiver collected mid-test looks
    exactly like a gate that never fired.
    """
    seen: list[Event] = []

    def receiver(sender, events, **kwargs):
        """Keep every event that arrives, in arrival order."""
        seen.extend(events)

    events_recorded.connect(receiver, weak=False, dispatch_uid='tests.metrics')
    # several of these tests drive a failing write on purpose, which leaves a real
    # drop counted — and the next successful flush then records a `log.dropped` row,
    # correctly, in whichever test happens to run next. Cleared at both ends so each
    # one starts from zero rather than from its predecessor's failures
    with recorder._counter:
        recorder._dropped = 0
    try:
        yield seen
    finally:
        events_recorded.disconnect(dispatch_uid='tests.metrics')
        recorder.stop(timeout=5)
        with recorder._counter:
            recorder._dropped = 0


@pytest.fixture
def clean_counters():
    """Leave the process-wide recorder's counters as they were found.

    `recorder` is a singleton, so a test that drives a failed write and leaves `_dropped`
    set hands a real gap to whichever test runs next — which then correctly reports it, in
    the wrong place, and reads as a defect in something unrelated. The same for
    `_touched_database`, which decides whether a stopping writer closes a database
    connection and is only otherwise cleared on a fork — a set of thread idents now, so a
    test that wrote on this thread must not leave this thread marked.

    `_reported_at` goes with them: a test that pushes it into the past to reach the
    once-a-minute report leaves the next drop reporting immediately, which is a log line
    appearing where the code says it should be suppressed.

    The `collected` fixture above does this for its own users; this is for the tests that
    do not need a receiver.
    """
    reported_at = recorder._reported_at
    with recorder._counter:
        recorder._dropped = 0
        # under the same lock `_deliver` and `_took_the_touch` use: clearing outside it
        # can erase a live writer's mark, or lose one it adds mid-clear
        recorder._touched_database.clear()
    try:
        yield
    finally:
        with recorder._counter:
            recorder._dropped = 0
            recorder._touched_database.clear()
        recorder._reported_at = reported_at


def kinds(events):
    """The kinds that arrived, in order, so a failure message names them."""
    return [event.kind for event in events]


def test_django_still_exposes_the_receiver_list(collected):
    """`recorder.active` gates on `bool(events_recorded.receivers)`.

    Measured at 7ns against 172ns for `has_listeners()`, and it is read once per
    event — but `receivers` is Django's attribute, not part of its documented API,
    so a rename would turn the gate permanently false and every metric silently
    empty. This is the assumption, written down where it fails loudly.
    """
    assert events_recorded.receivers, 'connecting a receiver did not fill `receivers`'
    events_recorded.disconnect(dispatch_uid='tests.metrics')
    assert not events_recorded.receivers, 'disconnecting left the gate open'
    events_recorded.connect(lambda sender, **kwargs: None, weak=False, dispatch_uid='tests.metrics')


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_receiver_gets_events_with_the_log_off(redis_server, collected):
    """The seam exists so a project can have metrics without a table.

    With `EVENT_LOG` unset, `recorder.enabled` is false — so a producer gated on it
    records nothing and the receiver is never called. This is the base case the
    other tests specialise.
    """
    TelegramBot().send_redis(chat_id=7, text='hi')
    recorder.flush(timeout=5)

    assert not recorder.enabled, 'this test is meaningless with the log on'
    assert kinds(collected) == ['outbound.queued'], f'the receiver saw {kinds(collected)}'
    assert collected[0].chat_id == 7


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_payload_is_not_summarised_for_a_receiver(redis_server, collected, monkeypatch):
    """`describe()` is the expensive half of recording and no part of counting.

    Patched rather than inspected: asserting `detail is None` would also pass if
    `describe` ran and returned nothing, which is the opposite of what this is
    about — the cost, not the value.
    """
    called = []
    monkeypatch.setattr('django_redis_aiogram.client.describe', lambda kwargs: called.append(kwargs) or {})

    TelegramBot().send_redis(chat_id=7, text='hi')
    recorder.flush(timeout=5)

    assert called == [], 'the payload was summarised for a receiver that cannot use it'
    assert kinds(collected) == ['outbound.queued'], 'the event itself must still arrive'
    assert collected[0].detail is None


@override_settings(TELEGRAM_BOT={**SETTINGS, 'TOKEN': '1:x', 'MAX_RETRIES': 0})
def test_a_send_reports_its_stages_to_a_receiver(redis_server, collected):
    """`_record_send` is one guard over `sent`, `failed`, `retried` and `dropped` —
    the entire advertised metric set. Gated on the table flag, a project connecting
    a receiver for send outcomes gets nothing at all, which is the single easiest
    way to get this design wrong."""
    bot = TelegramBot()
    bot._bot = a_bot()
    try:
        bot.send_raw(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        bot._bot = None
        bot.close()

    assert 'outbound.sent' in kinds(collected), f'no send stage reached the receiver: {kinds(collected)}'


def recording_middleware(dispatcher):
    """Find our middleware among aiogram's own, or return None.

    By type, not by position or by count: a bare `Dispatcher` already carries
    `ErrorsMiddleware`, `UserContextMiddleware` and `FSMContextMiddleware`, so
    asserting the list is non-empty — or reading `[0]` — passes with our
    registration removed entirely.
    """
    for middleware in dispatcher.update.outer_middleware:
        if type(middleware).__name__ == 'RecordingMiddleware':
            return middleware
    return None


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_update_middleware_is_installed_for_a_receiver(collected):
    """`install_instrumentation` returns before building anything when nothing reads
    events, which is what makes the inactive cost zero — so reading the table flag
    there means an update never reaches a receiver."""
    dispatcher = Dispatcher()
    install_instrumentation(dispatcher)

    assert recording_middleware(dispatcher) is not None, 'nothing was registered for a listening process'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_an_update_reaches_a_receiver(collected):
    """End to end through the middleware, because registration alone proves only
    that something was installed."""
    dispatcher = Dispatcher()
    install_instrumentation(dispatcher)
    middleware = recording_middleware(dispatcher)
    assert middleware is not None, 'nothing was registered, so nothing is being tested'

    update = Update(
        update_id=11,
        message=Message(
            message_id=2,
            date=0,
            chat=Chat(id=7, type='private'),
            from_user=User(id=9, is_bot=False, first_name='A'),
            text='hi',
        ),
    )

    async def handler(event, data):
        """Stand in for the handler chain the middleware wraps."""
        return 'done'

    asyncio.run(middleware(handler, update, {}))
    recorder.flush(timeout=5)

    assert 'inbound.received' in kinds(collected), f'the receiver saw {kinds(collected)}'
    assert collected[0].update_id == 11
    assert collected[0].detail is None, 'the update was summarised for a receiver'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_state_change_reaches_a_receiver(collected):
    """`instrumented` hands the storage back untouched when nothing reads events, so
    a receiver watching FSM transitions needs this gate too."""
    storage = instrumented(MemoryStorage())
    assert storage.__class__.__name__ == 'RecordingStorage', 'the storage was not wrapped for a listening process'

    from aiogram.fsm.storage.base import StorageKey

    asyncio.run(storage.set_state(StorageKey(bot_id=1, chat_id=7, user_id=9), 'waiting'))
    recorder.flush(timeout=5)

    assert kinds(collected) == ['fsm.transition'], f'the receiver saw {kinds(collected)}'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_nothing_is_recorded_and_no_thread_runs_when_nobody_reads(redis_server):
    """The default deployment pays nothing: no writer thread, and `record()` returns
    on its first branch.

    Deliberately without the `collected` fixture — this is the no-receiver case, and
    the writer thread is the observable difference.
    """
    before = {thread.name for thread in threading.enumerate()}
    TelegramBot().send_redis(chat_id=7, text='hi')
    recorder.record(Event(kind='outbound.queued', correlation_id=uuid.uuid4()))

    assert not recorder.active
    assert {thread.name for thread in threading.enumerate()} == before, 'a writer thread started for nobody'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG_KINDS': ['outbound.sent']})
def test_event_log_kinds_filters_receivers_too(redis_server, collected):
    """One answer to "which events does this deployment care about", not two.

    The alternative — receivers seeing everything while the table is filtered —
    would make the setting mean different things in two places, and a project
    reading `Event-log.md` would have no way to know which.

    Both kinds in one run, and the admitted one is the point: asserting only that
    nothing arrived would hold if the receiver never connected, if `record()` returned
    on its first branch, or if the send failed outright — none of which is this
    setting doing its job. The pass condition is that one arrived and the other did
    not.
    """
    TelegramBot().send_redis(chat_id=7, text='hi')  # outbound.queued, excluded
    recorder.record(Event(kind='outbound.sent'))  # admitted
    recorder.flush(timeout=5)

    assert kinds(collected) == ['outbound.sent'], f'the receiver saw {kinds(collected)}'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_one_broken_receiver_does_not_cost_the_others_their_batch(redis_server, collected, caplog):
    """`send_robust`, so a receiver raising is that receiver's problem.

    A metrics receiver is third-party code on the writer thread. Letting it escape
    would end the writer, and everything still queued would go with it — the
    failure mode the whole queue exists to avoid.
    """

    def broken(sender, events, **kwargs):
        """Fail the way a receiver with a bug in it fails."""
        msg = 'this receiver is broken'
        raise RuntimeError(msg)

    events_recorded.connect(broken, weak=False, dispatch_uid='tests.metrics.broken')
    try:
        with caplog.at_level('ERROR', logger='django_redis_aiogram'):
            TelegramBot().send_redis(chat_id=7, text='hi')
            recorder.flush(timeout=5)
    finally:
        events_recorded.disconnect(dispatch_uid='tests.metrics.broken')

    assert kinds(collected) == ['outbound.queued'], 'the working receiver lost its batch'
    assert 'an events_recorded receiver raised' in caplog.text
    named = [record for record in caplog.records if 'broken' in getattr(record, 'tg_receiver', '')]
    assert named, 'the line did not name the receiver that raised'


def test_one_writers_exit_does_not_clear_another_writers_mark():
    """Two writers can overlap, and the mark has to survive the wrong one leaving.

    `stop()` detaches the queue before it joins, so a join that times out leaves the old
    writer running while a replacement starts. With one flag for the process, the old
    writer's exit cleared the mark the replacement had earned, and the replacement then
    skipped closing the connection it had opened — a leak that only appears when a
    shutdown was already going badly.

    Driven at the seam rather than through two real threads: what is being asserted is
    that taking the mark takes only this thread's.
    """
    marks = recorder._touched_database
    with recorder._counter:
        marks.clear()
        marks.update({threading.get_ident(), -1})  # -1 stands in for the other writer

    assert recorder._took_the_touch() is True, 'this thread had written and was told otherwise'
    assert -1 in marks, "the other writer's mark went with it"
    assert recorder._took_the_touch() is False, 'the mark survived being taken'

    with recorder._counter:
        marks.clear()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_metrics_only_writer_does_not_close_a_connection_it_never_opened(redis_server, collected, monkeypatch):
    """Closing one means importing `eventlog`, and that imports `django.db`.

    Which is the one import `recorder.py`'s own docstring exists to keep out of a
    process that does not need it — and a process with receivers and no table does
    not. Measured before the fix: `django_redis_aiogram.eventlog` appeared in
    `sys.modules` the moment the writer stopped, having written nothing.

    Asserted on the call rather than on `sys.modules`, because by the time this test
    runs the rest of the suite has imported `eventlog` for its own reasons.
    """
    closed = []
    monkeypatch.setattr(recorder, '_close_connections', lambda: closed.append(True))

    TelegramBot().send_redis(chat_id=7, text='hi')
    recorder.flush(timeout=5)
    recorder.stop(timeout=5)

    assert kinds(collected) == ['outbound.queued'], 'nothing was recorded, so nothing is being tested'
    assert closed == [], 'the writer closed a database connection it never opened'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True})
def test_a_writer_that_wrote_still_closes_its_connection(redis_server, collected, monkeypatch):
    """The other direction, which is what makes the check above a decision and not
    a way of never closing anything.

    The write is stubbed: this is about the bookkeeping, and a real one would need
    the table and a database the unit suite does not have.
    """
    closed = []
    monkeypatch.setattr(recorder, '_close_connections', lambda: closed.append(True))
    monkeypatch.setattr(recorder, '_write', lambda batch: None)

    TelegramBot().send_redis(chat_id=7, text='hi')
    recorder.flush(timeout=5)
    recorder.stop(timeout=5)

    assert recorder.enabled, 'this test is meaningless with the log off'
    assert closed == [True], 'the writer left its own connection open'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True})
def test_a_receiver_cannot_change_what_was_written(redis_server, collected, monkeypatch):
    """Receivers are project code, and containing their exceptions is half a job.

    They used to be handed the same list and the same `Event` objects the ORM was
    about to read — and a frozen dataclass does not freeze the `detail` dict inside
    it, so clearing the list or editing a `detail` changed what got persisted. The
    write goes first now, so the question cannot arise.
    """
    # a snapshot of the contents at the moment of the write, not the objects: the
    # real `write_batch` builds model instances and returns, after which nothing a
    # receiver does can reach the rows. Holding the `Event` objects instead would
    # show the mutation and prove nothing about ordering
    written: list[list[dict]] = []
    monkeypatch.setattr(recorder, '_write', lambda batch: written.append([dict(e.detail or {}) for e in batch]))

    def vandal(sender, events, **kwargs):
        """Rewrite every `detail` it is handed, which used to reach the rows."""
        for event in events:
            if event.detail is not None:
                event.detail.clear()
                event.detail['vandalised'] = True

    events_recorded.connect(vandal, weak=False, dispatch_uid='tests.metrics.vandal')
    try:
        TelegramBot().send_redis(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        events_recorded.disconnect(dispatch_uid='tests.metrics.vandal')

    assert written, 'nothing was written, so nothing is being tested'
    persisted = written[0][0]
    assert persisted, 'the log is on, so this row should have carried a summary'
    assert 'vandalised' not in persisted, 'a receiver rewrote a row before it was persisted'
    assert kinds(collected) == ['outbound.queued'], 'the fixture receiver saw nothing to compare against'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_receiver_cannot_take_the_batch_from_the_next_one(redis_server, collected):
    """`send_robust` hands every receiver the same argument, one after another.

    So a list would let the first receiver decide what the second one sees. A tuple
    costs nothing and removes the question.
    """
    shapes = []

    def inspect(sender, events, **kwargs):
        """Record what type the batch arrived as."""
        shapes.append(type(events).__name__)

    events_recorded.connect(inspect, weak=False, dispatch_uid='tests.metrics.inspect')
    try:
        TelegramBot().send_redis(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        events_recorded.disconnect(dispatch_uid='tests.metrics.inspect')

    assert shapes == ['tuple'], f'receivers were handed a {shapes}'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG_KINDS': ['outbound.sent']})
def test_the_gap_row_reaches_a_receiver_even_when_the_kinds_exclude_it(redis_server, collected, monkeypatch):
    """`log.dropped` is exempt from `EVENT_LOG_KINDS`, and has to be.

    It is the record that recording itself fell behind. A deployment that filtered
    it out would read the hole as quiet traffic — which is the exact failure
    `_record_gap` exists to prevent, and the reason the table has always been exempt
    for this row too. Receivers are exempt with it, so the two stay one answer.
    """
    monkeypatch.setattr(recorder, '_write', lambda batch: None)
    recorder._dropped = 3
    recorder._record_gap(3)

    assert kinds(collected) == ['log.dropped'], f'the receiver saw {kinds(collected)}'
    assert collected[0].detail == {'dropped': 3}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_receiver_still_gets_the_detail_a_seam_measured_itself(redis_server, collected, monkeypatch):
    """Only the *summarised arguments* are gated on the log, not all of `detail`.

    Claimed the other way round at first, and it was false: a send's `duration_ms`,
    a queueing failure's `stage` and a gap's `dropped` count are all measured by the
    recording seam rather than summarised from a payload, and all reach a receiver
    with the log off. Pinned so the documentation cannot drift back.
    """

    def refuse(*args, **kwargs):
        """Stand in for a Redis that has gone away."""
        message = 'redis is gone'
        raise ConnectionError(message)

    monkeypatch.setattr('django_redis_aiogram.client.get_redis', refuse)

    with pytest.raises(ConnectionError):
        TelegramBot().send_redis(chat_id=7, text='hi')
    recorder.flush(timeout=5)

    assert kinds(collected) == ['outbound.dropped'], f'the receiver saw {kinds(collected)}'
    assert collected[0].detail == {'stage': 'queueing'}, 'the stage a receiver needs was withheld'
    assert collected[0].error_code == 'ConnectionError'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True})
def test_a_failed_write_still_reaches_a_receiver(redis_server, collected, monkeypatch, caplog):
    """The whole reason the publish is in a `finally` rather than after the write.

    A database that is down or unmigrated is exactly when someone is watching a
    dashboard, so the metrics must not go down with it. Written the other way round
    — publish after a successful write — a project would lose its numbers precisely
    when it needed them, and the loss would look like the bot going quiet.
    """

    def refuse(batch):
        """Fail the way an unmigrated database fails."""
        message = 'no such table: django_redis_aiogram_telegramevent'
        raise RuntimeError(message)

    monkeypatch.setattr(recorder, '_write', refuse)

    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        TelegramBot().send_redis(chat_id=7, text='hi')
        recorder.flush(timeout=5)

    assert kinds(collected) == ['outbound.queued'], f'a failed write cost the receiver its batch: {kinds(collected)}'
    assert 'could not write an event batch' in caplog.text, 'the failure was not reported'


def test_the_drop_counter_is_only_ever_touched_under_its_own_lock():
    """`_drop`'s docstring names the threads it protects against, and the writer on
    a failed flush is one of them — but `_flush` read and wrote the count without
    taking the lock, so a producer's drop landing between that `+=`'s read and its
    write was discarded, and the `log.dropped` row then under-reported the gap.

    Asserted on the lock rather than by racing threads: a lost update is a
    read-modify-write interleaving, so a timing test would pass most runs and fail
    some, which is worse than no test. This records whether `_counter` was held at
    every write of `_dropped`, which is the invariant itself.
    """
    held: list[bool] = []

    class Watching(EventRecorder):
        """An `EventRecorder` that reports the lock state at each write."""

        @property
        def _dropped(self):
            """Read the count from the instance dictionary."""
            return self.__dict__.get('dropped_value', 0)

        @_dropped.setter
        def _dropped(self, value):
            """Record whether the counter lock was held, then store the value."""
            counter = self.__dict__.get('_counter')
            if counter is not None:
                # __init__ sets the count before it builds the lock
                held.append(counter.locked())
            self.__dict__['dropped_value'] = value

    watcher = Watching()

    def refuse(batch):
        """Fail every write, so the failure branch of `_flush` runs."""
        message = 'no such table'
        raise RuntimeError(message)

    watcher._write = refuse
    watcher._enabled = True

    watcher._drop(2)
    watcher._flush([Event(kind='outbound.queued')], failures=0)

    assert held, 'nothing wrote the counter, so nothing is being tested'
    assert all(held), f'the counter was written unguarded {held.count(False)} of {len(held)} times'
    assert watcher._dropped == 3, f'the count came out as {watcher._dropped} for two drops and one failed batch'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_last_events_before_shutdown_still_reach_a_receiver(redis_server, collected):
    """A queue the writer never drained is published by whoever calls `stop()`.

    `_abandon` exists because that queue will never be drained by a writer — either
    it died, or `stop()` detached it out from under a producer. So there is no
    writer thread to route through: publishing on the calling thread is the only
    thing available, and dropping the last events before the process goes would be
    worse. That is a documented exception to "on the writer's thread" rather than a
    hole in it, and this pins it.
    """
    # a standalone queue, so no writer is ever started: going through `record()`
    # starts one, and it could consume and publish the event on its own thread before
    # `_abandon` ran — which would make the assertion below depend on timing rather
    # than on where `_abandon` publishes
    buffer: queue.Queue = queue.Queue()
    buffer.put_nowait(Event(kind='outbound.queued'))
    assert recorder._thread is None, 'a writer is running, so this is not the abandoned path'

    on_thread = []
    events_recorded.connect(
        lambda sender, events, **kwargs: on_thread.append(threading.current_thread().name),
        weak=False,
        dispatch_uid='tests.metrics.thread',
    )
    try:
        recorder._abandon(buffer)
    finally:
        events_recorded.disconnect(dispatch_uid='tests.metrics.thread')

    assert kinds(collected) == ['outbound.queued'], f'the abandoned batch was lost: {kinds(collected)}'
    assert on_thread == [threading.current_thread().name], f'it ran on {on_thread}, not the calling thread'
    assert WRITER_THREAD not in on_thread, 'the writer was gone, so it cannot have run there'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG_SYNC': True})
def test_the_synchronous_flag_does_nothing_for_a_receiver_only_process(redis_server, collected):
    """`EVENT_LOG_SYNC` is about *where the insert happens*, so with nothing being
    inserted it has nothing to say.

    Left checking only its own flag, a process with receivers and no table would run
    them on the thread that recorded the event — inside the send path, which is the
    one thing this design exists to avoid. So the flag requires the log as well, and
    receivers still arrive on the writer's thread here.
    """
    on_thread = []
    events_recorded.connect(
        lambda sender, events, **kwargs: on_thread.append(threading.current_thread().name),
        weak=False,
        dispatch_uid='tests.metrics.syncthread',
    )
    try:
        TelegramBot().send_redis(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        events_recorded.disconnect(dispatch_uid='tests.metrics.syncthread')

    assert kinds(collected) == ['outbound.queued'], f'the receiver saw {kinds(collected)}'
    assert on_thread == [WRITER_THREAD], f'ran on {on_thread} rather than the writer thread'


@override_settings(TELEGRAM_BOT=SETTINGS)
@pytest.mark.parametrize('nameable', [True, False], ids=['django can name it', 'django cannot'])
def test_a_receiver_that_cannot_even_be_named_costs_nobody_their_batch(redis_server, collected, caplog, nameable):
    """The reporting path must not become the failure it reports.

    Two defects met here and the two cases below separate them, because each one
    only reaches the other's code.

    **Django cannot name a callable instance.** Its `send_robust` failure logging
    reads `receiver.__qualname__` unguarded, and an instance does not inherit its
    class's — so a callable object that raises makes `send_robust` *itself* raise
    `AttributeError`, measured on 6.1. Its containment does not cover that receiver
    shape at all, and `_publish` has to contain the dispatch as well.

    **`repr()` was the fallback when we name a receiver.** Python evaluates every
    argument before the call, so `getattr(receiver, '__qualname__', repr(receiver))`
    ran `repr` even with the attribute there. That one needs a receiver Django *can*
    name, or the dispatch fails first and our line never runs.

    Either would have landed in `_flush`'s `except` and been counted as a failed
    *write*: the other receivers lose the batch, a `log.dropped` row appears, and the
    log blames the database for what a receiver did.
    """

    class Hostile:
        """Raises from the call, and from every attempt to describe it."""

        def __call__(self, sender, events, **kwargs):
            """Raise, so the failure path runs."""
            message = 'this receiver is hostile'
            raise RuntimeError(message)

        def __repr__(self):
            """Raise as well, so naming it is not safe either."""
            message = 'and it will not be named either'
            raise RuntimeError(message)

    receiver = Hostile()
    if nameable:
        # on the instance, not in the class body: `type.__new__` takes `__qualname__`
        # out of the namespace to rename the class, so a class-body assignment leaves
        # instances with nothing — which is the very gap Django trips over
        receiver.__qualname__ = 'Hostile'
    assert hasattr(receiver, '__qualname__') is nameable, 'the arrangement does not match its own label'

    events_recorded.connect(receiver, weak=False, dispatch_uid='tests.metrics.hostile')
    try:
        with caplog.at_level('ERROR', logger='django_redis_aiogram'):
            TelegramBot().send_redis(chat_id=7, text='hi')
            recorder.flush(timeout=5)
    finally:
        events_recorded.disconnect(dispatch_uid='tests.metrics.hostile')

    assert kinds(collected) == ['outbound.queued'], 'the working receiver lost its batch'
    assert 'could not write an event batch' not in caplog.text, 'a receiver was counted as a failed write'
    # which line appears depends on which layer caught it, and the point is that
    # neither escapes
    assert 'receiver raised' in caplog.text or 'publishing recorded events failed' in caplog.text


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_failure_while_reporting_a_receiver_costs_nobody_their_batch(redis_server, collected, monkeypatch):
    """The reporting loop is inside the guard, not only the dispatch.

    The review asked for this by way of a receiver whose `__getattr__` raises
    something `getattr(..., None)` cannot absorb — and that turned out to be
    unreachable: `Signal.connect` calls `iscoroutinefunction`, which reads `__name__`
    itself, so such a receiver is rejected before it can ever be published to. Naming
    it is therefore safe by construction.

    What *is* reachable is the logging: a handler or formatter that raises, which is
    ordinary enough in a project with custom logging. Same consequence either way —
    outside the guard it lands in `_flush`'s `except` and is counted as a failed
    write — so the guard covers the whole loop, and this drives it through the path
    that can actually happen.
    """
    original = recorder_module.logger.error
    calls = []

    def hostile(*args, **kwargs):
        """Stand in for a logging handler or formatter that raises."""
        calls.append(args)
        message = 'the logging handler is broken'
        raise RuntimeError(message)

    def broken_receiver(sender, events, **kwargs):
        """Raise, so the reporting line runs at all."""
        message = 'this receiver raised'
        raise RuntimeError(message)

    events_recorded.connect(broken_receiver, weak=False, dispatch_uid='tests.metrics.broken')
    monkeypatch.setattr(recorder_module.logger, 'error', hostile)
    try:
        TelegramBot().send_redis(chat_id=7, text='hi')
        recorder.flush(timeout=5)
    finally:
        events_recorded.disconnect(dispatch_uid='tests.metrics.broken')
        monkeypatch.setattr(recorder_module.logger, 'error', original)

    assert calls, 'the reporting line never ran, so nothing is being tested'
    assert kinds(collected) == ['outbound.queued'], 'the working receiver lost its batch'
    assert recorder._dropped == 0, f'a broken log line was counted as {recorder._dropped} dropped events'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True})
def test_a_gap_row_that_cannot_be_written_keeps_its_count(redis_server, monkeypatch, clean_counters):
    """The hole outlives the row that failed to describe it.

    The count was subtracted *before* the write and the write's failure suppressed, so a
    gap row the database refused took the hole with it: `_dropped` was already zero, no
    later flush would report those events, and the feed then read as complete coverage of
    a period that had lost rows. Subtracted after the write now, and the count stays for
    the next flush to report.
    """

    def refuse(batch):
        raise OperationalError('no such table: django_redis_aiogram_event')

    monkeypatch.setattr(recorder, '_write', refuse)
    recorder._dropped = 7

    recorder._record_gap(7)

    assert recorder._dropped == 7, 'the gap was forgotten with the row that could not report it'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True})
def test_a_gap_row_that_lands_clears_its_count(redis_server, monkeypatch, clean_counters):
    """The control, so the fix above cannot be "never subtract"."""
    # 0 refused, which is what `write_batch` returns on a clean write: a double returning
    # None would be a shape no real path produces
    monkeypatch.setattr(recorder, '_write', lambda batch: 0)
    recorder._dropped = 7

    recorder._record_gap(7)

    assert recorder._dropped == 0, 'a reported gap was reported twice'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_receiver_django_cannot_name_costs_the_receivers_behind_it(redis_server, monkeypatch):
    """Django's limit, pinned as a limit rather than described as one.

    `send_robust` logs a failing receiver with `receiver.__qualname__`, unguarded. A
    callable *instance* — an ordinary shape for a metrics collector — has no such
    attribute, so raising inside one makes `send_robust` itself raise and abandon its own
    loop: every receiver connected after it misses that batch. The containment here keeps
    the write and the earlier receivers whole, and cannot reach past a dispatch that has
    already stopped.

    If a Django release guards that logging, this test fails — which is the moment the
    docstring saying otherwise has to change too.
    """
    seen = []

    class Unnameable:
        """A collector with no `__qualname__`, which is what breaks the naming."""

        def __call__(self, sender, events, **kwargs):
            """Raise, so Django reaches for a name it cannot find."""
            message = 'the collector is broken'
            raise RuntimeError(message)

    def behind(sender, events, **kwargs):
        """Connected after it, and therefore never reached for that batch."""
        seen.append(len(events))

    unnameable = Unnameable()
    assert not hasattr(unnameable, '__qualname__'), 'the premise no longer holds'
    events_recorded.connect(unnameable, dispatch_uid='unnameable')
    events_recorded.connect(behind, dispatch_uid='behind')
    try:
        monkeypatch.setattr(recorder, '_write', lambda batch: 0)
        failures, blocked = recorder._flush([Event(kind='outbound.sent')], failures=0)
    finally:
        events_recorded.disconnect(dispatch_uid='unnameable')
        events_recorded.disconnect(dispatch_uid='behind')

    assert failures == 0, 'a receiver was counted as a failed write'
    assert blocked == 0.0
    assert seen == [], 'Django named the receiver after all; the docstring needs updating'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True})
def test_two_overlapping_flushes_report_one_gap_between_them(redis_server, monkeypatch, clean_counters):
    """`drain_once()` runs on the caller's thread while the writer runs its own.

    Both snapshot the drop count before their batch, so with the subtraction *after* the
    write each of them reported the same hole and took it off — one gap counted twice in
    the feed and a count driven negative, which then swallowed the next real gap. The
    count is claimed under the lock before the write now, so the second flush finds
    nothing left to report.

    The overlap is arranged rather than raced: the first write blocks until the second has
    been through, which is the interleaving that makes the defect certain.
    """
    inside = threading.Event()
    release = threading.Event()
    rows: list[int] = []

    def blocking_write(batch):
        """Hold the first gap row open, so the second flush runs beside it."""
        for event in batch:
            if event.kind == 'log.dropped':
                rows.append(event.detail['dropped'])
                inside.set()
                release.wait(5)
        return 0

    monkeypatch.setattr(recorder, '_write', blocking_write)
    recorder._dropped = 7
    first = threading.Thread(target=recorder._record_gap, args=(7,), daemon=True)
    first.start()

    assert inside.wait(5), 'the first gap row never reached the write'
    recorder._record_gap(7)
    release.set()
    first.join(timeout=5)

    assert rows == [7], f'the same hole was reported {len(rows)} times: {rows}'
    assert recorder._dropped == 0, f'the count went to {recorder._dropped}'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'EVENT_LOG': True})
def test_a_gap_row_the_database_refuses_one_at_a_time_keeps_its_count(
    redis_server, monkeypatch, caplog, clean_counters
):
    """A refusal is the same loss as a raise, and this was the path that ignored it.

    `write_batch` reports how many rows the database refused individually — a return this
    branch introduced. The gap batch is one row, so a return of 1 means the `log.dropped`
    row did not land, while the claim had already been taken off: the hole disappeared with
    no exception anywhere to notice it.
    """

    def refuse_every_row(batch):
        """What `write_batch` returns when the database took none of them."""
        return len(batch)

    monkeypatch.setattr(recorder, '_write', refuse_every_row)
    recorder._dropped = 5

    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        recorder._record_gap(5)

    assert recorder._dropped == 5, 'the gap was lost to a refusal nobody checked'
    assert 'refused the gap row' in caplog.text


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_dropping_nothing_says_nothing(redis_server, caplog, clean_counters):
    """Callers pass the refused count straight through, and it is zero on every good write.

    Without the guard, a batch that landed in full still reached the once-a-minute report
    and logged `the event log is falling behind` — a false alarm on the one line an
    operator watches for real ones, emitted by the successful path.

    `_reported_at` is pushed into the past on purpose: the interval is what would otherwise
    hide the defect, and a test that relied on it would pass either way.
    """
    recorder._reported_at = time.monotonic() - DROP_REPORT_INTERVAL - 1

    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        recorder._drop(0)

    assert recorder._dropped == 0
    assert 'falling behind' not in caplog.text, 'a batch that lost nothing reported a backlog'
