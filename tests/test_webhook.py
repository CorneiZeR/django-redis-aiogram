"""Updates arriving over HTTP instead of being polled for.

The view is the one place in this package that a stranger can reach, so most of
what is checked here is what it refuses.
"""

import json
import threading
import time
from datetime import datetime, timezone
from io import StringIO

import pytest
from aiogram import Dispatcher, F, types
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import RequestFactory, override_settings

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.checks import check_settings
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
