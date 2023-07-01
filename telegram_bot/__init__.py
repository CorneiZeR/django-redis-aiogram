from telegram_bot.telegram_bot import TelegramBot
from telegram_bot.redis import redis_conn
from telegram_bot.settings import conf

bot = TelegramBot()

__all__ = ('bot', 'redis_conn', 'conf')
