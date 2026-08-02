# Security

## Reporting a vulnerability

Please report privately through
[GitHub security advisories](https://github.com/CorneiZeR/django-redis-aiogram/security/advisories/new)
rather than opening a public issue.

## Supported versions

| Version | Supported |
|---------|-----------|
| 2.x     | yes       |
| 1.x     | no        |

## The Redis queue is a trust boundary

`send_redis` puts a serialized aiogram call into a Redis list, and the bot
worker executes whatever it finds there. Anything able to write to that list
can therefore choose which Telegram API call the bot makes, with which
arguments.

Keep Redis reachable only from your own services, and require authentication.

### Pickle

1.x serialized queue payloads with `pickle`, which turns "can write to the
list" into "can execute code in the bot container". 2.0 defaults to JSON.

Reads still accept pickled payloads so an upgrade does not have to drain the
queue first. Once your queue no longer holds 1.x payloads, close that path:

```python
TELEGRAM_BOT = {
    'SERIALIZER': 'json',
    'ALLOW_PICKLE': False,
}
```

Setting `'SERIALIZER': 'pickle'` re-enables writing pickled payloads. Only do
so if you must queue objects JSON cannot represent, and only with a trusted
Redis.

Decoding a JSON payload will only instantiate `aiogram.types` members that
subclass `TelegramObject`; a payload cannot name an arbitrary import path.

## Tokens

The bot token is read from `TELEGRAM_BOT['TOKEN']` or the
`DJANGO_REDIS_AIOGRAM_TOKEN` environment variable. It is never logged. Set
`ENABLED` to `False` in processes that should not hold it at all.
