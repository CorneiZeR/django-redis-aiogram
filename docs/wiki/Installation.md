# Installation

```shell
pip install django-redis-aiogram
```

Requires Python 3.10–3.14, Django 5.2+, aiogram 3.30+, redis 6.2+.

The redis floor is 6.2 because aiogram's `RedisStorage` asks for it, and
`FSM_STORAGE: 'redis'` is the default. On redis-py below 5.0.1 the storage
raises `AttributeError: 'Redis' object has no attribute 'aclose'`. redis-py 8 is
tested here and works, though aiogram's own optional extra stops at 7.

## Add the app

```python
# settings.py
import os

INSTALLED_APPS = [
    ...,
    'django_redis_aiogram',
]

TELEGRAM_BOT = {
    'TOKEN': os.environ.get('TELEGRAM_BOT_TOKEN', ''),
    'REDIS_URL': os.environ.get('REDIS_URL', ''),
}
```

That is the whole minimum, and both values may be empty at startup. The
package needs them only when something actually reaches Telegram or Redis, so
tests, migrations and a build all run without them.

The package ships one table, so run migrations after adding it:

```shell
python manage.py migrate
```

The table is created whether or not you turn the event log on — `EVENT_LOG` is
off by default, and nothing is written until you set it. See
**[[Event-log|Event log]]**.

## Configure from the environment

Scalar settings can come from `DJANGO_REDIS_AIOGRAM_<NAME>`:

```ini
# .env
DJANGO_REDIS_AIOGRAM_TOKEN=123:abc
DJANGO_REDIS_AIOGRAM_REDIS_URL=redis://redis:6379/0
DJANGO_REDIS_AIOGRAM_ENABLED=0
```

Django settings win over the environment. Callables and mappings —
`DEFAULT_KWARGS`, `DEFAULT_BOT_PROPERTIES`, `RATE_LIMIT` — have no sensible
textual form and stay in `settings.py`.

## Run the bot

```yaml
# docker-compose.yml
services:
  telegram_bot:
    image: ${IMAGE}
    command: python manage.py start_tgbot
    restart: always
    env_file: .env
    depends_on: [redis]

  redis:
    image: redis:7-alpine
    restart: always
```

See **[[Deployment]]** for the whole file, and for turning the bot off in every
other process.

## Check the configuration

```shell
python manage.py check
```

Settings are validated: wrong types, unknown keys, misspelled bot properties
and impossible rate limits all fail here rather than at the first message.
Missing credentials are reported as warnings, not errors, so a build or a
migration container is not blocked by them.

Run it somewhere the bot is enabled: a process with `ENABLED` off registers no
checks, so it reports nothing either way.

## Next

* **[[Handlers]]** to answer messages
* **[[Sending-messages|Sending messages]]** to send them
