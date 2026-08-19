"""Updates arriving over HTTP instead of being polled for.

The view is the one place in this package that a stranger can reach, so most of
what is checked here is what it refuses.
"""

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from io import StringIO

import pytest
from aiogram import Dispatcher, F, types
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import RequestFactory, override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.checks import check_settings
from django_redis_aiogram.client import Outbound, loop_lock
from django_redis_aiogram.exceptions import LoopThreadNotStartedError, ShuttingDownError
from django_redis_aiogram.webhook import (
    SECRET_HEADER,
    current_mode,
    telegram_webhook,
    webhook_settings,
)

SECRET = 'a-long-random-string'
#: what the deliberately failing handler below raises with
BOOM = 'boom'
SETTINGS = {
    'TOKEN': '42:x',
    'FSM_STORAGE': 'memory',
    'MODE': 'webhook',
    'WEBHOOK_URL': 'https://example.test/tg/hook/',
    'WEBHOOK_SECRET': SECRET,
    'RATE_LIMIT': None,
}


def an_update(text='/start', update_id=1):
    return {
        'update_id': update_id,
        'message': {
            'message_id': 1,
            'date': int(datetime.now(timezone.utc).timestamp()),
            'chat': {'id': 42, 'type': 'private'},
            'text': text,
        },
    }


def post(payload, secret=SECRET, path='/tg/hook/'):
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    request = RequestFactory().post(path, data=json.dumps(payload), content_type='application/json', **headers)
    return telegram_webhook(request)


@pytest.fixture
def handled(monkeypatch):
    """A bot whose handlers only record, so nothing reaches Telegram."""
    seen = []
    instance = TelegramBot()

    @instance.message(F.text)
    async def record(message: types.Message) -> None:
        seen.append(message.text)

    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)
    try:
        yield seen, instance
    finally:
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_an_update_reaches_the_handler(handled):
    seen, _ = handled

    response = post(an_update('/start'))

    assert response.status_code == 200
    assert seen == ['/start']


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_wrong_secret_is_refused(handled):
    seen, _ = handled

    response = post(an_update(), secret='not-the-secret')

    assert response.status_code == 403
    assert seen == [], 'an update with a wrong secret reached a handler'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_missing_secret_is_refused(handled):
    seen, _ = handled

    response = post(an_update(), secret=None)

    assert response.status_code == 403
    assert seen == []


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_secret_that_is_a_prefix_is_refused(handled):
    """compare_digest, not startswith."""
    response = post(an_update(), secret=SECRET[:-1])

    assert response.status_code == 403


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_SECRET': ''})
def test_serving_without_a_secret_refuses_to_run(handled):
    with pytest.raises(ImproperlyConfigured, match='WEBHOOK_SECRET'):
        post(an_update())


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_get_is_not_allowed():
    request = RequestFactory().get('/tg/hook/')

    response = telegram_webhook(request)

    assert response.status_code == 405


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_body_that_is_not_an_update_is_rejected(handled, caplog):
    seen, _ = handled

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        response = post({'not': 'an update'})

    assert response.status_code == 400
    assert seen == []
    assert 'could not read an update' in caplog.text
    # the type as a field, and no traceback: the body is whoever posted it
    record = next(item for item in caplog.records if 'could not read' in item.message)
    assert record.tg_error == 'ValidationError'
    assert record.exc_info is None, 'the traceback would carry unvalidated input'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_body_that_is_not_json_is_rejected(handled):
    request = RequestFactory().post(
        '/tg/hook/', data=b'{oops', content_type='application/json', **{SECRET_HEADER: SECRET}
    )

    assert telegram_webhook(request).status_code == 400


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_failing_handler_still_answers_200(monkeypatch, caplog):
    """A non-2xx makes Telegram redeliver, and a handler that failed once will
    fail again — that is a loop, not a retry."""
    instance = TelegramBot()

    @instance.message()
    async def explode(message: types.Message) -> None:
        raise RuntimeError(BOOM)

    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)
    try:
        with caplog.at_level('ERROR', logger='django_redis_aiogram'):
            response = post(an_update())
    finally:
        instance.close()

    assert response.status_code == 200
    assert 'webhook handler failed' in caplog.text


