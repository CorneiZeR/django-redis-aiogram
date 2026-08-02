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
    # accept pickled payloads left in the queue by 1.x; turn off once drained
    'ALLOW_PICKLE': True,
    # 'redis', 'memory', or a dotted path to a BaseStorage subclass
    'FSM_STORAGE': 'redis',
    # forwarded to aiogram's DefaultBotProperties, e.g. {'parse_mode': 'HTML'}
    'DEFAULT_BOT_PROPERTIES': {},
    # per-function extras for what DefaultBotProperties cannot express
    'DEFAULT_KWARGS': no_default_kwargs,
    'MAX_RETRIES': 10,
    'RAISE_EXCEPTION': False,
    'REDIS_MESSAGES_KEY': 'TELEGRAM_BOT_MESSAGE',
    # how long a blocking pop waits before re-checking the shutdown flag
    'BLPOP_TIMEOUT': 5,
    # keyspace delivery only
    'REDIS_EXP_KEY': 'TELEGRAM_BOT_EXP',
    'REDIS_EXP_TIME': 5,
}
