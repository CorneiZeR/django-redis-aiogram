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
from django_redis_aiogram.delivery import BlpopDelivery, KeyspaceDelivery
from django_redis_aiogram.serializers import JsonSerializer

LOGGER = "django_redis_aiogram"
QUEUE = "TELEGRAM_BOT_MESSAGE"
# the in-flight list is per worker, so ask the delivery for its own name
SETTINGS = {"DELIVERY": "blpop", "BLPOP_TIMEOUT": 1, "WORKER_NAME": "tests"}
PROCESSING = f"{QUEUE}:processing:tests"


def payload(chat_id):
    return JsonSerializer().dumps({"function": "send_message", "chat_id": chat_id})


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
    assert [item["chat_id"] for item in delivery.handled] == [1]
    assert redis_server.llen(QUEUE) == 0
    assert redis_server.llen(PROCESSING) == 0, "delivered message left in processing"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
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

    assert [item["chat_id"] for item in survivor.handled] == [7]
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_reclaim_preserves_the_original_order(redis_server):
    for chat_id in (1, 2):
        redis_server.rpush(PROCESSING, payload(chat_id))
    redis_server.rpush(QUEUE, payload(3))

    survivor = Recording()
    drain(survivor, expected_handled=3)

    assert [item["chat_id"] for item in survivor.handled] == [1, 2, 3]


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_failing_handler_is_not_redelivered_forever(redis_server):
    """Handler errors are logged and acknowledged — only a crash redelivers."""
    calls = []

    def exploding(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("boom")

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
        raise ResponseError("unknown command 'LMOVE'")

    def blmove(self, *args, **kwargs):
        raise ResponseError("unknown command 'BLMOVE'")

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def old_redis_server(redis_server, monkeypatch):
    wrapped = OldRedis(redis_server)
    for target in (
        "django_redis_aiogram.redis.get_redis",
        "django_redis_aiogram.delivery.get_redis",
        "django_redis_aiogram.client.get_redis",
    ):
        monkeypatch.setattr(target, lambda wrapped=wrapped: wrapped)
    return redis_server


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_falls_back_to_plain_pops_on_an_old_server(old_redis_server):
    old_redis_server.rpush(QUEUE, payload(5))

    delivery = Recording()
    drain(delivery, expected_handled=1)

    assert [item["chat_id"] for item in delivery.handled] == [5]
    assert delivery._reliable is False


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace"})
def test_keyspace_acknowledges_too(redis_server):
    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs))
    redis_server.rpush(QUEUE, payload(3))

    delivery._on_expired({"data": b"TELEGRAM_BOT_EXP"})

    assert [item["chat_id"] for item in handled] == [3]
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace"})
def test_keyspace_accepts_str_event_data(redis_server):
    """pubsub hands back str when the URL enables decode_responses; 1.x-style
    unconditional .decode() crashed the consumer thread on it."""
    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs))
    redis_server.rpush(QUEUE, payload(9))

    delivery._on_expired({"data": "TELEGRAM_BOT_EXP"})

    assert [item["chat_id"] for item in handled] == [9]


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace"})
def test_keyspace_drains_a_backlog_left_while_the_worker_was_down(redis_server):
    """Expiry events are not replayed, so a backlog would otherwise sit in the
    list until some later message happened to trigger one."""
    for chat_id in (1, 2):
        redis_server.rpush(QUEUE, payload(chat_id))

    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs["chat_id"]))
    thread = delivery.start_thread()
    waiter = threading.Event()
    for _ in range(500):
        if len(handled) >= 2:
            break
        waiter.wait(0.01)
    delivery.stop()
    thread.join(timeout=5)

    assert sorted(handled) == [1, 2], handled
    assert redis_server.llen(QUEUE) == 0


def test_only_telegram_api_methods_may_be_named():
    """A queued payload picks the method, so `getattr` must not be open season."""
    from django_redis_aiogram.api import API_METHODS, check_function

    assert "send_message" in API_METHODS
    assert check_function("send_photo") == "send_photo"

    for forbidden in ("download_file", "token", "session", "me", "__init__"):
        with pytest.raises(ValueError, match="not a Telegram API method"):
            check_function(forbidden)