@override_settings(TELEGRAM_BOT={**SETTINGS, 'ENABLED': False})
def test_a_disabled_process_does_not_serve(handled):
    seen, _ = handled

    response = post(an_update())

    assert response.status_code == 503
    assert seen == []


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_two_updates_in_a_row_are_both_handled(handled):
    """The router is attached once; attaching it twice is an aiogram error."""
    seen, _ = handled

    first = post(an_update('/one', update_id=1))
    second = post(an_update('/two', update_id=2))

    assert (first.status_code, second.status_code) == (200, 200)
    assert seen == ['/one', '/two']


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_what_set_webhook_is_given():
    arguments = webhook_settings()

    assert arguments['url'] == 'https://example.test/tg/hook/'
    assert arguments['secret_token'] == SECRET
    assert arguments['allowed_updates'] is None
    assert arguments['drop_pending_updates'] is False


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_ALLOWED_UPDATES': ('message',)})
def test_allowed_updates_are_passed_through():
    assert webhook_settings()['allowed_updates'] == ['message']


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_URL': ''})
def test_registering_without_a_url_is_refused():
    with pytest.raises(ImproperlyConfigured, match='WEBHOOK_URL'):
        webhook_settings()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_SECRET': '', 'TOKEN': '42:x'})
def test_a_url_without_a_secret_is_a_check_error():
    assert 'django_redis_aiogram.E027' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_URL': 'http://example.test/tg/'})
def test_a_url_that_is_not_https_is_a_check_error():
    assert 'django_redis_aiogram.E027' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://x'})
def test_no_webhook_configured_is_not_an_error():
    """Polling is still the default; the checks must not nag about it."""
    assert 'django_redis_aiogram.E027' not in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_bot_is_not_a_worker_in_the_web_process(handled):
    """Sends from a handler still queue: the consumer runs elsewhere."""
    _, instance = handled

    assert instance.is_worker is False


class FakeBotApi:
    """Records what the command asked Telegram to do."""

    def __init__(self):
        self.calls = []

    async def set_webhook(self, **kwargs):
        self.calls.append(('set_webhook', kwargs))

    async def delete_webhook(self, **kwargs):
        self.calls.append(('delete_webhook', kwargs))

    async def get_webhook_info(self):
        self.calls.append(('get_webhook_info', {}))
        return types.WebhookInfo(
            url='https://example.test/tg/hook/',
            has_custom_certificate=False,
            pending_update_count=3,
            last_error_message='wrong response from the webhook',
        )

    class session:
        @staticmethod
        async def close():
            pass


@pytest.fixture
def telegram(monkeypatch):
    """A bot whose API calls are recorded instead of sent."""
    instance = TelegramBot()
    api = FakeBotApi()
    instance._bot = api
    monkeypatch.setattr('django_redis_aiogram.management.commands.tgbot_webhook.bot', instance)
    return api


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_command_registers_the_webhook(telegram):
    out = StringIO()
    call_command('tgbot_webhook', 'set', stdout=out)

    name, kwargs = telegram.calls[0]
    assert name == 'set_webhook'
    assert kwargs['url'] == 'https://example.test/tg/hook/'
    assert kwargs['secret_token'] == SECRET
    assert kwargs['drop_pending_updates'] is False
    assert 'webhook set' in out.getvalue()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_pending_updates_can_be_dropped(telegram):
    call_command('tgbot_webhook', 'set', '--drop-pending', stdout=StringIO())

    assert telegram.calls[0][1]['drop_pending_updates'] is True


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_command_deletes_the_webhook(telegram):
    out = StringIO()
    call_command('tgbot_webhook', 'delete', stdout=out)

    assert telegram.calls == [('delete_webhook', {'drop_pending_updates': False})]
    assert 'polling can start again' in out.getvalue()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_info_reports_what_telegram_knows(telegram):
    out = StringIO()
    call_command('tgbot_webhook', 'info', stdout=out)

    printed = out.getvalue()
    assert 'https://example.test/tg/hook/' in printed
    assert 'pending updates: 3' in printed
    assert 'wrong response from the webhook' in printed


@override_settings(TELEGRAM_BOT={**SETTINGS, 'ENABLED': False})
def test_the_command_refuses_when_disabled(telegram):
    with pytest.raises(CommandError, match='disabled'):
        call_command('tgbot_webhook', 'set', stdout=StringIO())

    assert telegram.calls == [], 'it talked to Telegram from a disabled process'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'MODE': 'polling'})
def test_the_view_refuses_while_the_deployment_polls(handled):
    """Two sources of updates and no way to tell which handled what."""
    seen, _ = handled

    response = post(an_update())

    assert response.status_code == 503
    assert seen == []


