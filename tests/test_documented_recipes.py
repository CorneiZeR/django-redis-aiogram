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
import subprocess
import sys

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


def test_the_deployment_healthcheck_recipe_names_a_runnable_module():
    """The page tells readers what to put in `test:`, so it has to be real.

    The old recipe named `manage.py tgbot_healthcheck` with `timeout: 10s`, and that
    combination cannot work in a project of ordinary size — a management command runs
    `django.setup()` first. If the page and the package drift again, the wrong half is
    the one a reader copies into a compose file and only finds out about in production.
    """
    page = (pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'wiki' / 'Deployment.md').read_text(
        encoding='utf-8'
    )

    assert "test: ['CMD', 'python', '-m', 'django_redis_aiogram.healthcheck']" in page
    assert "test: ['CMD', 'python', 'manage.py', 'tgbot_healthcheck']" not in page, (
        'the page still tells readers to put the management command in a healthcheck'
    )

    module = importlib.import_module('django_redis_aiogram.healthcheck')
    assert callable(module.main), 'the module the page names has no main() to run'

    # run it, rather than grep the source for `if __name__`: that string is equally
    # present in a comment or a docstring. `--help` is the invocation that needs neither
    # settings nor a Redis, so it answers "is this runnable with python -m" and nothing
    # else — the probe's real exit codes are pinned in tests/test_lazy_init.py
    helped = subprocess.run(
        [sys.executable, '-m', 'django_redis_aiogram.healthcheck', '--help'],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert helped.returncode == 0, helped.stderr
    assert 'python -m django_redis_aiogram.healthcheck' in helped.stdout, helped.stdout
    for flag in ('--max-queue', '--max-age', '--stranded', '--guarantee'):
        assert flag in helped.stdout, f'{flag} is not on the module the page names'


#: `DJANGO_SETTINGS_MODULE: core.settings` or `DJANGO_SETTINGS_MODULE=core.settings`,
#: with something after the separator that is not the start of a comment
SETTINGS_MODULE_ASSIGNMENT = re.compile(r'DJANGO_SETTINGS_MODULE\s*[:=]\s*[^\s#]')


@pytest.mark.parametrize('page_name', ['Deployment', 'Troubleshooting'])
def test_every_published_healthcheck_carries_the_settings_module(page_name):
    """A recipe that omits `DJANGO_SETTINGS_MODULE` reads unhealthy forever.

    The probe is a separate process and the conventional `manage.py` sets that variable
    with `os.environ.setdefault(...)` inside its own, so a container that runs `manage.py`
    need never export it. Omitting it from a compose snippet costs the reader their whole
    healthcheck, in the one place they have no reason to doubt. Pinned per page, because
    the pages are copied from independently.
    """
    page = (pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'wiki' / f'{page_name}.md').read_text(
        encoding='utf-8'
    )

    blocks = [block for block in page.split('```') if 'django_redis_aiogram.healthcheck' in block]
    assert blocks, f'{page_name} publishes no healthcheck recipe any more'
    for block in blocks:
        # an assignment with a value, not the name anywhere in the block: the prose that
        # explains why the variable is needed mentions it too, and a recipe whose only
        # mention is a comment — or `DJANGO_SETTINGS_MODULE:` with nothing after it — is
        # exactly the one that reads unhealthy for ever
        assigned = [
            line
            for line in block.splitlines()
            if not line.lstrip().startswith('#') and SETTINGS_MODULE_ASSIGNMENT.match(line.lstrip().lstrip('- '))
        ]
        assert assigned, f'a healthcheck recipe on {page_name} does not set DJANGO_SETTINGS_MODULE to anything'


#: fragments of what the probe writes, stable across the interpolated parts. Held here as
#: well as in the source and on the page on purpose: rewording a refusal has to touch all
#: three, which is the only thing that keeps the catalogue on Troubleshooting true
PROBE_REFUSALS = (
    'redis is unreachable',
    'is not a number',
    'cannot read the settings',
    'the consumer has not written one within',
    'is not a timestamp',
    'could not read the heartbeat',
    'could not read the queue length',
    'messages are queued, over the limit of',
    'message(s) are in flight under',
    'disabled in this process; nothing to check',
    'could not scan for stranded in-flight lists',
    'could not establish which delivery guarantee is in force',
)


#: the four reasons the webhook view answers 503, each quoted on Troubleshooting so an
#: operator can grep the line they have. Same bidirectional pin as the probe's refusals
WEBHOOK_REFUSALS = (
    'webhook received an update while the bot is disabled',
    'webhook received an update while this deployment polls',
    'webhook cannot build the bot',
    'webhook refused an update',
)


def webhook_source():
    """The view, as text."""
    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / 'src' / 'django_redis_aiogram' / 'webhook.py').read_text(encoding='utf-8')


