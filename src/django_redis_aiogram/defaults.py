"""Every setting the package reads, with its default and the reason for it."""

from typing import Any


def no_default_kwargs(_function: str, /) -> dict[str, Any]:
    """Return no extra kwargs, whatever aiogram function is asked about."""
    return {}


DEFAULTS: dict[str, Any] = {
    # whether this process should talk to Telegram or Redis at all
    'ENABLED': True,
    'TOKEN': '',
    'REDIS_URL': '',
    # import <app>.<MODULE_NAME> for every installed app on startup
    'AUTODISCOVER': True,
    'MODULE_NAME': 'tg_router',
    # blpop; the keyspace consumer 1.x used was removed in 3.0
    'DELIVERY': 'blpop',
    # either json or pickle, json recommended
    'SERIALIZER': 'json',
    # the escape hatch for payloads JSON cannot describe. Off by default because
    # unpickling queued data lets whoever writes the queue execute code
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
    'REDIS_TIMEOUT': 10,
    # how often the consumer refreshes the key `tgbot_healthcheck` reads. The key
    # lives three times as long, so one missed refresh is not a failure
    'HEARTBEAT_INTERVAL': 10,
    # a queue longer than this fails the healthcheck; 0 turns the check off
    'HEALTHCHECK_MAX_QUEUE': 0,
    # where updates come from: 'polling' (a process calling getUpdates) or
    # 'webhook' (Telegram posting them to a URL you serve). Both are supported;
    # polling is the default because it needs nothing but an outbound connection
    'MODE': 'polling',
    # webhook mode: where Telegram posts updates, and the secret it echoes back
    # in X-Telegram-Bot-Api-Secret-Token so the view can tell it is Telegram
    'WEBHOOK_URL': '',
    'WEBHOOK_SECRET': '',
    # which update types to receive; empty means Telegram's own default set
    'WEBHOOK_ALLOWED_UPDATES': (),
}
