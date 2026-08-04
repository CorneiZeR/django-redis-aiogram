# Troubleshooting

## Nothing is delivered, no errors anywhere

Check the bot container is actually running the bot:

```shell
docker compose logs telegram_bot | grep 'delivery started'
```

No such line means either `ENABLED` is off in that container, or the command
never got past startup. `manage.py check` will say which.

If you are on `keyspace` delivery, confirm the notifications are enabled:

```shell
redis-cli config get notify-keyspace-events   # needs to contain E and x
```

Managed Redis usually refuses `CONFIG SET`, so the worker cannot enable it
itself — it logs `cannot enable keyspace notifications`. Switch to
`'DELIVERY': 'blpop'`, which needs no server configuration.

## A send hangs instead of failing

`REDIS_TIMEOUT` (5 seconds by default) bounds both connecting and waiting for an
answer, so a Redis that accepts the connection and then stops responding raises
`redis.exceptions.TimeoutError` rather than holding the request thread.

redis-py only started applying a read deadline of its own in 8.0. On 5.x, 6.x
and 7.x a stalled server blocks the caller until the process is killed, which is
why the package sets the deadline itself rather than relying on the client.

## Messages pile up in Redis

```shell
redis-cli -n <db> llen TELEGRAM_BOT_MESSAGE
```

A growing list means the consumer is not running — see above. Messages wait
there until a worker takes them. On Redis 6.2+ a taken message sits in
`TELEGRAM_BOT_MESSAGE:processing:<worker>` until the send returns, and a
restart with the same `WORKER_NAME` reclaims it: at-least-once, so a crash
mid-send can duplicate a send. Without `LMOVE` it is at-most-once. A send that
exhausted `MAX_RETRIES` is logged and acknowledged, not redelivered.

## Handlers never fire

```python
from django_redis_aiogram import bot

len(bot.router.observers['message'].handlers)
```

Zero means autodiscovery did not find them. Usual causes:

- the file is not called `tg_router.py` (or `MODULE_NAME` says otherwise)
- the app is not in `INSTALLED_APPS`
- `AUTODISCOVER` or `ENABLED` is off in that process

If a router raises while importing, the error surfaces at startup — it is not
swallowed. 1.x did swallow it, so a typo there disabled the whole file
silently.

## The project will not start without a token

It should. 2.0 does not build a bot or connect to Redis at import time. If it
still fails, something in *your* code is touching `bot.bot`, `redis_conn` or
`send_raw` at import time — those are the points that genuinely need
credentials.

Placeholder tokens are no longer necessary; drop them.

## FSM state is lost on restart

`FSM_STORAGE` is `'redis'` by default. If you set it to `'memory'`, state lives
in the process and does not survive. 1.x had no storage at all, so this is
often left over from then.

## Duplicate messages

Check whether two bot containers are polling the same token — Telegram allows
one `getUpdates` consumer per bot.

The queue pop is atomic, so each message goes to one worker — that is ownership,
not exactly-once. Two other sources of duplicates: a worker killed mid-send has
its message reclaimed and sent again, and two workers sharing a `WORKER_NAME`
share an in-flight list, so one can reclaim a message the other is still
sending. Give each its own name.

## The bot ignores ENABLED

`ENABLED` is parsed, so `'false'` disables. If a value cannot be parsed you get
`ImproperlyConfigured` rather than a silent fallback. Both the app startup and
the send path read it the same way.

## Sends are slow

That is likely the pacing in **[[Rate limits]]** doing its job: one message per
second to the same chat, 20 per minute to a group. Verify with `RATE_LIMIT`
set to `None`; if it speeds up, tune the numbers rather than removing them, or
Telegram will start refusing.

## Something imports differently after upgrading

The package is `django_redis_aiogram`; `telegram_bot` still works as a
deprecated shim. `TelegramBot` moved to `django_redis_aiogram.client`, and the
settings module is `django_redis_aiogram.settings`. See
**[[Migrating from 1.x]]**.

## Getting more detail

Merge this logger into your existing `LOGGING`, keeping your own `version` and
`handlers`:

```python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {
        'django_redis_aiogram': {'handlers': ['console'], 'level': 'DEBUG'},
    },
}
```

See **[[Logging]]** for the fields each event carries.