@override_settings(TELEGRAM_BOT={**SETTINGS, 'MODE': 'nonsense'})
def test_an_unknown_mode_is_refused(handled):
    with pytest.raises(ImproperlyConfigured, match="\\['MODE'\\]"):
        post(an_update())


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://x'})
def test_polling_is_the_default_mode():
    assert current_mode() == 'polling'


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://x'})
def test_the_mode_can_come_from_the_environment(monkeypatch):
    """Choosing at startup must not need a code change."""
    monkeypatch.setenv('DJANGO_REDIS_AIOGRAM_MODE', 'webhook')

    assert current_mode() == 'webhook'


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://x', 'MODE': 'sideways'})
def test_an_unknown_mode_is_a_check_error():
    assert 'django_redis_aiogram.E028' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'REDIS_URL': 'redis://x', 'MODE': 'webhook'})
def test_webhook_mode_without_a_url_is_a_check_error():
    """Half-configured webhook mode receives nothing, silently."""
    assert 'django_redis_aiogram.E027' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_registering_a_webhook_while_polling_warns(telegram):
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'MODE': 'polling'}):
        out = StringIO()
        call_command('tgbot_webhook', 'set', stdout=out)

    assert 'stops getUpdates from working' in out.getvalue()
    assert telegram.calls[0][0] == 'set_webhook', 'it refused instead of warning'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_concurrent_first_requests_share_one_dispatcher(monkeypatch):
    """Two first requests would each build one, and the router would attach to
    whichever was discarded — so half the updates would reach no handler."""
    instance = TelegramBot()
    seen = []

    @instance.message(F.text)
    async def record(message: types.Message) -> None:
        seen.append(message.text)

    built = []
    real_dispatcher = Dispatcher

    def slow_dispatcher(*args, **kwargs):
        time.sleep(0.05)  # widen the window both threads race through
        made = real_dispatcher(*args, **kwargs)
        built.append(made)
        return made

    monkeypatch.setattr('django_redis_aiogram.client.Dispatcher', slow_dispatcher)
    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)

    ready = threading.Barrier(4, timeout=10)
    errors = []

    def deliver(index):
        try:
            ready.wait()
            assert post(an_update(f'/probe{index}', update_id=index)).status_code == 200
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=deliver, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 15
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))

    try:
        # a request thread still blocked is the failure this test exists to catch
        assert not [thread for thread in threads if thread.is_alive()], 'a request never returned'
        assert errors == [], errors
        assert len(built) == 1, f'{len(built)} dispatchers were built'
        assert sorted(seen) == [f'/probe{index}' for index in range(4)], seen
    finally:
        instance.close()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'TOKEN': ''})
def test_a_missing_token_is_not_reported_as_a_bad_request(handled, caplog):
    """503 is ours to fix; 400 would blame Telegram for our configuration."""
    with caplog.at_level('ERROR', logger='django_redis_aiogram'):
        response = post(an_update())

    assert response.status_code == 503
    assert 'cannot build the bot' in caplog.text


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_handler_sending_from_the_web_process_queues(monkeypatch):
    """The consumer runs elsewhere, so a send from a handler must go to Redis."""
    instance = TelegramBot()
    queued, direct = [], []

    @instance.message(F.text)
    async def answer(message: types.Message) -> None:
        instance.send(chat_id=message.chat.id, text='queued from a handler')

    monkeypatch.setattr(instance, 'send_redis', lambda *args, correlation_id=None, **kwargs: queued.append(kwargs))
    monkeypatch.setattr(instance, 'send_raw', lambda *args, correlation_id=None, **kwargs: direct.append(kwargs))
    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)

    try:
        assert post(an_update()).status_code == 200
    finally:
        instance.close()

    assert queued == [{'chat_id': 42, 'text': 'queued from a handler'}]
    assert direct == [], 'it talked to Telegram from the web process'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_ALLOWED_UPDATES': 'message'})
def test_a_string_of_allowed_updates_is_a_check_error():
    """list('message') is nine update types Telegram has never heard of."""
    assert 'django_redis_aiogram.E029' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_ALLOWED_UPDATES': ('message', 'messages')})
