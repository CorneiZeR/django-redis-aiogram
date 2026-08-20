"""Record what happened, without making anyone wait for the database.

Every seam that records runs somewhere the ORM cannot be used directly: the
send coroutine runs on the bot's event loop, the delivery consumer runs on a
thread nothing manages a connection for, and a Django view must not pay a
second round trip to log the one it just made. All of them hand an :class:`Event`
to a bounded queue that one writer thread drains in batches.

``record()`` reaches only ``Queue.put_nowait`` — a lock, a deque append and a
notify. Nothing in that chain is decorated ``@async_unsafe``, which is what
makes it legal from a coroutine with no ``sync_to_async`` and no
``SynchronousOnlyOperation``. One setting suspends that, and only one:
``EVENT_LOG_SYNC`` inserts on the calling thread on purpose, which is why it is
documented as a testing setting and why it declines to act inside a running loop.

Going through the queue also avoids what a synchronous insert would do
inside a caller's ``atomic()`` block: on PostgreSQL a failed statement aborts
the whole transaction, so logging would corrupt the caller's data.

This module must not import ``django.db``. :mod:`django_redis_aiogram.eventlog`
does, and the writer thread imports it on its first flush — on its *first write*,
more precisely, because since 3.1.0 the writer also runs with the log off, for
``events_recorded`` receivers alone, and such a process never reaches it at all.
"""

import asyncio
import atexit
import contextlib
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed

from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.events import known_kinds, new_correlation_id, worker_identity
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf
from django_redis_aiogram.signals import events_recorded

logger = logging.getLogger('django_redis_aiogram')

#: the writer's thread name, so a log line or a test can name it
WRITER_THREAD = 'tgbot-event-writer'


#: what a signed BIGINT holds, which is the width of every id column here
ID_RANGE = range(-(2**63), 2**63)


