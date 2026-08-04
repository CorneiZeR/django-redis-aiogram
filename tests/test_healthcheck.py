"""`docker ps` cannot tell whether the consumer is consuming.

The heartbeat is the only thing another process can observe about the consumer
thread, and `tgbot_healthcheck` is what reads it.
"""

import time
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from django_redis_aiogram.delivery import BlpopDelivery, KeyspaceDelivery

QUEUE = "TELEGRAM_BOT_MESSAGE"
WORKER = "tests"
HEARTBEAT = f"{QUEUE}:heartbeat:{WORKER}"
SETTINGS = {
    "TOKEN": "42:x",
    "REDIS_URL": "redis://localhost:6379/0",
    "WORKER_NAME": WORKER,
    "DELIVERY": "blpop",
    "BLPOP_TIMEOUT": 1,
}
#: what the fakes below raise with, named up here so each raise stays one line
REFUSED = "Connection refused"
READONLY = "READONLY You cannot write against a read only replica"
RESET = "Connection reset by peer"
STOP_AFTER_ONE_READ = "stop here"


def healthcheck(**options):
    out = StringIO()
    call_command("tgbot_healthcheck", stdout=out, **options)
    return out.getvalue()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_consumer_writes_a_heartbeat(redis_server):
    delivery = BlpopDelivery(handler=lambda **kwargs: None)

    delivery.heartbeat()

    assert redis_server.get(HEARTBEAT) is not None
    assert redis_server.ttl(HEARTBEAT) > 0, "the heartbeat must expire on its own"


@override_settings(TELEGRAM_BOT={**SETTINGS, "HEARTBEAT_INTERVAL": 30})
def test_the_heartbeat_is_paced(redis_server):
    """Refreshing per message would be a write per message."""
    delivery = BlpopDelivery(handler=lambda **kwargs: None)

    delivery.heartbeat()
    first = redis_server.get(HEARTBEAT)
    redis_server.delete(HEARTBEAT)
    delivery.heartbeat()

    assert first is not None
    assert redis_server.get(HEARTBEAT) is None, "it wrote again inside the interval"


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_redis_that_refuses_the_write_does_not_stop_the_loop(redis_server, caplog):
    class Refuses:
        def set(self, *args, **kwargs):
            raise ConnectionError(REFUSED)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    delivery = BlpopDelivery(handler=lambda **kwargs: None)
    with pytest.MonkeyPatch.context() as patch, caplog.at_level("ERROR"):
        patch.setattr("django_redis_aiogram.delivery.get_redis", Refuses)
        delivery.heartbeat()  # must not raise

    assert "could not write the heartbeat" in caplog.text


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace"})
def test_both_consumers_have_their_own_key(redis_server):
    blpop = BlpopDelivery(handler=lambda **kwargs: None)
    keyspace = KeyspaceDelivery(handler=lambda **kwargs: None)

    assert blpop.heartbeat_key == keyspace.heartbeat_key == HEARTBEAT


@override_settings(TELEGRAM_BOT={**SETTINGS, "WORKER_NAME": "worker-b"})
def test_the_key_is_per_worker(redis_server):
    assert BlpopDelivery(handler=lambda **kwargs: None).heartbeat_key.endswith(":worker-b")


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_healthy_when_the_heartbeat_is_fresh(redis_server):
    redis_server.set(HEARTBEAT, str(int(time.time())))

    assert "healthy" in healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_unhealthy_when_there_is_no_heartbeat(redis_server):
    with pytest.raises(CommandError, match="no heartbeat"):
        healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_unhealthy_when_the_heartbeat_is_stale(redis_server):
    """The failure this command exists for: the thread died, the process lives."""
    redis_server.set(HEARTBEAT, str(int(time.time()) - 300))

    with pytest.raises(CommandError, match="last reported"):
        healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_unhealthy_when_redis_is_unreachable(monkeypatch):
    class Down:
        def ping(self):
            raise ConnectionError(REFUSED)

    monkeypatch.setattr("django_redis_aiogram.management.commands.tgbot_healthcheck.get_redis", Down)

    with pytest.raises(CommandError, match="redis is unreachable"):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, "HEALTHCHECK_MAX_QUEUE": 2})