def test_an_unknown_update_type_is_a_check_error():
    assert 'django_redis_aiogram.E029' in {message.id for message in check_settings()}


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_ALLOWED_UPDATES': ('message', 'poll_answer')})
def test_real_update_types_are_accepted():
    assert 'django_redis_aiogram.E029' not in {message.id for message in check_settings()}
    assert webhook_settings()['allowed_updates'] == ['message', 'poll_answer']


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_ALLOWED_UPDATES': (['message'], {'poll': 1}, 7)})
def test_members_that_are_not_strings_are_reported_not_raised():
    """A list member is unhashable, so the membership test used to raise out of
    manage.py check instead of reporting anything."""
    reported = {message.id for message in check_settings()}

    assert 'django_redis_aiogram.E029' in reported


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_updates_in_one_process_are_handled_concurrently(monkeypatch):
    """A web process drives nothing, so every update took `run_until_complete`
    **under `loop_lock`** and they handled strictly one at a time.

    The rendezvous is what makes this a test rather than a stopwatch: four
    handlers must all be inside the dispatcher at once for the barrier to
    release. Serialized, the first one waits there for ever and the others never
    arrive — which is exactly what happened before the loop had a thread.
    """
    instance = TelegramBot()
    together = threading.Barrier(4, timeout=5)
    arrived = []
    broken = []

    @instance.message(F.text)
    async def rendezvous(message: types.Message) -> None:
        arrived.append(message.text)
        # a thread, because the barrier is a blocking primitive and this runs on
        # the loop: four handlers have to be in flight for it to release
        try:
            await asyncio.get_running_loop().run_in_executor(None, together.wait)
        except threading.BrokenBarrierError:
            # serialized, each handler waits here alone and times out. Recorded
            # rather than raised: the view answers 200 to a handler that raised,
            # so letting it propagate would leave the test green
            broken.append(message.text)

    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)

    errors = []

    def deliver(index):
        try:
            assert post(an_update(f'/together{index}', update_id=index)).status_code == 200
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=deliver, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 20
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))

    try:
        assert not [thread for thread in threads if thread.is_alive()], 'a request never returned'
        assert errors == [], errors
        assert sorted(arrived) == [f'/together{index}' for index in range(4)], arrived
        assert broken == [], 'the handlers never overlapped, so the barrier timed out'
    finally:
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_second_request_waits_for_the_loop_to_be_running(monkeypatch):
    """Returning as soon as the *thread exists* is not the same as running.

    A second request that returned there would find `is_running()` still false
    and drive its update with `run_until_complete`, while the thread it saw
    called `run_forever` on the same loop — which kills that thread, leaves the
    bot pointing at a dead one, and quietly returns the process to handling
    updates one at a time. Silently: the requests still answer 200.

    The thread here is alive throughout and merely slow to arrive. A harness that
    left it *unstarted* would look dead to the replacement logic and get a second
    thread on the same loop — which is what this test is about, so it must not be
    how the test produces its window.
    """
    instance = TelegramBot()
    arrive = threading.Event()
    real_set_event_loop = asyncio.set_event_loop

    def wait_then_set(loop):
        arrive.wait(10)
        return real_set_event_loop(loop)

    monkeypatch.setattr(asyncio, 'set_event_loop', wait_then_set)
    first = threading.Thread(target=instance._ensure_loop_runs, daemon=True)
    first.start()
    for _ in range(500):
        if instance._runner is not None and instance._runner.is_alive():
            break
        time.sleep(0.01)
    assert instance._runner is not None, 'the runner was never registered'
    assert instance._runner.is_alive(), 'the runner was not started'
    running_at_the_time = instance.loop.is_running()

    seen = []

    def second():
        instance._ensure_loop_runs()
        seen.append(instance.loop.is_running())

    later = threading.Thread(target=second, daemon=True)
    later.start()
    time.sleep(0.05)  # let the second caller reach the point it used to return at
    arrive.set()
    later.join(timeout=10)
    first.join(timeout=10)

    try:
        assert running_at_the_time is False, 'the window this test needs did not exist'
        assert seen == [True], 'a caller was let past before the loop was running'
        assert instance._runner.is_alive(), 'the loop thread died'
    finally:
        monkeypatch.setattr(asyncio, 'set_event_loop', real_set_event_loop)
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_handlers_send_does_not_warn_on_the_normal_path(monkeypatch, caplog):
    """Webhook mode runs handlers on a loop this process drives, so a send from
    one is ordinary. Warning about it would put a WARNING in the log for every
    message a bot sends, which is how people learn to stop reading them."""
    instance = TelegramBot()
    sent = []

    @instance.message(F.text)
    async def reply(message: types.Message) -> None:
        instance._schedule(asyncio.sleep(0), Outbound(uuid.uuid4(), 'send_message', {}))
        sent.append(message.text)

    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)

    try:
        with caplog.at_level('WARNING', logger='django_redis_aiogram'):
            assert post(an_update('/quiet')).status_code == 200
        assert sent == ['/quiet'], sent
        assert 'nothing in this process runs' not in caplog.text, caplog.text
    finally:
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_send_raw_stops_waiting_once_the_loop_has_a_thread():
    """The documented cost of giving the loop a thread, pinned.

    `send_raw` hands work to a running loop rather than driving one, so in a web
    process that serves the webhook it schedules and returns from the first
    update onwards, where before it blocked until Telegram answered. That is a
    real change for a caller relying on `RAISE_EXCEPTION` to reach their view,
    so it is written down on **Sending-messages** — and written down is worth
    nothing without something that fails when it stops being true.
    """

    class Slow:
        def __init__(self, called):
            self.called = called

        async def send_message(self, **kwargs):
            await asyncio.sleep(0.3)
            self.called.append(True)

        class session:
            @staticmethod
            async def close():
                pass

    def measure(*, with_runner):
        instance = TelegramBot()
        called = []
        instance._bot = Slow(called)
        try:
            if with_runner:
                instance._ensure_loop_runs()
            began = time.monotonic()
            instance.send_raw('send_message', chat_id=1, text='x')
            return time.monotonic() - began, bool(called)
        finally:
            # closed with the stub still installed: the handed-off send has only
            # been scheduled, and clearing it here would let the drain resolve
            # `self.bot` and build a real one — a unit test reaching the network
            instance.close()

    drove, drove_called = measure(with_runner=False)
    handed, handed_called = measure(with_runner=True)

    assert drove_called is True, 'without a thread it must drive the send to completion'
    assert drove >= 0.25, f'it returned in {drove:.2f}s, so it did not wait'
    assert handed_called is False, 'with a thread it must hand off, not wait'
    assert handed < 0.1, f'it took {handed:.2f}s, so it waited after all'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_close_does_not_strand_a_request_waiting_on_its_update(monkeypatch, caplog):
    """`close()` stops the loop thread before its teardown, and a request thread
    is blocked on `future.result()` with no deadline.

    Stopping the loop under one leaves that thread waiting on a future nothing
    will ever finish — a web worker held for the life of the process, while the
    teardown carries on to `loop.close()`. Waiting for updates in flight, and
    cancelling what outlasts the drain, is what turns that into an exception the
    request can answer with.
    """
    instance = TelegramBot()
    inside = threading.Event()
    answered = []

    @instance.message(F.text)
    async def slow(message: types.Message) -> None:
        inside.set()
        await asyncio.sleep(30)  # far longer than the drain: it must be cancelled

    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)

    def deliver():
        try:
            answered.append(post(an_update('/slow')).status_code)
        except BaseException as error:
            answered.append(type(error).__name__)

    request = threading.Thread(target=deliver, daemon=True)
    request.start()
    assert inside.wait(10), 'the handler never ran'

    with caplog.at_level('WARNING', logger='django_redis_aiogram'):
        instance.close(drain_timeout=0.2)
    request.join(timeout=10)

    assert not request.is_alive(), 'the request thread is still waiting on a stopped loop'
    # and the cancellation is answered as the refusal it is. Left as a
    # cancellation it reads as a handler that failed, which answers 200 — telling
    # Telegram to forget an update nothing handled, on the one path where losing
    # it is guaranteed rather than possible
    assert answered == [503], answered
    assert 'webhook refused an update' in caplog.text
    assert 'webhook handler failed' not in caplog.text


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_slow_loop_thread_is_not_driven_by_the_request(monkeypatch):
    """Slow to start is not the same as absent.

    Waiting `RUNNER_TIMEOUT` and carrying on left `feed_update` free to drive the
    update itself, while the thread it had already started called `run_forever`
    on that same loop — two threads on one loop, the thread dies, and `_runner`
    points at a corpse. It refuses now: the request fails rather than corrupting
    the loop every later update depends on.
    """
    instance = TelegramBot()
    monkeypatch.setattr('django_redis_aiogram.client.RUNNER_TIMEOUT', 0.05)
    real_set_event_loop = asyncio.set_event_loop

    def slow_set_event_loop(loop):
        # the runner's first statement: the thread is started and alive, and has
        # not reached `run_forever`, which is the window the wait used to give up
        time.sleep(0.5)
        return real_set_event_loop(loop)

    monkeypatch.setattr(asyncio, 'set_event_loop', slow_set_event_loop)
    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)

    try:
        with pytest.raises(LoopThreadNotStartedError):
            instance.feed_update(types.Update.model_validate(an_update('/slow-start')))
    finally:
        # let the late thread finish arriving before tearing down, or `close()`
        # races the very loop this test delayed
        monkeypatch.setattr(asyncio, 'set_event_loop', real_set_event_loop)
        monkeypatch.setattr('django_redis_aiogram.client.RUNNER_TIMEOUT', 5.0)
        instance._runner_ready.wait(5)
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_an_update_is_refused_once_the_shutdown_has_started(monkeypatch):
    """A request that arrives mid-shutdown must be turned away, not queued.

    `close()` snapshots the updates in flight and then stops the loop. One
    submitted after that snapshot would be neither waited for nor cancelled, and
    its request would wait for ever on a stopped loop — the stranded worker
    again, through a narrower door. The refusal is decided under the same
    `loop_lock` the snapshot is taken under, which is what leaves no window
    between them; this pins the refusal itself.
    """
    instance = TelegramBot()
    instance._ensure_loop_runs()
    try:
        # what a request sees while close() is between its snapshot and the stop.
        # close() clears this again at the end, so it cannot be observed after
        instance._closing = True

        with pytest.raises(ShuttingDownError):
            instance.feed_update(types.Update.model_validate(an_update('/too-late')))
        assert instance._updates == set(), 'the update was submitted anyway'
    finally:
        instance._closing = False
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_view_asks_telegram_to_redeliver_a_refused_update(monkeypatch):
    """A refusal and a handler failure must not answer the same way.

    The view answers 200 when a handler raised, because retrying a handler that
    fails once is a loop rather than a retry. But nothing ran here — the process
    is shutting down — so the update is still Telegram's, and a 2xx would tell it
    to forget one nobody handled. During a rolling restart that is the difference
    between an update moving to the next instance and disappearing.
    """
    instance = TelegramBot()
    instance._ensure_loop_runs()
    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)
    try:
        instance._closing = True
        response = post(an_update('/mid-restart'))
    finally:
        instance._closing = False
        instance.close()

    assert response.status_code == 503, response.status_code


