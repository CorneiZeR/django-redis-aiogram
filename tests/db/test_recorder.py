"""The buffered writer: what it guarantees, and what it gives up.

Most of these drive `drain_once()` on the calling thread rather than starting
the writer and racing it — the same trick `TokenBucket`'s injectable clock plays
for the rate limiter.
"""

import threading
import time

import pytest
from django.db import DatabaseError
from django.test import override_settings

from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.eventlog import write_batch
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.recorder import Event, EventRecorder

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

    original = TelegramEvent.objects.bulk_create
    calls = []

    def refuse_the_batch(rows, *args, **kwargs):
        calls.append(len(rows))
        if len(calls) == 1:
            msg = 'no'
            raise DatabaseError(msg)
        return original(rows, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(TelegramEvent.objects.__class__, 'bulk_create', refuse_the_batch, raising=False)
        recorder.drain_once()

    assert TelegramEvent.objects.count() == 3, 'the whole batch was lost over one refusal'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_the_writer_thread_writes_and_stops():
    recorder = EventRecorder()
    recorder.record(an_event(chat_id=11))
    try:
        recorder.flush(timeout=5)
        assert TelegramEvent.objects.filter(chat_id=11).count() == 1
    finally:
        recorder.stop(timeout=5)

    assert not any(thread.name == 'tgbot-event-writer' for thread in threading.enumerate())


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
