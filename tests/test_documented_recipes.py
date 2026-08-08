"""The Testing wiki page tells consumers how to test their own code.

Its snippets are executed here, because documentation that cannot run is worse
than none: a reader trusts it and then debugs our page instead of their code.
"""

import ast
import asyncio
import datetime
import importlib
import pathlib
import re

import fakeredis
import pytest
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.session.base import BaseSession
from django.test import override_settings

from django_redis_aiogram import bot
from django_redis_aiogram.delivery import BlpopDelivery
from django_redis_aiogram.envelope import unpack
from django_redis_aiogram.serializers import loads

QUEUE = 'TELEGRAM_BOT_MESSAGE'


def a_message(text):
    return types.Message(
        message_id=1,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=types.Chat(id=42, type='private'),
        text=text,
    )


@override_settings(TELEGRAM_BOT={'REDIS_URL': 'redis://localhost:6379/0'})
def test_the_fakeredis_queue_assertion(monkeypatch):
    """Patching client.get_redis is what the page tells the reader to patch."""
    server = fakeredis.FakeRedis()
    monkeypatch.setattr('django_redis_aiogram.client.get_redis', lambda: server)

    approve({'reviewer': 42})

    queued = [unpack(loads(raw)) for raw in server.lrange(QUEUE, 0, -1)]
    assert [(call.function, call.kwargs) for call in queued] == [
        ('send_message', {'chat_id': 42, 'text': 'Order approved'}),
    ]
    assert queued[0].correlation_id is not None, 'the envelope lost the id the page promises'


def approve(order):
    """Stands in for the caller the page's snippets are written around."""
    bot.send(chat_id=order['reviewer'], text='Order approved')


def test_faking_the_send(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, 'send', lambda **kwargs: sent.append(kwargs))

    approve({'reviewer': 42})

    assert sent == [{'chat_id': 42, 'text': 'Order approved'}]


def test_calling_a_handler_directly():
    """object.__setattr__ is needed, and the page says so."""

    async def start_handler(message):
        await message.answer(f'Hello {message.chat.id}')

    message = a_message('/start')
    replies = []

    async def answer(text, **kwargs):
        replies.append(text)

    object.__setattr__(message, 'answer', answer)
    asyncio.run(start_handler(message))

    assert replies == ['Hello 42']


def test_routing_through_a_dispatcher():
    seen = []
    router = Router()

    @router.message(F.text == '/probe')
    async def probe(message):
        seen.append(message.text)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    asyncio.run(dispatcher.feed_update(Bot(token='42:x'), types.Update(update_id=1, message=a_message('/probe'))))

    assert seen == ['/probe']


def test_a_catch_all_registered_earlier_swallows_the_update():
    """The ordering caveat on the page — tests/fake_app holds a catch-all."""
    seen = []
    before = list(bot.router.observers['message'].handlers)

    @bot.message(F.text == '/late')
    async def late(message):  # pragma: no cover - the point is that it is not called
        seen.append(message.text)

    dispatcher = Dispatcher()
    dispatcher.include_router(bot.router)

    observers = bot.router.observers['message'].handlers
    try:
        asyncio.run(dispatcher.feed_update(Bot(token='42:x'), types.Update(update_id=2, message=a_message('/late'))))

        assert seen == [], 'a later handler received an update the catch-all should have taken'
    finally:
        # bot.router is the shared singleton. A handler left registered would
        # answer updates in every test after this one, and a router left
        # attached makes the next include_router() raise
        observers[:] = [handler for handler in observers if handler.callback is not late]
        bot.router._parent_router = None  # the public setter refuses None

    assert observers == before, 'the recipe left the shared router changed'


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop'})
def test_draining_the_queue_without_a_thread(redis_server):
    """Queued by send_redis and read by the consumer, which is the whole path."""
    bot.send_redis(chat_id=42, text='hi')
    assert redis_server.llen(QUEUE) == 1

    handled = []
    BlpopDelivery(handler=lambda function, **payload: handled.append((function, payload))).consume_pending()

    function, payload = handled[0]
    assert function == 'send_message'
    assert payload['chat_id'] == 42
    assert payload['text'] == 'hi'
    assert redis_server.llen(QUEUE) == 0


