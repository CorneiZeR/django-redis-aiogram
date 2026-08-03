# Deployment

One container runs the bot. Every other process just queues messages.

```yaml
# docker-compose.yml
services:
  back:
    image: ${IMAGE}
    command: gunicorn core.wsgi:application -b 0:8000
    env_file: .env
    environment:
      DJANGO_REDIS_AIOGRAM_ENABLED: 0

  celery_worker:
    image: ${IMAGE}
    command: celery -A core worker -l info
    env_file: .env
    environment:
      DJANGO_REDIS_AIOGRAM_ENABLED: 0

  telegram_bot:
    image: ${IMAGE}
    command: python manage.py start_tgbot
    restart: always
    env_file: .env
    environment:
      DJANGO_REDIS_AIOGRAM_ENABLED: 1
    depends_on: [redis]
```

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

## Scaling

One bot container is normally enough — Telegram's limits bind long before the
consumer does. Several are safe if you want the redundancy: each queued
message is taken exactly once.

Do not run two containers polling the **same token**, though. Telegram allows
only one `getUpdates` consumer per bot, and the second will fight the first for
updates.

## Not using containers

Nothing here is docker-specific. Run `python manage.py start_tgbot` under
systemd or supervisor and set `DJANGO_REDIS_AIOGRAM_ENABLED=0` in the
environment of your web and worker services.
