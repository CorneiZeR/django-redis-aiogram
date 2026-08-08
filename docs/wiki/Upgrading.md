# Upgrading

What each major release changed, newest first. Start at the section for the
version you are on and work down.

# From 2.x to 3.0

## The `telegram_bot` package name is gone

2.0 renamed the package and kept `telegram_bot` as a deprecated shim; 3.0
removes it. A project that upgrades without touching `INSTALLED_APPS` fails at
startup with `ModuleNotFoundError: No module named 'telegram_bot'`, which is the
loudest this could reasonably be.

```python
INSTALLED_APPS = ['django_redis_aiogram']
```

```python
from django_redis_aiogram import bot, conf, redis_conn
from django_redis_aiogram.client import TelegramBot
```

## Deploy the bot container before the web tier

3.0 nests a queued call under an envelope so it can carry a correlation id. The
3.0 consumer reads both shapes, so a backlog written by 2.x drains — but a **2.x
consumer handed a 3.0 payload** calls the Telegram method with `__envelope__` as
a keyword, raises, logs it and swallows it. The message is gone, with nothing
left to redeliver.

So: upgrade and restart the container running `start_tgbot` **first**, then the
web and Celery processes. A brief window where the new consumer reads old
payloads is fine; the reverse is not.

## Run migrate

The package ships one table now, and `migrate` creates it whether or not you
turn the event log on. Creating it later on a live database is the more
expensive order, so do it with the upgrade — see **[[Event-log|Event log]]**
before switching the feature on.

## Everything else that moved

`keyspace` delivery is gone: remove `'DELIVERY': 'keyspace'`, which check `E009`
now refuses rather than ignoring. The module-level string constants that
aliased enum members are gone too — import the member and interpolate `.value`.

If you are still on 1.x, do the 2.x section below first — the shim exists only
in 2.x, so 1.x to 3.0 is one jump with no compatibility layer to lean on.

# From 1.x to 2.x

If the Redis queue holds messages written by 1.x at the moment you deploy, set
`'ALLOW_PICKLE': True` for the upgrade window — unpickling queue data is code
execution, so it is refused by default. Remove the setting once the queue has
drained.

## Requirements

Python 3.10–3.14, Django 5.2+, aiogram 3.30+, redis 6.2+. Django 4.2 reached
end of life, and aiogram 3.30 needs Python 3.10.

## 1. Rename the app and the imports

```python
INSTALLED_APPS = ['django_redis_aiogram']
```

```python
from django_redis_aiogram import bot, conf, redis_conn
from django_redis_aiogram.client import TelegramBot
```

`TelegramBot` moved out of `telegram_bot.telegram_bot`, and the settings module
is `django_redis_aiogram.settings`. The package exports `bot` and `conf`, which
would otherwise shadow submodules of the same name. In 2.x the old name kept
working through a shim; in 3.0 it does not exist at all.

## 2. Drop placeholder tokens

The package no longer builds a bot or connects to Redis at import time, so a
project without credentials boots and tests normally. If you added something
like this to keep `manage.py test` working, delete it:

```python
# no longer needed
TG_BOT_KEY = os.getenv('TG_BOT_KEY') or '0:placeholder'
```

Instead, switch the bot off where it does not belong:

```python
TELEGRAM_BOT = {'ENABLED': os.getenv('RUN_BOT') == '1'}
```

or per container with `DJANGO_REDIS_AIOGRAM_ENABLED`. See **[[Deployment]]**.

## 3. Move parse_mode onto the bot

1.x had no way to reach aiogram's `DefaultBotProperties`, so projects injected
`parse_mode` into every call:

```python
# before
def default_kwargs(function):
    return {
        'send_message': {'parse_mode': 'HTML'},
        'send_photo': {'parse_mode': 'Markdown'},
    }.get(function, {})
```

```python
# after
TELEGRAM_BOT = {
    'DEFAULT_BOT_PROPERTIES': {'parse_mode': 'HTML'},
}
```

`DEFAULT_KWARGS` stays for what bot properties cannot express, such as a
default caption.

## 4. Use the public router

```python
dispatcher.include_router(bot.router)  # was bot._router
```

## 5. Prefer bot.send()

```python
bot.send(chat_id=chat_id, text=text)
```

It queues from your app and calls Telegram directly inside the bot container.
`send_redis` and `send_raw` still work.

## 6. Drain the 1.x queue, then drop the flag

If you needed `'ALLOW_PICKLE': True` for the upgrade window, the order in which
you close it again matters — a 1.x producer keeps writing pickled payloads:

1. upgrade or stop **every** producer: web, celery, anything calling
   `send_redis`
2. wait for the queue **and** every in-flight list to reach zero:
   `LLEN <REDIS_MESSAGES_KEY>` and `LLEN` on each
   `<REDIS_MESSAGES_KEY>:processing*` key — on Redis 6.2+ a message being sent
   sits in one of those, not in the queue
3. only then remove the setting

`ALLOW_PICKLE` controls reads: `True` accepts a pickled payload, `False`
refuses it. So removing it while an old producer is still running means its
messages are written and then refused on read. On Redis 6.2+ the consumer
leaves each refused message in its in-flight list and says so in the log, so
setting `ALLOW_PICKLE` back and restarting the worker delivers them; without
`LMOVE` they are gone. Either way it is the code-execution door, so close it as
soon as step 2 holds.

## 7. Re-silence checks if you had to

Ids moved from `telegram_bot.EXXX` to `django_redis_aiogram.EXXX`.

## Behaviour that changed by itself

| | 1.x | 2.0 |
| --- | --- | --- |
| Import without credentials | breaks the project | fine |
| Delivery | keyspace expiry events | `BLPOP`, no server config needed |
| Redis database | hardcoded to 0 | taken from `REDIS_URL` |
| Queue format | pickle | JSON; pickle refused unless opted in |
| Crash mid-send | message lost | redelivered on the next start (Redis 6.2+) |
| FSM state | lost on restart | stored in Redis |
| Rate limiting | retry after refusal | paced under the published limits |
| System checks | could never fail | actually validate |
| Logging | root logger | `django_redis_aiogram`, structured fields |
| Retries exhausted | silent drop | logged, and raised if configured |

2.x let you keep the old mechanism with `'DELIVERY': 'keyspace'`. 3.0 removed
it — see the section above.

## Verifying the upgrade

```shell
python manage.py check
python manage.py test
```

Then, in a shell on a non-bot process:

```python
from django_redis_aiogram import bot

bot.enabled  # False where you disabled it
bot.send(chat_id=YOUR_ID, text='upgrade check')
```

and confirm the bot container logs `message sent`.
