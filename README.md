# django-redis-aiogram

[![PyPI](https://img.shields.io/pypi/v/django-redis-aiogram.svg)](https://pypi.org/project/django-redis-aiogram/)
[![Python](https://img.shields.io/pypi/pyversions/django-redis-aiogram.svg)](https://pypi.org/project/django-redis-aiogram/)
[![CI](https://github.com/CorneiZeR/django-redis-aiogram/actions/workflows/ci.yml/badge.svg)](https://github.com/CorneiZeR/django-redis-aiogram/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/django-redis-aiogram.svg)](LICENSE)

Run [aiogram](https://docs.aiogram.dev/) next to Django: write handlers as
ordinary Django app code, and send Telegram messages from anywhere in the
project.

One container runs the bot. Every other process — web, Celery, a management
command — pushes the call onto a Redis list and returns, so a request never
waits on Telegram.

```text
  web, celery  ──bot.send()──▶  Redis list  ──▶  start_tgbot  ──▶  Telegram
```

## Install

```shell
pip install django-redis-aiogram
```

```python
# settings.py
import os

INSTALLED_APPS = [..., "django_redis_aiogram"]

TELEGRAM_BOT = {
    "TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "REDIS_URL": os.environ.get("REDIS_URL", ""),
}
```

Both may be empty. Nothing connects or validates credentials at import time, so
tests and migrations run without them. Requires Python 3.10–3.14, Django 5.2+,
aiogram 3.30+, redis 5.0+.

## Use it

```python
# myapp/tg_router.py — imported automatically from every installed app
from aiogram import F, types

from django_redis_aiogram import bot


@bot.message(F.text == "/start")
async def start(message: types.Message) -> None:
    await message.answer("hi")
```

```python
# anywhere else in the project
from django_redis_aiogram import bot

bot.send(chat_id=CHAT_ID, text="Order approved")
```

```shell
python manage.py start_tgbot
```

A router module, a call, and one process running the bot. Everything else —
webhooks instead of polling, rate limits, per-process opt-out, healthchecks — is
configuration, and it is documented rather than required.

## Documentation

The [wiki](https://github.com/CorneiZeR/django-redis-aiogram/wiki) is the
documentation. Pages live in [`docs/wiki/`](docs/wiki), so they are reviewed in
the same pull request as the code they describe and published from `master`.

| | |
| --- | --- |
| [Installation](../../wiki/Installation) | install, configure, run |
| [Settings](../../wiki/Settings) | every setting, with defaults and check ids |
| [Handlers](../../wiki/Handlers) | routers, filters, FSM, the async ORM |
| [Sending messages](../../wiki/Sending-messages) | routes, keyboards, files, errors |
| [Testing](../../wiki/Testing) | your suite without Redis, asserting what was queued |
| [API](../../wiki/API) | the instance, its internals, and what stays public |
| [Delivery](../../wiki/Delivery) | how queued messages reach Telegram |
| [Webhook](../../wiki/Webhook) | receiving updates over HTTP instead of polling |
| [Rate limits](../../wiki/Rate-limits) | staying inside Telegram's published limits |
| [Deployment](../../wiki/Deployment) | compose recipes, healthchecks, per-process opt-out |
| [Logging](../../wiki/Logging) | the logger and its structured fields |
| [Serialization](../../wiki/Serialization) | what can be queued |
| [Troubleshooting](../../wiki/Troubleshooting) | symptoms and their usual causes |
| [Migrating from 1.x](../../wiki/Migrating-from-1.x) | what changed, and what you must do |
| [AI assistants](../../wiki/AI-assistants) | the brief to hand a coding agent |

Upgrading from 1.x: `telegram_bot` still imports and still works in
`INSTALLED_APPS` until 3.0, so nothing breaks on the version bump alone. The
migration page lists the settings that do need attention.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) for the workflow, [AGENTS.md](AGENTS.md) for
the same ground in the form coding agents read. Changes are in
[CHANGELOG.md](CHANGELOG.md); security reports go through
[SECURITY.md](SECURITY.md).
