"""What the middleware and the storage wrapper record, and what they cost off."""

import asyncio
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User
from django.test import override_settings

from django_redis_aiogram.enums import EventKind
from django_redis_aiogram.instrumentation import (
    RecordingStorage,
    install_instrumentation,
    instrumented,
)
from django_redis_aiogram.models import TelegramEvent
from django_redis_aiogram.recorder import recorder

# sync mode falls back to the writer thread inside a running loop, which is
# where every one of these runs, so flush() is what waits for the rows
ON = {'EVENT_LOG': True}
TOKEN = '123456:AAFakeTokenThatLooksLikeARealOneXXXXXXX'


class SpyStorage(MemoryStorage):
    """Records which forwarded call arrived, since MemoryStorage.close() is silent."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def set_state(self, key, state=None):
        self.calls.append('set_state')
        await super().set_state(key, state)

    async def get_state(self, key):
        self.calls.append('get_state')
        return await super().get_state(key)

    async def set_data(self, key, data):
        self.calls.append('set_data')
        await super().set_data(key, data)

    async def get_data(self, key):
        self.calls.append('get_data')
        return await super().get_data(key)

    async def close(self):
        self.calls.append('close')
        await super().close()


class Form(StatesGroup):
    """A state to move into."""

    name = State()


def an_update(text='hi', update_id=7):
    return Update(
        update_id=update_id,
        message=Message(
            message_id=1,
            date=datetime.now(tz=timezone.utc),
            chat=Chat(id=42, type='private'),
            from_user=User(id=99, is_bot=False, first_name='A'),
            text=text,
        ),
    )


def a_dispatcher(handler):
    dispatcher = Dispatcher(storage=MemoryStorage())
    install_instrumentation(dispatcher)
    router = Router()
    router.message()(handler)
    dispatcher.include_router(router)
    return dispatcher


def feed(dispatcher, update):
    bot = Bot(token=TOKEN, default=DefaultBotProperties())
    return asyncio.run(dispatcher.feed_update(bot, update))


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_an_update_is_recorded_from_arrival_to_handled():
    async def handler(message):
        return None

    feed(a_dispatcher(handler), an_update())
    recorder.flush(timeout=5)

    rows = list(TelegramEvent.objects.order_by('id'))
    kinds = [row.kind for row in rows]
    assert kinds == [EventKind.INBOUND_RECEIVED.value, EventKind.INBOUND_HANDLED.value], kinds
    assert rows[0].update_id == 7
    assert rows[0].chat_id == 42
    assert rows[0].user_id == 99
    # both halves of one update share an id, which is the point of recording it
    assert rows[0].correlation_id == rows[1].correlation_id
    assert rows[1].duration_ms is not None


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_handler_that_raises_is_recorded_and_still_raises():
    async def handler(message):
        msg = 'boom'
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match='boom'):
        feed(a_dispatcher(handler), an_update())
    recorder.flush(timeout=5)

    failed = TelegramEvent.objects.get(kind=EventKind.INBOUND_FAILED.value)
    assert failed.error_code == 'RuntimeError'
    assert 'boom' in failed.error


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_reply_inherits_the_update_it_answers():
    """The whole reason for the context variable: a project writes no plumbing
    and its reply still joins to the update that caused it."""
    seen = {}

    async def handler(message):
        from django_redis_aiogram.context import current_correlation_id

        seen['inside'] = current_correlation_id()

    feed(a_dispatcher(handler), an_update())
    recorder.flush(timeout=5)

    received = TelegramEvent.objects.get(kind=EventKind.INBOUND_RECEIVED.value)
    assert seen['inside'] == received.correlation_id


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_PAYLOAD': 'summary'})
def test_the_message_text_is_not_stored_by_default():
    async def handler(message):
        return None

    feed(a_dispatcher(handler), an_update(text='a secret plan'))
    recorder.flush(timeout=5)

    received = TelegramEvent.objects.get(kind=EventKind.INBOUND_RECEIVED.value)
    assert 'a secret plan' not in str(received.detail)
    assert received.detail['text'] == {'__omitted__': 'text', 'length': len('a secret plan')}


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT={**ON, 'EVENT_LOG_PAYLOAD': 'full'})
def test_the_message_text_is_stored_when_asked_for():
    async def handler(message):
        return None

    feed(a_dispatcher(handler), an_update(text='a secret plan'))
    recorder.flush(timeout=5)

    received = TelegramEvent.objects.get(kind=EventKind.INBOUND_RECEIVED.value)
    assert received.detail['text'] == 'a secret plan'


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_a_state_change_is_recorded_where_it_happens():
    """set_state is the transition, so wrapping the storage catches the ones a
    filter or a scene makes as well as the ones a handler makes."""
    storage = instrumented(MemoryStorage())
    key = StorageKey(bot_id=1, chat_id=42, user_id=99)

    asyncio.run(storage.set_state(key, Form.name))
    recorder.flush(timeout=5)

    row = TelegramEvent.objects.get(kind=EventKind.FSM_TRANSITION.value)
    assert row.detail['to'] == 'Form:name'
    assert row.chat_id == 42
    assert row.user_id == 99


@pytest.mark.django_db(transaction=True)
@override_settings(TELEGRAM_BOT=ON)
def test_the_wrapper_forwards_everything_else():
    """Built directly rather than through instrumented(), and against a spy.

    MemoryStorage.close() is a no-op, so a close() that forwarded nothing would
    have passed — the wrapper has to be watched, not just exercised.
    """
    inner = SpyStorage()
    storage = RecordingStorage(inner)
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)

    async def exercise():
        await storage.set_state(key, Form.name)
        await storage.set_data(key, {'answer': 42})
        state = await storage.get_state(key)
        data = await storage.get_data(key)
        await storage.close()
        return state, data

    state, data = asyncio.run(exercise())

    assert state == 'Form:name'
    assert data == {'answer': 42}
    # TelegramBot.close releases the storage through this, so it has to arrive
    assert inner.calls == ['set_state', 'set_data', 'get_state', 'get_data', 'close'], inner.calls


def test_nothing_is_installed_while_the_log_is_off():
    """Not a middleware that checks a flag — no middleware at all."""
    dispatcher = Dispatcher(storage=MemoryStorage())
    before = list(dispatcher.update.outer_middleware)

    install_instrumentation(dispatcher)

    assert list(dispatcher.update.outer_middleware) == before
    storage = MemoryStorage()
    assert instrumented(storage) is storage


@override_settings(TELEGRAM_BOT={'EVENT_LOG': True})
def test_the_storage_is_wrapped_when_the_log_is_on():
    assert isinstance(instrumented(MemoryStorage()), RecordingStorage)
