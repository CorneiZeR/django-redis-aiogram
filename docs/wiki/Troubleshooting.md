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

```python
from django_redis_aiogram import bot

bot.queue_depth()  # messages waiting for a worker
bot.inflight_depth()  # what this worker is part-way through sending
```

Or from a shell, if that is where you are:

```shell
redis-cli -n <db> llen TELEGRAM_BOT_MESSAGE
```

`TELEGRAM_BOT_MESSAGE` is the default `REDIS_MESSAGES_KEY`; if you set your own,
it is that key here and in every path below — which is the reason to prefer the
two calls above in anything you keep, an exporter especially. They read the keys
this package owns, so a monitor does not encode a scheme that is ours to change.

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

## The container is unhealthy while the probe says `healthy`

The probe was killed by Docker's `timeout`, so its exit code never arrived — `docker
inspect` shows `ExitCode: -1` and a growing `FailingStreak` beside a last line that
reads `healthy: heartbeat 6s old, 0 queued`.

It happens when the healthcheck runs `manage.py tgbot_healthcheck`, because a
management command runs `django.setup()` first: the whole of your `INSTALLED_APPS`,
every `AppConfig.ready()`, before the first Redis call. In one project that was 17.9
seconds. Use the form that does not:

```yaml
    environment:
      DJANGO_SETTINGS_MODULE: core.settings   # a healthcheck is a separate process
    healthcheck:
      test: ['CMD', 'python', '-m', 'django_redis_aiogram.healthcheck']
      timeout: 5s
```

If the probe answers `cannot read the settings: …` instead, that variable is the thing
missing: `manage.py` sets it inside its own process, so a container running it may
never export it.

See **[[Deployment]]**. Raising `timeout:` also stops the killing and leaves your whole
Django app being imported twice a minute to read two keys.

## What the healthcheck's refusals mean

Every line below is what the probe writes to stderr before exiting 1, from either form.
Grepping one out of `docker inspect` should land here.

| The line | What it is telling you |
| --- | --- |
| `redis is unreachable: …` | The client could not be built or could not `PING`. Covers a missing or malformed `REDIS_URL` and an unreadable `REDIS_TIMEOUT` as well as a Redis that is genuinely down — the probe cannot tell a server it cannot reach from one it cannot address |
| `TELEGRAM_BOT['…'] is not a number: …` | `HEARTBEAT_INTERVAL` or `HEALTHCHECK_MAX_QUEUE` holds something `int()` refuses. `manage.py check` reports these as `E023`/`E024`, but the container form never runs it — that is the point of it — so it says so itself |
| `cannot read the settings: …` | `DJANGO_SETTINGS_MODULE` is missing from the container's environment, or names a module that does not import. See the section above |
| `no heartbeat at …: the consumer has not written one within Ns, or it never started` | The key is absent: the consumer never ran, died before its first beat, or has been silent longer than the key's TTL. If the line adds that a limit over the TTL cannot be observed, `--max-age` is set above `3 × HEARTBEAT_INTERVAL` and is doing nothing |
| `the consumer last reported Ns ago, over the Ns limit` | The key is there and stale. The consumer thread is stuck or gone while the process lives — the failure this probe exists for |
| `the heartbeat at … is not a timestamp` | Something else writes to that key. Give the worker its own `REDIS_MESSAGES_KEY`, or its own database |
| `could not read the heartbeat: …` | `PING` answered and the next command did not: a failover in between, a replica that cannot serve the key, or `decode_responses` in a URL shared with a cache backend meeting bytes it cannot decode |
| `could not read the queue length: …` | The same, one command later |
| `N messages are queued, over the limit of N` | Work is backing up. `HEALTHCHECK_MAX_QUEUE` or `--max-queue` is what set that number; see **Messages pile up in Redis** above |

Two lines are not refusals and do not change the exit code:

| The line | What it is telling you |
| --- | --- |
| `N message(s) are in flight under other worker names …` | Written to stderr while still exiting 0, and only with `--stranded`. Another worker may be sending them this second; if it is gone, `manage.py tgbot_reclaim --worker <name>` requeues them. `at least N` means the bounded sweep stopped early, so the count is a floor |
| `disabled in this process; nothing to check` | `ENABLED` is off here, so nothing is meant to be running and nothing is wrong. Exit 0, and deliberately not coloured as a success |

Two more reach the log rather than the output — both mean the probe declined to answer
that part rather than fail the container over it:

- `could not scan for stranded in-flight lists`
- `could not establish which delivery guarantee is in force`

## The webhook answers 503, or every update 403s

**503** means the view refused the update rather than handling it, so Telegram will
redeliver — which is what you want. Four reasons, each with its own log line:

- `webhook received an update while the bot is disabled` — `ENABLED` is off here
- `webhook received an update while this deployment polls` — `MODE` is not `webhook`, so
  a worker is polling and this process must not also feed the dispatcher
- `webhook cannot build the bot` — `TOKEN` is missing or malformed
- `webhook refused an update` — nothing ran it: the process is shutting down, its loop was
  already closed by an earlier `close()`, or the loop's own thread had not started yet.
  The closed-loop case is worth knowing about in a web worker that stays up — something
  closed the bot and requests kept arriving

**403** means the `X-Telegram-Bot-Api-Secret-Token` header did not match
`WEBHOOK_SECRET`. Check that the value you registered with `manage.py tgbot_webhook set`
is the one the process now reads — rotating the setting without re-registering gives
exactly this. The comparison is on bytes, so a secret outside ASCII is compared like any
other: a matching one passes, and a mismatched one gets this 403 rather than a traceback.

**400** means the body did not parse as an update. Something other than Telegram is
posting to that URL.

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

That is likely the pacing in **[[Rate-limits|Rate limits]]** doing its job: one
message per second to the same chat, 20 per minute to a group. Verify with `RATE_LIMIT`
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
