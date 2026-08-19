"""The bot object Django code talks to.

One facade over an aiogram ``Bot``, ``Dispatcher`` and ``Router``, built lazily
so that importing the package costs nothing in the processes — web workers, cron
jobs, the test suite — that only ever queue a message.
"""

import asyncio
import contextlib
import logging
import math
import threading
import time
import uuid
import weakref
from asyncio import AbstractEventLoop
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping
from concurrent import futures
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher, Router, exceptions
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.handler import CallbackType
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Update
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string
from redis import Redis

from django_redis_aiogram.api import check_function
from django_redis_aiogram.context import current_correlation_id
from django_redis_aiogram.defaults import DEFAULTS
from django_redis_aiogram.enums import EventKind, StorageKind
from django_redis_aiogram.envelope import pack
from django_redis_aiogram.events import new_correlation_id
from django_redis_aiogram.exceptions import LoopThreadNotStartedError, ShuttingDownError
from django_redis_aiogram.instrumentation import install_instrumentation, instrumented
from django_redis_aiogram.payloads import describe
from django_redis_aiogram.recorder import Event, as_identifier, recorder
from django_redis_aiogram.redis import (
    aclose_redis,
    aget_redis,
    connection_kwargs,
    get_redis,
    processing_key,
    queue_key,
)
from django_redis_aiogram.serializers import get_serializer
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf
from django_redis_aiogram.throttling import RateLimiter, get_rate_limiter

logger = logging.getLogger('django_redis_aiogram')

#: how a scheduled send carries its correlation id, so shutdown can name what
#: it cancelled without threading an argument through asyncio
TASK_PREFIX = 'tgbot:'

#: the loop thread a web process starts, so a log line or a test can name it
LOOP_THREAD = 'tgbot-loop'
#: how long starting or stopping that thread may take before it is worth saying so
RUNNER_TIMEOUT = 5.0


def resolve_correlation_id(supplied: uuid.UUID | str | None) -> uuid.UUID:
    """Pick the identifier this send belongs to.

    An explicit argument wins, then whatever update is being handled here, then
    a fresh one. Reading the context variable happens synchronously, before any
    scheduling: _hand_off creates its task from a call_soon_threadsafe callback
    whose context belongs to the loop, so a read from in there is empty.
    """
    if isinstance(supplied, uuid.UUID):
        return supplied
    if isinstance(supplied, str) and supplied:
        try:
            return uuid.UUID(supplied)
        except ValueError:
            msg = f'correlation_id must be a UUID, got {supplied!r}.'
            raise ValueError(msg) from None
    return current_correlation_id() or new_correlation_id()


@dataclass(frozen=True)
class Outbound:
    """What every stage of one outbound send needs to name itself."""

    correlation_id: uuid.UUID
    function: str
    call_kwargs: dict[str, Any]


def task_correlation_id(task: 'asyncio.Task[Any]') -> uuid.UUID:
    """Recover the id a scheduled send was named with, or mint one to say so."""
    name = task.get_name()
    if name.startswith(TASK_PREFIX):
        try:
            return uuid.UUID(name.removeprefix(TASK_PREFIX))
        except ValueError:
            pass
    return new_correlation_id()


# run_until_complete is not reentrant, and the loop — not the bot — is what
# cannot be entered twice. Two bots handed the same loop must share one lock.
_loop_locks: 'weakref.WeakKeyDictionary[AbstractEventLoop, threading.Lock]' = weakref.WeakKeyDictionary()
_loop_locks_guard = threading.Lock()


def _completion(on_complete: Callable[[], None]) -> 'Callable[[asyncio.Task[None]], None]':
    """Turn a completion callback into a task done-callback.

    Cancellation is not completion. A send drained away at shutdown never reached
    Telegram, so the consumer must *not* acknowledge it — leaving it in the
    in-flight list is exactly what lets the next start pick it up again, and is
    what makes the at-least-once guarantee true rather than documented.

    Everything else counts as finished, including a send that was refused or that
    gave up: redelivering those would only fail again, which is the contract
    `Delivery.dispatch` has always had.
    """

    def done(task: 'asyncio.Task[None]') -> None:
        """Settle unless the task was cancelled, which is the one case that must not.

        Cancellation says the task did not finish, and nothing about what Telegram saw:
        the request may already have been sent, or even acted on, when the cancel landed
        on the await. So the message stays unacknowledged and will be redelivered — which
        can duplicate it, and is the trade this release makes deliberately, because the
        alternative is acknowledging a send whose outcome nobody ever learned.

        This is not a rare path: it is what ``_drain`` does to whatever outlasts
        ``DRAIN_TIMEOUT`` at shutdown.
        """
        if task.cancelled():
            return
        _settle(on_complete)

    return done


def _settle(on_complete: Callable[[], None] | None) -> None:
    """Say the send is finished, without letting that break anything.

    The callback runs on the loop's callback path, where an exception would be
    reported against the task rather than against whatever the callback does —
    and the send itself is over either way.
    """
    if on_complete is None:
        return
    try:
        on_complete()
    except Exception:
        logger.exception('could not acknowledge a completed send')


@dataclass
class Queueing:
    """One write a producer is about to make, and what it stands for.

    Carries the ids so a failure can be recorded against the messages that were
    actually lost: a variadic ``RPUSH`` fails for its whole chunk, not one entry.
    """

    key: str
    payloads: list[bytes]
    messages: list[tuple[uuid.UUID, dict[str, Any]]]
    queued_at: float


