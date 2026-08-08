"""The bot object Django code talks to.

One facade over an aiogram ``Bot``, ``Dispatcher`` and ``Router``, built lazily
so that importing the package costs nothing in the processes — web workers, cron
jobs, the test suite — that only ever queue a message.
"""

import asyncio
import logging
import threading
import weakref
from asyncio import AbstractEventLoop
from collections.abc import Coroutine, Mapping
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
from django_redis_aiogram.enums import StorageKind
from django_redis_aiogram.redis import get_redis
from django_redis_aiogram.serializers import get_serializer
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf
from django_redis_aiogram.throttling import RateLimiter, get_rate_limiter

logger = logging.getLogger('django_redis_aiogram')

# run_until_complete is not reentrant, and the loop — not the bot — is what
# cannot be entered twice. Two bots handed the same loop must share one lock.
_loop_locks: 'weakref.WeakKeyDictionary[AbstractEventLoop, threading.Lock]' = weakref.WeakKeyDictionary()
_loop_locks_guard = threading.Lock()


def loop_lock(loop: AbstractEventLoop) -> threading.Lock:
    """Return the one lock that everything driving ``loop`` has to hold."""
    with _loop_locks_guard:
        lock = _loop_locks.get(loop)
        if lock is None:
            lock = _loop_locks[loop] = threading.Lock()
        return lock


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
        return MemoryStorage()
    if name == StorageKind.REDIS:
        url = str(conf['REDIS_URL'] or '').strip()
        if not url:
            msg = f"{SETTINGS_NAME}['REDIS_URL'] is required for the redis FSM storage."
            raise ImproperlyConfigured(msg)
        return RedisStorage.from_url(url)

    try:
        storage_class = import_string(name)
    except ImportError as error:
        msg = f"{SETTINGS_NAME}['FSM_STORAGE'] cannot be imported: {error}"
        raise ImproperlyConfigured(msg) from error
    if not (isinstance(storage_class, type) and issubclass(storage_class, BaseStorage)):
        msg = f"{SETTINGS_NAME}['FSM_STORAGE'] must point to a BaseStorage subclass, got {name!r}."
        raise ImproperlyConfigured(msg)
    return storage_class()


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
        self._sends: set[asyncio.Task[None]] = set()
        self._polling = False
        self._closing = False
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

        coroutine = self.dispatcher.feed_update(self.bot, update)
        loop = self.loop
        with loop_lock(loop):
            if not loop.is_running():
                loop.run_until_complete(coroutine)
                return
            # polling drives this loop, so hand the update over. Decided under
            # the lock: a loop another request is driving looks running until it
            # stops, and the update would then wait for ever
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)

        # waiting outside the lock, so the next request is not held up by ours
        future.result()

    def send(self, function: str = 'send_message', **kwargs: Any) -> None:
        """Deliver a message the way this process can.

        Inside the bot container that means calling Telegram directly; anywhere
        else it means handing the call to the queue for the bot to pick up. It
        saves every caller from having to know which process it is running in.
        """
        if self.is_worker:
            self.send_raw(function, **kwargs)
        else:
            self.send_redis(function, **kwargs)

    def close(self, drain_timeout: float = 5.0) -> None:
        """Finish what is in flight, then release everything this bot owns.

        A send waiting in the rate limiter is an ordinary state — pacing means
        waiting — so closing the loop without draining silently dropped those
        messages on every `docker stop`.
        """
        self._closing = True
        try:
            if self._loop is not None or self._bot is not None or self._dispatcher is not None:
                loop = self.loop
                if loop.is_running():
                    # run_until_complete and loop.close() both raise on a running
                    # loop; leaving everything in place keeps close() retryable
                    logger.warning('skipping close: stop polling before closing the bot')
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

    def send_raw(self, function: str = 'send_message', **kwargs: Any) -> None:
        """Call an aiogram bot method, retrying on Telegram rate limits."""
        check_function(function)
        if not self.enabled:
            logger.debug('send skipped: bot disabled', extra={'tg_function': function})
            return

        async def send() -> None:
            last_error: exceptions.TelegramRetryAfter | None = None
            retries = 0
            while retries <= self.max_retries:
                try:
                    limiter = self.rate_limiter
                    if limiter is not None:
                        await limiter.acquire(call_kwargs.get('chat_id'))
                    await getattr(self.bot, function)(**call_kwargs)
                except exceptions.TelegramRetryAfter as error:  # noqa: PERF203 - retrying is what the loop is for
                    last_error = error
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
                except Exception:
                    logger.exception('send failed', extra={'tg_function': function})
                    if conf['RAISE_EXCEPTION']:
                        raise
                    return
                else:
                    logger.info('message sent', extra={'tg_function': function})
                    return

            # exhausting the retries used to return silently
            logger.error(
                'giving up on message',
                extra={'tg_function': function, 'tg_max_retries': self.max_retries},
            )
            if conf['RAISE_EXCEPTION'] and last_error is not None:
                raise last_error

        call_kwargs = {**conf['DEFAULT_KWARGS'](function), **kwargs}
        self._schedule(send())

    def _register(self, task: 'asyncio.Task[None]') -> None:
        """Track a send so :meth:`close` can wait for it.

        Registration happens when the task is created, not when it starts
        running: a task that has been scheduled but not yet stepped is exactly
        the one shutdown must not lose.
        """
        self._sends.add(task)
        task.add_done_callback(self._sends.discard)
        task.add_done_callback(self._log_task_failure)

    @staticmethod
    def _log_task_failure(task: 'asyncio.Task[None]') -> None:
        """Report what a finished send raised, since nobody awaits these tasks."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error('scheduled send failed', exc_info=error)

    def _schedule(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Run a coroutine on the bot loop from whichever thread we are on.

        The delivery consumer runs in its own thread while the loop belongs to
        the polling thread; calling create_task across that boundary is not
        thread safe and silently corrupts the loop's internals.
        """
        if self._closing:
            # the loop is being torn down, so nothing would ever run this
            coroutine.close()
            logger.error('send refused: the bot is shutting down')
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        loop = self.loop
        if running is loop:
            self._register(loop.create_task(coroutine))
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
                logger.error('send refused: the event loop was closed')
                return
            if loop.is_running():
                # decided under the lock: seen from outside it, a loop another
                # thread drives for one run_until_complete looks running right
                # up to the moment it stops, and the handoff would be lost
                self._hand_off(coroutine, loop)
                return
            try:
                loop.run_until_complete(coroutine)
            except RuntimeError:
                # polling started between the check above and this call
                if not loop.is_running():
                    raise
                self._hand_off(coroutine, loop)

    def _hand_off(self, coroutine: Coroutine[Any, Any, None], loop: AbstractEventLoop) -> None:
        """Create the task on the loop thread, so it is registered before it runs."""

        def start() -> None:
            if self._closing:
                # close() began after this was queued; the loop will not run it
                coroutine.close()
                logger.error('send dropped: the bot started shutting down')
                return
            self._register(loop.create_task(coroutine))

        try:
            loop.call_soon_threadsafe(start)
        except RuntimeError:
            coroutine.close()
            logger.exception('send dropped: the event loop is closed')

    def _drain(self, timeout: float) -> None:
        """Let scheduled sends finish, cancelling whatever outlasts the timeout."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if loop.is_running():
            # cannot drive it from here; the caller is expected to stop polling first
            logger.warning('skipping drain: the event loop is still running')
            return

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
            task.cancel()
        loop.run_until_complete(asyncio.gather(*dropped, return_exceptions=True))
        logger.warning(
            'dropped in-flight sends at shutdown',
            extra={'tg_dropped': len(dropped), 'tg_drain_timeout': timeout},
        )

    def send_redis(self, function: str = 'send_message', **kwargs: Any) -> None:
        """Queue a message in Redis for the bot worker to deliver."""
        check_function(function)
        if not self.enabled:
            logger.debug('queueing skipped: bot disabled', extra={'tg_function': function})
            return

        get_redis().rpush(
            conf['REDIS_MESSAGES_KEY'],
            get_serializer().dumps({'function': function, **kwargs}),
        )

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
            observer = self._router.observers[event_name]
            observer.register(callback, *args, **kwargs)
            return callback

        return wrapper

    def __repr__(self) -> str:
        """Say whether the aiogram bot behind this facade has been built yet."""
        return f'<TelegramBot bot={"built" if self._bot else "lazy"}>'