@override_settings(TELEGRAM_BOT={"TOKEN": "42:x", "RATE_LIMIT": None})
def test_send_raw_refuses_a_non_api_method():
    with pytest.raises(ValueError, match="not a Telegram API method"):
        TelegramBot().send_raw("download_file", file_path="x", destination="/tmp/y")


@override_settings(TELEGRAM_BOT={"TOKEN": "42:x"})
def test_send_redis_refuses_a_non_api_method(redis_server):
    with pytest.raises(ValueError, match="not a Telegram API method"):
        TelegramBot().send_redis("download_file", file_path="x")
    assert redis_server.llen("TELEGRAM_BOT_MESSAGE") == 0


@override_settings(TELEGRAM_BOT={"DELIVERY": "blpop", "BLPOP_TIMEOUT": 1})
def test_a_queued_non_api_method_is_dropped_not_executed(redis_server):
    """A payload written by something malicious must not kill the worker either."""
    redis_server.rpush(QUEUE, JsonSerializer().dumps({"function": "download_file", "file_path": "x"}))
    redis_server.rpush(QUEUE, payload(5))

    delivery = Recording()
    drain(delivery, expected_handled=1)

    assert [item["chat_id"] for item in delivery.handled] == [5]


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_reclaim_survives_a_redis_that_is_not_up_yet(redis_server, monkeypatch):
    """run() is the thread target: anything escaping reclaim ends the consumer."""

    class Unreachable:
        def lmove(self, *args, **kwargs):
            raise ConnectionError("Connection refused")

        def __getattr__(self, name):
            return getattr(redis_server, name)

    monkeypatch.setattr("django_redis_aiogram.delivery.get_redis", lambda: Unreachable())

    delivery = Recording()
    delivery.reclaim()  # must not raise

    assert delivery._reliable is True, "a connection error is not a missing LMOVE"