@contextlib.contextmanager
def queueing(function: str, messages: list[tuple[uuid.UUID, dict[str, Any]]]) -> 'Iterator[Queueing]':
    """Everything a queue write does, except the write.

    The one step that cannot be shared between a synchronous producer and an
    asynchronous one is the ``await`` — the language will not allow it. Everything
    around it can be, and is: the serialisation, the key, and both event rows,
    including the rule that a message lost on the way to Redis records a drop
    rather than letting silence imply it was queued. Resolving the serializer and
    the key sits outside that guard on purpose — a misconfigured ``SERIALIZER``
    fails identically for every send ever made, and the exception is where that
    belongs, not a drop row per message for as long as it stays misconfigured.

    So each transport is the two lines that write, and nothing else. The consumer
    knows one payload shape and the event log has one definition of ``queued``;
    neither can drift between the two paths, because neither path owns them.

    The two ways a message is lost here are **not** the same, and the ``stage`` on
    the drop row is what tells them apart. ``serialising`` means the payload never
    left this process, so re-sending it is safe. ``queueing`` means the write to
    Redis raised, and a variadic ``RPUSH`` that raised may still have been applied —
    the reply is what went missing — so re-sending may duplicate. A broadcast makes
    that distinction the only one available, because the ids go with the exception.
    """
    queued_at = time.time()
    serializer = get_serializer()
    # through the helper, not the setting: the consumer, both depth reads and
    # `tgbot_reclaim` all derive their keys from it, and a producer reading the
    # setting itself is the one writer that would not follow it anywhere it goes
    key = queue_key()

    def dropped(stage: str, error: Exception) -> None:
        """Record every message this failure lost, and where it lost them."""
        for identifier, kwargs in messages:
            recorder.record(
                Event(
                    kind=EventKind.OUTBOUND_DROPPED.value,
                    correlation_id=identifier,
                    function=function,
                    chat_id=as_identifier(kwargs.get('chat_id')),
                    error_code=type(error).__name__,
                    error=str(error),
                    detail={'stage': stage},
                )
            )

    try:
        # guarded, not left to the caller: a payload that cannot be serialised
        # loses its message exactly as a refused write does, and for a chunk the
        # ids go with the exception — so these rows are the only record of which
        # messages were lost
        write = Queueing(
            key=key,
            payloads=[
                serializer.dumps(pack(function, kwargs, identifier, queued_at)) for identifier, kwargs in messages
            ],
            messages=messages,
            queued_at=queued_at,
        )
    except Exception as error:
        dropped('serialising', error)
        raise
    try:
        yield write
    except Exception as error:
        dropped('queueing', error)
        raise
    if not recorder.active:
        # nothing keeps the table and nothing listens, so there is no event to make
        return
    # two gates, not one: whether to record at all is a different question from
    # whether to summarise the arguments, and describing them is the expensive
    # half. A metrics receiver counts sends; it does not read message bodies
    described = recorder.wants_payload
    for identifier, kwargs in messages:
        recorder.record(
            Event(
                kind=EventKind.OUTBOUND_QUEUED.value,
                correlation_id=identifier,
                created_at=queued_at,
                function=function,
                chat_id=as_identifier(kwargs.get('chat_id')),
                detail=describe(kwargs) if described else None,
            )
        )


#: latched once per process: a line per send would be noise nobody can act on
_asend_mentioned = threading.Event()


def _mention_asend(alternative: str) -> None:
    """Say once that there is a version of this that does not block the loop.

    Deliberately not a ``DeprecationWarning``: calling the synchronous method from
    async code is *correct* and nothing about it will stop working. It writes to a
    socket on the thread the loop is running on, which is worth knowing once and is
    not worth an exception.

    From every synchronous route that writes to Redis, which is three of them —
    :meth:`send`, :meth:`send_redis` and :meth:`send_many`. Naming only the first
    left the two a web tier is most likely to reach for silent, and the fan-out is
    the one that holds the loop longest.

    Not from :meth:`send_raw`: there the caller is the worker's own consumer, which
    has no async alternative to move to, and a warning on a healthy path is how
    people learn to stop reading them.
    """
    if _asend_mentioned.is_set():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _asend_mentioned.set()
    logger.warning(
        'a synchronous send was called from a running event loop',
        extra={'tg_alternative': alternative},
    )


def loop_lock(loop: AbstractEventLoop) -> threading.Lock:
    """Return the one lock that everything driving ``loop`` has to hold."""
    with _loop_locks_guard:
        lock = _loop_locks.get(loop)
        if lock is None:
            lock = _loop_locks[loop] = threading.Lock()
        return lock


def drain_budget() -> float:
    """How long :meth:`TelegramBot.close` may spend draining.

    Falls back rather than raising: check E044 reports an unreadable value at boot,
    and shutdown is the worst moment to refuse — the drain sits between stopping
    the consumer and flushing the event log, so an exception here costs the rows
    that describe what the drain just did.
    """
    try:
        budget = float(conf['DRAIN_TIMEOUT'])
    except (TypeError, ValueError):
        budget = float(DEFAULTS['DRAIN_TIMEOUT'])
    return budget if math.isfinite(budget) and budget >= 0 else float(DEFAULTS['DRAIN_TIMEOUT'])


def build_default_properties() -> DefaultBotProperties:
    """Build the bot-wide defaults such as parse_mode.

    aiogram applies these to every call, which is why unset fields carry a
    ``Default`` sentinel rather than None.
    """
    properties: Mapping[str, Any] = conf['DEFAULT_BOT_PROPERTIES']
    try:
        return DefaultBotProperties(**properties)
    except TypeError as error:
        msg = f"{SETTINGS_NAME}['DEFAULT_BOT_PROPERTIES']: {error}"
        raise ImproperlyConfigured(msg) from None


def build_storage() -> BaseStorage:
    """Build the FSM storage: 'redis', 'memory', or a dotted path to a BaseStorage."""
    name: str = conf['FSM_STORAGE']
    if name == StorageKind.MEMORY:
        return instrumented(MemoryStorage())
    if name == StorageKind.REDIS:
        url = str(conf['REDIS_URL'] or '').strip()
        if not url:
            msg = f"{SETTINGS_NAME}['REDIS_URL'] is required for the redis FSM storage."
            raise ImproperlyConfigured(msg)
        # the same deadlines the shared client gets: every update reads FSM state,
        # so a half-open Redis here wedges the whole bot rather than one send
        return instrumented(RedisStorage.from_url(url, connection_kwargs=connection_kwargs()))

    try:
        storage_class = import_string(name)
    except ImportError as error:
        msg = f"{SETTINGS_NAME}['FSM_STORAGE'] cannot be imported: {error}"
        raise ImproperlyConfigured(msg) from error
    if not (isinstance(storage_class, type) and issubclass(storage_class, BaseStorage)):
        msg = f"{SETTINGS_NAME}['FSM_STORAGE'] must point to a BaseStorage subclass, got {name!r}."
        raise ImproperlyConfigured(msg)
    # wrapped last, so the project's own class is validated before it is hidden
    return instrumented(storage_class())


