import asyncio
import logging
from asyncio import AbstractEventLoop
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any

from aiogram import Bot, Dispatcher, Router, exceptions
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.handler import CallbackType
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from django_redis_aiogram.delivery import KEYSPACE_DELIVERY
from django_redis_aiogram.redis import get_redis
from django_redis_aiogram.serializers import get_serializer
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf

logger = logging.getLogger('django_redis_aiogram')

MEMORY_STORAGE = 'memory'
REDIS_STORAGE = 'redis'


def build_default_properties() -> DefaultBotProperties:
    """Bot-wide defaults such as parse_mode.

    aiogram applies these to every call, which is why unset fields carry a
    ``Default`` sentinel rather than None.
    """
    properties = conf['DEFAULT_BOT_PROPERTIES']
    try:
        return DefaultBotProperties(**properties)
    except TypeError as error:
        raise ImproperlyConfigured(f"{SETTINGS_NAME}['DEFAULT_BOT_PROPERTIES']: {error}") from None


def build_storage() -> BaseStorage:
    """FSM storage: 'redis', 'memory', or a dotted path to a BaseStorage."""
    name = conf['FSM_STORAGE']
    if name == MEMORY_STORAGE:
        return MemoryStorage()
    if name == REDIS_STORAGE:
        url = str(conf['REDIS_URL'] or '').strip()
        if not url:
            raise ImproperlyConfigured(
                f"{SETTINGS_NAME}['REDIS_URL'] is required for the redis FSM storage."
            )
        return RedisStorage.from_url(url)

    try:
        storage_class = import_string(name)
    except ImportError as error:
        raise ImproperlyConfigured(
            f"{SETTINGS_NAME}['FSM_STORAGE'] cannot be imported: {error}"
        ) from error
    if not (isinstance(storage_class, type) and issubclass(storage_class, BaseStorage)):
        raise ImproperlyConfigured(
            f"{SETTINGS_NAME}['FSM_STORAGE'] must point to a BaseStorage subclass, got {name!r}."
        )
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
        self._max_retries = max_retries
        self._loop = loop
        self._bot: Bot | None = None
        self._dispatcher: Dispatcher | None = None
        self._router = Router()

    @property
    def enabled(self) -> bool:
        """Whether this process should reach Telegram or Redis at all."""
        return coerce_bool(conf['ENABLED'], f"{SETTINGS_NAME}['ENABLED']")

    @property
    def max_retries(self) -> int:
        if self._max_retries is not None:
            return self._max_retries
        return int(conf['MAX_RETRIES'])

    @property
    def loop(self) -> AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            token = conf['TOKEN']
            if not token:
                raise ImproperlyConfigured(
                    f"{SETTINGS_NAME}['TOKEN'] is required to talk to Telegram."
                )
            self._bot = Bot(token=token, default=build_default_properties())
        return self._bot

    @property
    def dispatcher(self) -> Dispatcher:
        if self._dispatcher is None:
            self._dispatcher = Dispatcher(storage=build_storage())
        return self._dispatcher

    @property
    def router(self) -> Router:
        """Router holding every handler registered through the decorators."""
        return self._router

    @property
    def redis_conn(self) -> Any:
        return get_redis()

    def start_polling(self) -> None:
        """Attach the router and block on Telegram long polling."""
        self.dispatcher.include_router(self._router)
        self.loop.run_until_complete(self.dispatcher.start_polling(self.bot))

    def close(self) -> None:
        """Release the aiogram session and the owned event loop."""
        if self._bot is not None:
            self.loop.run_until_complete(self._bot.session.close())
            self._bot = None
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()
        self._loop = None

    def send_raw(self, function: str = 'send_message', **kwargs: Any) -> None:
        """Call an aiogram bot method, retrying on Telegram rate limits."""
        if not self.enabled:
            logger.debug('send skipped: bot disabled', extra={'tg_function': function})
            return

        async def send() -> None:
            last_error: exceptions.TelegramRetryAfter | None = None
            retries = 0
            while retries <= self.max_retries:
                try:
                    await getattr(self.bot, function)(**call_kwargs)
                    logger.info('message sent', extra={'tg_function': function})
                    return
                except exceptions.TelegramRetryAfter as error:
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

            # exhausting the retries used to return silently
            logger.error(
                'giving up on message',
                extra={'tg_function': function, 'tg_max_retries': self.max_retries},
            )
            if conf['RAISE_EXCEPTION'] and last_error is not None:
                raise last_error

        call_kwargs = {**conf['DEFAULT_KWARGS'](function), **kwargs}
        self._schedule(send())

    def _schedule(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Run a coroutine on the bot loop from whichever thread we are on.

        The delivery consumer runs in its own thread while the loop belongs to
        the polling thread; calling create_task across that boundary is not
        thread safe and silently corrupts the loop's internals.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is self.loop:
            self.loop.create_task(coroutine)
        elif self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            future.add_done_callback(self._log_failure)
        else:
            self.loop.run_until_complete(coroutine)

    @staticmethod
    def _log_failure(future: 'Future[None]') -> None:
        error = future.exception()
        if error is not None:
            logger.error('scheduled send failed', exc_info=error)

    def send_redis(self, function: str = 'send_message', **kwargs: Any) -> None:
        """Queue a message in Redis for the bot worker to deliver."""
        if not self.enabled:
            logger.debug('queueing skipped: bot disabled', extra={'tg_function': function})
            return

        connection = get_redis()
        connection.rpush(
            conf['REDIS_MESSAGES_KEY'],
            get_serializer().dumps({'function': function, **kwargs}),
        )
        if conf['DELIVERY'] == KEYSPACE_DELIVERY:
            connection.set(conf['REDIS_EXP_KEY'], '1', ex=conf['REDIS_EXP_TIME'])

    def message(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'message' observer."""
        return self._add_router(*args, event_name='message', **kwargs)

    def edited_message(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'edited_message' observer."""
        return self._add_router(*args, event_name='edited_message', **kwargs)

    def channel_post(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'channel_post' observer."""
        return self._add_router(*args, event_name='channel_post', **kwargs)

    def edited_channel_post(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'edited_channel_post' observer."""
        return self._add_router(*args, event_name='edited_channel_post', **kwargs)

    def inline_query(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'inline_query' observer."""
        return self._add_router(*args, event_name='inline_query', **kwargs)

    def chosen_inline_result(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'chosen_inline_result' observer."""
        return self._add_router(*args, event_name='chosen_inline_result', **kwargs)

    def callback_query(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'callback_query' observer."""
        return self._add_router(*args, event_name='callback_query', **kwargs)

    def shipping_query(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'shipping_query' observer."""
        return self._add_router(*args, event_name='shipping_query', **kwargs)

    def pre_checkout_query(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'pre_checkout_query' observer."""
        return self._add_router(*args, event_name='pre_checkout_query', **kwargs)

    def poll(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'poll' observer."""
        return self._add_router(*args, event_name='poll', **kwargs)

    def poll_answer(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'poll_answer' observer."""
        return self._add_router(*args, event_name='poll_answer', **kwargs)

    def my_chat_member(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'my_chat_member' observer."""
        return self._add_router(*args, event_name='my_chat_member', **kwargs)

    def chat_member(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'chat_member' observer."""
        return self._add_router(*args, event_name='chat_member', **kwargs)

    def chat_join_request(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'chat_join_request' observer."""
        return self._add_router(*args, event_name='chat_join_request', **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> CallbackType:
        """Decorator for the 'error' observer."""
        return self._add_router(*args, event_name='error', **kwargs)

    def _add_router(self, *args: Any, event_name: str, **kwargs: Any) -> CallbackType:
        def wrapper(callback: CallbackType) -> CallbackType:
            observer = self._router.observers[event_name]
            observer.register(callback, *args, **kwargs)
            return callback

        return wrapper

    def __repr__(self) -> str:
        return f'<TelegramBot bot={"built" if self._bot else "lazy"}>'
