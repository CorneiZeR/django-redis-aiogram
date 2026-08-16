"""Crash-safe consuming: a worker killed mid-send must not lose the message.

The consumer moves each message to a processing list before sending and
removes it afterwards; a new worker reclaims whatever a crashed one left
behind. On servers without LMOVE it falls back to plain pops.
"""

import threading

import pytest
from aiogram import exceptions
from aiogram.methods import SendMessage
from django.test import override_settings
from redis.exceptions import ResponseError

from django_redis_aiogram import TelegramBot
from django_redis_aiogram.api import API_METHODS, check_function
from django_redis_aiogram.delivery import BlpopDelivery
from django_redis_aiogram.serializers import JsonSerializer, PickleSerializer

LOGGER = 'django_redis_aiogram'
QUEUE = 'TELEGRAM_BOT_MESSAGE'
# the in-flight list is per worker, so ask the delivery for its own name
SETTINGS = {'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1, 'WORKER_NAME': 'tests'}
PROCESSING = f'{QUEUE}:processing:tests'


def payload(chat_id):
    return JsonSerializer().dumps({'function': 'send_message', 'chat_id': chat_id})


def drain(delivery, expected_handled, timeout=5):
    thread = delivery.start_thread()
    waiter = threading.Event()
    for _ in range(int(timeout * 100)):
        if len(delivery.handled) >= expected_handled:
            break
        waiter.wait(0.01)
    delivery.stop()
    thread.join(timeout=timeout)


class Recording(BlpopDelivery):
    def __init__(self, handler=None):
        self.handled = []
        super().__init__(handler=handler or (lambda **kwargs: self.handled.append(kwargs)))


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_delivered_message_is_acknowledged(redis_server):
    redis_server.rpush(QUEUE, payload(1))
    delivery = Recording()
    drain(delivery, expected_handled=1)

    # both lists are also empty when the payload was dropped before the handler
    assert [item['chat_id'] for item in delivery.handled] == [1]
    assert redis_server.llen(QUEUE) == 0
    assert redis_server.llen(PROCESSING) == 0, 'delivered message left in processing'


@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
@override_settings(TELEGRAM_BOT=SETTINGS)
def test_message_survives_a_worker_killed_mid_send(redis_server):
    redis_server.rpush(QUEUE, payload(7))

    class Killed(BaseException):
        """Bypasses dispatch()'s except Exception, like a real kill would."""

    dying = Recording(handler=lambda **kwargs: (_ for _ in ()).throw(Killed()))
    thread = dying.start_thread()
    thread.join(timeout=5)
    assert not thread.is_alive()

    # the message is stranded in processing, not lost
    assert redis_server.llen(PROCESSING) == 1
    assert redis_server.llen(QUEUE) == 0

    survivor = Recording()
    drain(survivor, expected_handled=1)

    assert [item['chat_id'] for item in survivor.handled] == [7]
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_reclaim_preserves_the_original_order(redis_server):
    for chat_id in (1, 2):
        redis_server.rpush(PROCESSING, payload(chat_id))
    redis_server.rpush(QUEUE, payload(3))

    survivor = Recording()
    drain(survivor, expected_handled=3)

    assert [item['chat_id'] for item in survivor.handled] == [1, 2, 3]


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_failing_handler_is_not_redelivered_forever(redis_server):
    """Handler errors are logged and acknowledged — only a crash redelivers."""
    calls = []

    def exploding(**kwargs):
        calls.append(kwargs)
        msg = 'boom'
        raise RuntimeError(msg)

    delivery = Recording(handler=exploding)
    delivery.handled = calls
    redis_server.rpush(QUEUE, payload(1))
    drain(delivery, expected_handled=1)

    assert len(calls) == 1
    assert redis_server.llen(PROCESSING) == 0


class OldRedis:
    """A server from before 6.2: LMOVE does not exist."""

    def __init__(self, inner):
        self._inner = inner

    def lmove(self, *args, **kwargs):
        msg = "unknown command 'LMOVE'"
        raise ResponseError(msg)

    def blmove(self, *args, **kwargs):
        msg = "unknown command 'BLMOVE'"
        raise ResponseError(msg)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def old_redis_server(redis_server, monkeypatch):
    wrapped = OldRedis(redis_server)
    for target in (
        'django_redis_aiogram.redis.get_redis',
        'django_redis_aiogram.delivery.get_redis',
        'django_redis_aiogram.client.get_redis',
    ):
        monkeypatch.setattr(target, lambda wrapped=wrapped: wrapped)
    return redis_server


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_falls_back_to_plain_pops_on_an_old_server(old_redis_server):
    old_redis_server.rpush(QUEUE, payload(5))

    delivery = Recording()
    drain(delivery, expected_handled=1)

    assert [item['chat_id'] for item in delivery.handled] == [5]
    assert delivery._reliable is False


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_draining_acknowledges_too(redis_server):
    """consume_pending is the no-thread drain the Testing page documents; it
    has to clear the processing list the same way the blocking loop does."""
    handled = []
    delivery = BlpopDelivery(handler=lambda **kwargs: handled.append(kwargs))
    redis_server.rpush(QUEUE, payload(3))

    delivery.consume_pending()

    assert [item['chat_id'] for item in handled] == [3]
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_draining_clears_a_backlog_left_while_the_worker_was_down(redis_server):
    for chat_id in (1, 2):
        redis_server.rpush(QUEUE, payload(chat_id))

    handled = []
    BlpopDelivery(handler=lambda **kwargs: handled.append(kwargs['chat_id'])).consume_pending()

    assert sorted(handled) == [1, 2], handled
    assert redis_server.llen(QUEUE) == 0


def test_only_telegram_api_methods_may_be_named():
    """A queued payload picks the method, so `getattr` must not be open season."""
    assert 'send_message' in API_METHODS
    assert check_function('send_photo') == 'send_photo'

    for forbidden in ('download_file', 'token', 'session', 'me', '__init__'):
        with pytest.raises(ValueError, match='not a Telegram API method'):
            check_function(forbidden)


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x', 'RATE_LIMIT': None})
def test_send_raw_refuses_a_non_api_method():
    destination = '/tmp/y'
    with pytest.raises(ValueError, match='not a Telegram API method'):
        TelegramBot().send_raw('download_file', file_path='x', destination=destination)


@override_settings(TELEGRAM_BOT={'TOKEN': '42:x'})
def test_send_redis_refuses_a_non_api_method(redis_server):
    with pytest.raises(ValueError, match='not a Telegram API method'):
        TelegramBot().send_redis('download_file', file_path='x')
    assert redis_server.llen('TELEGRAM_BOT_MESSAGE') == 0


@override_settings(TELEGRAM_BOT={'DELIVERY': 'blpop', 'BLPOP_TIMEOUT': 1})
def test_a_queued_non_api_method_is_dropped_not_executed(redis_server):
    """A payload written by something malicious must not kill the worker either."""
    redis_server.rpush(QUEUE, JsonSerializer().dumps({'function': 'download_file', 'file_path': 'x'}))
    redis_server.rpush(QUEUE, payload(5))

    delivery = Recording()
    drain(delivery, expected_handled=1)

    assert [item['chat_id'] for item in delivery.handled] == [5]
    # permanently invalid, so it is acknowledged: redelivery cannot fix a name
    assert redis_server.llen(QUEUE) == 0
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_reclaim_survives_a_redis_that_is_not_up_yet(redis_server, monkeypatch):
    """run() is the thread target: anything escaping reclaim ends the consumer."""

    class Unreachable:
        def lmove(self, *args, **kwargs):
            msg = 'Connection refused'
            raise ConnectionError(msg)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    monkeypatch.setattr('django_redis_aiogram.delivery.get_redis', Unreachable)

    delivery = Recording()
    delivery.reclaim()  # must not raise

    assert delivery._reliable is True, 'a connection error is not a missing LMOVE'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'worker-a'})
