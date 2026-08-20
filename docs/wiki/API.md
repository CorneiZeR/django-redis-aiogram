# API

The shared instance is what you normally use:

```python
from django_redis_aiogram import bot
```

`bot` is a `TelegramBot`. Building one needs no credentials — everything that
does appears on first use — so importing it anywhere is safe, including in a
process that never talks to Telegram.

`import django_redis_aiogram` costs about a millisecond, because the package
resolves its exports on attribute access. Naming `bot` is what loads aiogram and
the pydantic stack under it (~900 ms), so `from django_redis_aiogram import bot`
pays that once, at the moment of import. Put it in the modules that send —
router modules, the views and tasks that call `bot.send()` — and a process that
imports none of them never loads aiogram at all.

`ENABLED=0` covers the package's own boot: no autodiscover, so no `tg_router`
module is imported, and no checks are registered. It does not un-import
anything. A module that names `bot` at import time still loads aiogram in a
disabled process — the send is a no-op, the import is not free. Move it inside
the function if that matters.

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
| `bot.send_many(chat_ids, function='send_message', *, chunk_size=100, **kwargs)` | always queue, one message per chat, a chunk per round trip |

`function` must name a Telegram API method aiogram exposes; anything else raises
`ValueError` before it reaches the queue. See **[[Sending-messages|Sending messages]]**.

`send`, `send_redis` and `send_raw` return a **correlation id** — a `uuid.UUID`
that ties every row about that message together, whichever process wrote it.
`send_many` returns one per chat, in the order the chats were given. Store it
beside your own model if you want to join your records to the feed later. Each of
them also accepts one as a keyword argument, and a handler replying to an update
inherits that update's id without passing anything:

```python
identifier = bot.send(chat_id=chat_id, text='hello')
Receipt.objects.create(order=order, telegram_correlation_id=identifier)
```

Before 3.0 they returned `None`, so every existing call site still compiles.

### From code already on an event loop

| | |
| --- | --- |
| `await bot.asend(...)` | as `send`, without the blocking socket write |
| `await bot.asend_redis(...)` | as `send_redis` |
| `await bot.asend_many(...)` | as `send_many` |

Same signatures, same rows, and the same correlation id — resolved on the caller's
context before the first `await`, so a handler's replies still inherit the id of
the update that caused them.

The difference is not *where* the write happens. `redis.asyncio` writes on the
same thread the loop is running on; it just yields while waiting instead of
holding that thread, so under ASGI the thread goes on serving other requests
rather than sitting on a socket. Reach for these from an async view or an async
task. Note that only the waiting yields: `asend_many` iterates the chats and
serializes each chunk between its awaits, and that part is ordinary CPU work on
the loop's thread. A fan-out large enough to matter belongs in a task, not in a
request.

Each loop gets its own client, because `redis.asyncio` connections are loop-affine.
`await bot.aclose()` closes the one belonging to the loop that calls it, and it is
worth calling from a lifespan shutdown if your server has one — it is the only
path that closes the connection on the loop it belongs to, which is the only loop
that may close it. Without it the connection stays open until the client is
collected, and Python may say so with a `ResourceWarning`. **[[Deployment]]** has
the shutdown recipe.

### Queue introspection

| | |
| --- | --- |
| `bot.queue_depth()` | messages waiting for a worker, one `LLEN` |
| `bot.inflight_depth(worker=None)` | messages one worker is part-way through sending |
| `await bot.aqueue_depth()` / `await bot.ainflight_depth(...)` | the same read, without holding the loop |

`inflight_depth` defaults to this process's own worker identity; naming another is
how a monitor reads a list left behind by a worker that is gone. The key scheme
behind them is this package's business — an exporter should not have to reproduce
`<REDIS_MESSAGES_KEY>:processing:<worker>` by hand.

Each returns an `int` — a length at the moment it was read, not a correlation id
and not a reservation. A depth read and then acted on is already out of date, so
these answer "is the backlog growing" rather than "how many will this worker send".

## Handlers

One decorator per aiogram observer, all registering on `bot.router`:

