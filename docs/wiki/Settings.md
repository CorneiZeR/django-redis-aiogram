# Settings

Everything lives under `TELEGRAM_BOT` in `settings.py`. Scalar values can also
come from `DJANGO_REDIS_AIOGRAM_<NAME>`; Django settings take precedence.

All of it is validated by `manage.py check` — in processes where the bot is
enabled. A disabled process registers no checks at all.

## Credentials

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `TOKEN` | `''` | Telegram bot token |
| `REDIS_URL` | `''` | Redis connection URL, including the database index |

Neither is required for the project to boot.

## Which processes run the bot

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `ENABLED` | `True` | Run the bot in this process at all |
| `AUTODISCOVER` | `True` | Import `<app>.<MODULE_NAME>` on startup |
| `MODULE_NAME` | `'tg_router'` | Module to look for in each installed app |

`ENABLED` is parsed, not tested for truthiness: `'false'`, `'no'`, `'off'` and
`0` all disable the bot. Anything unparseable raises `ImproperlyConfigured`
rather than being read as enabled. See **[[Deployment]]**.

## Bot behaviour

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `DEFAULT_BOT_PROPERTIES` | `{}` | Passed to aiogram's `DefaultBotProperties` |
| `DEFAULT_KWARGS` | `lambda fn: {}` | Per-function extras the above cannot express |
| `FSM_STORAGE` | `'redis'` | `'redis'`, `'memory'`, or a dotted path |
| `MAX_RETRIES` | `10` | Retries after a Telegram rate-limit refusal |
| `RAISE_EXCEPTION` | `False` | Let `send_raw` propagate failures |

`DEFAULT_BOT_PROPERTIES` accepts every field aiogram defines: `parse_mode`,
`disable_notification`, `protect_content`, `allow_sending_without_reply`,
`link_preview`, `link_preview_is_disabled`, `link_preview_prefer_small_media`,
`link_preview_prefer_large_media`, `link_preview_show_above_text`,
`show_caption_above_media`. A misspelling fails at `manage.py check`.

```python
TELEGRAM_BOT = {
    'DEFAULT_BOT_PROPERTIES': {
        'parse_mode': 'HTML',
        'link_preview_is_disabled': True,
    },
}
```

`DEFAULT_KWARGS` covers what bot properties cannot, such as a default caption:

```python
def default_kwargs(function: str) -> dict:
    return {'send_photo': {'caption': 'Photo'}}.get(function, {})
```

## Updates

These decide how updates reach the bot. They have nothing to do with the queue,
which carries outbound messages in both modes — see **[[Webhook]]**.

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `MODE` | `'polling'` | Where updates come from: `'polling'` or `'webhook'` |
| `WEBHOOK_URL` | `''` | Where Telegram posts updates; required when `MODE` is `'webhook'` |
| `WEBHOOK_SECRET` | `''` | Required with `WEBHOOK_URL`; the view compares it with the header Telegram echoes |
| `WEBHOOK_ALLOWED_UPDATES` | `()` | Update types to receive; empty means Telegram's default set |

## Queue

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `DELIVERY` | `'blpop'` | The only consumer; `'keyspace'` was removed in 3.0 — see **[[Delivery]]** |
| `REDIS_MESSAGES_KEY` | `'TELEGRAM_BOT_MESSAGE'` | List holding queued calls |
| `WORKER_NAME` | hostname | Names this worker's in-flight list — see **[[Delivery]]** |
| `BLPOP_TIMEOUT` | `5` | How often the consumer checks for shutdown; capped just below `REDIS_TIMEOUT` |
| `DRAIN_TIMEOUT` | `5` | Seconds `close()` gives in-flight sends to finish before cancelling them |
| `REDIS_TIMEOUT` | `10` | Seconds a single Redis call may take before the server counts as gone |
| `HEARTBEAT_INTERVAL` | `10` | Seconds between the consumer's reports; the key lives three times as long |
| `HEALTHCHECK_MAX_QUEUE` | `0` | Longest queue still considered healthy; the check fails only above it, and `0` disables it |
| `SERIALIZER` | `'json'` | `'json'` or `'pickle'` — see **[[Serialization]]** |
| `ALLOW_PICKLE` | `False` | Let the reader accept pickled payloads. Needed to *read* them at all, and needed alongside `SERIALIZER: 'pickle'` to write them. Unpickling queue data is code execution, so only on a queue nothing untrusted can write to |

## Rate limits

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `RATE_LIMIT` | see below | Proactive pacing, or `None` to disable |

```python
TELEGRAM_BOT = {
    'RATE_LIMIT': {
        'overall_per_second': 30,
        'per_chat_per_second': 1,
        'group_per_minute': 20,
    },
}
```

See **[[Rate limits]]**.

## Event log

