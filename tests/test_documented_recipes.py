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
from django.test import override_settings

from django_redis_aiogram import bot
from django_redis_aiogram.delivery import BlpopDelivery
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

    queued = [loads(raw) for raw in server.lrange(QUEUE, 0, -1)]
    assert queued == [{'function': 'send_message', 'chat_id': 42, 'text': 'Order approved'}]


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

    @bot.message(F.text == '/late')
    async def late(message):  # pragma: no cover - the point is that it is not called
        seen.append(message.text)

    dispatcher = Dispatcher()
    dispatcher.include_router(bot.router)

    asyncio.run(dispatcher.feed_update(Bot(token='42:x'), types.Update(update_id=2, message=a_message('/late'))))

    assert seen == [], 'a later handler received an update the catch-all should have taken'


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop'})
def test_draining_the_queue_without_a_thread(redis_server):
    """Queued by send_redis and read by the consumer, which is the whole path."""
    bot.send_redis(chat_id=42, text='hi')
    assert redis_server.llen(QUEUE) == 1

    handled = []
    BlpopDelivery(handler=lambda **payload: handled.append(payload)).consume_pending()

    assert handled == [{'function': 'send_message', 'chat_id': 42, 'text': 'hi'}]
    assert redis_server.llen(QUEUE) == 0


PAGE = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'wiki' / 'Testing.md'
SNIPPETS = re.findall(r'```python\n(.*?)```', PAGE.read_text(), re.DOTALL)


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
    text = PAGE.read_text()
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
    assert setting in PAGE.read_text()
