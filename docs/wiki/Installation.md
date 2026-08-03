# Installation

```shell
pip install django-redis-aiogram
```

Requires Python 3.10–3.14, Django 5.2+, aiogram 3.30+, redis 5.0+.

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

Both may be empty at startup — the package only needs them when something
actually reaches Telegram or Redis.

That is the whole minimum. Neither value has to be present for the project to
boot — they are only needed when something actually reaches Telegram or Redis,
so tests and migrations run fine without them.

## Configure from the environment

Scalar settings can come from `DJANGO_REDIS_AIOGRAM_<NAME>`:

```shell
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
```

See **[[Deployment]]** for turning the bot off in every other process.

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
* **[[Sending messages]]** to send them
