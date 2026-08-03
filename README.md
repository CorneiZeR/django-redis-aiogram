# django-redis-aiogram

[![PyPI](https://img.shields.io/pypi/v/django-redis-aiogram.svg)](https://pypi.org/project/django-redis-aiogram/)
[![Python](https://img.shields.io/pypi/pyversions/django-redis-aiogram.svg)](https://pypi.org/project/django-redis-aiogram/)
[![CI](https://github.com/CorneiZeR/django-redis-aiogram/actions/workflows/ci.yml/badge.svg)](https://github.com/CorneiZeR/django-redis-aiogram/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/django-redis-aiogram.svg)](LICENSE)

Run [aiogram](https://docs.aiogram.dev/) in a container next to Django, write
your handlers as ordinary Django app code, and send Telegram messages from
anywhere in the project — directly or through a Redis queue.

Only the bot container runs the polling loop. Your web and worker processes
queue messages instead — though `send_raw` can also call Telegram directly from
any process when you need it to.

📖 **[Full documentation is in the wiki](https://github.com/CorneiZeR/django-redis-aiogram/wiki)** —
settings reference, delivery modes, deployment recipes, troubleshooting and the
1.x migration guide.

## Installation

```shell
pip install django-redis-aiogram
```

```python
# settings.py

INSTALLED_APPS = [
    ...,
    'django_redis_aiogram',
]

TELEGRAM_BOT = {
    'TOKEN': TELEGRAM_BOT_TOKEN,
    'REDIS_URL': REDIS_URL,
}
```

Requires Python 3.10–3.14, Django 5.2+, aiogram 3.30+.

Scalar settings — strings, integers and booleans — can also come from the
environment as `DJANGO_REDIS_AIOGRAM_<NAME>`, which is handy in containers.
Callables and mappings such as `DEFAULT_KWARGS` and `DEFAULT_BOT_PROPERTIES`
have no sensible textual form, so they stay in `settings.py`:

```shell
DJANGO_REDIS_AIOGRAM_TOKEN=123:abc
DJANGO_REDIS_AIOGRAM_ENABLED=0
```

Django settings win over the environment.

## Sending messages

```python
from aiogram import types
from django_redis_aiogram import bot

# the usual call: queues from your app, calls Telegram directly inside the
# bot container, so callers do not have to know which process they are in
bot.send(chat_id=CHAT_ID, text=TEXT)
bot.send('send_photo', chat_id=CHAT_ID, caption=TEXT, photo=URL)

# or pick the route yourself
bot.send_redis(chat_id=CHAT_ID, text=TEXT)
bot.send_raw(chat_id=CHAT_ID, text=TEXT)

markup = types.InlineKeyboardMarkup(
    inline_keyboard=[[types.InlineKeyboardButton(text='open', web_app=types.WebAppInfo(url=URL))]]
)
bot.send(chat_id=CHAT_ID, text=TEXT, reply_markup=markup)
```

Any aiogram bot method works — pass its name as the first argument.

With `RAISE_EXCEPTION` enabled, `send_raw` propagates failures:

```python
from aiogram.exceptions import TelegramBadRequest

try:
    bot.send_raw(chat_id=CHAT_ID, text='**oops*', parse_mode='Markdown')
except TelegramBadRequest:
    ...
```

## Handlers

Create `tg_router.py` in any installed app; it is imported automatically while
`AUTODISCOVER` is on. `MODULE_NAME` changes which file name is looked for.

```python
# myapp/tg_router.py
from aiogram import types, F
from django_redis_aiogram import bot


@bot.message(F.text.startswith('/start'))
async def start_handler(message: types.Message) -> None:
    await message.answer('hi')
```

Every aiogram observer has a matching decorator: `message`, `callback_query`,
`inline_query`, `poll_answer`, `chat_member`, and so on.

Handlers are `async`, so reach for Django's async ORM API (`afirst`,
`acreate`, …) or wrap sync code in `sync_to_async`.

FSM state is stored in Redis by default, so conversations survive a restart.

## Running the bot

```yaml
# docker-compose.yml
services:
  telegram_bot:
    image: ${IMAGE}
    command: python manage.py start_tgbot
    restart: always
    env_file: .env
    depends_on: [redis]
```

## Turning the bot off per process

Every process loads your Django apps, but only one of them should run the bot.
`ENABLED` lets the rest opt out: no autodiscover, no system checks, and
`send_raw` / `send_redis` become no-ops that never build a bot or open a
connection. A disabled process needs no credentials at all.

```yaml
services:
  back:
    environment:
      DJANGO_REDIS_AIOGRAM_ENABLED: 0
  telegram_bot:
    environment:
      DJANGO_REDIS_AIOGRAM_ENABLED: 1
```

`start_tgbot` exits cleanly when disabled. Under `restart: always` a clean exit
still counts as a restart loop, so either use a compose profile:

```yaml
  telegram_bot:
    profiles: [bot]
```

or keep the container parked with `python manage.py start_tgbot --idle`.

## Delivery

`send_redis` pushes onto a Redis list that the bot container consumes.

| Aspect               | `blpop` (default) | `keyspace`                          |
| -------------------- | ----------------- | ----------------------------------- |
| Server configuration | none              | `CONFIG SET notify-keyspace-events` |
| Managed Redis        | works             | usually refused                     |
| Latency              | immediate         | up to `REDIS_EXP_TIME`              |
| Database index       | any               | any (hardcoded to 0 in 1.x)         |

`keyspace` reproduces the 1.x mechanism and exists for compatibility. Prefer
`blpop`.

## Rate limits

Telegram publishes its limits, so the bot paces itself against them rather than
waiting to be refused and retrying:

| Limit | Default |
| ----- | ------- |
| Overall | 30 messages/second |
| Same chat | 1 message/second |
| Same group or channel | 20 messages/minute |

```python
TELEGRAM_BOT = {
    'RATE_LIMIT': {
        'overall_per_second': 30,
        'per_chat_per_second': 1,
        'group_per_minute': 20,
    },
}
```

Set any entry to `0` to drop that limit, or `RATE_LIMIT` to `None` to disable
pacing entirely. Budgets belong to a bot instance — Telegram meters per token,
so a second bot gets its own.

Retrying on `TelegramRetryAfter` still happens; pacing just means it should
rarely be needed.

## Settings

**Credentials**

| Setting     | Default | Description          |
| ----------- | ------- | -------------------- |
| `TOKEN`     | `''`    | Telegram bot token   |
| `REDIS_URL` | `''`    | Redis connection URL |

**Which processes run the bot**

| Setting        | Default       | Description                             |
| -------------- | ------------- | --------------------------------------- |
| `ENABLED`      | `True`        | Run the bot in this process at all      |
| `AUTODISCOVER` | `True`        | Import `<app>.<MODULE_NAME>` on startup |
| `MODULE_NAME`  | `'tg_router'` | Module to look for in each app          |

**Bot behaviour**

| Setting                  | Default         | Description                                  |
| ------------------------ | --------------- | -------------------------------------------- |
| `DEFAULT_BOT_PROPERTIES` | `{}`            | Passed to aiogram's `DefaultBotProperties`   |
| `DEFAULT_KWARGS`         | `lambda fn: {}` | Per-function extras the above cannot express |
| `FSM_STORAGE`            | `'redis'`       | `'redis'`, `'memory'`, or a dotted path      |
| `RATE_LIMIT`             | see above       | Proactive pacing, or `None` to disable       |
| `MAX_RETRIES`            | `10`            | Retries on Telegram rate limits              |
| `RAISE_EXCEPTION`        | `False`         | Let `send_raw` propagate failures            |

**Queue**

| Setting              | Default                  | Description                                |
| -------------------- | ------------------------ | ------------------------------------------ |
| `DELIVERY`           | `'blpop'`                | `'blpop'` or `'keyspace'`                  |
| `REDIS_MESSAGES_KEY` | `'TELEGRAM_BOT_MESSAGE'` | List holding queued calls                  |
| `BLPOP_TIMEOUT`      | `5`                      | How often the consumer checks for shutdown |
| `SERIALIZER`         | `'json'`                 | `'json'` or `'pickle'`                     |
| `ALLOW_PICKLE`       | `False`                  | Accept pickled payloads left by 1.x        |
| `REDIS_EXP_KEY`      | `'TELEGRAM_BOT_EXP'`     | `keyspace` delivery only                   |
| `REDIS_EXP_TIME`     | `5`                      | `keyspace` delivery only                   |

`manage.py check` validates all of these, including misspelled keys and unknown
bot properties — in processes where the bot is enabled. A disabled process
registers no checks at all, so run it somewhere `ENABLED` is true.

### parse_mode

Set it once, on the bot, instead of on every call:

```python
TELEGRAM_BOT = {
    'DEFAULT_BOT_PROPERTIES': {
        'parse_mode': 'HTML',
        'link_preview_is_disabled': True,
    },
}
```

`DEFAULT_KWARGS` remains for what `DefaultBotProperties` has no field for:

```python
def default_kwargs(function: str) -> dict:
    return {'send_photo': {'caption': 'Photo'}}.get(function, {})
```

## Logging

Everything is logged to the `django_redis_aiogram` logger, and values are
attached as structured fields rather than baked into the message, so a JSON or
structlog backend can index them.

```python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        'django_redis_aiogram': {'handlers': ['console'], 'level': 'INFO'},
    },
}
```

Fields are prefixed with `tg_`: `tg_function`, `tg_retry_after`, `tg_retries`,
`tg_max_retries`, `tg_delivery`, `tg_key`, `tg_channel`. Drop the level to
`DEBUG` to also see the no-ops a disabled process skips.

## Upgrading from 1.x

Once you are on Python 3.10–3.14, Django 5.2+ and aiogram 3.30+, no application
code has to change — `telegram_bot` still imports and still works in
`INSTALLED_APPS`, with a deprecation warning. It is removed in 3.0.

Those requirements are the one hard part of the upgrade: Django 4.2 reached end
of life, and aiogram 3.30 needs Python 3.10.

Worth doing:

1. **Rename the app and imports:** `telegram_bot` → `django_redis_aiogram`.
2. **Drop placeholder tokens.** The package no longer builds a bot or connects
   to Redis at import time, so a project without credentials boots and tests
   normally. Set `ENABLED: False` in processes that should not reach Telegram.
3. **Move `parse_mode`** from `DEFAULT_KWARGS` into `DEFAULT_BOT_PROPERTIES`.
4. **Use `bot.router`** instead of the private `bot._router`.
5. **Draining a 1.x queue?** Pickled payloads are refused by default. If the
   queue holds messages at the moment you deploy, set `'ALLOW_PICKLE': True`
   for the upgrade window and remove it once the queue has drained.
6. **Re-silence checks if you had to.** Ids moved from `telegram_bot.EXXX` to
   `django_redis_aiogram.EXXX`.

Delivery switches to `blpop` automatically. Keep the old behaviour with
`'DELIVERY': 'keyspace'`.

See [CHANGELOG.md](CHANGELOG.md) for the full list.

## Documentation

The [wiki](https://github.com/CorneiZeR/django-redis-aiogram/wiki) covers
everything in depth:

| | |
| --- | --- |
| [Installation](../../wiki/Installation) | install, configure, run |
| [Settings](../../wiki/Settings) | every setting, with defaults and check ids |
| [Handlers](../../wiki/Handlers) | routers, filters, FSM, the async ORM |
| [Sending messages](../../wiki/Sending-messages) | routes, keyboards, files, errors |
| [Delivery](../../wiki/Delivery) | how messages reach Telegram, and which mode to pick |
| [Rate limits](../../wiki/Rate-limits) | staying inside Telegram's limits |
| [Deployment](../../wiki/Deployment) | compose recipes, disabling the bot per process |
| [Logging](../../wiki/Logging) | the logger and its structured fields |
| [Serialization](../../wiki/Serialization) | what can be queued |
| [Troubleshooting](../../wiki/Troubleshooting) | symptoms and their usual causes |
| [Migrating from 1.x](../../wiki/Migrating-from-1.x) | what changed, and what you must do |

Pages live in [`docs/wiki/`](docs/wiki) and are published to the wiki
automatically, so they are reviewed alongside the code they describe.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go through
[SECURITY.md](SECURITY.md).