class TelegramBot:
    """Facade over an aiogram bot, dispatcher and router.

    Everything expensive — the aiogram ``Bot``, the ``Dispatcher`` and the event
    loop — is built on first use. Instantiating this class must stay cheap and
    must not require a token, otherwise merely importing the package would break
    projects that never talk to Telegram.
    """

    def __init__(
        self,
        max_retries: int | None = None,
        loop: AbstractEventLoop | None = None,
    ) -> None:
        """Record the overrides; nothing aiogram or Redis owns is built here."""
        self._max_retries = max_retries
        self._loop = loop
        self._bot: Bot | None = None
        self._dispatcher: Dispatcher | None = None
        self._router = Router()
        #: sends this bot scheduled, so shutdown drains its own work only
        # the call behind each task, so shutdown can say what it cancelled
        self._sends: dict[asyncio.Task[None], Outbound] = {}
        self._polling = False
        self._closing = False
        #: the thread a web process gives the loop, so updates do not serialize
        self._runner: threading.Thread | None = None
        #: set once that thread is actually running the loop. Every caller waits
        #: on it, not only the one that started the thread
        self._runner_ready = threading.Event()
        #: updates a request thread is blocked on. Stopping the loop under one of
        #: these would leave that thread waiting on a future nothing will finish
        self._updates: set[futures.Future[None]] = set()
        # only true while close() is flushing the loop, so the refusal below can tell
        # a hand-off queued before shutdown from one queued during it
        self._draining = False
        # reentrant: _attach_router holds it while reading self.dispatcher
        self._build_guard = threading.RLock()

    @property
    def enabled(self) -> bool:
        """Whether this process should reach Telegram or Redis at all."""
        return coerce_bool(conf['ENABLED'], f"{SETTINGS_NAME}['ENABLED']")

    @property
    def rate_limiter(self) -> RateLimiter | None:
        """Paced per token: Telegram meters the bot, not this object.

        Two instances holding the same token therefore share one budget; a
        different token gets its own.
        """
        # no instance cache: the registry already caches per token, and holding
        # a second copy here is what kept a bot on stale RATE_LIMIT settings
        # after the registry was reset
        return get_rate_limiter(str(conf['TOKEN'] or ''))

    @property
    def max_retries(self) -> int:
        """How many rate-limited attempts a send gets before it is given up on."""
        if self._max_retries is not None:
            return self._max_retries
        return int(conf['MAX_RETRIES'])

    @property
    def loop(self) -> AbstractEventLoop:
        """The event loop every send and the dispatcher run on."""
        if self._loop is None:
            # two first sends from different web threads would otherwise each
            # build one, and loop_lock would then serialize nothing: the two
            # senders would hold locks belonging to different loops
            with self._build_guard:
                if self._loop is None:
                    self._loop = asyncio.new_event_loop()
        return self._loop

    @property
    def bot(self) -> Bot:
        """The aiogram ``Bot``, which is the first thing that needs a token."""
        if self._bot is None:
            token = conf['TOKEN']
            if not token:
                msg = f"{SETTINGS_NAME}['TOKEN'] is required to talk to Telegram."
                raise ImproperlyConfigured(msg)
            with self._build_guard:
                if self._bot is None:
                    self._bot = Bot(token=token, default=build_default_properties())
        return self._bot

    @property
    def dispatcher(self) -> Dispatcher:
        """The aiogram ``Dispatcher``, holding the configured FSM storage."""
        if self._dispatcher is None:
            # two concurrent first requests would otherwise build one each, and
            # the router would attach to whichever was discarded
            with self._build_guard:
                if self._dispatcher is None:
                    self._dispatcher = Dispatcher(storage=build_storage())
                    # one place: polling and webhook both feed this dispatcher,
                    # so one update middleware sees every update exactly once
                    install_instrumentation(self._dispatcher)
        return self._dispatcher

    @property
    def router(self) -> Router:
        """Router holding every handler registered through the decorators."""
        return self._router

    @property
    def redis_conn(self) -> Redis:
        """The connection every part of this package shares, opened on first use."""
        return get_redis()

    @property
    def is_worker(self) -> bool:
        """True only inside the process that runs the bot itself."""
        return self._polling

    def _attach_router(self) -> None:
        """Attach the router once; aiogram refuses a second attachment.

        Under the build lock: two concurrent first requests would both see no
        parent and the second would raise.
        """
        with self._build_guard:
            if self._router.parent_router is None:
                self.dispatcher.include_router(self._router)

    def start_polling(self) -> None:
        """Attach the router and block on Telegram long polling."""
        self._attach_router()

        async def poll() -> None:
            """Long-poll Telegram, with ``_polling`` true only while this is running.

            A coroutine rather than a straight call, so the flag is raised and lowered
            on the loop itself — see the comment below for what setting it earlier cost.
            """
            # marked from inside the loop: setting it before run_until_complete
            # left a window where send() chose send_raw while the loop was not
            # running yet, and a consumer thread would then drive it from the
            # wrong thread
            self._polling = True
            try:
                await self.dispatcher.start_polling(self.bot)
            finally:
                self._polling = False

        self.loop.run_until_complete(poll())

    def feed_update(self, update: Update) -> None:
        """Hand one update to the dispatcher and wait for the handlers.

        Webhook mode calls this from a request thread. It waits rather than
        scheduling: the response must not be sent before the handlers have run,
        or a failure would go unreported and the request would look successful.
        """
        self._attach_router()
        owned = self._ensure_loop_runs()

        coroutine = self.dispatcher.feed_update(self.bot, update)
        loop = self.loop
        with loop_lock(loop):
            if self._closing:
                # decided under the same lock the shutdown snapshot is taken
                # under, or an update submitted just after it would be neither
                # waited for nor cancelled, and its request would never return
                coroutine.close()
                raise ShuttingDownError
            if not loop.is_running():
                if owned:
                    # our own thread owns this loop and was slow to start.
                    # Driving it here would put two threads on one loop
                    coroutine.close()
                    raise LoopThreadNotStartedError(RUNNER_TIMEOUT)
                # nothing could be started to run it, so drive it here — which is
                # what every update did before, one at a time under this lock
                loop.run_until_complete(coroutine)
                return
            # something runs this loop — polling, or the thread started above —
            # so hand the update over. Decided under the lock: a loop another
            # request is driving looks running until it stops, and the update
            # would then wait for ever
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            self._updates.add(future)

        try:
            # waiting outside the lock, so the next request is not held up by ours
            future.result()
        except (futures.CancelledError, asyncio.CancelledError) as cancelled:
            # `_stop_runner` cancelled this one: no handler finished, so it is the
            # same refusal a request arriving mid-shutdown gets, and the view has
            # to answer it the same way. Left as a cancellation it reads as a
            # handler that failed — a 200 telling Telegram to forget an update
            # nothing handled. Both classes: they are one object on some versions
            # and, where they are not, only one of them is an `Exception`
            raise ShuttingDownError from cancelled
        finally:
            self._forget_update(future)

    def _forget_update(self, future: 'futures.Future[None]') -> None:
        """Drop a finished update from the set the shutdown snapshot reads.

        Under the same lock it was added under. `_stop_runner` takes `list()` over
        this set while holding that lock, and a `discard` from a request thread
        mid-iteration raises `RuntimeError: Set changed size during iteration`
        inside `close()` — aborting the shutdown before anything is torn down.
        """
        loop = self._loop
        if loop is None:
            self._updates.discard(future)
            return
        with loop_lock(loop):
            self._updates.discard(future)

    def _ensure_loop_runs(self) -> bool:
        """Give this process's loop a thread of its own, once.

        A web process serving the webhook drives nothing: every `feed_update`
        took `run_until_complete` **under `loop_lock`**, so updates in one process
        handled strictly one at a time, and a send a handler scheduled was not
        stepped until the next update arrived — or until `close()`, or never.
        Measured on four concurrent updates with a 200 ms handler: 0.81 s
        serialized against 0.21 s with the loop running.

        Not started in the polling process: `start_polling` runs the loop itself,
        and `loop.is_running()` below is what says so.

        Returns whether a thread of ours owns this loop — ready or not. A caller
        that owns it must never fall back to driving the loop itself, even when
        the thread was slow to start: the two would be running the same loop.
        """
        if self._closing:
            return False
        # judged under the guard the thread is also created under. `is_alive()` is
        # false *before* `start()` too, so a check outside it can read a runner
        # registered a moment ago as dead and start a second one — two threads on
        # one loop, which is the collision this method exists to prevent
        with self._build_guard:
            if self._closing:
                return False
            runner = self._runner
            if runner is not None and not runner.is_alive():
                # a thread that died before it ran the loop would otherwise be
                # kept for the life of the process: every later update would wait
                # out the timeout, log the warning and be refused, and no
                # redelivery can recover a condition that never clears
                logger.warning('the event loop thread is gone; starting another')
                self._runner = runner = None
                self._runner_ready.clear()
            if runner is None:
                loop = self.loop
                if loop.is_running():
                    # polling drives it; there is nothing to start
                    return False
                self._runner_ready.clear()

                def run() -> None:
                    """Own this loop on this thread, and say so before blocking in it.

                    The readiness signal is queued *on the loop* rather than set here:
                    scheduled with ``call_soon`` it can only fire once ``run_forever``
                    is actually turning, so a caller that waits for it cannot find a
                    loop that exists but is not running yet.
                    """
                    asyncio.set_event_loop(loop)
                    loop.call_soon(self._runner_ready.set)
                    loop.run_forever()

                self._runner = threading.Thread(target=run, name=LOOP_THREAD, daemon=True)
                self._runner.start()
        # every caller waits, not only the one that started the thread: a second
        # request that returned as soon as the thread existed would find
        # `is_running()` still false below and drive the update with
        # `run_until_complete`, while the thread it saw called `run_forever` on
        # the same loop. That kills the thread, leaves `_runner` pointing at a
        # dead one, and quietly returns the process to handling updates one at a
        # time — the thing this method exists to stop
        if self._runner is None:
            return False
        if not self._runner_ready.wait(RUNNER_TIMEOUT):
            # slow, not absent. Saying so is the whole point of the return value:
            # a caller that drove the update here would collide with the thread
            # the moment it did start
            logger.warning('the event loop thread did not start in time', extra={'tg_timeout': RUNNER_TIMEOUT})
        return True

    def _stop_runner(self, drain_timeout: float) -> None:
        """Stop the thread this process gave the loop, if it started one.

        Before the teardown, not after: `close()` refuses outright on a running
        loop, so a bot that started a runner could never be closed.

        Updates in flight are waited for first, and cancelled if they outlast the
        drain. A request thread blocks on `future.result()` with no deadline, so
        stopping the loop under one would leave that thread waiting on a future
        nothing will ever finish — a web worker held for the life of the process.
        Cancelling before the loop stops is what turns that into an exception the
        request can answer with.
        """
        # under the guard `_ensure_loop_runs` registers a thread beneath, and
        # released before `loop_lock` below rather than held across it: a send
        # driven under `loop_lock` reaches the `bot` property, which takes this
        # guard, so guard-inside-lock is the order that already exists.
        #
        # Without it a request that passed the `_closing` check could register a
        # runner *after* this snapshot read None, and that thread would call
        # `run_forever` on the loop the teardown below is closing
        with self._build_guard:
            runner, self._runner = self._runner, None
            self._runner_ready.clear()
        if runner is None:
            return
        loop = self._loop
        if loop is not None:
            # under the lock `feed_update` submits beneath, so an update cannot
            # slip in between this snapshot and the loop stopping. The waiting
            # stays outside it: holding it would block nothing useful and delay
            # the refusal above
            with loop_lock(loop):
                pending = list(self._updates)
        else:
            pending = list(self._updates)
        if pending:
            logger.info('waiting for updates in flight', extra={'tg_pending': len(pending)})
            futures.wait(pending, timeout=max(0.0, drain_timeout))
            unfinished = [future for future in pending if not future.done()]
            if unfinished:
                logger.warning('cancelling updates still in flight', extra={'tg_pending': len(unfinished)})
                for future in unfinished:
                    future.cancel()
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=RUNNER_TIMEOUT)
        if runner.is_alive():
            logger.warning('the event loop thread did not stop in time', extra={'tg_timeout': RUNNER_TIMEOUT})

    def send(
        self,
        function: str = 'send_message',
        *,
        correlation_id: uuid.UUID | str | None = None,
        **kwargs: Any,
    ) -> uuid.UUID:
        """Deliver a message the way this process can.

        Inside the bot container that means calling Telegram directly; anywhere
        else it means handing the call to the queue for the bot to pick up. It
        saves every caller from having to know which process it is running in.

        Returns the correlation id every event about this message carries, so a
        project can store it next to its own model and join the two.
        """
        # resolved here so both routes agree on the id, and so a caller reading
        # the return value gets the same one the rows carry
        identifier = resolve_correlation_id(correlation_id)
        if self.is_worker:
            return self.send_raw(function, correlation_id=identifier, **kwargs)
        return self.send_redis(function, correlation_id=identifier, **kwargs)

    async def asend(
        self,
        function: str = 'send_message',
        *,
        correlation_id: uuid.UUID | str | None = None,
        **kwargs: Any,
    ) -> uuid.UUID:
        """Deliver a message from code that is already on an event loop.

        The same routing as :meth:`send`, without the blocking socket write that
        one does on the calling thread — which under ASGI is the thread serving
        requests, and on a first call includes a TCP connect bounded by
        ``REDIS_TIMEOUT``.

        In the bot container it still calls Telegram directly, and that path was
        never blocking: :meth:`send_raw` schedules onto the loop and returns.
        """
        # before the first await, so a handler's correlation_scope is still the
        # one in effect: after an await the caller's context may have moved on
        identifier = resolve_correlation_id(correlation_id)
        if self.is_worker:
            return self.send_raw(function, correlation_id=identifier, **kwargs)
        return await self.asend_redis(function, correlation_id=identifier, **kwargs)

    def close(self, drain_timeout: float | None = None) -> None:
        """Finish what is in flight, then release everything this bot owns.

        A send waiting in the rate limiter is an ordinary state — pacing means
        waiting — so closing the loop without draining silently dropped those
        messages on every `docker stop`.

        ``drain_timeout`` defaults to ``DRAIN_TIMEOUT``. It used to be a hardcoded
        five seconds that `start_tgbot` never passed, so a deployment could raise
        `stop_grace_period` all it liked and never buy the drain a second more.
        """
        if drain_timeout is None:
            drain_timeout = drain_budget()
        self._closing = True
        # before anything else: close() refuses on a running loop, so a process
        # that gave the loop a thread could otherwise never close its bot
        self._stop_runner(drain_timeout)
        try:
            if self._loop is not None or self._bot is not None or self._dispatcher is not None:
                loop = self.loop
                if loop.is_running():
                    # run_until_complete and loop.close() both raise on a running
                    # loop; leaving everything in place keeps close() retryable
                    logger.warning('skipping close: stop polling, or the loop thread, before closing the bot')
                    return
                # a send from another thread may be driving this loop; the lock
                # keeps the teardown from interleaving with it
                with loop_lock(loop):
                    self._drain(drain_timeout)
                    # RedisStorage owns a second, async Redis client nothing else closes
                    if self._dispatcher is not None:
                        loop.run_until_complete(self._dispatcher.storage.close())
                        self._dispatcher = None
                    if self._bot is not None:
                        loop.run_until_complete(self._bot.session.close())
                        self._bot = None
                    if not loop.is_closed():
                        loop.close()
            self._loop = None
        finally:
            # a closed bot can be built again, so this must not stick
            self._closing = False

    def send_raw(
        self,
        function: str = 'send_message',
        *,
        correlation_id: uuid.UUID | str | None = None,
        queued_at: float = 0.0,
        on_complete: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> uuid.UUID:
        """Call an aiogram bot method, retrying on Telegram rate limits.

        Returns the correlation id every event about this message carries.
        ``queued_at`` is set by the consumer when the call came off the queue,
        which is what makes time-in-queue measurable and tells the two apart.

        ``on_complete`` is called once the send has actually finished — sent,
        refused or given up on — and **not** called when the send is refused
        before it starts or cancelled at shutdown. The consumer passes one so it
        can acknowledge the message then rather than now: this method returns as
        soon as the coroutine is *scheduled*, which is long before Telegram has
        seen anything.
        """
        check_function(function)
        identifier = resolve_correlation_id(correlation_id)
        if not self.enabled:
            logger.debug('send skipped: bot disabled', extra={'tg_function': function})
            return identifier

        async def send() -> None:
            """Make the call, and keep making it while Telegram asks for a wait.

            The loop owns three things the caller cannot see: the rate limiter's wait,
            which is per attempt rather than per message; the ``retry_after`` sleeps,
            which are Telegram's number and not ours; and the decision that a refusal is
            final. Returning is therefore the only definition of *finished* this package
            has — sent, refused or out of retries alike — which is what the done-callback
            on the task is watching for.
            """
            last_error: exceptions.TelegramRetryAfter | None = None
            retries = 0
            started = time.monotonic()
            while retries <= self.max_retries:
                try:
                    # per attempt, not since the first: measured from `started`
                    # this would fold in the earlier attempts and the sleeps
                    # Telegram asked for, and stop being the limiter's wait
                    attempted = time.monotonic()
                    limiter = self.rate_limiter
                    if limiter is not None:
                        await limiter.acquire(call_kwargs.get('chat_id'))
                    paced = time.monotonic()
                    result = await getattr(self.bot, function)(**call_kwargs)
                except exceptions.TelegramRetryAfter as error:  # noqa: PERF203 - retrying is what the loop is for
                    last_error = error
                    self._record_send(
                        EventKind.OUTBOUND_RETRIED,
                        outbound,
                        attempt=retries,
                        detail={'retry_after': error.retry_after},
                    )
                    logger.warning(
                        'rate limited by telegram',
                        extra={
                            'tg_function': function,
                            'tg_retry_after': error.retry_after,
                            'tg_retries': retries,
                        },
                    )
                    retries += 1
                    await asyncio.sleep(error.retry_after)
                except Exception as error:
                    self._record_send(
                        EventKind.OUTBOUND_FAILED,
                        outbound,
                        attempt=retries,
                        error=error,
                    )
                    logger.exception('send failed', extra={'tg_function': function})
                    if conf['RAISE_EXCEPTION']:
                        raise
                    return
                else:
                    self._record_send(
                        EventKind.OUTBOUND_SENT,
                        outbound,
                        attempt=retries,
                        # the return value used to be thrown away, and it carries
                        # the only id Telegram will ever give for this message
                        message_id=getattr(result, 'message_id', None),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        detail={
                            'paced_ms': int((paced - attempted) * 1000),
                            'queue_ms': int((time.time() - queued_at) * 1000) if queued_at else None,
                        },
                    )
                    logger.info('message sent', extra={'tg_function': function})
                    return

            # exhausting the retries used to return silently
            self._record_send(
                EventKind.OUTBOUND_DROPPED,
                outbound,
                attempt=retries,
                error=last_error,
                detail={'max_retries': self.max_retries},
            )
            logger.error(
                'giving up on message',
                extra={'tg_function': function, 'tg_max_retries': self.max_retries},
            )
            if conf['RAISE_EXCEPTION'] and last_error is not None:
                raise last_error

        call_kwargs = {**conf['DEFAULT_KWARGS'](function), **kwargs}
        outbound = Outbound(identifier, function, call_kwargs)
        self._schedule(send(), outbound, on_complete)
        return identifier

    @staticmethod
    def _record_send(kind: EventKind, outbound: 'Outbound', **fields: Any) -> None:
        """Record one stage of an outbound message.

        Called from inside the send coroutine, which is the only place that
        knows the outcome: _schedule returns once the task is *created*, so the
        consumer acknowledges the message before Telegram has seen it.
        """
        # `active`, not `enabled`: this one guard gates every `outbound.sent`,
        # `failed`, `retried` and `dropped` row there is — the whole of what a
        # metrics receiver was connected for. Reading the table flag here is how
        # the advertised set comes out empty for anyone who left the table off
        if not recorder.active:
            return
        error = fields.pop('error', None)
        detail = fields.pop('detail', None) or {}
        recorder.record(
            Event(
                kind=kind.value,
                correlation_id=outbound.correlation_id,
                function=outbound.function,
                chat_id=as_identifier(outbound.call_kwargs.get('chat_id')),
                error_code=type(error).__name__ if error is not None else '',
                error=str(error) if error is not None else '',
                # None means 'not measured here', and a column is a better place
                # for it than a JSON key that reads as a measurement of zero
                detail={key: value for key, value in detail.items() if value is not None},
                **fields,
            )
        )

    def _register(
        self,
        task: 'asyncio.Task[None]',
        outbound: 'Outbound',
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        """Track a send so :meth:`close` can wait for it.

        Registration happens when the task is created, not when it starts
        running: a task that has been scheduled but not yet stepped is exactly
        the one shutdown must not lose.
        """
        self._sends[task] = outbound
        task.add_done_callback(self._sends.pop)
        task.add_done_callback(self._log_task_failure)
        if on_complete is not None:
            task.add_done_callback(_completion(on_complete))

    @staticmethod
    def _log_task_failure(task: 'asyncio.Task[None]') -> None:
        """Report what a finished send raised, since nobody awaits these tasks."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error('scheduled send failed', exc_info=error)

    def _schedule(
        self,
        coroutine: Coroutine[Any, Any, None],
        outbound: 'Outbound',
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        """Run a coroutine on the bot loop from whichever thread we are on.

        The delivery consumer runs in its own thread while the loop belongs to
        the polling thread; calling create_task across that boundary is not
        thread safe and silently corrupts the loop's internals.

        Every path that refuses the coroutine leaves ``on_complete`` uncalled: the
        message was not sent, so the consumer has to keep it in flight.
        """
        if self._closing:
            # the loop is being torn down, so nothing would ever run this
            coroutine.close()
            self._record_drop(outbound, 'the bot is shutting down')
            logger.error('send refused: the bot is shutting down')
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        loop = self.loop
        if running is loop:
            if not self._polling and self._runner is None:
                # nothing in this process drives this loop — not polling, and no
                # runner — so the task is created and never stepped. With a
                # runner it *is* stepped, which is the normal webhook path and
                # must not warn: a warning on the healthy path is how people
                # learn to stop reading them
                logger.warning(
                    'scheduling a send on a loop nothing in this process runs',
                    extra={'tg_function': outbound.function, 'tg_correlation_id': str(outbound.correlation_id)},
                )
            self._register(self._start(coroutine, loop, outbound), outbound, on_complete)
            return

        # several web threads may send at once, and run_until_complete is not
        # reentrant — the second caller would get "this event loop is already
        # running". The lock belongs to the loop, so two bots sharing one are
        # still serialized.
        with loop_lock(loop):
            # close() holds the same lock, so it may have finished the whole
            # teardown while this thread waited for it
            if self._closing or loop.is_closed():
                coroutine.close()
                self._record_drop(outbound, 'the event loop was closed')
                logger.error('send refused: the event loop was closed')
                return
            if loop.is_running():
                # decided under the lock: seen from outside it, a loop another
                # thread drives for one run_until_complete looks running right
                # up to the moment it stops, and the handoff would be lost
                self._hand_off(coroutine, loop, outbound, on_complete)
                return
            try:
                loop.run_until_complete(coroutine)
            except RuntimeError:
                # polling started between the check above and this call
                if not loop.is_running():
                    raise
                self._hand_off(coroutine, loop, outbound, on_complete)
                return
            # only a return settles. Cancellation is not completion — the same
            # rule the task path follows — and a failure RAISE_EXCEPTION let
            # through is already owned by the consumer's own except branch, so
            # settling here too would report one message finished twice and
            # drive the in-flight count below zero, quietly widening the bound
            # MAX_IN_FLIGHT exists to hold.
            #
            # Driven to completion right here, so there is no task to hang a
            # done-callback on — webhook mode takes this path for every send
            _settle(on_complete)

    def _hand_off(
        self,
        coroutine: Coroutine[Any, Any, None],
        loop: AbstractEventLoop,
        outbound: 'Outbound',
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        """Create the task on the loop thread, so it is registered before it runs."""

        def start() -> None:
            """Turn the coroutine into a registered task, or refuse it and say so.

            Runs on the loop thread, which is the point: creating the task here means it
            is in ``_sends`` before it can run, so a drain that comes next cannot miss
            it. The refusal below is what stops a coroutine queued a moment before
            ``close()`` from being garbage-collected unmentioned.
            """
            if self._closing and not self._draining:
                # close() began after this was queued; the loop will not run it.
                # _draining is the exception: close() runs one turn of the loop on
                # purpose, precisely so callbacks queued before it become tasks
                coroutine.close()
                self._record_drop(outbound, 'the bot started shutting down')
                logger.error('send dropped: the bot started shutting down')
                return
            self._register(self._start(coroutine, loop, outbound), outbound, on_complete)

        try:
            loop.call_soon_threadsafe(start)
        except RuntimeError:
            coroutine.close()
            self._record_drop(outbound, 'the event loop is closed')
            logger.exception('send dropped: the event loop is closed')

    @staticmethod
    def _start(
        coroutine: Coroutine[Any, Any, None],
        loop: AbstractEventLoop,
        outbound: 'Outbound',
    ) -> 'asyncio.Task[None]':
        """Create the task, named so shutdown can say which message it cancelled."""
        return loop.create_task(coroutine, name=f'{TASK_PREFIX}{outbound.correlation_id.hex}')

    @staticmethod
    def _record_drop(outbound: 'Outbound', reason: str) -> None:
        """Record a send that never reached Telegram, and used to leave only a log line.

        Carries the call, not just its id: a direct `send_raw` was never queued,
        so this row is the only one that will ever exist for that message.
        """
        recorder.record(
            Event(
                kind=EventKind.OUTBOUND_DROPPED.value,
                correlation_id=outbound.correlation_id,
                function=outbound.function,
                chat_id=as_identifier(outbound.call_kwargs.get('chat_id')),
                error_code='NotScheduled',
                error=reason,
            )
        )

    def _drain(self, timeout: float) -> None:
        """Let scheduled sends finish, cancelling whatever outlasts the timeout."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if loop.is_running():
            # cannot drive it from here; the caller is expected to stop polling first
            logger.warning('skipping drain: the event loop is still running')
            return

        # a hand-off is a call_soon_threadsafe callback until the loop steps, and
        # a callback is not in _sends — so without one turn here, a send queued
        # just before shutdown is invisible to the drain below and dies with the
        # loop. _register's own docstring says that is the one it must not lose
        self._draining = True
        try:
            loop.run_until_complete(asyncio.sleep(0))
        finally:
            self._draining = False

        # only this bot's sends: cancelling unrelated tasks on the loop is not
        # ours to do, and aiogram keeps its own there
        pending = [task for task in self._sends if not task.done()]
        if not pending:
            return

        logger.info('draining in-flight sends', extra={'tg_pending': len(pending)})
        loop.run_until_complete(asyncio.wait(pending, timeout=timeout))

        dropped = [task for task in pending if not task.done()]
        if not dropped:
            return
        for task in dropped:
            self._record_drop(self._sends[task], 'cancelled at shutdown')
            task.cancel()
        loop.run_until_complete(asyncio.gather(*dropped, return_exceptions=True))
        logger.warning(
            'dropped in-flight sends at shutdown',
            extra={'tg_dropped': len(dropped), 'tg_drain_timeout': timeout},
        )

    def _accept(self, function: str, correlation_id: uuid.UUID | str | None) -> tuple[uuid.UUID, bool]:
        """Judge a queueing request, and name the message either way.

        Both producers ask the same three questions, and the third has a shape
        worth keeping in one place: a disabled bot returns the id rather than
        raising, so a caller storing it beside its own model gets the same value
        whether or not this deployment sends anything.
        """
        check_function(function)
        identifier = resolve_correlation_id(correlation_id)
        if not self.enabled:
            logger.debug('queueing skipped: bot disabled', extra={'tg_function': function})
            return identifier, False
        return identifier, True

    def send_redis(
        self,
        function: str = 'send_message',
        *,
        correlation_id: uuid.UUID | str | None = None,
        **kwargs: Any,
    ) -> uuid.UUID:
        """Queue a message in Redis for the bot worker to deliver.

        Returns the correlation id the delivered row will carry too.
        """
        identifier, accepted = self._accept(function, correlation_id)
        if not accepted:
            return identifier

        _mention_asend('asend')
        with queueing(function, [(identifier, kwargs)]) as write:
            get_redis().rpush(write.key, *write.payloads)
        return identifier

    async def asend_redis(
        self,
        function: str = 'send_message',
        *,
        correlation_id: uuid.UUID | str | None = None,
        **kwargs: Any,
    ) -> uuid.UUID:
        """Queue a message without blocking the loop this coroutine runs on.

        The synchronous twin writes to a socket on the calling thread, which under
        ASGI is the thread serving requests — including, on the first call, a TCP
        connect bounded by ``REDIS_TIMEOUT``. Everything else about the message is
        identical: same payload, same event rows, same key.
        """
        identifier, accepted = self._accept(function, correlation_id)
        if not accepted:
            return identifier

        client = await aget_redis()
        with queueing(function, [(identifier, kwargs)]) as write:
            await client.rpush(write.key, *write.payloads)
        return identifier

    def _accept_bulk(self, function: str) -> bool:
        """Whether this process should write, decided once for the whole batch.

        A disabled bot reaches neither Telegram nor Redis, and still returns an id
        per message — the same contract :meth:`send_redis` has, so a caller can
        store the ids beside its own rows whether or not this deployment sends.
        """
        if self.enabled:
            return True
        logger.debug('queueing skipped: bot disabled', extra={'tg_function': function})
        return False

    def send_many(
        self,
        chat_ids: 'Iterable[int | str]',
        function: str = 'send_message',
        *,
        chunk_size: int = 100,
        **kwargs: Any,
    ) -> list[uuid.UUID]:
        """Queue one message per chat, a chunk of them per round trip.

        Returns an id per message, in the order the chats were given — not a
        single receipt for the batch. A batch id would trade the indexed
        ``correlation_id__in`` lookup the event log is built for against a scan of
        the JSON column, and ``unpack`` drops keys it does not know, so the
        consumer's own rows could never carry it anyway.

        This speeds up **queueing**, not delivery: the rate limits still pace what
        leaves for Telegram, so fifty thousand chats is still about half an hour at
        the default thirty a second. It also removes the pacing that sequential
        round trips gave the event log — see **Event log** before broadcasting.

        A chunk that fails records a drop for its own messages and raises; earlier
        chunks are already queued, and their ids are lost with the exception, which
        is why the drops are recorded rather than left to the caller to infer.
        """
        writing = self._accept_bulk(function)
        if writing:
            _mention_asend('asend_many')
        identifiers: list[uuid.UUID] = []
        for chunk in self._chunks(chat_ids, function, chunk_size, kwargs):
            if writing:
                with queueing(function, chunk) as write:
                    get_redis().rpush(write.key, *write.payloads)
            identifiers.extend(identifier for identifier, _ in chunk)
        return identifiers

    async def asend_many(
        self,
        chat_ids: 'Iterable[int | str]',
        function: str = 'send_message',
        *,
        chunk_size: int = 100,
        **kwargs: Any,
    ) -> list[uuid.UUID]:
        """Queue one message per chat without blocking the loop.

        Everything :meth:`send_many` says applies, and the reason to reach for this
        one is stronger than for :meth:`asend`: a fan-out writes once per chunk and
        serialises every payload, so on a serving loop it blocks longer and more
        often than a single send does.
        """
        writing = self._accept_bulk(function)
        # after the decision, not before: a disabled process may have no REDIS_URL
        # at all, and building a client would raise where the point is to do nothing
        client = await aget_redis() if writing else None
        identifiers: list[uuid.UUID] = []
        for chunk in self._chunks(chat_ids, function, chunk_size, kwargs):
            if client is not None:
                with queueing(function, chunk) as write:
                    await client.rpush(write.key, *write.payloads)
            identifiers.extend(identifier for identifier, _ in chunk)
        return identifiers

    def _chunks(
        self,
        chat_ids: 'Iterable[int | str]',
        function: str,
        chunk_size: int,
        kwargs: dict[str, Any],
    ) -> 'Iterator[list[tuple[uuid.UUID, dict[str, Any]]]]':
        """Group the chats into the batches one write covers.

        Serialisation happens inside :func:`queueing`, one chunk at a time, which
        is what keeps peak memory bounded: a ``BufferedInputFile`` payload times
        fifty thousand chats would otherwise all exist at once.
        """
        check_function(function)
        size = max(1, int(chunk_size))
        chunk: list[tuple[uuid.UUID, dict[str, Any]]] = []
        for chat_id in chat_ids:
            chunk.append((new_correlation_id(), {**kwargs, 'chat_id': chat_id}))
            if len(chunk) >= size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk

    async def aclose(self) -> None:
        """Release the async Redis client this loop was using.

        Not the mirror of :meth:`close`, and deliberately not named to suggest it:
        that one tears down the bot — loop, session, FSM storage — while this one
        closes the single thing an ASGI process opens lazily and Django gives no
        hook to close.

        This is the only path that closes the connection on the loop it belongs to,
        which is the only loop allowed to close it. Skipping it does not accumulate
        clients — the registry drops the ones whose loop has closed — but the
        connection stays open until its client is collected, and Python may say so
        with a ``ResourceWarning``. Call it from a lifespan shutdown if your server
        has one, and from anything that runs a loop per unit of work.
        """
        await aclose_redis()

    def queue_depth(self) -> int:
        """How many messages are waiting for a worker to take them.

        One ``LLEN``. Named for what it measures rather than the command, because
        the queue is a Redis list only until 4.0 makes the broker pluggable.

        Growing is not by itself a fault — producers can outpace delivery, and
        ``MAX_IN_FLIGHT`` holds intake back on purpose. See **Troubleshooting**.
        """
        return int(get_redis().llen(queue_key()) or 0)

    async def aqueue_depth(self) -> int:
        """:meth:`queue_depth` without blocking the loop this coroutine runs on."""
        client = await aget_redis()
        return int(await client.llen(queue_key()) or 0)

    def inflight_depth(self, worker: str | None = None) -> int:
        """How many messages one worker is part-way through sending.

        Defaults to this process's own worker identity. Naming another is how a
        monitor reads a list left behind by a worker that is gone — the scheme
        those keys follow is this package's business, not an exporter's to
        reproduce.
        """
        return int(get_redis().llen(processing_key(worker)) or 0)

    async def ainflight_depth(self, worker: str | None = None) -> int:
        """:meth:`inflight_depth` without blocking the loop this coroutine runs on."""
        client = await aget_redis()
        return int(await client.llen(processing_key(worker)) or 0)

    def message(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'message' observer."""
        return self._add_router(*args, event_name='message', **kwargs)

    def edited_message(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'edited_message' observer."""
        return self._add_router(*args, event_name='edited_message', **kwargs)

    def channel_post(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'channel_post' observer."""
        return self._add_router(*args, event_name='channel_post', **kwargs)

    def edited_channel_post(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'edited_channel_post' observer."""
        return self._add_router(*args, event_name='edited_channel_post', **kwargs)

    def inline_query(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'inline_query' observer."""
        return self._add_router(*args, event_name='inline_query', **kwargs)

    def chosen_inline_result(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'chosen_inline_result' observer."""
        return self._add_router(*args, event_name='chosen_inline_result', **kwargs)

    def callback_query(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'callback_query' observer."""
        return self._add_router(*args, event_name='callback_query', **kwargs)

    def shipping_query(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'shipping_query' observer."""
        return self._add_router(*args, event_name='shipping_query', **kwargs)

    def pre_checkout_query(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'pre_checkout_query' observer."""
        return self._add_router(*args, event_name='pre_checkout_query', **kwargs)

    def poll(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'poll' observer."""
        return self._add_router(*args, event_name='poll', **kwargs)

    def poll_answer(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'poll_answer' observer."""
        return self._add_router(*args, event_name='poll_answer', **kwargs)

    def my_chat_member(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'my_chat_member' observer."""
        return self._add_router(*args, event_name='my_chat_member', **kwargs)

    def chat_member(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'chat_member' observer."""
        return self._add_router(*args, event_name='chat_member', **kwargs)

    def chat_join_request(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'chat_join_request' observer."""
        return self._add_router(*args, event_name='chat_join_request', **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Return a decorator registering a handler for the 'error' observer."""
        return self._add_router(*args, event_name='error', **kwargs)

    def _add_router(self, *args: Any, event_name: str, **kwargs: Any) -> CallbackType:
        """Build the decorator every observer method above returns."""

        def wrapper(callback: CallbackType) -> CallbackType:
            """Register the handler and hand it back unchanged.

            Returning the callback rather than a wrapper is what lets these decorators
            stack, and what keeps the handler directly callable from a test.
            """
            observer = self._router.observers[event_name]
            observer.register(callback, *args, **kwargs)
            return callback

        return wrapper

    def __repr__(self) -> str:
        """Say whether the aiogram bot behind this facade has been built yet."""
        return f'<TelegramBot bot={"built" if self._bot else "lazy"}>'
