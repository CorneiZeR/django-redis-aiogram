from redis import Redis

from telegram_bot.settings import conf

redis_conn = Redis.from_url(conf.REDIS_URL)
