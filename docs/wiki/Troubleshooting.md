# Troubleshooting

## Nothing is delivered, no errors anywhere

Check the bot container is actually running the bot:

```shell
docker compose logs telegram_bot | grep 'delivery started'
```

No such line means either `ENABLED` is off in that container, or the command
never got past startup. `manage.py check` will say which.

## `manage.py check` fails with `E009` after upgrading

`'DELIVERY': 'keyspace'` was the 1.x mechanism and 3.0 removed it. Drop the key,
or set it to `'blpop'` — see **[[Delivery]]**. The check fails rather than
falling back silently, because a delivery mode that quietly changes is worse
than one that refuses to start.

## A send hangs instead of failing

`REDIS_TIMEOUT` (10 seconds by default) bounds both connecting and waiting for an
answer, so a Redis that accepts the connection and then stops responding raises
`redis.exceptions.TimeoutError` rather than holding the request thread.

redis-py only started applying a read deadline of its own in 8.0. On 5.x, 6.x
and 7.x a stalled server blocks the caller until the process is killed, which is
why the package sets the deadline itself rather than relying on the client.

## Messages pile up in Redis

```shell
redis-cli -n <db> llen TELEGRAM_BOT_MESSAGE
```

`TELEGRAM_BOT_MESSAGE` is the default `REDIS_MESSAGES_KEY`; if you set your own,
it is that key here and in every path below.

A growing list does not by itself mean the consumer is stopped: producers can
simply be outpacing it, and `MAX_IN_FLIGHT` deliberately holds intake back while
sends are outstanding. Check the heartbeat and the in-flight list below before
concluding the worker is down — see above for one that genuinely is. Messages
wait in the queue until a worker takes them. On Redis 6.2+ a taken message sits in
`<key>:processing:<worker>` until the send has finished, and a restart under the
same **worker identity** reclaims it: at-least-once, so a crash mid-send can
duplicate a send. That identity is `WORKER_NAME` when it is set and the hostname
otherwise — so on a platform that gives each container a fresh hostname, a
recreated worker looks like a different one and leaves the old list untouched.
Set `WORKER_NAME` to something stable wherever hostnames change.

All of that holds for the worker `start_tgbot` runs; a handler of your own is
only held that way if it takes an `on_complete` keyword.

That list is expected to be non-empty while sends are in flight, and an entry
stays until its send finishes or shutdown cancels it. `MAX_IN_FLIGHT` bounds how
many sends the consumer leaves outstanding, and so how far the list can run
ahead. Without `LMOVE` it is at-most-once, unless `REQUIRE_CRASH_SAFE` is on —
then the worker refuses to start rather than deliver that way. A send that
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

## `ModuleNotFoundError: No module named 'telegram_bot'`

The 1.x package name was a deprecated shim in 2.x and is gone in 3.0. The
package is `django_redis_aiogram`: use it in `INSTALLED_APPS`, import from it,
and note that `TelegramBot` lives in `django_redis_aiogram.client` while the
settings module is `django_redis_aiogram.settings`. See **[[Upgrading]]**.

## The event log writes nothing

In order of how often it is the answer:

1. `TELEGRAM_BOT['EVENT_LOG']` is off. It is off by default, and `record()`
   returns before it reads anything else.
2. `migrate` has not run. The writer logs `no such table` once per batch and
   drops what it held; after five failures in a row it suspends for a minute
   rather than hammering the database, and records a `log.dropped` row for the
   gap once it gets through again.
3. The process you are looking at is not the one that records. `outbound.queued`
   is written by whichever process called `send_redis`; `outbound.sent` by the
   bot container. Enabling the log in one and not the other gives you half a
   story, and that is not a bug.
4. `EVENT_LOG_KINDS` is set and excludes what you are looking for. The list is
   **inclusive**: naming anything drops everything unnamed, including kinds a
   later release adds. `W008` warns when it names a kind this version does not
   know.
5. Nothing has been flushed yet. The writer batches on a timer, so a test that
   asserts immediately needs `recorder.flush()`. See **[[Testing]]**.

`manage.py check` catches the configuration half of this: `W005` if the log is
on with no database configured, `E041` if `EVENT_LOG_DATABASE` names an alias
that does not exist.

## The admin page is missing

The changelist is registered in `ready()` only when **both** are true:
`EVENT_LOG` is on, and `django.contrib.admin` is in `INSTALLED_APPS`. It is
above the `ENABLED` gate on purpose — reading the feed is not talking to
Telegram, so a web tier with `ENABLED=0` still shows it.

The flag is read per request as well, so turning it off hides the page without
a restart. If the app shows but every row 403s, the user is missing
`view_telegramevent`; if the rows show but `detail` and `error` are absent,
that is `view_telegramevent_payload` doing its job. See
**[[Event-log|Event log]]**.

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
