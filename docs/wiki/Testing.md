# Testing

Nothing happens at import time, so your suite needs neither a token nor a
reachable Redis. What it does need is a decision: are the sends part of what you
assert, or noise you want gone?

## Settings for tests

```python
# settings/test.py
TELEGRAM_BOT = {
    'FSM_STORAGE': 'memory',  # no Redis for dialogue state
}
```

That is enough. `TOKEN` and `REDIS_URL` may stay empty — they are only read when
something actually reaches Telegram or Redis.

Setting `'ENABLED': False` goes further: `send`, `send_redis` and `send_raw`
become no-ops. Convenient when Telegram is irrelevant to the suite, wrong if any
test asserts that a message was queued — those assertions would pass over
nothing.

## Asserting that your code queued a message

Point the connection at [fakeredis](https://pypi.org/project/fakeredis/) and
read the list back. `loads` decodes a payload the same way the worker does:

```python
import fakeredis
from django.test import override_settings

from django_redis_aiogram import bot
from django_redis_aiogram.serializers import loads


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/0'})
def test_approval_notifies_the_reviewer(monkeypatch):
    server = fakeredis.FakeRedis()
    monkeypatch.setattr('django_redis_aiogram.client.get_redis', lambda: server)

    approve(order)  # your code, which calls bot.send(...)

    queued = [loads(raw) for raw in server.lrange('TELEGRAM_BOT_MESSAGE', 0, -1)]
    assert queued == [{'function': 'send_message', 'chat_id': 42, 'text': 'Order approved'}]
```

Patch `django_redis_aiogram.client.get_redis` — the name the sending code looks
up. Patching `django_redis_aiogram.redis.get_redis` alone leaves the real
connection in place.

## Faking the send instead

When the payload is not the point, replace the call:

```python
def test_approval_notifies(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, 'send', lambda **kwargs: sent.append(kwargs))

    approve(order)

    assert sent == [{'chat_id': 42, 'text': 'Order approved'}]
```

## Testing a handler

Call it. A handler is an ordinary coroutine, and `Message` is a pydantic model
you can build:

```python
import asyncio
import datetime

from aiogram import types

from myapp.tg_router import start_handler


def a_message(text):
    return types.Message(
        message_id=1,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=types.Chat(id=42, type='private'),
        text=text,
    )


def test_start_greets():
    message = a_message('/start')
    replies = []

    async def answer(text, **kwargs):
        replies.append(text)

    # aiogram models refuse plain attribute assignment
    object.__setattr__(message, 'answer', answer)

    asyncio.run(start_handler(message))

    assert replies == ['Hello 42']
```

## Testing that the filters route

Feed a dispatcher an update when the filter is the thing under test:

```python
from aiogram import Bot, Dispatcher, F, Router


def test_only_text_reaches_the_echo():
    seen = []
    router = Router()

    @router.message(F.text == '/probe')
    async def probe(message):
        seen.append(message.text)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    asyncio.run(dispatcher.feed_update(Bot(token='42:x'), types.Update(update_id=1, message=a_message('/probe'))))

    assert seen == ['/probe']
```

`Bot(token='42:x')` opens no connection on its own — nothing here reaches
Telegram as long as the handler does not call `answer`.

To exercise your real routing, include `bot.router` instead of a fresh one. Mind
the order: aiogram stops at the first handler that matches, so a catch-all
`@bot.message()` registered earlier swallows everything after it — in a test as
much as in production.

## Testing the worker itself

You should not have to. If you want an end-to-end check, queue a message with
`send_redis`, then run the consumer once against fakeredis:

```python
from django_redis_aiogram import bot
from django_redis_aiogram.delivery import BlpopDelivery

# with get_redis patched to fakeredis as above
bot.send_redis(chat_id=42, text='hi')

handled = []
delivery = BlpopDelivery(handler=lambda **payload: handled.append(payload))
delivery.consume_pending()  # drains the list without blocking

assert handled == [{'function': 'send_message', 'chat_id': 42, 'text': 'hi'}]
```

`consume_pending()` returns as soon as the list is empty, so it needs no thread
and no timeout.

## What this project's own tests do

`tests/conftest.py` patches every alias of `get_redis` at once, which is why a
single fixture covers the client, the delivery consumer and the helpers. If a
recipe here stops working, `tests/test_documented_recipes.py` fails — the
snippets above are executed, not just written down.
