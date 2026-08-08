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

Unpickling queue data turns "can write to the list" into "can execute code in
the bot container". Payloads are JSON, and pickled ones are **refused by
default**.

`ALLOW_PICKLE` lifts the refusal. It exists as the escape hatch for payloads
JSON cannot describe — not as a migration aid — so treat turning it on as
extending the bot container's trust boundary to everything that can write to
the Redis list:

```python
TELEGRAM_BOT = {
    'ALLOW_PICKLE': True,
}
```

Setting `'SERIALIZER': 'pickle'` is not enough on its own: the reader still
refuses pickled payloads, so writing them means `'ALLOW_PICKLE': True` as well.
Only do so if you must queue objects JSON cannot represent, and only with a
Redis nothing untrusted can write to.

Decoding a JSON payload will only instantiate `aiogram.types` members that
subclass `TelegramObject`; a payload cannot name an arbitrary import path. Of
the file wrappers only `FSInputFile`, `URLInputFile` and `BufferedInputFile` are
rebuilt — any other input-file type is rejected rather than resolved.

### File payloads

A queued `FSInputFile` names a filesystem path, and the bot uploads that file
to whatever chat the payload says. Anyone able to write to the queue can
therefore read any file the bot container can — not just make Telegram calls.
This is inherent to supporting file sends through the queue; it is another
reason the Redis behind it must stay inside your own trust boundary.

## Tokens

The bot token is read from `TELEGRAM_BOT['TOKEN']` or the
`DJANGO_REDIS_AIOGRAM_TOKEN` environment variable. It is never logged.

`ENABLED=0` means a process needs no token: it reaches neither Telegram nor
Redis, and `manage.py check` stops asking for credentials. It does not take the
token away from a process that is given one — keeping it out of an environment
is the deployment's job, and the flag is what makes that possible.