@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_loop_thread_that_dies_is_replaced(monkeypatch):
    """A dead runner must not become a permanent 503.

    `_runner` was set once and cleared only by `close()`, so a thread that ended
    before it ran the loop was kept for the life of the process: every later
    update waited out `RUNNER_TIMEOUT`, logged the startup warning and was
    refused. Redelivery cannot recover a condition that never clears — the next
    attempt hits the same dead thread — so the process serves 503 until someone
    restarts it, five seconds at a time.
    """
    instance = TelegramBot()
    monkeypatch.setattr('django_redis_aiogram.client.RUNNER_TIMEOUT', 0.05)
    real_set_event_loop = asyncio.set_event_loop
    attempts = []

    def fail_the_first(loop):
        attempts.append(loop)
        if len(attempts) == 1:
            msg = 'the thread ends here, before it can signal readiness'
            raise RuntimeError(msg)
        return real_set_event_loop(loop)

    monkeypatch.setattr(asyncio, 'set_event_loop', fail_the_first)
    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)
    handled = []

    @instance.message(F.text)
    async def note(message: types.Message) -> None:
        handled.append(message.text)

    try:
        # the first update loses its thread and is refused, which is correct
        assert post(an_update('/first')).status_code == 503
        # observably dead before the second update, or it would be waiting on a
        # thread that is merely slow — a different path from the one under test
        for _ in range(500):
            if instance._runner is not None and not instance._runner.is_alive():
                break
            time.sleep(0.01)
        corpse = instance._runner
        assert corpse is not None, 'the runner was never registered'
        assert not corpse.is_alive(), 'the thread did not die'
        # and the replacement gets the real deadline: 50 ms was only needed to
        # make the first request give up quickly, and a loaded machine can take
        # longer than that to start a thread and reach run_forever
        monkeypatch.setattr('django_redis_aiogram.client.RUNNER_TIMEOUT', 5.0)

        # the second must not inherit that corpse
        assert post(an_update('/second', update_id=2)).status_code == 200
        assert handled == ['/second'], handled
        assert len(attempts) == 2, 'no replacement thread was started'
    finally:
        monkeypatch.setattr(asyncio, 'set_event_loop', real_set_event_loop)
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_forgetting_an_update_waits_for_the_shutdown_snapshot():
    """The set is added to and read under `loop_lock`; removal has to match.

    `_stop_runner` takes `list()` over this set while holding that lock. A
    `discard` from a request thread mid-iteration raises `RuntimeError: Set
    changed size during iteration` **inside `close()`**, which aborts the
    shutdown before anything is torn down — the loop, the session and the storage
    all left open, from a request that merely finished at the wrong moment.
    """
    instance = TelegramBot()
    instance._ensure_loop_runs()
    loop = instance.loop
    finished = object()
    instance._updates.add(finished)  # type: ignore[arg-type] - a stand-in for a future
    done = threading.Event()

    try:
        with loop_lock(loop):
            threading.Thread(
                target=lambda: (instance._forget_update(finished), done.set()),  # type: ignore[arg-type,func-returns-value]
                daemon=True,
            ).start()
            # it must not get in while the snapshot could be running
            held_off = not done.wait(0.3)

        assert done.wait(5), 'the removal never completed once the lock was free'
        assert held_off, 'the removal did not take the lock'
        assert instance._updates == set()
    finally:
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_runner_registered_during_shutdown_is_not_missed():
    """`_stop_runner` reads and clears `_runner`; `_ensure_loop_runs` writes it.

    Both must hold `_build_guard`. Otherwise a request already inside that
    critical section registers its thread *after* the snapshot read `None`, and
    that thread calls `run_forever` on the loop the teardown is about to close —
    ending in `RuntimeError: This event loop is already running` part-way through
    the teardown, with the storage already closed, or `loop.close()` under a live
    thread.

    The guard is held by *another* thread here. Held by this one it would be
    re-entered rather than waited on, and the test would pass either way.
    """
    instance = TelegramBot()
    inside = threading.Event()
    late = threading.Thread(target=lambda: None, name='late-runner', daemon=True)

    def registering() -> None:
        # stands in for a caller between the `_closing` check and the assignment
        with instance._build_guard:
            inside.set()
            time.sleep(0.2)
            instance._runner = late
            late.start()

    racer = threading.Thread(target=registering, daemon=True)
    racer.start()
    assert inside.wait(5), 'the racing caller never took the guard'

    try:
        instance._closing = True
        instance._stop_runner(0.1)
        # joined *after*, so the assertion cannot pass merely because the snapshot
        # ran before the assignment: without the guard it does exactly that
        racer.join(timeout=5)

        # it waited for the guard, so it saw the thread registered under it
        assert instance._runner is None, 'a runner was left behind by the snapshot'
    finally:
        instance._closing = False
        instance.close()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_non_ascii_secret_is_refused_rather_than_raised():
    """The 403 branch used to answer 500 to anyone who sent one non-ASCII byte.

    `hmac.compare_digest` refuses `str` arguments outside ASCII, so the comparison
    itself raised `TypeError` — an unauthenticated traceback in the log and a 500 from
    the one branch whose whole job is to say no. Compared as bytes now.
    """
    response = post(an_update(), secret='пароль')

    assert response.status_code == 403


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WEBHOOK_SECRET': 'пароль'})
def test_a_matching_non_ascii_secret_passes():
    """Comparing bytes means comparing, not refusing.

    The other half of that fix, and the half the documentation first got wrong: a secret
    outside ASCII is not rejected for being outside ASCII — it is compared like any other,
    so one that matches is accepted and only a mismatch is a 403.
    """
    assert post(an_update(), secret='пароль').status_code == 200
    assert post(an_update(update_id=2), secret='другой').status_code == 403


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_an_update_reaching_a_closed_loop_is_refused_not_swallowed(monkeypatch):
    """A 200 here tells Telegram to stop redelivering an update nothing handled.

    `close()` puts `_closing` back to False in its `finally`, so a request that captured
    the loop before the teardown and reached the lock after it found a loop that was
    neither closing nor running — and drove `run_until_complete` on a closed one. The
    `RuntimeError` landed in the view's `except Exception`, which answers 200.

    Asserted on the status code, because that is what Telegram acts on.
    """
    instance = TelegramBot()
    instance.close()
    assert instance._loop is None or instance._loop.is_closed(), 'the loop was not closed'
    closed = asyncio.new_event_loop()
    closed.close()
    monkeypatch.setattr(type(instance), 'loop', property(lambda self: closed))
    monkeypatch.setattr('django_redis_aiogram.webhook.bot', instance)

    with pytest.raises(ShuttingDownError):
        instance.feed_update(an_update())

    assert post(an_update()).status_code == 503


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_loop_thread_that_outlives_the_join_is_kept_so_close_can_retry(monkeypatch):
    """`close()` returns without closing anything when it cannot stop the loop thread.

    It said so itself — "leaving everything in place keeps `close()` retryable" — while
    `_stop_runner` had already set `_runner` to None. Every later `close()` then returned
    in no time without asking the orphan to stop again, and the loop, the aiogram session
    and the FSM client stayed open for the life of the process.
    """
    monkeypatch.setattr('django_redis_aiogram.client.RUNNER_TIMEOUT', 0.1)
    instance = TelegramBot()
    blocked = threading.Event()
    released = threading.Event()

    async def hold():
        """Block the loop thread itself, so `loop.stop()` cannot be processed."""
        blocked.set()
        released.wait(5)

    # the try opens before the first assertion after the runner exists: a failure between
    # here and the finally would otherwise leave a blocked thread turning a loop, and the
    # report would show the next test's timeout rather than the assertion that broke
    try:
        assert instance._ensure_loop_runs(), 'the runner never started'
        asyncio.run_coroutine_threadsafe(hold(), instance.loop)
        assert blocked.wait(5), 'the loop never reached the blocking coroutine'

        instance.close()

        assert instance._runner is not None, 'the orphan was forgotten, so nothing can stop it'
        assert instance._runner.is_alive()
        assert instance._runner_ready.is_set(), 'a cleared event refuses every update for the timeout'
    finally:
        released.set()
        if instance._runner is not None:
            instance._runner.join(timeout=5)
        # the retry, which is also the assertion below: run in the teardown so that a
        # failure above still closes the loop this test opened
        instance.close()

    assert instance._loop is None or instance._loop.is_closed(), 'the retry closed nothing'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_send_waits_for_our_own_runner_instead_of_driving_the_loop(monkeypatch):
    """`is_running()` is False for the whole window before the runner reaches `run_forever`.

    A send arriving in it drove the loop itself, which killed our thread with "this event
    loop is already running" — and set `_runner_ready` on the way, because the driving
    call ran the `call_soon` the dead thread had queued. `_ensure_loop_runs` then reported
    a runner it owned while nothing ran the loop, and every update answered 503.

    The state is built rather than raced: a live thread registered as the runner, with the
    loop not yet running. Driving completes the send inline; handing off does not.
    """
    instance = TelegramBot()
    sent = []

    class Telegram:
        async def send_message(self, **kwargs):
            """Record the call, so 'was it driven here' is observable."""
            sent.append(kwargs['chat_id'])

        class session:
            @staticmethod
            async def close():
                """aiogram's session, reduced to what `close()` calls."""

    instance._bot = Telegram()
    release = threading.Event()
    instance._runner = threading.Thread(target=lambda: release.wait(5), daemon=True)
    instance._runner.start()

    try:
        instance.send_raw('send_message', chat_id=1, text='x')

        assert sent == [], 'the send drove the loop our own thread was about to run'
    finally:
        release.set()
        instance._runner.join(timeout=5)
        # closing is what steps the handed-off send, and it is also this test's cleanup:
        # in the finally so a failure above does not leave the loop and the thread behind
        instance.close()

    assert sent == [1], 'the handed-off send was never stepped'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_no_loop_thread_is_started_on_a_closed_loop(monkeypatch):
    """Starting one raises inside the thread, where nothing catches it.

    And the caller then waits out `RUNNER_TIMEOUT` for a readiness event that no living
    thread can set — ten seconds of it for two updates, with the real failure reported
    only as an unhandled thread exception. Asserted on the thread not existing rather
    than on how long the call took, which would pass either way.
    """
    instance = TelegramBot()
    closed = asyncio.new_event_loop()
    closed.close()
    monkeypatch.setattr(type(instance), 'loop', property(lambda self: closed))

    assert instance._ensure_loop_runs() is False, 'it claimed to own a loop it cannot run'
    assert instance._runner is None, 'a thread was started on a closed loop'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'DRAIN_TIMEOUT': 0.2})
