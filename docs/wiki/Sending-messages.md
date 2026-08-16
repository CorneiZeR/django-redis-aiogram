# Sending messages

```python
from django_redis_aiogram import bot

bot.send(chat_id=CHAT_ID, text='hello')
```

`send()` picks the route: inside the bot container it calls Telegram directly,
anywhere else it queues the call through Redis. Callers do not have to know
which process they are in.

Any Telegram API method aiogram exposes works — pass its name first. The name
is checked against that allowlist, so a queued payload cannot reach anything
else on the bot:

```python
bot.send('send_photo', chat_id=CHAT_ID, photo=URL, caption='look')
bot.send('send_chat_action', chat_id=CHAT_ID, action='typing')
```

## Choosing the route yourself

| Method | Behaviour |
| ------ | --------- |
| `send()` | direct in the bot container, queued elsewhere |
| `send_redis()` | always queue |
| `send_raw()` | always call Telegram from this process |

`send_raw` from a web process builds its own event loop and HTTP session. That
works, but it does not share the bot's rate-limit budget. Prefer `send()`.

Whether it *waits* depends on where it is called from, and the difference matters
in webhook mode. From ordinary request code it drives the loop and waits. From
inside a **handler** — which runs on that loop — it can only schedule, so the
request returns before Telegram has been called and a failure appears in the log
rather than in the response. Until 3.1.0 that scheduled send was not even
stepped until the next update arrived, because a web process ran the loop only
for the duration of one update; it now has a thread of its own.

## Keyboards

```python
from aiogram import types

markup = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [types.InlineKeyboardButton(text='Approve', callback_data='approve:42')],
        [types.InlineKeyboardButton(text='Open', web_app=types.WebAppInfo(url=URL))],
    ]
)

bot.send(chat_id=CHAT_ID, text='Review this', reply_markup=markup)
```

Keyboards survive the queue intact, including through a JSON round trip.

## Files

`file_id` and URLs are the cheapest thing to send, and always safe to queue:

```python
bot.send('send_photo', chat_id=CHAT_ID, photo='https://example.test/a.png')
bot.send('send_document', chat_id=CHAT_ID, document=EXISTING_FILE_ID)
```

Actual uploads work too:

```python
from aiogram.types import BufferedInputFile, FSInputFile, URLInputFile

bot.send('send_document', chat_id=CHAT_ID, document=FSInputFile('/app/media/report.pdf'))
bot.send('send_photo', chat_id=CHAT_ID, photo=BufferedInputFile(data, filename='chart.png'))
```

`FSInputFile` carries a path, so the file has to exist in the **bot container**
too — share a volume, or send bytes with `BufferedInputFile`.

## Errors

Queued messages are delivered by the worker; failures are logged there, not
raised in your view. For direct calls, `RAISE_EXCEPTION` propagates them:

```python
from aiogram.exceptions import TelegramBadRequest

try:
    bot.send_raw(chat_id=CHAT_ID, text='**broken*', parse_mode='Markdown')
except TelegramBadRequest:
    ...
```

Telegram rate-limit refusals are retried up to `MAX_RETRIES`; exhausting them
logs an error and, with `RAISE_EXCEPTION`, re-raises. See **[[Rate limits]]**
for staying under the limits in the first place.

## From Celery

Queue the call and let the bot container do the talking:

```python
@app.task
def notify(chat_id: int, text: str) -> None:
    bot.send(chat_id=chat_id, text=text)
```

Do not build a `TelegramBot()` per task — the shared `bot` is lazy and safe to
import anywhere; a fresh instance means a fresh event loop and HTTP session
that nothing closes.