`message`, `edited_message`, `channel_post`, `edited_channel_post`,
`inline_query`, `chosen_inline_result`, `callback_query`, `shipping_query`,
`pre_checkout_query`, `poll`, `poll_answer`, `my_chat_member`, `chat_member`,
`chat_join_request`, `error`.

```python
@bot.message(F.text == '/start')
async def start(message):
    await message.answer('hi')
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

## Values the settings accept

Every choice a setting offers exists as an enum member, so a project can import
the value instead of spelling the string:

```python
from django_redis_aiogram.enums import DeliveryKind, StorageKind, UpdateMode

TELEGRAM_BOT = {
    'DELIVERY': DeliveryKind.BLPOP,
    'FSM_STORAGE': StorageKind.REDIS,
    'MODE': UpdateMode.POLLING,
}
```

| | Members |
| --- | --- |
| `DeliveryKind` | `BLPOP` |
| `SerializerKind` | `JSON`, `PICKLE` |
| `StorageKind` | `REDIS`, `MEMORY` |
| `UpdateMode` | `POLLING`, `WEBHOOK` |
| `RateLimitKey` | the three `RATE_LIMIT` keys |
| `SerializationTag` | the `__model__`-style markers a queued payload carries |
| `EventKind` | what an event log row can be: `outbound.*`, `inbound.*`, `fsm.transition`, `queue.*`, `log.dropped` |

They are `(str, Enum)`, so a member compares equal to its string and works
anywhere the string does. `choices(DeliveryKind)` gives the plain-string set,
which is what the system checks validate against.

The values are **frozen**: queued payloads and stored settings carry them, so a
member may be renamed but never revalued.

## The event log

```python
from django_redis_aiogram.events import failure_kinds, kind_choices, register_kind
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.signals import events_recorded
```

`TelegramEvent` is the append-only feed: one row per thing that happened, insert
only, and `kind` is an unconstrained `CharField` whose legal values live in a
Python registry rather than in the schema. `register_kind(code, label,
failure=False)` adds your own; `kind_choices()` and `failure_kinds()` are what
the admin filters read.

Querying it is ordinary ORM work, and the indexes are built for two questions —
one message's history, and what a chat has seen:

```python
TelegramEvent.objects.filter(correlation_id=identifier).order_by('id')
TelegramEvent.objects.filter(chat_id=chat_id).order_by('-id')[:50]
```

`events_recorded` is the metrics seam: a `django.dispatch.Signal` fired once per
batch with the `Event` objects in it, from the event writer's own thread — except
under `EVENT_LOG_SYNC` and at shutdown, where there is no writer thread to run on:
there they run on the thread that recorded the event, or the one that called
`recorder.stop()` — which is not `bot.close()`, documented above, but the event
writer's own shutdown. `EVENT_LOG_SYNC` only takes effect with the log on, so a
receiver-only process still gets the writer thread whatever that flag says. It fires whether or not `EVENT_LOG` is on, which is
the point: counting what the bot does and keeping a row for it are separate
decisions. `Event`'s field names are pinned by `tests/test_public_surface.py` and
are therefore API.

Nothing here is imported unless you import it: `models.py` pulls no aiogram, so
a migration container pays nothing for it, and `signals.py` pulls neither aiogram
nor the ORM so a metrics module can import it at settings time. See
**[[Event-log|Event log]]**.

## Errors

```python
from django_redis_aiogram.exceptions import DjangoRedisAiogramError
```

| | Raised when |
| --- | --- |
| `DjangoRedisAiogramError` | base of everything this package raises |
| `SerializationError` | a payload cannot be encoded, or cannot be decoded |
| `UnknownApiMethodError` | a call names something that is not a Telegram API method |

Catching `DjangoRedisAiogramError` catches all of them. The two you are likely
to name keep the bases they had before the family existed —
`UnknownApiMethodError` is still a `ValueError`, and the serializer errors are
still `SerializationError` — so existing `except` clauses keep working.
Configuration problems remain Django's `ImproperlyConfigured`, since that is
what `manage.py check` and Django's own machinery expect.
