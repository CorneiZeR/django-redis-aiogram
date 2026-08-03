# Handlers

Create the module named by `MODULE_NAME` — `tg_router.py` by default — in any
installed app. It is imported when the bot starts, provided `AUTODISCOVER` is
enabled and the module actually exists in that app.

```python
# myapp/tg_router.py
from aiogram import types, F
from django_redis_aiogram import bot


@bot.message(F.text.startswith('/start'))
async def start_handler(message: types.Message) -> None:
    await message.answer('hi')


@bot.message()
async def echo(message: types.Message) -> None:
    await message.reply(message.text)
```

Every aiogram observer has a decorator of the same name: `message`,
`edited_message`, `channel_post`, `edited_channel_post`, `inline_query`,
`chosen_inline_result`, `callback_query`, `shipping_query`,
`pre_checkout_query`, `poll`, `poll_answer`, `my_chat_member`, `chat_member`,
`chat_join_request`, `error`.

Arguments are passed straight to aiogram, so filters work as documented there.

## The Django ORM

Handlers are `async`, so use Django's async ORM API, or wrap synchronous code:

```python
from asgiref.sync import sync_to_async


@bot.callback_query(F.data.startswith('approve:'))
async def approve(query: types.CallbackQuery) -> None:
    order = await Order.objects.filter(pk=query.data.split(':')[1]).afirst()
    if order is None:
        await query.answer('gone')
        return

    await sync_to_async(order.approve)()
    await query.answer('done')
```

Calling the synchronous ORM directly raises `SynchronousOnlyOperation`.

## FSM

State is stored in Redis by default, so conversations survive a restart of the
bot container.

```python
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


class Wizard(StatesGroup):
    waiting_for_address = State()


@bot.message(Command('setup'))
async def setup(message: types.Message, state: FSMContext) -> None:
    await state.set_state(Wizard.waiting_for_address)
    await message.answer('Send me the address')


@bot.message(Wizard.waiting_for_address)
async def got_address(message: types.Message, state: FSMContext) -> None:
    await state.update_data(address=message.text)
    await state.clear()
    await message.answer('Saved')
```

Switch storage with `FSM_STORAGE` — `'memory'` for tests, or a dotted path to
your own `BaseStorage`.

Give every input state a way out. A `/cancel` handler that clears the state
avoids users getting stuck in a validation loop:

```python
from aiogram.filters import Command


@bot.message(Command('cancel'))
async def cancel(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer('Cancelled')
```

## Importing routers explicitly

Set `AUTODISCOVER` to `False` and import them yourself, for example from your
`AppConfig.ready()`.

## Testing handlers

`bot.router` is public — feed it a dispatcher without starting the bot:

```python
from aiogram import Bot, Dispatcher
from django_redis_aiogram import bot as tg


def test_start_answers():
    dispatcher = Dispatcher()
    dispatcher.include_router(tg.router)
    # feed_update with a constructed Update, patching Message.answer
```

Set `FSM_STORAGE` to `'memory'` in test settings so tests need no Redis.
