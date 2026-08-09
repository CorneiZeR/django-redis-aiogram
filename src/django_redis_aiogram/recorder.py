"""Record what happened, without making anyone wait for the database.

Every seam that records runs somewhere the ORM cannot be used directly: the
send coroutine runs on the bot's event loop, the delivery consumer runs on a
thread nothing manages a connection for, and a Django view must not pay a
second round trip to log the one it just made. All of them hand an :class:`Event`
to a bounded queue that one writer thread drains in batches.

``record()`` reaches only ``Queue.put_nowait`` — a lock, a deque append and a
notify. Nothing in that chain is decorated ``@async_unsafe``, which is what
makes it legal from a coroutine with no ``sync_to_async`` and no
``SynchronousOnlyOperation``. It also avoids what a synchronous insert would do
inside a caller's ``atomic()`` block: on PostgreSQL a failed statement aborts
the whole transaction, so logging would corrupt the caller's data.

This module must not import ``django.db``. :mod:`django_redis_aiogram.eventlog`
does, and the writer thread imports it on its first flush.
"""

import atexit
import contextlib
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from django.core.signals import setting_changed

from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.events import known_kinds, new_correlation_id, worker_identity
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf

logger = logging.getLogger('django_redis_aiogram')

#: the writer's thread name, so a log line or a test can name it
WRITER_THREAD = 'tgbot-event-writer'

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
        if not self.enabled:
            return
        try:
            if not self.wants(event.kind):
                return
            if not event.worker:
                # every row says which process recorded it, and only the
                # consumer knew its own name before
                event = replace(event, worker=self.worker)
            if coerce_bool(conf['EVENT_LOG_SYNC'], f"{SETTINGS_NAME}['EVENT_LOG_SYNC']"):
                self._write([event])
                return
            self._buffer().put_nowait(event)
        except queue.Full:
            self._drop(1)
        except Exception:
            # the recorder failing is not the caller's problem to handle
            logger.exception('could not record an event', extra={'tg_kind': event.kind})

    def _drop(self, count: int) -> None:
        """Count lost events, and say so at most once a minute."""
        self._dropped += count
        now = time.monotonic()
        if now - self._reported_at < DROP_REPORT_INTERVAL:
            return
        self._reported_at = now
        logger.error(
            'the event log is falling behind; events are being dropped',
            extra={'tg_dropped': self._dropped},
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
                buffer = queue.Queue(maxsize=max(1, int(conf['EVENT_LOG_BUFFER_SIZE'])))
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
        self._queue = None
        self._thread = None
        self._owner_pid = os.getpid()
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
            self._close_connections()

    def _collect(self, buffer: queue.Queue[Event | Wake]) -> tuple[list[Event], list[Wake]]:
        """Gather up to one batch, with any wake-ups that ended the wait."""
        interval = max(0.01, float(conf['EVENT_LOG_FLUSH_INTERVAL']))
        limit = max(1, int(conf['EVENT_LOG_BATCH_SIZE']))
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
        """Write one batch, containing whatever it raises."""
        dropped_before = self._dropped
        try:
            self._write(batch)
        except Exception:
            failures += 1
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
        if dropped_before:
            self._record_gap(dropped_before)
        return 0, 0.0

    def _record_gap(self, dropped: int) -> None:
        """Put the gap in the feed, not only in the log: a silent hole reads as coverage."""
        self._dropped = 0
        with contextlib.suppress(Exception):
            self._write([Event(kind=EventKind.LOG_DROPPED.value, detail={'dropped': dropped})])

    @staticmethod
    def _write(batch: list[Event]) -> None:
        """Hand a batch to the ORM, importing it here so a disabled process never does."""
        from django_redis_aiogram.eventlog import write_batch  # noqa: PLC0415 - the point: no django.db above

        write_batch(batch)

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