@override_settings(TELEGRAM_BOT={**SETTINGS, "WORKER_NAME": "worker-a"})
def test_a_starting_worker_does_not_steal_another_workers_message(redis_server):
    """A shared processing list would let a restart pull a message back out
    from under the worker that is still sending it."""
    other = Recording()
    with override_settings(TELEGRAM_BOT={**SETTINGS, "WORKER_NAME": "worker-b"}):
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
                raise ConnectionError("Connection refused")
            return redis_server.lmove(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    delivery = Recording()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("django_redis_aiogram.delivery.get_redis", lambda: FlakyOnce())
        drain(delivery, expected_handled=1)

    assert [item["chat_id"] for item in delivery.handled] == [1]
    assert redis_server.llen(PROCESSING) == 0


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace"})
def test_the_keyspace_consumer_also_retries_a_failed_reclaim(redis_server):
    """Both loops call reclaim once at startup, so both need the retry."""
    redis_server.rpush(PROCESSING, payload(7))
    failures = []

    class DownForTwoAttempts:
        def lmove(self, *args, **kwargs):
            if len(failures) < 2:
                failures.append(True)
                raise ConnectionError("Connection refused")
            return redis_server.lmove(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs["chat_id"]))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("django_redis_aiogram.delivery.get_redis", lambda: DownForTwoAttempts())
        thread = delivery.start_thread()
        waiter = threading.Event()
        for _ in range(500):
            if handled:
                break
            waiter.wait(0.01)
        alive_through_the_outage = thread.is_alive()
        delivery.stop()
        thread.join(timeout=5)

    assert alive_through_the_outage, "the consumer thread died during the outage"
    assert len(failures) == 2, "the outage did not last, so the retry loop was not tested"
    assert handled == [7], handled


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_response_error_that_is_not_a_missing_lmove_keeps_crash_safety(redis_server, caplog):
    """WRONGTYPE says nothing about LMOVE support; downgrading on it would give
    up the processing list for the life of the container."""

    class WrongType:
        def lmove(self, *args, **kwargs):
            raise ResponseError("WRONGTYPE Operation against a key holding the wrong kind")

        def __getattr__(self, name):
            return getattr(redis_server, name)

    delivery = Recording()
    with pytest.MonkeyPatch.context() as patch, caplog.at_level("ERROR", logger=LOGGER):
        patch.setattr("django_redis_aiogram.delivery.get_redis", lambda: WrongType())
        assert delivery.reclaim() is False, "the caller was not asked to retry"

    assert delivery._reliable is True, "crash-safe mode was given up on the wrong error"
    assert "could not reclaim previous messages" in caplog.text


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace"})
def test_a_redis_that_fails_to_subscribe_does_not_kill_the_worker(redis_server, caplog):
    """subscribe() is a network call; 1.x-style setup outside the loop died on it."""
    attempts = []

    class RefusesSubscribe:
        def pubsub(self, *args, **kwargs):
            attempts.append(True)
            raise ConnectionError("Connection refused")

        def __getattr__(self, name):
            return getattr(redis_server, name)

    delivery = KeyspaceDelivery(handler=lambda **kwargs: None)
    with pytest.MonkeyPatch.context() as patch, caplog.at_level("ERROR", logger=LOGGER):
        patch.setattr("django_redis_aiogram.delivery.get_redis", lambda: RefusesSubscribe())
        thread = delivery.start_thread()
        waiter = threading.Event()
        for _ in range(300):
            if len(attempts) >= 2:  # it came back for a second go
                break
            waiter.wait(0.01)
        still_running = thread.is_alive()
        delivery.stop()
        thread.join(timeout=5)

    assert len(attempts) >= 2, f"the consumer did not retry the subscription: {len(attempts)}"
    assert still_running, "a failed subscribe ended the consumer"
    assert "keyspace consumer error, retrying" in caplog.text


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace"})
def test_a_redis_that_refuses_config_set_only_warns(redis_server, caplog):
    """Managed providers (ElastiCache, Upstash) refuse CONFIG SET.

    1.x died on that; the worker has to say so and carry on, because the list is
    still readable — which is what the startup drain below proves.
    """
    redis_server.rpush(QUEUE, payload(3))

    class RefusesConfigSet:
        def config_get(self, *args, **kwargs):
            raise ResponseError("unknown command 'CONFIG'")

        def config_set(self, *args, **kwargs):
            raise ResponseError("unknown command 'CONFIG'")

        def __getattr__(self, name):
            return getattr(redis_server, name)

    handled = []
    delivery = KeyspaceDelivery(handler=lambda **kwargs: handled.append(kwargs["chat_id"]))
    with pytest.MonkeyPatch.context() as patch, caplog.at_level("WARNING", logger=LOGGER):
        patch.setattr("django_redis_aiogram.delivery.get_redis", lambda: RefusesConfigSet())
        thread = delivery.start_thread()
        waiter = threading.Event()
        for _ in range(500):
            if handled:
                break
            waiter.wait(0.01)
        still_running = thread.is_alive()
        delivery.stop()
        thread.join(timeout=5)

    assert "cannot enable keyspace notifications" in caplog.text
    assert still_running, "the refusal killed the consumer thread"
    assert handled == [3], handled


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace"})
def test_a_redis_that_is_down_at_the_notification_probe_does_not_kill_the_worker(redis_server, caplog):
    """config_get runs first in the thread target, before anything catches."""
    attempts = []

    class DownAtFirst:
        def config_get(self, *args, **kwargs):
            attempts.append(True)
            raise ConnectionError("Connection refused")

        def __getattr__(self, name):
            return getattr(redis_server, name)

    delivery = KeyspaceDelivery(handler=lambda **kwargs: None)
    with pytest.MonkeyPatch.context() as patch, caplog.at_level("ERROR", logger=LOGGER):
        patch.setattr("django_redis_aiogram.delivery.get_redis", lambda: DownAtFirst())
        thread = delivery.start_thread()
        waiter = threading.Event()
        for _ in range(200):
            if attempts:
                break
            waiter.wait(0.01)
        waiter.wait(0.1)
        still_running = thread.is_alive()
        delivery.stop()
        thread.join(timeout=5)

    assert attempts, "the probe never ran"
    assert still_running, "a connection error at the probe ended the consumer"
    assert "could not probe keyspace notifications" in caplog.text


@override_settings(
    TELEGRAM_BOT={
        **SETTINGS,
        "TOKEN": "42:x",
        "FSM_STORAGE": "memory",
        "RAISE_EXCEPTION": True,
        "MAX_RETRIES": 1,
        "RATE_LIMIT": None,
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
                method=SendMessage(chat_id=1, text="x"),
                message="Too Many Requests",
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
    assert redis_server.llen(PROCESSING) == 0, "the refused message was left for reclaim"
    instance._bot = None
    instance.close()
