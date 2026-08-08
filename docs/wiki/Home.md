# django-redis-aiogram

Run [aiogram](https://docs.aiogram.dev/) in a container next to Django, write
handlers as ordinary Django app code, and send Telegram messages from anywhere
in the project.

Only the bot container runs the polling loop. Elsewhere `send()` queues the
message through Redis and returns; `send_raw()` skips the queue and talks to
Telegram from the calling process.

```python
from django_redis_aiogram import bot

bot.send(chat_id=CHAT_ID, text='hello')
```

## Start here

* **[[Installation]]** — install, configure, run
* **[[Settings]]** — every setting, with defaults
* **[[Handlers]]** — writing routers and using FSM
* **[[Sending messages]]** — `send`, `send_redis`, `send_raw`, keyboards, files
* **[[Testing]]** — running your suite without Redis, asserting what was queued
* **[[API]]** — the instance, its internals, and what stays public

## Going further

* **[[Delivery]]** — how messages get from Redis to Telegram, and which mode to pick
* **[[Webhook]]** — receiving updates over HTTP instead of polling for them
* **[[Rate limits]]** — staying inside Telegram's published limits
* **[[Deployment]]** — docker-compose recipes, disabling the bot per process
* **[[Logging]]** — the logger and its structured fields
* **[[Event-log|Event log]]** — recording what the bot did to a table
* **[[Serialization]]** — what can be queued, and the pickle-to-JSON move
* **[[Troubleshooting]]** — symptoms and their usual causes

## Upgrading

* **[[Upgrading]]** — what each major release changed, and what you have to do
* **[[AI-assistants|AI assistants]]** — the brief to hand a coding agent, and what they get wrong

---

Requires Python 3.10–3.14, Django 5.2+, aiogram 3.30+.
[Source](https://github.com/CorneiZeR/django-redis-aiogram) ·
[PyPI](https://pypi.org/project/django-redis-aiogram/) ·
[Changelog](https://github.com/CorneiZeR/django-redis-aiogram/blob/master/CHANGELOG.md)