def test_a_starting_worker_does_not_steal_another_workers_message(redis_server):
    """A shared processing list would let a restart pull a message back out
    from under the worker that is still sending it."""
    other = Recording()
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'worker-b'}):
        in_flight = other.processing_key
        redis_server.rpush(in_flight, payload(1))

    mine = Recording()
    assert mine.processing_key != in_flight
    mine.reclaim()

    assert redis_server.llen(in_flight) == 1, "another worker's message was reclaimed"
    assert redis_server.llen(QUEUE) == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_reclaim_is_retried_when_redis_was_down_at_startup(redis_server):
    """One attempt would strand those messages until the next restart."""
    redis_server.rpush(PROCESSING, payload(1))
    failures = []

    class FlakyOnce:
        def lmove(self, *args, **kwargs):
            if not failures:
                failures.append(True)
                msg = 'Connection refused'
                raise ConnectionError(msg)
            return redis_server.lmove(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    delivery = Recording()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('django_redis_aiogram.delivery.get_redis', FlakyOnce)
        drain(delivery, expected_handled=1)

    assert [item['chat_id'] for item in delivery.handled] == [1]
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_response_error_that_is_not_a_missing_lmove_keeps_crash_safety(redis_server, caplog):
    """WRONGTYPE says nothing about LMOVE support; downgrading on it would give
    up the processing list for the life of the container."""

    class WrongType:
        def lmove(self, *args, **kwargs):
            msg = 'WRONGTYPE Operation against a key holding the wrong kind'
            raise ResponseError(msg)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    delivery = Recording()
    with pytest.MonkeyPatch.context() as patch, caplog.at_level('ERROR', logger=LOGGER):
        patch.setattr('django_redis_aiogram.delivery.get_redis', WrongType)
        assert delivery.reclaim() is False, 'the caller was not asked to retry'

    assert delivery._reliable is True, 'crash-safe mode was given up on the wrong error'
    assert 'could not reclaim previous messages' in caplog.text


@override_settings(
    TELEGRAM_BOT={
        **SETTINGS,
        'TOKEN': '42:x',
        'FSM_STORAGE': 'memory',
        'RAISE_EXCEPTION': True,
        'MAX_RETRIES': 1,
        'RATE_LIMIT': None,
    }
)
def test_raise_exception_does_not_leave_a_message_in_flight(redis_server):
    """RAISE_EXCEPTION re-raises out of send_raw once the retries are gone.

    The consumer has to acknowledge anyway: leaving it in the processing list
    would redeliver a message Telegram has already refused, for ever.
    """
    instance = TelegramBot()
    attempts = []

    class AlwaysRetryAfter:
        async def send_message(self, **kwargs):
            attempts.append(kwargs)
            raise exceptions.TelegramRetryAfter(
                method=SendMessage(chat_id=1, text='x'),
                message='Too Many Requests',
                retry_after=0,
            )

        class session:
            @staticmethod
            async def close():
                pass

    instance._bot = AlwaysRetryAfter()
    delivery = Recording(handler=instance.send_raw)
    delivery.handled = attempts
    redis_server.rpush(QUEUE, payload(1))

    drain(delivery, expected_handled=2)  # the first try plus one retry

    assert len(attempts) == 2, attempts
    assert redis_server.llen(QUEUE) == 0
    assert redis_server.llen(PROCESSING) == 0, 'the refused message was left for reclaim'
    instance._bot = None
    instance.close()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'ALLOW_PICKLE': False})