def catalogued_refusals():
    """The messages Troubleshooting lists under 503, read out of the page itself."""
    root = pathlib.Path(__file__).resolve().parent.parent
    page = (root / 'docs' / 'wiki' / 'Troubleshooting.md').read_text(encoding='utf-8')
    section = page.split('**503** means')[1].split('**400** means')[0]
    return set(re.findall(r'^- `([^`]+)`', section, flags=re.MULTILINE))


@pytest.mark.parametrize('fragment', WEBHOOK_REFUSALS)
def test_every_reason_the_webhook_refuses_is_catalogued(fragment):
    """A 503 is the one answer that makes Telegram try again, so its causes are read.

    The page listed two of the four, and the audit that found this also found the same
    shape twice elsewhere: prose quoting a message the code no longer emits, or omitting
    one it does. Asserted in both directions, so a reworded line cannot leave the
    catalogue describing something that never happens.
    """
    assert fragment in webhook_source(), 'the view no longer logs this; the page and this list still do'
    assert fragment in catalogued_refusals(), 'Troubleshooting does not name a reason the view answers 503'


#: what each of the four 503 branches is called on Webhook.md, where the causes are prose
#: rather than log lines. Ordered as the view checks them, which is what the page claims
WEBHOOK_CAUSES = ('`ENABLED` is off', '`MODE` is not `webhook`', 'cannot be built', 'nothing ran the update')


def test_the_other_page_names_the_same_four_causes():
    """The helpers above read Troubleshooting, and Webhook.md carries the causes too.

    Dropping one from that page left every assertion here true — the same gap this file was
    just fixed for on the other side. Asserted in the page's own order, because the
    sentence claims to list them in the order the view checks them.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    page = (root / 'docs' / 'wiki' / 'Webhook.md').read_text(encoding='utf-8')
    paragraph = page.split('All four reasons for a 503')[1].split('\n\n')[0]
    positions = [paragraph.find(cause) for cause in WEBHOOK_CAUSES]
    absent = [cause for cause, position in zip(WEBHOOK_CAUSES, positions, strict=True) if position < 0]

    assert not absent, f'Webhook.md no longer names {absent}'
    assert positions == sorted(positions), 'the causes are no longer in the order the view checks them'
    assert len(WEBHOOK_CAUSES) == len(WEBHOOK_REFUSALS), 'the two pages describe a different number of causes'


def test_the_catalogue_and_the_view_agree_on_how_many_refusals_there_are():
    """The list above is written by hand, so on its own it cannot notice a fifth reason.

    A new `status=503` branch, or a bullet added to the page for something the view never
    logs, would both leave every per-fragment assertion true. Counted from the source and
    compared as sets with the page, so either side gaining or losing one fails here.
    """
    assert webhook_source().count('status=503') == len(WEBHOOK_REFUSALS), (
        'the view has a 503 branch this list does not name'
    )
    assert catalogued_refusals() == set(WEBHOOK_REFUSALS), 'the page and this list disagree about the causes'


@pytest.mark.parametrize('fragment', PROBE_REFUSALS)
def test_every_line_the_probe_prints_is_catalogued(fragment):
    """An operator greps the line out of `docker inspect`; the page has to have it.

    Eleven of these were documented nowhere. Asserted in both directions — the fragment
    has to be in the source *and* on the page — so a reworded message cannot leave the
    catalogue quietly describing a line the probe no longer prints.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    source = (root / 'src' / 'django_redis_aiogram' / 'healthcheck.py').read_text(encoding='utf-8')
    page = (root / 'docs' / 'wiki' / 'Troubleshooting.md').read_text(encoding='utf-8')

    assert fragment in source, 'the probe no longer says this; the page and this list still do'
    assert fragment in page, 'Troubleshooting does not catalogue a line the probe prints'
