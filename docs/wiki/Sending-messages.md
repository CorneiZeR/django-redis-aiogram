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

**Whether it waits changed in 3.1.0, in a web process that also serves the
webhook.** A process serving the webhook gives the loop a thread of its own from
the first update it handles, and `send_raw` hands work to a running loop rather
than driving it. So from that point on it *schedules* and returns, where before
it drove the loop and blocked until Telegram answered.

What that costs is the exception: a send that fails after the retries used to
raise into your view under `RAISE_EXCEPTION`, and now appears in the log instead.
A process that never serves the webhook is unaffected — with no thread running
the loop, `send_raw` still drives it and waits.

From inside a **handler** it could never wait — a handler already runs on that
loop, so it can only schedule. What 3.1.0 changes there is that the scheduled
send now *runs*: before, nothing stepped it until the next update arrived, or
`close()`, or never.

If you need the answer, `await` the aiogram call yourself, or send from a process
that does not serve the webhook.

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
raised in your view. For direct calls, `RAISE_EXCEPTION` propagates them — but
only where `send_raw` still waits for the answer, which in a process that serves
the webhook it does not. See [Choosing the route yourself](#choosing-the-route-yourself)
above: there the failure reaches the log rather than the `except` below.

```python
from aiogram.exceptions import TelegramBadRequest

try:
    bot.send_raw(chat_id=CHAT_ID, text='**broken*', parse_mode='Markdown')
except TelegramBadRequest:
    ...
```

Telegram rate-limit refusals are retried up to `MAX_RETRIES`; exhausting them
logs an error and, with `RAISE_EXCEPTION`, re-raises — into the caller that was
waiting, so the same qualification applies. See **[[Rate limits]]**
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
