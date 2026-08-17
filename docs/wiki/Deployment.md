# Deployment

One container runs the bot. Every other process just queues messages.

```yaml
# docker-compose.yml
services:
  back:
    image: ${IMAGE}
    command: gunicorn core.wsgi:application -b 0:8000
    env_file: .env

  celery_worker:
    image: ${IMAGE}
    command: celery -A core worker -l info
    env_file: .env

  telegram_bot:
    image: ${IMAGE}
    command: python manage.py start_tgbot
    restart: always
    # not decoration: the in-flight list is keyed on this name, and without it
    # Docker invents a new one for each container it creates — see Delivery
    hostname: telegram-bot-1
    env_file: .env
    environment:
      DJANGO_REDIS_AIOGRAM_ENABLED: 1
    depends_on: [redis]

  redis:
    image: redis:7-alpine
    restart: always
```

## Upgrading to 3.0: order matters, once

**Run `manage.py migrate` first.** The package ships one table from 3.0, and it
is created whether or not you turn the event log on.

**Then deploy the bot container before the web tier.** 3.0 nests a queued call
inside an envelope. The new consumer reads the old flat shape, so a backlog
drains across the upgrade — the reverse does not hold: a 2.x consumer handed a
new payload calls the Telegram method with `__envelope__` as a keyword, raises,
logs it and swallows it, and the message is gone with nothing to redeliver.

Both are one-time concerns. After 3.0 the order is whatever you like.

Note the absence of `ports:` on `redis`. Nothing outside the compose network
reaches it, which is why no password appears here. Publish that port and Redis
needs `requirepass` and a `REDIS_URL` carrying the credentials — the queue is a
list of Telegram API calls, and whoever can write to it can send as your bot.

Note what is **not** set: `back` and `celery_worker` leave `ENABLED` alone.
They queue messages, and `ENABLED=0` would make those calls no-ops — the
messages would vanish with a debug line and nothing else. The flag is for
processes that must not reach Telegram or Redis at all: image builds, a
migration container, CI. See below.

## What ENABLED=0 turns off

- no router autodiscovery, so those modules are never imported
- no system checks registered
- `send`, `send_redis` and `send_raw` become no-ops that build neither a bot
  nor a connection
- `start_tgbot` reports why and exits

A disabled process needs no token and no reachable Redis at all.

`ENABLED` is parsed rather than tested for truthiness — `'false'`, `'no'`,
`'off'` and `0` all disable the bot, and an unparseable value raises rather
than being read as enabled.

## The restart: always trap

A clean exit still counts as a crash under `restart: always`, so a disabled
`start_tgbot` would restart forever. Either keep the container out of the
default set:

```yaml
  telegram_bot:
    profiles: [bot]
```

or park it:

```yaml
    command: python manage.py start_tgbot --idle
```

`--idle` blocks until a signal instead of returning.

## Health and shutdown

`SIGTERM` unwinds cleanly: polling stops, the consumer thread is joined, the
aiogram session and FSM storage are closed. Give the container enough grace
period to finish an in-flight send:

```yaml
    stop_grace_period: 30s
```

The grace period has to cover the three waits shutdown makes, in order:

| wait | bounded by | default |
| --- | --- | --- |
| joining the consumer thread | `REDIS_TIMEOUT` + 1 | 11s |
| draining in-flight sends | `DRAIN_TIMEOUT` | 5s |
| flushing the event log | `recorder.STOP_TIMEOUT` | 5s |

So 21 seconds at the defaults, and `30s` leaves room. Raise `DRAIN_TIMEOUT` if
your sends spend long in the rate limiter — before 3.1.0 it was hardcoded at five
seconds and no grace period could buy more. Watch the other direction too:
raising `REDIS_TIMEOUT` raises the join, and a grace period shorter than the sum
means Docker sends `SIGKILL` partway through, which is exactly the crash the
in-flight list exists to survive.

## Is it working?

`docker ps` answers the wrong question: the process being up says nothing about
the consumer thread, which can be dead while polling continues.

```shell
python manage.py tgbot_healthcheck
```

Exit 0 and a line on stdout when healthy, non-zero with the reason on stderr
otherwise. It checks three things: Redis answers, the consumer reported in
recently, and the queue is not piling up.

The consumer writes `<REDIS_MESSAGES_KEY>:heartbeat:<worker>` every
`HEARTBEAT_INTERVAL` seconds, with a TTL of three times that — so one missed
refresh is not a failure, but a dead thread stops looking alive on its own. The
key is per worker, named like the in-flight list, so each container answers for
itself.

```yaml
  telegram_bot:
    command: python manage.py start_tgbot
    healthcheck:
      test: ['CMD', 'python', 'manage.py', 'tgbot_healthcheck']
      interval: 30s
      timeout: 10s
      start_period: 30s
      retries: 3
```

`start_period` matters: the first heartbeat is written when the consumer's loop
first turns, so a container checked immediately after start has nothing to show
yet.

To fail when work is backing up rather than only when the worker is gone, set a
queue limit — as a setting, or per invocation:

```shell
python manage.py tgbot_healthcheck --max-queue 1000 --max-age 60
```

A disabled process is not unhealthy: with `ENABLED=0` the command says so and
exits 0, since nothing is meant to be running there.

## The event log and your database

With `EVENT_LOG` on, every process that records owns **one more database
connection** — the writer thread's, which nothing but the writer closes. Size
the pool for it: a gunicorn worker is a process, so four of them open eight
connections rather than four. A `CONN_MAX_AGE` of 0 costs the writer nothing
extra, because it holds its own connection rather than borrowing the request's.

`EVENT_LOG_SYNC` writes on the calling thread instead, which keeps the count at
one per worker — and makes every send wait for the database, which is why
`W009` warns about it. It is for tests.

Point it somewhere else if the traffic warrants: `EVENT_LOG_DATABASE` names any
alias in `DATABASES`, and the writer and the admin both use it explicitly, so
the feature works with or without the router installed. See
**[[Event-log|Event log]]**.

The bot container needs `DATABASES` reachable too. It is the same Django
project, but a different place on the network — a database that only the web
tier can reach records `outbound.queued` and never the `outbound.sent` that
says the message actually arrived.

## Scaling

One bot container is normally enough — Telegram's limits bind long before the
consumer does. Several are safe if you want the redundancy: the pop is atomic,
so a queued message goes to exactly one worker.

On Redis 6.2+ a message is moved to a per-worker processing list while it is
being sent, and stays there until the send has actually finished; a restarted
worker reclaims what it left behind — delivery is **at-least-once**, so a crash
mid-send can produce a duplicate. Before 3.1.0 it was removed as soon as the send
was *scheduled*, which meant polling mode did not have that guarantee at all.
Waiting for the send is something the handler opts into: `bot.send_raw`, which
this command uses, does — a handler of your own taking only `**kwargs` is still
acknowledged when it returns. Older servers lack `LMOVE` and fall back to plain
pops, which is **at-most-once**: a kill between the pop and the call loses that
one message — unless `REQUIRE_CRASH_SAFE` is on, in which case the command
refuses to start at all rather than run that way. A send that *fails* is
acknowledged and logged either way, never redelivered for ever. See
**[[Delivery]]**.

Do not run two containers polling the **same token**, though. Telegram allows
only one `getUpdates` consumer per bot, and the second will fight the first for
updates.

## Not using containers

Nothing here is docker-specific. Run `python manage.py start_tgbot` under
systemd or supervisor; the web and worker services need no extra environment,
since only one process should run `start_tgbot` in the first place.