PAGE = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'wiki' / 'Testing.md'
SNIPPETS = re.findall(r'```python\n(.*?)```', PAGE.read_text(encoding='utf-8'), re.DOTALL)


def imported_from_the_package(tree: ast.Module) -> dict[str, object]:
    """What a snippet bound by importing from this package, resolved for real."""
    bound: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not (node.module or '').startswith('django_redis_aiogram'):
            continue
        module = importlib.import_module(node.module or '')
        for alias in node.names:
            bound[alias.asname or alias.name] = getattr(module, alias.name)
    # `delivery = BlpopDelivery(...)` makes `delivery` that class too
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in bound
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = bound[node.value.func.id]
    return bound


def test_the_page_carries_the_recipes():
    assert len(SNIPPETS) >= 6, f'only {len(SNIPPETS)} python snippets on the page'


@pytest.mark.parametrize('snippet', SNIPPETS, ids=range(len(SNIPPETS)))
def test_every_snippet_on_the_page_is_valid_python(snippet):
    """A reader copies these; a syntax error in one wastes their afternoon."""
    ast.parse(snippet)


@pytest.mark.parametrize('snippet', SNIPPETS, ids=range(len(SNIPPETS)))
def test_every_package_attribute_a_snippet_uses_exists(snippet):
    """Renaming something in the library must not leave the page pointing at it."""
    tree = ast.parse(snippet)
    bound = imported_from_the_package(tree)

    missing = [
        f'{node.value.id}.{node.attr}'
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in bound
        and not hasattr(bound[node.value.id], node.attr)
    ]
    assert not missing, f'the page uses what no longer exists: {missing}'


def test_the_page_documents_every_recipe_here():
    """A recipe that leaves the page should leave this file with it."""
    text = PAGE.read_text(encoding='utf-8')
    for needle in (
        'fakeredis',
        'django_redis_aiogram.client.get_redis',
        "monkeypatch.setattr(bot, 'send'",
        'object.__setattr__',
        'feed_update',
        'consume_pending',
    ):
        assert needle in text, f'{needle!r} is no longer on the Testing page'


@pytest.mark.parametrize('setting', ['FSM_STORAGE', 'ENABLED'])
def test_the_page_explains_the_test_settings(setting):
    """Both decisions a reader has to make before writing a test."""
    assert setting in PAGE.read_text(encoding='utf-8')


class RecordingSession(BaseSession):
    """Records the API calls a handler makes instead of performing them."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        """Nothing to close: this session never opened anything."""

    async def make_request(self, bot, method, timeout=None):
        """Record the call and answer nothing, as a stub should."""
        self.calls.append(method)

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - never used here
        """Satisfy the interface; no test downloads a file."""
        yield b''


def test_stubbing_the_session_is_what_catches_a_reply():
    """feed_update hands the handler a copy bound to the bot, so patching
    `answer` on the constructed message does nothing — the session is the seam.
    """
    router = Router()

    @router.message(F.text == '/orders')
    async def orders(message):
        await message.answer('you have 3 open orders')

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    session = RecordingSession()
    fake = Bot(token='42:x', session=session)

    asyncio.run(dispatcher.feed_update(fake, types.Update(update_id=1, message=a_message('/orders'))))

    assert [type(call).__name__ for call in session.calls] == ['SendMessage']
    assert session.calls[0].text == 'you have 3 open orders'


def test_the_message_the_handler_receives_is_not_the_one_constructed():
    """The reason the recipe above exists, stated as a test."""
    received = []
    router = Router()

    @router.message()
    async def record(message):
        received.append(message)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    original = a_message('/probe')

    asyncio.run(dispatcher.feed_update(Bot(token='42:x'), types.Update(update_id=1, message=original)))

    assert received, 'the handler never ran'
    assert received[0] is not original, 'patching the original would have worked'
