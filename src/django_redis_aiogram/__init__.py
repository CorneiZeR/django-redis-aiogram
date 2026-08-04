"""Run aiogram next to Django and send Telegram messages through a Redis queue."""

from django_redis_aiogram.client import TelegramBot
from django_redis_aiogram.redis import get_redis, redis_conn
from django_redis_aiogram.settings import conf

__version__ = '2.0.0'

bot = TelegramBot()

__all__ = ('TelegramBot', '__version__', 'bot', 'conf', 'get_redis', 'redis_conn')