Off by default. Turning it on records what the bot did into one append-only
table, which needs `manage.py migrate` and a retention job — see
**[[Event-log|Event log]]** before you enable it.

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `EVENT_LOG` | `False` | Record events at all. Gates both the writing and the admin |
| `EVENT_LOG_KINDS` | `()` | Which kinds to keep; empty means every kind this version knows. Naming any also opts out of the kinds a later release adds |
| `EVENT_LOG_PAYLOAD` | `'summary'` | `'none'`, `'summary'` (argument names and sizes) or `'full'` (message bodies) |
| `EVENT_LOG_MAX_PAYLOAD_BYTES` | `8192` | Cap on the JSON column; `0` stores no payload at all |
| `EVENT_LOG_REDACT_KEYS` | see `defaults.py` | Payload keys whose values are blanked before a row is written |
| `EVENT_LOG_BUFFER_SIZE` | `1000` | Events held in memory while the writer is behind; a full buffer drops the event rather than making a send wait |
| `EVENT_LOG_BATCH_SIZE` | `200` | Rows per `bulk_create` |
| `EVENT_LOG_FLUSH_INTERVAL` | `1` | Seconds before a partial batch is written anyway |
| `EVENT_LOG_RETENTION_DAYS` | `0` | Days a row is kept; `0` keeps them for ever. Read by `manage.py tgbot_prune_events`, never on the write path |
| `EVENT_LOG_DATABASE` | `''` | A `DATABASES` alias for the log; empty means the default one |
| `EVENT_LOG_SYNC` | `False` | Write on the calling thread instead of the writer's. Tests only |

`EVENT_LOG_KINDS` and `EVENT_LOG_REDACT_KEYS` are settings-only, like
`WEBHOOK_ALLOWED_UPDATES`: a tuple has no textual form the environment could
carry, and a string from it would be read one character per item.

## Check ids

Errors are `django_redis_aiogram.EXXX`, warnings `django_redis_aiogram.WXXX`.
They moved from `telegram_bot.EXXX` in 2.0 — update `SILENCED_SYSTEM_CHECKS`
if you silenced any. An id is never reused once its setting is gone, so an
entry naming a retired one is dead but harmless.

| Id | Meaning |
| -- | ------- |
| `W001` / `W002` | `TOKEN` / `REDIS_URL` empty while the bot is enabled |
| `W003` | `TELEGRAM_BOT` contains unknown keys |
| `W004` | `BLPOP_TIMEOUT` is at or above `REDIS_TIMEOUT`, so the consumer caps it |
| `E001`–`E003`, `E017` | a boolean setting is not a boolean |
| `E004`–`E007`, `E009`–`E011` | a string setting is wrong, or not one of the allowed values |
| `E012`, `E014` | an integer setting is wrong or below its minimum |
| `E015` / `E016` | `DEFAULT_KWARGS` not callable / `DEFAULT_BOT_PROPERTIES` not a mapping |
| `E018` | unknown key in `DEFAULT_BOT_PROPERTIES` |
| `E019` | `FSM_STORAGE` is not `redis`, `memory` or a dotted path |
| `E020` | `RATE_LIMIT` is malformed |
| `E021` | `WORKER_NAME` is not a string |
| `E022` | `SERIALIZER` is `pickle` while `ALLOW_PICKLE` is `False` |
| `E023` | `HEARTBEAT_INTERVAL` is wrong or below 1 |
| `E024` | `HEALTHCHECK_MAX_QUEUE` is wrong or negative |
| `E025` / `E026` | `WEBHOOK_URL` / `WEBHOOK_SECRET` is not a string |
| `E027` | `WEBHOOK_URL` is set without a secret or is not https, or `MODE` is `webhook` with no URL |
| `E028` | `MODE` is not `polling` or `webhook` |
| `E029` | `WEBHOOK_ALLOWED_UPDATES` is not a list, or names an update type Telegram does not have |
| `E030` | `REDIS_TIMEOUT` is wrong or below 1 |
| `E031`, `E042` | `EVENT_LOG` / `EVENT_LOG_SYNC` is not a boolean |
| `E032`, `E035` | `EVENT_LOG_KINDS` / `EVENT_LOG_REDACT_KEYS` is not a list or tuple of strings |
| `E033` | `EVENT_LOG_PAYLOAD` is not `none`, `summary` or `full` |
| `E034`, `E039` | `EVENT_LOG_MAX_PAYLOAD_BYTES` / `EVENT_LOG_RETENTION_DAYS` is wrong or negative |
| `E036`–`E038` | `EVENT_LOG_BUFFER_SIZE` / `EVENT_LOG_BATCH_SIZE` / `EVENT_LOG_FLUSH_INTERVAL` is wrong or below 1 |
| `E040` | `EVENT_LOG_DATABASE` is not a string |
| `E041` | `EVENT_LOG_DATABASE` names an alias that is not in `DATABASES` |
| `E043` | `REDIS_URL` sets `decode_responses` while `ALLOW_PICKLE` is `True` |
| `E044` | `DRAIN_TIMEOUT` is not a finite number, or is negative |
| `W005` | the log is on while its database has no engine, so every event is dropped |
| `W006` | the log is on with `EVENT_LOG_RETENTION_DAYS` at 0, so nothing ever deletes a row |
| `W007` | `EVENT_LOG_BATCH_SIZE` is above `EVENT_LOG_BUFFER_SIZE`, so the batch can never fill |
| `W008` | `EVENT_LOG_KINDS` names a kind nothing records |
| `W009` | `EVENT_LOG_SYNC` is on, so a send waits for the database |
