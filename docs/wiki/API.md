# API

The shared instance is what you normally use:

```python
from django_redis_aiogram import bot
```

`bot` is a `TelegramBot`. Building one is cheap and needs no credentials —
everything expensive appears on first use — so importing it anywhere is safe,
including in a process that never talks to Telegram.

## What the instance holds

| | What it is | Built |
| --- | --- | --- |
| `bot.bot` | the aiogram `Bot` | on first use; raises if `TOKEN` is empty |
| `bot.dispatcher` | the aiogram `Dispatcher`, with the configured FSM storage | on first use |
| `bot.loop` | the event loop this bot's work runs on | on first use, one per instance |
| `bot.router` | the `Router` the decorators register on | with the instance |
| `bot.redis_conn` | the shared Redis connection | on first use; raises if `REDIS_URL` is empty |
| `bot.max_retries` | how many times a rate-limited send is retried | from `MAX_RETRIES`, or the constructor |
| `bot.enabled` | whether this process should reach Telegram or Redis at all | read per access |
| `bot.rate_limiter` | the limiter for this token, shared with any other instance holding it | on first use |
| `bot.is_worker` | whether this process is the one polling Telegram | read per access |

These are public. 1.x code drives them directly — running the loop by hand,
feeding the dispatcher, reusing the connection — and that keeps working;
`tests/test_public_surface.py` fails if any of them disappears.

## Sending

| | |
| --- | --- |
| `bot.send(function='send_message', **kwargs)` | queue it, or call Telegram directly inside the bot container |
| `bot.send_redis(...)` | always queue |
| `bot.send_raw(...)` | always call Telegram from this process |

`function` must name a Telegram API method aiogram exposes; anything else raises
`ValueError` before it reaches the queue. See **[[Sending-messages|Sending messages]]**.

## Handlers

One decorator per aiogram observer, all registering on `bot.router`:

`message`, `edited_message`, `channel_post`, `edited_channel_post`,
`inline_query`, `chosen_inline_result`, `callback_query`, `shipping_query`,
`pre_checkout_query`, `poll`, `poll_answer`, `my_chat_member`, `chat_member`,
`chat_join_request`, `error`.

```python
@bot.message(F.text == "/start")
async def start(message):
    await message.answer("hi")
```

Arguments pass straight through to aiogram, so filters behave exactly as they do
there. See **[[Handlers]]**.

## Running and stopping

```python
bot.start_polling()  # attaches the router, then blocks on long polling
bot.close()  # drains in-flight sends, releases the storage, session and loop
```

`close(drain_timeout=5.0)` waits that long for sends still pacing behind the rate
limiter, cancels whatever outlasts it with a warning, then releases the FSM
storage's own Redis client, the bot's HTTP session and the loop. A closed
instance builds itself again on next use.

`start_tgbot` does both around the delivery consumer; you only need them when
running the bot yourself.

## A second instance

```python
own = TelegramBot(max_retries=3)
```

`TelegramBot(max_retries=None, loop=None)` — pass a loop to put its work on one
you already run.

Each instance builds its **own** aiogram `Bot`, HTTP session, dispatcher and,
unless you hand it one, event loop. What they share is the token, which comes
from settings either way, and therefore the rate-limit budget: `get_rate_limiter`
is keyed by token, because Telegram counts per bot and two limiters would let one
bot send at twice the rate. The Redis connection is shared too — it is
process-wide, not per instance.

Polling from more than one instance on the same token is not supported by
Telegram itself: one `getUpdates` consumer per bot.

Prefer the shared `bot`. A fresh instance inside a task or a request means a
fresh event loop and HTTP session that nothing closes — see **[[Sending-messages|Sending messages]]**.

## Module level

```python
from django_redis_aiogram import TelegramBot, bot, conf, get_redis, redis_conn, __version__
```

`conf` reads `settings.TELEGRAM_BOT` on first access, falls back to
`DJANGO_REDIS_AIOGRAM_<NAME>` for scalars, and resets itself on
`override_settings`. `redis_conn` is a lazy proxy over `get_redis()`; both hand
back the one connection.

## Deprecated

`telegram_bot` still imports and still works in `INSTALLED_APPS`, with a
`DeprecationWarning`. It is removed in 3.0 — see **[[Migrating-from-1.x|Migrating from 1.x]]**.