def as_identifier(value: object) -> int | None:
    """Keep what a BIGINT column can hold, and nothing else.

    A Telegram chat_id may be a `@username`, which is a valid destination and
    not a number; `True` is an int to Python and not an id to anyone; and a
    Python integer has no width, so one off an untrusted queue can be wider
    than the column and cost the row it was meant to describe.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in ID_RANGE else None


#: how long stop() waits for the writer before giving up on what it holds
STOP_TIMEOUT = 5.0
#: consecutive failed flushes after which the writer stops trying for a while
FAILURE_LIMIT = 5
#: how long it drains and discards before probing the database again
FAILURE_BACKOFF = 60.0
#: how often the drop counter is allowed to reach the log
DROP_REPORT_INTERVAL = 60.0


@dataclass(frozen=True)
class Wake:
    """A marker that ends the writer's current wait.

    ``done`` is what makes :meth:`EventRecorder.flush` honest: the queue going
    empty means the writer has *taken* the batch, not that it has written it.
    """

    done: threading.Event | None = None


@dataclass(frozen=True)
class Event:
    """One thing that happened. Indexed columns first, the rest in ``detail``."""

    kind: str
    correlation_id: uuid.UUID = field(default_factory=new_correlation_id)
    created_at: float = field(default_factory=time.time)
    function: str = ''
    chat_id: int | None = None
    user_id: int | None = None
    message_id: int | None = None
    update_id: int | None = None
    worker: str = ''
    attempt: int = 0
    duration_ms: int | None = None
    error_code: str = ''
    error: str = ''
    #: already JSON-safe by the time it arrives: encoding aiogram objects is the
    #: caller's job, because this module must stay free of aiogram
    detail: dict[str, Any] | None = None


def _number(key: str, cast: Callable[[Any], float]) -> float:
    """Read one of the writer's own dials, falling back to its default.

    Checks E036-E038 report a value that cannot be read, at boot and once. This
    runs on the writer thread, in a loop, on the far side of `_flush`'s net — a
    raise here ends the writer and takes the whole buffer with it, which is a
    steep price for a typo in a batch size.
    """
    try:
        return cast(conf[key])
    except (ImproperlyConfigured, KeyError, TypeError, OverflowError, ValueError):
        # ImproperlyConfigured from resolving the settings, the rest from the cast.
        # OverflowError is the one that is not a typo: `int(float('inf'))` raises it, and a
        # settings dict can hold `inf` directly — the environment cannot, it is refused
        # there — so without this the writer thread ends on a value E044 only reports
        return cast(DEFAULTS[key])


def _receiver_name(receiver: object) -> str:
    """Name a signal receiver for a log line, without calling anything that can raise.

    ``repr()`` is deliberately not the fallback. Python evaluates every argument
    before the call, so ``getattr(receiver, '__qualname__', repr(receiver))``
    evaluates ``repr`` *even when the attribute is there* — and a receiver whose
    ``__repr__`` raises would then take this line, and with it the rest of the batch's
    receivers, out through :meth:`EventRecorder._flush`'s ``except``, where it would
    be counted as a failed write. That is the failure this whole method exists to
    contain, arriving through the code that reports it.

    ``type(receiver).__name__`` is the last resort because reading it runs nothing.
    """
    for attribute in ('__qualname__', '__name__'):
        name = getattr(receiver, attribute, None)
        if isinstance(name, str) and name:
            return name
    return type(receiver).__name__


def _acknowledge(wakes: list[Wake]) -> None:
    """Release everything waiting on this batch."""
    for wake in wakes:
        if wake.done is not None:
            wake.done.set()


class EventRecorder:
    """A bounded queue, and the one thread that drains it into the database."""

    def __init__(self) -> None:
        """Hold nothing: no setting is read and no thread starts until the first event."""
        self._queue: queue.Queue[Event | Wake] | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._stopping = threading.Event()
        self._enabled: bool | None = None
        self._kinds: frozenset[str] | None = None
        self._worker: str | None = None
        self._owner_pid = os.getpid()
        self._fork_hook = False
        self._dropped = 0
        # which threads have handed a batch to the ORM, and so have a connection to
        # close on the way out. Per thread rather than one flag: this object is
        # process-wide, `close_old_connections()` acts on the calling thread, and two
        # writers can overlap — a `stop()` whose join times out leaves the old one
        # running while a replacement starts
        self._touched_database: set[int] = set()
        # its own lock, not _guard: _guard is held across starting a thread, and the
        # counter and the touch set are reached from paths that must not wait on that
        self._counter = threading.Lock()
        # far enough back that the first drop always reports: monotonic() is
        # time since boot on Linux, so a fresh container starts it near zero
        self._reported_at = -DROP_REPORT_INTERVAL

    @property
    def enabled(self) -> bool:
        """Whether this process writes the event feed at all."""
        # one read, kept local: a reset() between two reads would return None
        enabled = self._enabled
        if enabled is None:
            enabled = self._enabled = self._read_flag()
        return enabled

    @property
    def active(self) -> bool:
        """Whether anything at all is reading events: the table, a receiver, or both.

        This is the gate every seam that *produces* an event belongs behind, and
        :attr:`enabled` is not — a project that connects a receiver and leaves the
        table off must still get its events, and gating on the table alone is how
        an advertised metric comes out silently empty.

        ``bool(receivers)`` rather than ``has_listeners()``: 7ns against 172ns
        measured, and this is read once per event. The difference is a receiver
        whose weak reference has died but whose entry has not been cleaned up yet,
        which makes this answer yes while nothing listens — so an event is recorded
        that nobody reads, and ``send_robust`` short-circuits on it. Wasted work,
        never a wrong row. ``tests/test_metrics_seam.py`` pins the attribute, since
        it is Django's to rename.
        """
        return self.enabled or bool(events_recorded.receivers)

    @property
    def wants_payload(self) -> bool:
        """Whether anything will read a payload summary, which is the costly part.

        Only the table does. ``describe()`` redacts credentials, walks the
        structure and bounds the result — measured in tens of microseconds, against
        nothing for a counter keyed on ``kind`` and ``function``. So unless the log
        is on too, a receiver gets ``Event`` objects whose ``detail`` carries what
        the seam measured itself and not the summarised arguments. Rows are what the
        table gets; with the log off there are none.
        """
        return self.enabled

    @property
    def worker(self) -> str:
        """Name the process recording, cached: gethostname() is a system call."""
        # one read, kept local, for the same reason `enabled` does it
        worker = self._worker
        if worker is None:
            worker = self._worker = worker_identity()
        return worker

    def _read_flag(self) -> bool:
        """Read the flag once, treating an unreadable one as off."""
        try:
            return coerce_bool(conf['EVENT_LOG'], f"{SETTINGS_NAME}['EVENT_LOG']")
        except Exception:
            # a misconfigured flag is E031's finding at boot; at runtime it must
            # not become the reason a message was not sent
            logger.exception('could not read the event log flag; recording is off')
            return False

    def wants(self, kind: str) -> bool:
        """Whether this kind is one the project asked to keep."""
        kinds = self._kinds
        if kinds is None:
            configured = conf['EVENT_LOG_KINDS'] or ()
            kinds = self._kinds = frozenset(str(name) for name in configured) or known_kinds()
        return kind in kinds

    def record(self, event: Event) -> None:
        """Hand one event over. Never blocks, never raises, never touches the ORM."""
        if not self.active:
            return
        try:
            if not self.wants(event.kind):
                return
            if not event.worker:
                # every row says which process recorded it, and only the
                # consumer knew its own name before
                event = replace(event, worker=self.worker)
            if self._write_here():
                # counted, which it was not: under EVENT_LOG_SYNC the row is written on
                # this thread, and a database that refuses it raises `EventLogRefusedError`
                # — which the broad `except` below logged and did not count, so the row
                # vanished with no gap marker. A one-row batch either lands or raises, so
                # there is no partial case to weigh here, only this one
                try:
                    self._deliver([event])
                except Exception:
                    self._drop(1)
                    logger.exception('could not record an event on the calling thread', extra={'tg_kind': event.kind})
                finally:
                    # this thread wrote, and this thread is not the one that closes: the
                    # mark exists for the writer's exit, and a caller's mark left behind
                    # outlives the caller. Thread idents are reused, so a receiver-only
                    # writer could inherit one and close a connection it never opened —
                    # importing `eventlog`, and `django.db` with it
                    self._forget_touch()
                return
            buffer = self._buffer()
            buffer.put_nowait(event)
            if self._queue is not buffer:
                # stop() detached this queue between the lookup and the put, and
                # nothing will ever drain a detached one again
                self._rehome(buffer)
        except queue.Full:
            self._drop(1)
        except Exception:
            # the recorder failing is not the caller's problem to handle
            logger.exception('could not record an event', extra={'tg_kind': event.kind})

    def _rehome(self, orphan: 'queue.Queue[Event | Wake]') -> None:
        """Move what is in a detached queue onto the live one.

        Not "move my own event": ``get_nowait`` may hand back somebody else's, and
        it does not matter which — what matters is that the detached queue ends up
        empty and everything in it lands somewhere that will be drained. Two
        producers doing this at once take disjoint items, and stop()'s own drain
        competing with them is equally harmless.

        Both halves are non-blocking, so :meth:`record`'s promise holds: an event
        that cannot be rehomed because the live queue is full is counted, which is
        what would have happened to it there anyway.
        """
        while True:
            try:
                item = orphan.get_nowait()
            except queue.Empty:
                return
            try:
                self._buffer().put_nowait(item)
            except queue.Full:
                if isinstance(item, Wake):
                    _acknowledge([item])
                    continue
                self._drop(1)

    def _write_here(self) -> bool:
        """Whether to write on this thread instead of handing it to the writer.

        Only when asked to, and never from inside a running loop: the ORM is
        @async_unsafe there, so the seam that records an update would raise
        SynchronousOnlyOperation instead of recording anything.

        Requires the log as well as the flag. ``EVENT_LOG_SYNC`` is about *where the
        insert happens*, so with nothing being inserted it has nothing to say — and
        answering yes would have a process that only has receivers run them on the
        thread that recorded the event, which is the one thing this design exists
        to avoid.
        """
        if not self.enabled:
            return False
        if not coerce_bool(conf['EVENT_LOG_SYNC'], f"{SETTINGS_NAME}['EVENT_LOG_SYNC']"):
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return True
        return False

    def _drop(self, count: int) -> None:
        """Count lost events, and say so at most once a minute.

        The counter is guarded because more than one thread reaches it: producers
        on a full queue, the writer on a failed flush, and whichever thread called
        stop() draining what the writer left. `+=` is a read and a write, so
        without this a drop is silently swallowed by a concurrent one.
        """
        if not count:
            # callers pass the refused count straight through, and that is zero on every
            # successful write — reporting "the event log is falling behind" for a batch
            # that landed in full is a false alarm on the line people watch for real ones
            return
        with self._counter:
            self._dropped += count
            dropped = self._dropped
        now = time.monotonic()
        if now - self._reported_at < DROP_REPORT_INTERVAL:
            return
        self._reported_at = now
        logger.error(
            'the event log is falling behind; events are being dropped',
            extra={'tg_dropped': dropped},
        )

    def _buffer(self) -> queue.Queue[Event | Wake]:
        """Return the queue, starting the writer the first time anything is recorded."""
        if self._owner_pid != os.getpid():
            # a thread does not survive fork(), but the queue object does, so a
            # child would fill one nobody drains
            self._forget()
        buffer = self._queue
        if buffer is not None:
            return buffer
        with self._guard:
            if self._queue is None:
                self._install_fork_hook()
                self._stopping.clear()
                self._owner_pid = os.getpid()
                buffer = queue.Queue(maxsize=max(1, int(_number('EVENT_LOG_BUFFER_SIZE', int))))
                thread = threading.Thread(target=self._run, args=(buffer,), name=WRITER_THREAD, daemon=True)
                self._queue, self._thread = buffer, thread
                try:
                    thread.start()
                except RuntimeError:
                    # out of threads: leave nothing half-built for the next call,
                    # and count the event this loses so the gap row still says so
                    self._queue = self._thread = None
                    self._drop(1)
                    raise
                # CPython runs atexit callbacks while daemon threads are still
                # alive, so the writer is still joinable from one
                atexit.register(self.stop)
            return self._queue

    def _install_fork_hook(self) -> None:
        """Reset in the child as well as on the pid check, where the platform allows."""
        if self._fork_hook or not hasattr(os, 'register_at_fork'):
            return
        self._fork_hook = True
        os.register_at_fork(after_in_child=self._forget)

    def _forget(self) -> None:
        """Drop everything a fork invalidated, so the next event starts fresh."""
        # a new lock: the parent may have held this one at the moment of the fork
        self._guard = threading.Lock()
        self._counter = threading.Lock()
        self._queue = None
        self._thread = None
        self._owner_pid = os.getpid()
        self._touched_database = set()
        self._dropped = 0
        self._reported_at = -DROP_REPORT_INTERVAL

    def _run(self, buffer: queue.Queue[Event | Wake]) -> None:
        """Drain the queue into the database until stopped.

        A thread target: anything escaping it would end recording for the life
        of the process, so the slot is cleared on the way out and the next
        record() starts a replacement.
        """
        failures = 0
        blocked_until = 0.0
        try:
            while True:
                batch, wakes = self._collect(buffer)
                try:
                    if batch:
                        if time.monotonic() < blocked_until:
                            # the database has been refusing us; keep draining so
                            # producers never fill up, but do not hammer it
                            self._drop(len(batch))
                        else:
                            failures, blocked_until = self._flush(batch, failures=failures)
                finally:
                    # after the write, never before: a waiter released early was
                    # told the batch was durable while it was still in flight
                    _acknowledge(wakes)
                if wakes and self._stopping.is_set() and buffer.empty():
                    return
        except Exception:
            logger.exception('the event writer stopped; it restarts on the next event')
        finally:
            with self._guard:
                if self._queue is buffer:
                    self._queue = self._thread = None
            # the slot is cleared above, so nothing will ever drain this queue
            # again: without this, everything still in it disappears with no row
            # and no counter, and the gap reads as quiet traffic
            self._abandon(buffer)
            if self._took_the_touch():
                # a process that only has receivers never opened one, and importing
                # `eventlog` to close it would pull in `django.db` — the one import
                # this module exists to keep out of a process that does not need it
                self._close_connections()

    def _forget_touch(self) -> None:
        """Drop this thread's mark without acting on it, for a thread that does not close.

        Only the writer's exit closes connections. `record()` under ``EVENT_LOG_SYNC`` and
        `drain_once()` write on their caller's thread, and Django owns that thread's
        connection — so their marks are bookkeeping nobody reads, and idents get reused.
        """
        with self._counter:
            self._touched_database.discard(threading.get_ident())

    def _took_the_touch(self) -> bool:
        """Whether *this* thread handed a batch to the ORM, clearing the mark as it answers.

        Read and cleared together, because the mark describes this writer: left set it
        outlives the thread that earned it, and a later writer with only receivers closes
        a connection it never opened — importing `eventlog`, and with it `django.db`, into
        the one process this module exists to keep it out of. Only a fork cleared it before,
        so a process that wrote once and then had the log turned off carried it for good.

        Per thread, because one flag was not enough either: `stop()` detaches the queue
        before joining, so a join that times out leaves the old writer running while a
        replacement starts, and the old one's exit cleared the new one's flag — the
        replacement then skipped closing the connection it had opened. It is also what the
        mark always meant, since `close_old_connections()` acts on the calling thread.
        """
        ident = threading.get_ident()
        with self._counter:
            touched = ident in self._touched_database
            self._touched_database.discard(ident)
        return touched

    @staticmethod
    def _empty(buffer: 'queue.Queue[Event | Wake]') -> tuple[list[Event], list[Wake]]:
        """Take everything left in a queue, without waiting for more."""
        events: list[Event] = []
        wakes: list[Wake] = []
        while True:
            try:
                item = buffer.get_nowait()
            except queue.Empty:
                return events, wakes
            if isinstance(item, Wake):
                wakes.append(item)
            else:
                events.append(item)

    def _abandon(self, buffer: 'queue.Queue[Event | Wake]') -> None:
        """Write what is left in a queue nobody will drain again, or count it lost.

        **Receivers run on whatever thread calls this**, which is the writer's own
        when it is exiting and the caller's when :meth:`stop` reached a queue the
        writer had already left behind. That is not a lapse in the writer-thread
        contract so much as the end of it: this queue exists precisely because no
        writer will ever drain it, so there is no writer thread to route through.

        Publishing anyway rather than dropping, because these are the last events
        before the process goes — the same reasoning that makes this method write
        them instead of discarding them. The contract says so on all three surfaces
        that state it.
        """
        leftover, wakes = self._empty(buffer)
        _acknowledge(wakes)
        if not leftover:
            return
        try:
            # the refused count, not only the raise: a database that takes some of these
            # rows and refuses others leaves a hole exactly as large as what it refused,
            # and ignoring the return counted that hole as zero
            refused = self._deliver(leftover)
        except Exception:
            logger.exception('could not write the events a stopping writer left behind')
        else:
            self._drop(refused)
            return
        # counted, not silent: the next flush that succeeds turns this into a
        # log.dropped row, which is the only place the gap becomes visible
        self._drop(len(leftover))

    @staticmethod
    def flush_interval() -> int:
        """Seconds before a partial batch is written anyway, as ``E038`` defines it.

        An integer, matching the check and the settings page. Read as a float this
        honoured a fractional interval the check refuses, so a value could pass
        ``manage.py check`` and then behave in a way the check called impossible — one
        setting with two rules. Named rather than inline so the rule has one reader and a
        test can ask it directly.
        """
        return int(max(1, _number('EVENT_LOG_FLUSH_INTERVAL', int)))

    def _collect(self, buffer: queue.Queue[Event | Wake]) -> tuple[list[Event], list[Wake]]:
        """Gather up to one batch, with any wake-ups that ended the wait."""
        interval = self.flush_interval()
        limit = max(1, int(_number('EVENT_LOG_BATCH_SIZE', int)))
        deadline = time.monotonic() + interval
        batch: list[Event] = []
        wakes: list[Wake] = []
        while len(batch) < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = buffer.get(timeout=remaining)
            except queue.Empty:
                break
            if isinstance(item, Wake):
                wakes.append(item)
                break
            batch.append(item)
        return batch, wakes

    def _flush(self, batch: list[Event], *, failures: int) -> tuple[int, float]:
        """Write one batch and publish it, containing whatever the write raises.

        Both happen inside :meth:`_deliver`, which writes first and publishes in a
        ``finally`` — so a failing database costs rows and not metrics, and nothing
        reaching the ``except`` here came from a receiver: :meth:`_publish` cannot
        raise.

        Which means **receivers run on whatever thread calls this**, and that is not
        only the writer's: :meth:`drain_once` calls it on the caller's, which is what
        lets a test drive the real flush path. The signal's own documentation states
        the rule that way round rather than listing the threads, so a fourth one does
        not make it wrong.
        """
        # under the counter's lock, both of them: `_drop`'s docstring already names
        # "the writer on a failed flush" among the threads it protects against, and
        # this was the one place that read and wrote the count without taking it —
        # so a producer's drop landing between this `+=`'s read and its write was
        # silently discarded, and the `log.dropped` row then under-reported the gap
        with self._counter:
            dropped_before = self._dropped
        try:
            refused = self._deliver(batch)
        except Exception:
            failures += 1
            with self._counter:
                self._dropped += len(batch)
            # one line per failure, not two: the suspension is a different
            # sentence about the same exception, not a second thing that broke
            if failures >= FAILURE_LIMIT:
                logger.exception(
                    'the event log is suspended after repeated failures; run migrate or check the database',
                    extra={'tg_count': len(batch), 'tg_failures': failures},
                )
                return 0, time.monotonic() + FAILURE_BACKOFF
            logger.exception(
                'could not write an event batch',
                extra={'tg_count': len(batch), 'tg_failures': failures},
            )
            return failures, 0.0
        if refused:
            # rows this very batch lost, one at a time, on the ladder below `write_batch`.
            # Counted rather than reported now: the gap row belongs to the *next*
            # successful flush, the same way a producer's drop does
            with self._counter:
                self._dropped += refused
            logger.warning(
                'the database refused part of an event batch',
                extra={'tg_count': refused, 'tg_batch': len(batch)},
            )
        if dropped_before:
            self._record_gap(dropped_before)
        return 0, 0.0

    def _record_gap(self, dropped: int) -> None:
        """Put the gap in the feed, not only in the log: a silent hole reads as coverage.

        **Claimed, then written, and given back if the write fails.** Two flushes can be
        in progress at once — the writer thread's and a ``drain_once()`` on somebody
        else's — and both snapshot the drop count before their batch. Subtracting after
        the write let each of them report the same hole and take it off twice, which
        drives the count negative; subtracting before the write, which is where this
        started, lost the hole whenever the gap row itself was refused. Taking the count
        out of the counter first makes the claim exclusive, and putting it back on failure
        keeps it for the next flush. Both properties, one lock.

        Claims no more than is there: a count that another flush has already taken leaves
        nothing to report, and this returns rather than writing a row about zero events.
        Anything a producer drops while the write is in flight stays for the next one.

        A refusal counts as a failure here, not only an exception. ``_deliver`` returns how
        many rows the database refused one at a time, and this batch is one row — so a
        return of 1 means the gap row did *not* land, which is the same loss as a raise and
        was the one path this method used to ignore. Both give the claim back.

        The failure stays suppressed either way: the batch this follows did land, and a
        gap row that cannot be written must not turn a successful flush into a failed one.
        """
        with self._counter:
            claimed = min(dropped, self._dropped)
            self._dropped -= claimed
        if not claimed:
            return
        try:
            refused = self._deliver([Event(kind=EventKind.LOG_DROPPED.value, detail={'dropped': claimed})])
        except Exception:
            self._reclaim(claimed)
            logger.exception('could not record the gap; keeping the count for the next flush')
            return
        if refused:
            self._reclaim(claimed)
            logger.error(
                'the database refused the gap row; keeping the count for the next flush',
                extra={'tg_dropped': claimed},
            )

    def _reclaim(self, claimed: int) -> None:
        """Put a claim back, so a gap nobody could record survives to be recorded."""
        with self._counter:
            self._dropped += claimed

    @staticmethod
    def _write(batch: list[Event]) -> int:
        """Hand a batch to the ORM, importing it here so a disabled process never does.

        Returns how many rows did not land, which only a partial refusal produces.
        """
        from django_redis_aiogram.eventlog import write_batch  # noqa: PLC0415 - the point: no django.db above

        return write_batch(batch)

    def _publish(self, batch: list[Event]) -> None:
        """Hand a batch to whoever connected to :data:`events_recorded`.

        ``send_robust``, so one broken receiver neither loses the batch for the
        others nor stops the writer, and it is logged here because a receiver that
        fails silently is a metric that reads as zero traffic. Django logs it too, on
        its own ``django.dispatch`` logger; the line here is on the logger a project
        configures for this package, which is where it will actually be seen.

        **Wrapped anyway, because ``send_robust`` does not contain everything.**
        Django's own failure logging reads ``receiver.__qualname__`` unguarded, and a
        callable *instance* — an ordinary shape for a metrics collector — has no such
        attribute. So a receiver like that raising makes ``send_robust`` itself raise
        ``AttributeError``, measured on Django 6.1, and without this ``try`` it would
        land in :meth:`_flush`'s ``except`` and be counted as a failed *write*: the
        other receivers lose the batch, a ``log.dropped`` row appears, and the log
        blames the database for something a receiver did. Containing it here makes
        this method's promise true whatever Django does with it, on any supported
        version.

        The upshot is a method that **cannot raise**, which is the property the rest
        of the writer needs from it rather than a defensive habit.

        One limit worth stating, because it is Django's and not ours: when
        ``send_robust`` raises on that unnamed receiver it abandons **its own loop**, so
        receivers connected after the offending one do not run for that batch at all.
        Containing it here keeps the write and every earlier receiver whole; it cannot
        reach past Django into a dispatch that already stopped. Calling receivers
        ourselves would need ``Signal._live_receivers``, a private API, which is a worse
        trade than one documented sentence. A collector written as a callable instance
        can close the gap on its side by defining ``__qualname__``; one written as a
        function or a bound method has it already, and is the shape every recipe uses.

        A tuple rather than the list itself: receivers run one after another with
        the same argument, so one of them sorting or clearing a list would decide
        what the next one sees.
        """
        if not events_recorded.receivers:
            return
        # the reporting loop is inside the guard as well as the dispatch, because
        # `getattr(..., None)` absorbs only `AttributeError` — a receiver whose
        # `__getattr__` raises anything else makes naming it raise, and the whole
        # point is that nothing about a receiver reaches `_flush`'s failure counter
        try:
            for receiver, outcome in events_recorded.send_robust(sender=self, events=tuple(batch)):
                if isinstance(outcome, BaseException):
                    logger.error(
                        'an events_recorded receiver raised',
                        exc_info=outcome,
                        extra={'tg_receiver': _receiver_name(receiver), 'tg_count': len(batch)},
                    )
        except Exception:
            # even this is suppressed: `logger.exception` is `logger.error` with
            # `exc_info`, so a project whose handler or formatter raises would take
            # the fallback out too — and the whole purpose here is that **nothing**
            # about publishing reaches `_flush`'s failure counter, where it would be
            # reported as a database refusing a batch it never saw
            with contextlib.suppress(Exception):
                logger.exception('publishing recorded events failed', extra={'tg_count': len(batch)})

    def _deliver(self, batch: list[Event]) -> int:
        """Write a batch if this process keeps the table, then publish it either way.

        Returns how many rows the database refused individually — zero unless a partial
        refusal happened, and zero in a process that does not write at all.


        The order is the contract, and it is two claims rather than one. The write
        is **attempted first**, so nothing a receiver does can change a row that was
        written — which is why receivers get the real ``Event`` objects and not
        copies. And the publish is in a ``finally``, so a write that *failed* still
        reaches them: a database that is down or unmigrated is exactly when someone
        is watching a dashboard, and the metrics have no reason to go with it. A
        receiver seeing a batch is therefore not evidence that a row exists for it.

        Publishing first was the original order, and it handed receivers the same
        list and the same ``Event`` objects the ORM was about to read. A frozen
        dataclass does not freeze the ``detail`` dict inside it, so a receiver
        clearing the list or editing a ``detail`` changed what got persisted. This
        way round makes that impossible instead of asking receivers to be careful.
        """
        refused = 0
        try:
            if self.enabled:
                with self._counter:
                    self._touched_database.add(threading.get_ident())
                refused = self._write(batch)
        finally:
            self._publish(batch)
        return refused

    @staticmethod
    def _close_connections() -> None:
        """Release the writer thread's own connection on the way out."""
        try:
            from django_redis_aiogram.eventlog import close_connections  # noqa: PLC0415 - as above
        except Exception:
            logger.exception('could not import the event log to close its connection')
            return
        close_connections()

    def drain_once(self, timeout: float = 0.0) -> int:
        """Write whatever is buffered, on the calling thread. Returns rows written.

        Goes through the same flush the writer uses, gap recording included, so
        a test driving this exercises the path production takes.
        """
        buffer = self._queue
        if buffer is None:
            return 0
        batch: list[Event] = []
        wakes: list[Wake] = []
        deadline = time.monotonic() + timeout
        while True:
            try:
                item = buffer.get(timeout=max(0.0, deadline - time.monotonic())) if timeout else buffer.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Wake):
                wakes.append(item)
                continue
            batch.append(item)
        try:
            if batch:
                self._flush(batch, failures=0)
        finally:
            # same reason as the synchronous `record()` path: this thread wrote, and it is
            # not the thread whose exit closes connections
            self._forget_touch()
            _acknowledge(wakes)
        return len(batch)

    def flush(self, timeout: float = STOP_TIMEOUT) -> None:
        """Wait until what has been recorded so far has reached the database.

        Waits on an acknowledgement from the writer rather than on the queue
        going empty: the queue empties when a batch is *taken*, which is before
        it is written, so polling it would return mid-insert.
        """
        buffer = self._queue
        if buffer is None:
            return
        done = threading.Event()
        try:
            buffer.put_nowait(Wake(done))
        except queue.Full:
            # no room even for the marker, so there is nothing to wait behind
            return
        if not done.wait(timeout):
            logger.warning('the event writer did not flush in time', extra={'tg_timeout': timeout})

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Flush and end the writer. Idempotent: atexit and start_tgbot both call it."""
        with self._guard:
            buffer, thread = self._queue, self._thread
            self._queue = self._thread = None
        if buffer is None:
            return
        with contextlib.suppress(Exception):
            atexit.unregister(self.stop)
        self._stopping.set()
        with contextlib.suppress(queue.Full):
            buffer.put_nowait(Wake())
        if thread is not None:
            # a thread that never started cannot be joined, and this runs from
            # atexit where raising is noise nobody can act on
            with contextlib.suppress(RuntimeError):
                thread.join(timeout)
            if thread.is_alive():
                logger.warning('the event writer did not finish in time', extra={'tg_timeout': timeout})
        # a record() that read self._queue before the swap above puts into a queue
        # this method has already detached, and nothing else will ever look at it.
        # Draining after the join is what keeps those events; the few instructions
        # between this drain and the producer's put stay a gap, because closing it
        # would mean a lock on the one path that may never wait
        try:
            self._abandon(buffer)
        finally:
            # the same rule as the synchronous `record()` and `drain_once()`: this ran on
            # whoever called `stop()`, and that thread is not the one whose exit closes
            # connections. Left behind, its mark can be inherited by a later
            # receiver-only writer through a reused ident.
            #
            # Unless `stop()` was called *from* the writer, where `_run` is still on the
            # stack below and has that mark to consume on its way out. Not covered by a
            # test: reaching it means a receiver calling `stop()`, and doing that leaves
            # the writer alive — measured, a 10 second join and it never exits — so the
            # test would pin a hang rather than this branch. Tracked in #136
            if threading.current_thread() is not thread:
                self._forget_touch()

    def reset(self) -> None:
        """Re-read the settings next time; used by override_settings.

        It does not flush. Every ``override_settings(TELEGRAM_BOT=...)`` in a
        consumer's own test suite fires this twice, and waiting for the writer
        there would put a second on each one. A test that needs its rows calls
        :meth:`flush`; queued events survive the reset either way.
        """
        self._enabled = None
        self._kinds = None
        self._worker = None


recorder = EventRecorder()


def _reset_on_setting_change(setting: str, **_kwargs: object) -> None:
    """Drop the cached flags after writing whatever was recorded under the old ones."""
    if setting == SETTINGS_NAME:
        recorder.reset()


# dispatch_uid keeps autoreload from stacking duplicate receivers
setting_changed.connect(_reset_on_setting_change, dispatch_uid='django_redis_aiogram.recorder')
