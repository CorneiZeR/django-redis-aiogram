import asyncio
import logging
import pickle
from asyncio import AbstractEventLoop
from dataclasses import dataclass, field
from typing import Dict, Any

from aiogram import Bot, Dispatcher, Router, exceptions
from aiogram.dispatcher.event.handler import CallbackType
from redis.client import Redis

from telegram_bot.redis import redis_conn
from telegram_bot.settings import conf


@dataclass(repr=False)
class TelegramBot:
    loop: AbstractEventLoop = field(default_factory=asyncio.new_event_loop)

    redis_conn: Redis = field(init=False, default=redis_conn)
    bot: Bot = field(init=False)
    dispatcher: Dispatcher = field(init=False)
    _router: Router = field(init=False, default_factory=Router)

    def __post_init__(self) -> None:
        async def setup() -> None:
            """setup bot and dispatcher"""
            self.bot = Bot(token=conf['TOKEN'])
            self.dispatcher = Dispatcher()

        self.loop.run_until_complete(setup())

    def start_polling(self) -> None:
        """start telegram polling"""
        self.dispatcher.include_router(self._router)
        self.loop.run_until_complete(self.dispatcher.start_polling(self.bot))

    def send_raw(self, function: str = 'send_message', **kwargs) -> None:
        """sending message"""

        async def send():
            max_retries = 20
            while max_retries:
                try:
                    await getattr(self.bot, function)(**kwargs)
                    return logging.info(log_text.format('message sent'))
                except exceptions.TelegramRetryAfter as e:
                    logging.exception(log_text.format(e))
                    max_retries -= 1
                    await asyncio.sleep(e.retry_after)
                except Exception as e:
                    logging.exception(log_text.format(e))
                    break

        default_kwargs = conf['DEFAULT_KWARGS'](function)
        kwargs = {**default_kwargs, **kwargs}
        log_text = 'send_message: {}'

        if self.loop.is_running():
            self.loop.create_task(send())
        else:
            self.loop.run_until_complete(send())

    @staticmethod
    def send_redis(function: str = 'send_message', **kwargs: Dict[str, Any]) -> None:
        """sending message via redis"""
        redis_conn.rpush(conf['REDIS_MESSAGES_KEY'], pickle.dumps({'function': function, **kwargs}))
        redis_conn.set(conf['REDIS_EXP_KEY'], 'EX', conf['REDIS_EXP_TIME'])

    def message(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'message' observer"""
        return self._add_router(event_name='message', *args, **kwargs)

    def edited_message(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'edited_message' observer"""
        return self._add_router(event_name='edited_message', *args, **kwargs)

    def channel_post(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'channel_post' observer"""
        return self._add_router(event_name='channel_post', *args, **kwargs)

    def edited_channel_post(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'edited_channel_post' observer"""
        return self._add_router(event_name='edited_channel_post', *args, **kwargs)

    def inline_query(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'inline_query' observer"""
        return self._add_router(event_name='inline_query', *args, **kwargs)

    def chosen_inline_result(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'chosen_inline_result' observer"""
        return self._add_router(event_name='chosen_inline_result', *args, **kwargs)

    def callback_query(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'callback_query' observer"""
        return self._add_router(event_name='callback_query', *args, **kwargs)

    def shipping_query(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'shipping_query' observer"""
        return self._add_router(event_name='shipping_query', *args, **kwargs)

    def pre_checkout_query(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'pre_checkout_query' observer"""
        return self._add_router(event_name='pre_checkout_query', *args, **kwargs)

    def poll(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'poll' observer"""
        return self._add_router(event_name='poll', *args, **kwargs)

    def poll_answer(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'poll_answer' observer"""
        return self._add_router(event_name='poll_answer', *args, **kwargs)

    def my_chat_member(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'my_chat_member' observer"""
        return self._add_router(event_name='my_chat_member', *args, **kwargs)

    def chat_member(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'chat_member' observer"""
        return self._add_router(event_name='chat_member', *args, **kwargs)

    def chat_join_request(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'chat_join_request' observer"""
        return self._add_router(event_name='chat_join_request', *args, **kwargs)

    def error(self, *args, **kwargs) -> CallbackType:
        """Decorator for 'error' observer"""
        return self._add_router(event_name='error', *args, **kwargs)

    def _add_router(self, *args, event_name: str, **kwargs) -> CallbackType:
        def wrapper(callback: CallbackType) -> CallbackType:
            observer = self._router.observers[event_name]
            observer.register(callback, *args, **kwargs)
            return callback

        return wrapper
