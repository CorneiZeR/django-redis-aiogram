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
| `tg_timeout` | blocking-pop timeout, or how long a shutdown waited |
| `tg_error` | text of a non-fatal error |
| `tg_crash_safe` | whether the consumer holds messages in flight; false on a Redis without `LMOVE` |
| `tg_mode` | `polling` or `webhook` |
| `tg_update` | the update id being handled |
| `tg_correlation_id` | the id every event about one message carries |
| `tg_alternative` | the awaitable method a synchronous send from a loop should move to |
| `tg_router` | a router module autodiscovery imported |
| `tg_pending` | work still in flight at shutdown: sends, or the updates a webhook process is answering |
| `tg_drain_timeout` | how long shutdown gave them |
| `tg_kind` | the event log kind of a row |
| `tg_count` | events in the batch being written |
| `tg_dropped` | events lost because the buffer was full, or sends dropped at shutdown |
| `tg_failures` | consecutive failures of the event writer |

## Events worth alerting on

| Message | Level | Meaning |
| ------- | ----- | ------- |
| `giving up on message` | ERROR | retries exhausted, the message was dropped |
| `handler failed for queued message` | ERROR | the send itself raised |
| `dropping undecodable queued message` | ERROR | a payload could not be deserialized |
| `blocking pop failed, retrying` | ERROR | lost the Redis connection; it retries |
| `the delivery consumer did not stop in time` | WARNING | the consumer outlived its join at shutdown; a message it holds may be redelivered |
| `cancelling updates still in flight` | WARNING | a webhook update outlasted the drain at shutdown; its request is answered 503, so Telegram redelivers it rather than a worker hanging on a stopped loop |
| `webhook refused an update` | WARNING | an update arrived while the process was shutting down; answered 503 so Telegram redelivers it |
| `the event loop thread did not start in time` | WARNING | a webhook process cannot hand updates to its loop; every request is refused with 503 until a thread starts |
| `the event loop thread is gone; starting another` | WARNING | that thread died and was replaced; the update that lost it was refused |
| `the event loop thread did not stop in time` | WARNING | it outlived its join at shutdown, so the teardown was skipped and `close()` can be retried |
| `skipping close` | WARNING | the loop was still running, so nothing was torn down; stop polling or the loop thread and call it again |
| `skipping drain` | WARNING | the same, for the drain alone: in-flight sends were left rather than waited for |
| `scheduling a send on a loop nothing in this process runs` | WARNING | nothing polls this process and no loop thread exists, so the send is created and never stepped |
| `rate limited by telegram` | WARNING | refused and backing off |
| `a synchronous send was called from a running event loop` | WARNING | `send`, `send_redis` or `send_many` from async code: correct, but it writes on the loop's own thread. `tg_alternative` names the awaitable form. Said once per process |
| `delivery started` | INFO | the consumer is up |
| `message sent` | INFO | one call succeeded |
| `the event log is falling behind; events are being dropped` | ERROR | the writer cannot keep up; rows are being lost, messages are not |
| `the event log is suspended after repeated failures` | ERROR | five failed batches in a row, usually a missing `migrate` |
| `leaving a refused pickle message in flight` | ERROR | `ALLOW_PICKLE` is off and a pickled payload is waiting for it |
| `leaving a message from a newer version in flight` | ERROR | the web tier was deployed ahead of the bot container |

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
