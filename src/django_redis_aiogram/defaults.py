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
    # record what the bot did to a table, one row per event, insert only. Off by
    # default: it is a table whose size is set by traffic, so turning it on is a
    # decision, and it needs a retention job to go with it
    'EVENT_LOG': False,
    # which kinds to keep; empty means every kind this version knows. Naming any
    # also opts out of the kinds a later release adds
    'EVENT_LOG_KINDS': (),
    # 'none', 'summary' (argument names and sizes) or 'full' (message bodies).
    # The default keeps personal data out of the table until you ask for it
    'EVENT_LOG_PAYLOAD': 'summary',
    'EVENT_LOG_MAX_PAYLOAD_BYTES': 8192,
    # values under these keys are blanked before a row is written
    'EVENT_LOG_REDACT_KEYS': ('token', 'secret', 'password', 'authorization', 'api_key', 'session'),
    # events held in memory while the writer is behind; a full buffer drops the
    # event rather than making a send wait on the database
    'EVENT_LOG_BUFFER_SIZE': 1000,
    'EVENT_LOG_BATCH_SIZE': 200,
    'EVENT_LOG_FLUSH_INTERVAL': 1,
    # days a row is kept; 0 keeps them for ever. Nothing on the write path
    # deletes anything — `manage.py tgbot_prune_events` is what reads this
    'EVENT_LOG_RETENTION_DAYS': 0,
    # a DATABASES alias for the log; empty means the default one
    'EVENT_LOG_DATABASE': '',
    # write on the calling thread instead of the writer's: tests only
    'EVENT_LOG_SYNC': False,
}
