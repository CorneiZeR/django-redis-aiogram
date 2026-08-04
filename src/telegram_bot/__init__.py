"""Deprecated alias for :mod:`django_redis_aiogram`.

``telegram_bot`` is a very generic name for a top-level package in
site-packages and collided easily with project apps, so 2.0 renamed it. This
shim keeps existing imports and ``INSTALLED_APPS`` entries working.
"""

import warnings

from django_redis_aiogram import TelegramBot, bot, conf, get_redis, redis_conn

warnings.warn(
    "The 'telegram_bot' package is deprecated, use 'django_redis_aiogram' instead. It will be removed in 3.0.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ('TelegramBot', 'bot', 'conf', 'get_redis', 'redis_conn')
