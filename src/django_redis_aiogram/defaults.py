from typing import Any


def no_default_kwargs(function: str) -> dict[str, Any]:
    """Return no extra kwargs for any aiogram function."""
    return {}


DEFAULTS: dict[str, Any] = {
    # whether this process should talk to Telegram or Redis at all
    'ENABLED': True,
    'TOKEN': '',
    'REDIS_URL': '',
    # import <app>.<MODULE_NAME> for every installed app on startup
    'AUTODISCOVER': True,
    'MODULE_NAME': 'tg_router',
    # 'blpop' (recommended) or 'keyspace' (legacy, expiry-event based)
    'DELIVERY': 'blpop',
    # 'json' (recommended) or 'pickle'
    'SERIALIZER': 'json',
    # unpickling queued data means whoever writes the queue can execute code
    # in the bot container; enable only for the 1.x upgrade window, then drop it
    'ALLOW_PICKLE': False,
    # 'redis', 'memory', or a dotted path to a BaseStorage subclass
    'FSM_STORAGE': 'redis',
    # forwarded to aiogram's DefaultBotProperties, e.g. {'parse_mode': 'HTML'}
    'DEFAULT_BOT_PROPERTIES': {},
    # per-function extras for what DefaultBotProperties cannot express
    'DEFAULT_KWARGS': no_default_kwargs,
    # stay under Telegram's published limits instead of waiting to be refused;
    # set to None to disable. Budgets are per bot, so a second token gets its own
    'RATE_LIMIT': {
        'overall_per_second': 30,
        'per_chat_per_second': 1,
        'group_per_minute': 20,
    },
    'MAX_RETRIES': 10,
    'RAISE_EXCEPTION': False,
    'REDIS_MESSAGES_KEY': 'TELEGRAM_BOT_MESSAGE',
    # names this worker's in-flight list; defaults to the hostname. Set it when
    # several workers share a host, so they cannot reclaim each other's messages
    'WORKER_NAME': '',
    # how long a blocking pop waits before re-checking the shutdown flag
    'BLPOP_TIMEOUT': 5,
    # keyspace delivery only
    'REDIS_EXP_KEY': 'TELEGRAM_BOT_EXP',
    'REDIS_EXP_TIME': 5,
}
