# Webhook

Polling needs a process that runs forever and adds a round trip of latency. The
alternative is to let Telegram post each update to a URL you serve.

Both are supported the same way, and one setting says which one this deployment
uses:

```python
TELEGRAM_BOT = {'MODE': 'webhook'}  # or 'polling', the default
```

Scalars can come from the environment, so the choice can be made at startup
without touching code:

```shell
DJANGO_REDIS_AIOGRAM_MODE=webhook
```

`start_tgbot` prints which mode it is running in, honours `--mode` for a single
run, and the view refuses to serve while the mode is `polling` — two sources of
updates at once means no way to tell which one handled what.

Polling is the default because it needs nothing but an outbound connection.
Reach for a webhook when the delay matters, or when an inbound HTTP endpoint is
easier to run than a process that calls Telegram in a loop.

It does **not** remove the need for a long-running process. Only inbound polling
goes away: outbound messages still travel through Redis, so a worker still has
to consume them. Webhook mode is not a route to serverless.

Webhooks are also not always possible — a public HTTPS endpoint with a valid
certificate is a hard requirement, and plenty of deployments cannot offer one.
Nothing here pushes you towards them.

## What changes

| | Polling | Webhook |
| --- | --- | --- |
| Who receives updates | the `start_tgbot` container | whichever process serves the URL, normally web |
| Where handlers run | that container | in a request thread of your web server |
| Public HTTPS needed | no | yes, Telegram refuses plain HTTP |
| Outbound queue | consumed by the same container | still needs a consumer somewhere |

The last row is the one that catches people. Inbound updates and outbound
`bot.send()` calls are unrelated: the queue still needs a worker, and in webhook
mode that worker no longer polls.

## Setting it up

**1. Configure it.**

```python
# settings.py
import os

TELEGRAM_BOT = {
    'TOKEN': os.environ['TELEGRAM_BOT_TOKEN'],
    'REDIS_URL': os.environ['REDIS_URL'],
    'MODE': 'webhook',
    'WEBHOOK_URL': 'https://example.com/tg/9c1f2b7a/',
    'WEBHOOK_SECRET': os.environ['TELEGRAM_WEBHOOK_SECRET'],
}
```

`MODE` set to `webhook` without a URL is check error `E027`: half-configured,
the bot would receive nothing and say nothing about it.

The secret is not optional: the view refuses to run without one, and
`manage.py check` reports `E027`. Telegram echoes it back in the
`X-Telegram-Bot-Api-Secret-Token` header, and the view compares it with
`hmac.compare_digest`. Give the path an unguessable segment too — that is one
less thing scanning the internet will find.

**2. Serve the view.**

```python
# urls.py
from django.urls import path

from django_redis_aiogram.webhook import telegram_webhook

urlpatterns = [
    path('tg/9c1f2b7a/', telegram_webhook),
]
```

It is CSRF-exempt, accepts `POST` only, and is a plain synchronous view — which
is deliberate. An async view runs on the server's loop under ASGI but on a
throwaway loop per request under WSGI, and the bot's HTTP session binds to the
first loop that uses it. Driving the bot's own loop behaves the same either way.

**3. Register it with Telegram.**

```shell
python manage.py tgbot_webhook set
python manage.py tgbot_webhook info
```

Telegram remembers the URL, not your settings file. `info` prints the pending
count and the last delivery error, which is the first thing to look at when
updates stop arriving.

**4. Run a worker for the queue.**

```shell
python manage.py start_tgbot
```

In webhook mode the same command consumes the queue and never calls
`getUpdates`. Skipping it means `bot.send()` from your app queues messages
nobody delivers.

`--mode polling` and `--mode webhook` override the setting for one run, which is
what you want when trying the other mode without editing anything. It changes
**this process only** — the view reads the setting — so the command warns when
the two disagree and says what will happen: a webhook worker whose setting says
polling gets no updates, because the view refuses them, and polling while a
webhook is registered fails at `getUpdates`. For a real switch, change `MODE` (or
`DJANGO_REDIS_AIOGRAM_MODE`) everywhere.

## Going back to polling

```shell
python manage.py tgbot_webhook delete
python manage.py start_tgbot --mode polling
```

`getUpdates` refuses to run while a webhook is registered, so polling will not
start until the webhook is deleted. Set `MODE` back to `'polling'` once you have
decided to stay there — `tgbot_webhook set` warns when you register a webhook
that the configured mode will not use.

## What to watch out for

**Handlers run in your web workers.** A handler that blocks holds a request
worker for as long as it takes. That is fine for a reply and wrong for a job
that takes a minute — queue that work instead.

**FSM state must be shared.** With several web workers, the update that starts a
dialogue and the one that continues it land in different processes.
`FSM_STORAGE: 'redis'` is the default and it is what makes that work; `'memory'`
cannot.

**Telegram retries a non-2xx.** The view answers 200 even when a handler raised,
because a handler that failed once will fail the same way on redelivery — that
is a loop, not a retry. Failures are logged; see **[[Logging]]**.

It answers **503** for the other case: an update that was *refused* rather than
handled, because the process is shutting down and nothing ran. There redelivery
is the point — during a rolling restart it is the difference between the update
moving to the next instance and disappearing into a 200 nobody acted on.

**Updates are not queued through Redis.** They go straight from the request to
the dispatcher. Redis carries outbound messages only, in both modes.

## Health

`tgbot_healthcheck` reads the consumer's heartbeat, so in webhook mode it
answers for the `start_tgbot` worker rather than for the web process — the
worker still runs the queue consumer, it just does not poll. See
**[[Deployment]]**.
