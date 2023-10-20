from typing import TypedDict, Callable

from django.conf import settings

from telegram_bot.defaults import DEFAULTS


class Settings(TypedDict):
    REDIS_EXP_TIME: int
    REDIS_EXP_KEY: str
    REDIS_MESSAGES_KEY: str
    TOKEN: str
    REDIS_URL: str
    MODULE_NAME: str
    DEFAULT_KWARGS: Callable


# noinspection PyTypeChecker
conf: Settings = {**DEFAULTS, **getattr(settings, 'TELEGRAM_BOT', {})}
