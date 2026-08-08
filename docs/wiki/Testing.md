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
read the list back. `loads` decodes a payload the same way the worker does, and
`unpack` reads the envelope it is wrapped in:

```python
import fakeredis
from django.test import override_settings

from django_redis_aiogram import bot
from django_redis_aiogram.envelope import unpack
from django_redis_aiogram.serializers import loads


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/0'})
def test_approval_notifies_the_reviewer(monkeypatch):
    server = fakeredis.FakeRedis()
    monkeypatch.setattr('django_redis_aiogram.client.get_redis', lambda: server)

    approve(order)  # your code, which calls bot.send(...)

    queued = [unpack(loads(raw)) for raw in server.lrange('TELEGRAM_BOT_MESSAGE', 0, -1)]
    assert [(call.function, call.kwargs) for call in queued] == [
        ('send_message', {'chat_id': 42, 'text': 'Order approved'}),
    ]
```

3.0 nests the arguments under an envelope so a message can carry a correlation
id — see **[[Event-log|Event log]]**. Reading through `unpack` is what keeps a
test from having to know that: `call.correlation_id` is there if you want it,
and `bot.send()` returns the same value.

Patch `django_redis_aiogram.client.get_redis` — the name the sending code looks
up. Patching `django_redis_aiogram.redis.get_redis` alone leaves the real
connection in place.

## Faking the send instead

When the payload is not the point, replace the call:

```python
from django_redis_aiogram import bot


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

Feed a dispatcher an update when the filter is the thing under test. `a_message` is the builder from the section above:

```python
import asyncio

from aiogram import Bot, Dispatcher, F, Router, types


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

`Bot(token='42:x')` opens no connection on its own, and the handler above only
records — so nothing reaches Telegram.

**A handler that answers is a different matter.** `feed_update` hands the
handler a *copy* of the event bound to the bot, so patching `answer` on the
message you constructed has no effect: the copy carries the real one, and
`await message.answer(...)` performs an actual API call. Stub the bot's session
instead, which is the seam every reply goes through:

```python
from aiogram.client.session.base import BaseSession


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - unused
        yield b''


def test_the_handler_answers():
    session = RecordingSession()
    fake = Bot(token='42:x', session=session)
    ...
    asyncio.run(dispatcher.feed_update(fake, update))

    assert [type(call).__name__ for call in session.calls] == ['SendMessage']
    assert session.calls[0].text == 'you have 3 open orders'
```

Calling the handler directly, as in the section above, avoids all of this and is
the better choice unless the routing itself is what you are testing.

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
# the consumer also passes correlation_id and queued_at, which a handler
# standing in for send_raw can ignore
delivery = BlpopDelivery(handler=lambda function, **payload: handled.append((function, payload)))
delivery.consume_pending()  # drains the list without blocking

function, payload = handled[0]
assert function == 'send_message'
assert payload['chat_id'] == 42
assert payload['text'] == 'hi'
```

`consume_pending()` returns as soon as the list is empty, so it needs no thread
and no timeout.

## What this project's own tests do

`tests/conftest.py` patches every alias of `get_redis` at once, which is why a
single fixture covers the client, the delivery consumer and the helpers. If a
recipe here stops working, `tests/test_documented_recipes.py` fails — the
snippets above are executed, not just written down.