def test_a_refused_pickle_message_stays_in_flight(redis_server, caplog):
    """A missing setting must not destroy a 1.x queue: the payload is valid and
    the refusal is the operator's to fix, so it waits for a reclaim."""
    redis_server.rpush(QUEUE, PickleSerializer().dumps({'function': 'send_message', 'chat_id': 1}))
    redis_server.rpush(QUEUE, payload(2))

    delivery = Recording()
    with caplog.at_level('ERROR', logger=LOGGER):
        drain(delivery, expected_handled=1)

    assert [item['chat_id'] for item in delivery.handled] == [2], 'the JSON message behind it was blocked'
    assert redis_server.llen(QUEUE) == 0
    assert redis_server.llen(PROCESSING) == 1, 'the refused message was acknowledged away'
    assert 'set ALLOW_PICKLE to deliver it' in caplog.text


@override_settings(TELEGRAM_BOT={**SETTINGS, 'ALLOW_PICKLE': True})
def test_the_refused_message_is_delivered_once_the_operator_relents(redis_server):
    """The other half: reclaim plus the setting turns refusal into delivery."""
    redis_server.rpush(PROCESSING, PickleSerializer().dumps({'function': 'send_message', 'chat_id': 7}))

    delivery = Recording()
    drain(delivery, expected_handled=1)

    assert [item['chat_id'] for item in delivery.handled] == [7]
    assert redis_server.llen(PROCESSING) == 0


@pytest.mark.parametrize('method', ['set_webhook', 'delete_webhook', 'log_out', 'close'])
def test_administrative_methods_are_denied_even_though_telegram_has_them(method):
    """Sending is not administering: set_webhook would point updates at someone
    else's URL, and log_out or close ends the session for the deployment."""
    from django_redis_aiogram.api import API_METHODS, DENIED_METHODS, check_function

    assert method in DENIED_METHODS
    assert method not in API_METHODS
    with pytest.raises(ValueError, match='not a Telegram API method'):
        check_function(method)


def test_the_deny_list_only_removes_methods_that_exist():
    """A typo in the deny list would silently protect nothing."""
    import re as regex

    import aiogram.methods
    from aiogram import Bot

    from django_redis_aiogram.api import DENIED_METHODS

    discovered = {regex.sub(r'(?<!^)(?=[A-Z])', '_', name).lower() for name in aiogram.methods.__all__}
    public = {name for name in dir(Bot) if not name.startswith('_')}

    assert discovered & public >= DENIED_METHODS, 'the deny list names something aiogram lacks'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_draining_by_hand_downgrades_on_an_old_server(old_redis_server):
    """`consume_pending` is documented as the drain that needs no thread.

    `run()` learns the server has no LMOVE from `reclaim()`; nothing probes for a
    caller draining by hand, so the first pop used to raise `ResponseError` out of
    a documented helper instead of falling back to the at-most-once path.
    """
    old_redis_server.rpush(QUEUE, payload(9))

    handled = []
    delivery = BlpopDelivery(handler=lambda **kwargs: handled.append(kwargs))
    delivery.consume_pending()

    assert [item['chat_id'] for item in handled] == [9]
    assert delivery._reliable is False
