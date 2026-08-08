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
| `tg_delivery` | the consumer that started, always `blpop` |
| `tg_key` | Redis list being consumed |
| `tg_timeout` | blocking-pop timeout |
| `tg_error` | text of a non-fatal error |

## Events worth alerting on

| Message | Level | Meaning |
| ------- | ----- | ------- |
| `giving up on message` | ERROR | retries exhausted, the message was dropped |
| `handler failed for queued message` | ERROR | the send itself raised |
| `dropping undecodable queued message` | ERROR | a payload could not be deserialized |
| `blocking pop failed, retrying` | ERROR | lost the Redis connection; it retries |
| `rate limited by telegram` | WARNING | refused and backing off |
| `delivery started` | INFO | the consumer is up |
| `message sent` | INFO | one call succeeded |

## The database event log

This page is about the structured log: a stream, shipped somewhere, rotated.
**[[Event-log|Event log]]** is the other tool — an optional table you can query
and join against your own models, off by default. Use the log for volume and
alerting, and the table for the questions that outlive a retention window.

## With structlog

`ProcessorFormatter` drops stdlib `extra` unless `ExtraAdder` is in its
`foreign_pre_chain`, so wire that up:

```python
import logging

import structlog

handler = logging.StreamHandler()
handler.setFormatter(
    structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=[structlog.stdlib.ExtraAdder()],
    )
)
logging.getLogger('django_redis_aiogram').addHandler(handler)
```

With it in place the `tg_` fields arrive as event keys:

```python
logger = logging.getLogger('django_redis_aiogram')
logger.warning('rate limited by telegram', extra={'tg_function': 'send_message'})
# -> {"event": "rate limited by telegram", "tg_function": "send_message", ...}
```

The message text is a constant, so the same event groups together regardless
of its values.