def test_unhealthy_when_the_queue_is_over_the_limit(redis_server):
    redis_server.set(HEARTBEAT, str(int(time.time())))
    for _ in range(3):
        redis_server.rpush(QUEUE, b"{}")

    with pytest.raises(CommandError, match="3 messages are queued"):
        healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_queue_check_is_off_by_default(redis_server):
    redis_server.set(HEARTBEAT, str(int(time.time())))
    for _ in range(50):
        redis_server.rpush(QUEUE, b"{}")

    assert "healthy" in healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, "HEALTHCHECK_MAX_QUEUE": 100})
def test_the_limits_can_be_given_on_the_command_line(redis_server):
    redis_server.set(HEARTBEAT, str(int(time.time())))
    for _ in range(3):
        redis_server.rpush(QUEUE, b"{}")

    with pytest.raises(CommandError, match="over the limit of 2"):
        healthcheck(max_queue=2)


@override_settings(TELEGRAM_BOT={**SETTINGS, "ENABLED": False})
def test_a_disabled_process_is_not_unhealthy():
    """Nothing is meant to be running there, so nothing is wrong."""
    assert "disabled" in healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_heartbeat_that_is_not_a_timestamp_is_reported(redis_server):
    redis_server.set(HEARTBEAT, b"soon")

    with pytest.raises(CommandError, match="not a timestamp"):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, "DELIVERY": "keyspace", "HEARTBEAT_INTERVAL": 1})
def test_the_keyspace_consumer_reports_in_too(redis_server):
    """Both loops have to beat; only one of them blocks on the queue."""
    delivery = KeyspaceDelivery(handler=lambda **kwargs: None)
    thread = delivery.start_thread()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and redis_server.get(HEARTBEAT) is None:
            time.sleep(0.02)
    finally:
        delivery.stop()
        thread.join(timeout=5)

    assert redis_server.get(HEARTBEAT) is not None, "the keyspace consumer never reported in"
    assert not thread.is_alive()


@override_settings(TELEGRAM_BOT={**SETTINGS, "BLPOP_TIMEOUT": 300, "HEARTBEAT_INTERVAL": 5})
def test_a_long_blocking_read_cannot_outlast_the_heartbeat(redis_server, monkeypatch):
    """The loop beats between reads, so a read longer than the interval would
    let the key expire under a consumer that is doing fine."""
    seen = []

    class Spy:
        def blmove(self, source, destination, timeout, *args, **kwargs):
            seen.append(timeout)
            raise ConnectionError(STOP_AFTER_ONE_READ)  # one read is enough to observe

        def __getattr__(self, name):
            return getattr(redis_server, name)

    monkeypatch.setattr("django_redis_aiogram.delivery.get_redis", Spy)
    delivery = BlpopDelivery(handler=lambda **kwargs: None)
    thread = delivery.start_thread()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not seen:
            time.sleep(0.02)
    finally:
        delivery.stop()
        thread.join(timeout=10)

    assert seen, "the consumer never reached the blocking read"
    assert max(seen) <= 5, f"it blocked for {max(seen)}s with a 5s heartbeat interval"


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_heartbeat_read_that_fails_after_ping_is_reported(redis_server, monkeypatch):
    """A failover between the two commands must not surface as a traceback."""

    class FailsTheRead:
        def ping(self):
            return True

        def get(self, *args, **kwargs):
            raise ConnectionError(READONLY)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    monkeypatch.setattr(
        "django_redis_aiogram.management.commands.tgbot_healthcheck.get_redis",
        FailsTheRead,
    )

    with pytest.raises(CommandError, match="could not read the heartbeat"):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, "HEALTHCHECK_MAX_QUEUE": 5})
def test_a_queue_read_that_fails_is_reported(redis_server, monkeypatch):
    class FailsTheCount:
        def ping(self):
            return True

        def get(self, *args, **kwargs):
            return str(int(time.time())).encode()

        def llen(self, *args, **kwargs):
            raise ConnectionError(RESET)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    monkeypatch.setattr(
        "django_redis_aiogram.management.commands.tgbot_healthcheck.get_redis",
        FailsTheCount,
    )

    with pytest.raises(CommandError, match="could not read the queue length"):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, "HEALTHCHECK_MAX_QUEUE": 3})
def test_the_queue_limit_is_inclusive(redis_server):
    """Exactly at the limit is still healthy; the docs say so."""
    redis_server.set(HEARTBEAT, str(int(time.time())))
    for _ in range(3):
        redis_server.rpush(QUEUE, b"{}")

    assert "healthy" in healthcheck()

    redis_server.rpush(QUEUE, b"{}")
    with pytest.raises(CommandError, match="4 messages are queued"):
        healthcheck()
