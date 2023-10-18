from typing import TypedDict

from django.conf import settings

from telegram_bot.defaults import DEFAULTS


class Settings(TypedDict):
    REDIS_EXP_TIME: int
    REDIS_EXP_KEY: str
    REDIS_MESSAGES_KEY: str
    TOKEN: str
    REDIS_URL: str
    MODULE_NAME: str


# noinspection PyTypeChecker
conf: Settings = {**DEFAULTS, **getattr(settings, 'TELEGRAM_BOT', {})}