def test_a_close_that_gave_up_still_cancels_what_arrived_after_it(monkeypatch):
    """The half of keeping the orphan that matters to a request thread.

    A request waits on `future.result()` with no deadline, and `_stop_runner` is the only
    code that cancels `_updates`. With the orphan forgotten it was never reached again, so
    an update submitted after a give-up `close()` held its worker until SIGKILL. Keeping
    the runner is what makes the retry cancel it.
    """
    monkeypatch.setattr('django_redis_aiogram.client.RUNNER_TIMEOUT', 0.1)
    instance = TelegramBot()
    blocked = threading.Event()
    released = threading.Event()

    async def hold():
        """Block the loop thread for longer than both closes take.

        Its first version waited five seconds, which the second close's own drain
        outlasted — so the thread died mid-teardown, the join succeeded, and the test
        failed on a state the code was right to be in.
        """
        blocked.set()
        released.wait(30)

    async def later():
        """Stand in for an update submitted after the give-up."""
        await asyncio.sleep(30)

    try:
        assert instance._ensure_loop_runs()
        asyncio.run_coroutine_threadsafe(hold(), instance.loop)
        assert blocked.wait(5)
        instance.close()
        assert instance._runner is not None, 'the orphan was forgotten'

        arrived = asyncio.run_coroutine_threadsafe(later(), instance.loop)
        instance._updates.add(arrived)
        assert not arrived.done()

        instance.close()

        assert arrived.cancelled(), 'a request submitted after the give-up would wait for ever'
        assert instance._runner is not None, 'the retry forgot the orphan it could still not stop'
    finally:
        # whatever failed above, the thread must not outlive this test: left blocked it
        # keeps turning a loop beside the next one and hides which assertion broke
        released.set()
        if instance._runner is not None:
            instance._runner.join(timeout=5)
        # and the loop itself: the close under test gave up while the thread still held
        # it, so nothing had closed it by the time that thread finally exited
        instance.close()
