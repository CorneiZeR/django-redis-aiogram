# Logging

Everything goes to the `django_redis_aiogram` logger. Values are attached as
structured fields rather than interpolated into the message, so a JSON or
structlog backend can index and filter them.

```python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        'django_redis_aiogram': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
```

Drop to `DEBUG` to also see the sends a disabled process skips.

## Fields

All prefixed with `tg_`, to avoid colliding with `LogRecord` attributes.

| Field | Where |
| ----- | ----- |
| `tg_function` | the aiogram method being called |
| `tg_retry_after` | seconds Telegram asked to wait |
| `tg_retries` | attempts made so far |
| `tg_max_retries` | the limit that was reached |
| `tg_delivery` | `blpop` or `keyspace` |
| `tg_key` | Redis list being consumed |
| `tg_channel` | keyspace channel subscribed to |
| `tg_timeout` | blocking-pop timeout |
| `tg_error` | text of a non-fatal error |

## Events worth alerting on

| Message | Level | Meaning |
| ------- | ----- | ------- |
| `giving up on message` | ERROR | retries exhausted, the message was dropped |
| `handler failed for queued message` | ERROR | the send itself raised |
| `dropping undecodable queued message` | ERROR | a payload could not be deserialized |
| `blocking pop failed, retrying` | ERROR | lost the Redis connection; it retries |
| `cannot enable keyspace notifications` | WARNING | the server refused `CONFIG SET` |
| `rate limited by telegram` | WARNING | refused and backing off |
| `delivery started` | INFO | the consumer is up |
| `message sent` | INFO | one call succeeded |

## With structlog

Point structlog's `ProcessorFormatter` at the handler and the `tg_` fields
arrive as event keys:

```python
logger.warning('rate limited by telegram', extra={'tg_function': 'send_message'})
# -> {"event": "rate limited by telegram", "tg_function": "send_message", ...}
```

The message text is a constant, so the same event groups together regardless
of its values.
