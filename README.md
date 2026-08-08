# django-redis-aiogram

[![PyPI](https://img.shields.io/pypi/v/django-redis-aiogram.svg)](https://pypi.org/project/django-redis-aiogram/)
[![Python](https://img.shields.io/pypi/pyversions/django-redis-aiogram.svg)](https://pypi.org/project/django-redis-aiogram/)
[![CI](https://github.com/CorneiZeR/django-redis-aiogram/actions/workflows/ci.yml/badge.svg)](https://github.com/CorneiZeR/django-redis-aiogram/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/django-redis-aiogram.svg)](https://github.com/CorneiZeR/django-redis-aiogram/blob/master/LICENSE)

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

INSTALLED_APPS = [..., 'django_redis_aiogram']

TELEGRAM_BOT = {
    'TOKEN': os.environ.get('TELEGRAM_BOT_TOKEN', ''),
    'REDIS_URL': os.environ.get('REDIS_URL', ''),
}
```

Both may be empty. Nothing connects or validates credentials at import time, so
tests and migrations run without them. Requires Python 3.10–3.14, Django 5.2+,
aiogram 3.30+, redis 6.2+.

## Use it

```python
# myapp/tg_router.py — imported automatically from every installed app
from aiogram import F, types

from django_redis_aiogram import bot


@bot.message(F.text == '/start')
async def start(message: types.Message) -> None:
    await message.answer('hi')
```

```python
# anywhere else in the project
from django_redis_aiogram import bot

bot.send(chat_id=CHAT_ID, text='Order approved')
```

```shell
python manage.py start_tgbot
```

A router module, a call, and one process running the bot. Everything else — rate
limits, per-process opt-out, healthchecks — is configuration, and it is
documented rather than required. Webhook mode is the one alternative that also
asks for a URL route; [Webhook](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Webhook) has the four steps.

## Documentation

The [wiki](https://github.com/CorneiZeR/django-redis-aiogram/wiki) is the
documentation. Pages live in [`docs/wiki/`](https://github.com/CorneiZeR/django-redis-aiogram/tree/master/docs/wiki), so they are reviewed in
the same pull request as the code they describe and published from `master`.

| | |
| --- | --- |
| [Installation](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Installation) | install, configure, run |
| [Settings](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Settings) | every setting, with defaults and check ids |
| [Handlers](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Handlers) | routers, filters, FSM, the async ORM |
| [Sending messages](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Sending-messages) | routes, keyboards, files, errors |
| [Testing](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Testing) | your suite without Redis, asserting what was queued |
| [API](https://github.com/CorneiZeR/django-redis-aiogram/wiki/API) | the instance, its internals, and what stays public |
| [Delivery](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Delivery) | how queued messages reach Telegram |
| [Webhook](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Webhook) | receiving updates over HTTP instead of polling |
| [Rate limits](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Rate-limits) | staying inside Telegram's published limits |
| [Deployment](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Deployment) | compose recipes, healthchecks, per-process opt-out |
| [Logging](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Logging) | the logger and its structured fields |
| [Event log](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Event-log) | recording what the bot did to a table |
| [Serialization](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Serialization) | what can be queued |
| [Troubleshooting](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Troubleshooting) | symptoms and their usual causes |
| [Upgrading](https://github.com/CorneiZeR/django-redis-aiogram/wiki/Upgrading) | what each major release changed, and what you must do |
| [AI assistants](https://github.com/CorneiZeR/django-redis-aiogram/wiki/AI-assistants) | the brief to hand a coding agent |

Upgrading to 3.0: the deprecated `telegram_bot` package name is gone, so
`INSTALLED_APPS` and imports have to name `django_redis_aiogram`. The upgrading
page lists everything else that needs attention.

## Contributing

[CONTRIBUTING.md](https://github.com/CorneiZeR/django-redis-aiogram/blob/master/CONTRIBUTING.md) for the workflow, [AGENTS.md](https://github.com/CorneiZeR/django-redis-aiogram/blob/master/AGENTS.md) for
the same ground in the form coding agents read. Changes are in
[CHANGELOG.md](https://github.com/CorneiZeR/django-redis-aiogram/blob/master/CHANGELOG.md); security reports go through
[SECURITY.md](https://github.com/CorneiZeR/django-redis-aiogram/blob/master/SECURITY.md).
