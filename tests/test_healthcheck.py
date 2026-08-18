"""`docker ps` cannot tell whether the consumer is consuming.

The heartbeat is the only thing another process can observe about the consumer
thread, and `tgbot_healthcheck` is what reads it.
"""

import time
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

# redis-py's own ConnectionError, not the built-in one: it subclasses `RedisError` and
# the built-in does not, so a fake raising the built-in was pretending to be a failure
# no real client produces — which `except Exception` in the probe used to hide
from redis.exceptions import ConnectionError, RedisError, ResponseError  # noqa: A004 - the point is to shadow it

from django_redis_aiogram.delivery import BlpopDelivery
from django_redis_aiogram.healthcheck import check

QUEUE = 'TELEGRAM_BOT_MESSAGE'
WORKER = 'tests'
HEARTBEAT = f'{QUEUE}:heartbeat:{WORKER}'
SETTINGS = {
    'TOKEN': '42:x',
    'REDIS_URL': 'redis://localhost:6379/0',
    'WORKER_NAME': WORKER,
    'DELIVERY': 'blpop',
    'BLPOP_TIMEOUT': 1,
}
#: what the fakes below raise with, named up here so each raise stays one line
REFUSED = 'Connection refused'
READONLY = 'READONLY You cannot write against a read only replica'
RESET = 'Connection reset by peer'
STOP_AFTER_ONE_READ = 'stop here'


def healthcheck(**options):
    out = StringIO()
    call_command('tgbot_healthcheck', stdout=out, **options)
    return out.getvalue()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_consumer_writes_a_heartbeat(redis_server):
    delivery = BlpopDelivery(handler=lambda **kwargs: None)

    delivery.heartbeat()

    assert redis_server.get(HEARTBEAT) is not None
    assert redis_server.ttl(HEARTBEAT) > 0, 'the heartbeat must expire on its own'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEARTBEAT_INTERVAL': 30})
def test_the_heartbeat_is_paced(redis_server):
    """Refreshing per message would be a write per message."""
    delivery = BlpopDelivery(handler=lambda **kwargs: None)

    delivery.heartbeat()
    first = redis_server.get(HEARTBEAT)
    redis_server.delete(HEARTBEAT)
    delivery.heartbeat()

    assert first is not None
    assert redis_server.get(HEARTBEAT) is None, 'it wrote again inside the interval'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_redis_that_refuses_the_write_does_not_stop_the_loop(redis_server, caplog):
    class Refuses:
        def set(self, *args, **kwargs):
            raise ConnectionError(REFUSED)

        def __getattr__(self, name):
            return getattr(redis_server, name)

    delivery = BlpopDelivery(handler=lambda **kwargs: None)
    with pytest.MonkeyPatch.context() as patch, caplog.at_level('ERROR'):
        patch.setattr('django_redis_aiogram.delivery.get_redis', Refuses)
        delivery.heartbeat()  # must not raise

    assert 'could not write the heartbeat' in caplog.text


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'worker-b'})
def test_the_key_is_per_worker(redis_server):
    assert BlpopDelivery(handler=lambda **kwargs: None).heartbeat_key.endswith(':worker-b')


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_healthy_when_the_heartbeat_is_fresh(redis_server):
    redis_server.set(HEARTBEAT, str(int(time.time())))

    assert 'healthy' in healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_unhealthy_when_there_is_no_heartbeat(redis_server):
    with pytest.raises(CommandError, match='no heartbeat'):
        healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_unhealthy_when_the_heartbeat_is_stale(redis_server):
    """The failure this command exists for: the thread died, the process lives."""
    redis_server.set(HEARTBEAT, str(int(time.time()) - 300))

    with pytest.raises(CommandError, match='last reported'):
        healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_unhealthy_when_redis_is_unreachable(monkeypatch):
    class Down:
        def ping(self):
            raise ConnectionError(REFUSED)

    monkeypatch.setattr('django_redis_aiogram.healthcheck.get_redis', Down)

    with pytest.raises(CommandError, match='redis is unreachable'):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEALTHCHECK_MAX_QUEUE': 2})
def test_unhealthy_when_the_queue_is_over_the_limit(redis_server):
    redis_server.set(HEARTBEAT, str(int(time.time())))
    for _ in range(3):
        redis_server.rpush(QUEUE, b'{}')

    with pytest.raises(CommandError, match='3 messages are queued'):
        healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_queue_check_is_off_by_default(redis_server):
    redis_server.set(HEARTBEAT, str(int(time.time())))
    for _ in range(50):
        redis_server.rpush(QUEUE, b'{}')

    assert 'healthy' in healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEALTHCHECK_MAX_QUEUE': 100})
def test_the_limits_can_be_given_on_the_command_line(redis_server):
    redis_server.set(HEARTBEAT, str(int(time.time())))
    for _ in range(3):
        redis_server.rpush(QUEUE, b'{}')

    with pytest.raises(CommandError, match='over the limit of 2'):
        healthcheck(max_queue=2)


@override_settings(TELEGRAM_BOT={**SETTINGS, 'ENABLED': False})
def test_a_disabled_process_is_not_unhealthy():
    """Nothing is meant to be running there, so nothing is wrong."""
    assert 'disabled' in healthcheck()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_heartbeat_that_is_not_a_timestamp_is_reported(redis_server):
    redis_server.set(HEARTBEAT, b'soon')

    with pytest.raises(CommandError, match='not a timestamp'):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'BLPOP_TIMEOUT': 300, 'HEARTBEAT_INTERVAL': 5})
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

    monkeypatch.setattr('django_redis_aiogram.delivery.get_redis', Spy)
    delivery = BlpopDelivery(handler=lambda **kwargs: None)
    thread = delivery.start_thread()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not seen:
            time.sleep(0.02)
    finally:
        delivery.stop()
        thread.join(timeout=10)

    assert seen, 'the consumer never reached the blocking read'
    assert max(seen) <= 5, f'it blocked for {max(seen)}s with a 5s heartbeat interval'


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
        'django_redis_aiogram.healthcheck.get_redis',
        FailsTheRead,
    )

    with pytest.raises(CommandError, match='could not read the heartbeat'):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEALTHCHECK_MAX_QUEUE': 5})
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
        'django_redis_aiogram.healthcheck.get_redis',
        FailsTheCount,
    )

    with pytest.raises(CommandError, match='could not read the queue length'):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEALTHCHECK_MAX_QUEUE': 3})
def test_the_queue_limit_is_inclusive(redis_server):
    """Exactly at the limit is still healthy; the docs say so."""
    redis_server.set(HEARTBEAT, str(int(time.time())))
    for _ in range(3):
        redis_server.rpush(QUEUE, b'{}')

    assert 'healthy' in healthcheck()

    redis_server.rpush(QUEUE, b'{}')
    with pytest.raises(CommandError, match='4 messages are queued'):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_the_probe_says_which_guarantee_is_in_force(redis_server):
    """A probe that only says "healthy" cannot tell at-least-once from
    at-most-once, and the difference is whether a kill loses a message."""
    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))
    out = StringIO()

    call_command('tgbot_healthcheck', stdout=out)

    assert 'at-least-once' in out.getvalue()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_messages_stranded_under_another_worker_are_reported(redis_server):
    """A stranded list is invisible otherwise: nothing reads it and nothing
    counts it, which is how it stays stranded."""
    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))
    redis_server.rpush(f'{QUEUE}:processing:gone', b'{}', b'{}')
    out = StringIO()

    call_command('tgbot_healthcheck', stdout=out)

    reported = out.getvalue()
    assert '2 message(s) are in flight under other worker names' in reported
    assert 'tgbot_reclaim' in reported


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_an_old_server_is_not_reported_as_crash_safe(redis_server, monkeypatch):
    """This command builds its own `Delivery`, and a fresh one says it is crash
    safe until something proves otherwise — the consumer learns that from
    `reclaim()`, which a probe must not call. Reporting the default would tell an
    operator on a pre-6.2 Redis that messages survive a kill, which is the one
    thing they need to know is untrue."""

    def no_lmove(*args, **kwargs):
        msg = "unknown command 'LMOVE'"
        raise ResponseError(msg)

    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))
    monkeypatch.setattr(redis_server, 'lmove', no_lmove)
    out = StringIO()

    call_command('tgbot_healthcheck', stdout=out)

    reported = out.getvalue()
    assert 'at-most-once' in reported, reported
    assert 'at-least-once' not in reported, reported


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_the_stranded_sweep_is_bounded_and_says_when_it_stopped_early(redis_server):
    """`MATCH` filters on the server, but `SCAN` walks the whole keyspace.

    The compose recipe runs this probe every thirty seconds, and the settings
    page suggests sharing one Redis with a cache backend — so an unbounded sweep
    is a full pass over someone else's keys twice a minute. It stops instead, and
    a count it cannot stand behind is reported as a floor rather than a total.
    """
    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))
    redis_server.rpush(f'{QUEUE}:processing:gone', b'{}')
    # more keys than the bound can reach at a hundred a round
    for index in range(4000):
        redis_server.set(f'unrelated:{index}', b'x')
    out = StringIO()

    call_command('tgbot_healthcheck', stdout=out)

    reported = out.getvalue()
    assert 'healthy' in reported
    assert 'at least' in reported, reported


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_a_scan_that_fails_does_not_make_the_container_unhealthy(redis_server, monkeypatch):
    """The probe answers about this worker. A scan it could not finish is not a
    reason to restart a container that is doing its job."""
    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))

    def refuse(*args, **kwargs):
        raise RedisError('NOPERM')

    # the method the sweep actually calls: patching scan_iter left the handler
    # below unexercised while the test went on passing
    monkeypatch.setattr(redis_server, 'scan', refuse)
    out = StringIO()

    call_command('tgbot_healthcheck', stdout=out)

    assert 'healthy' in out.getvalue()


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_container_form_neither_scans_nor_writes(redis_server, monkeypatch):
    """The one place the two entry points differ, and the reason the split exists.

    A healthcheck runs twice a minute. `_stranded` is up to twenty `SCAN` rounds over a
    keyspace the settings page says is often shared with a cache backend, and
    `_guarantee` is a write — a no-op `LMOVE` on a missing key, but a write, which a
    read-only replica refuses outright. Neither can change the verdict, so neither
    belongs on that path by default.

    Asserted on the calls, not on the output: a message that happens not to mention
    stranded lists proves only that none were found.
    """
    calls: list[str] = []
    for name in ('scan', 'lmove'):
        original = getattr(redis_server, name)

        def recording(*args, _name=name, _original=original, **kwargs):
            """Note that this command was issued, then let it through."""
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(redis_server, name, recording)

    redis_server.set(HEARTBEAT, str(int(time.time())))
    report = check()

    assert report.ok, report.message
    assert report.message.startswith('healthy: heartbeat'), report.message
    assert calls == [], f'the container form paid for {sorted(set(calls))}'
    assert 'once' not in report.message, f'the guarantee was probed and reported: {report.message}'


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_management_command_still_scans_and_reports_the_guarantee(redis_server, monkeypatch):
    """The other half: the command's output must not change for anyone using it.

    Without this, moving the defaults to off would silently take the guarantee line and
    the stranded warning out of a command people read by hand — which is the sort of
    quiet removal a changelog entry cannot make up for.
    """
    calls: list[str] = []
    for name in ('scan', 'lmove'):
        original = getattr(redis_server, name)

        def recording(*args, _name=name, _original=original, **kwargs):
            """Note that this command was issued, then let it through."""
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(redis_server, name, recording)

    redis_server.set(HEARTBEAT, str(int(time.time())))
    out = StringIO()
    call_command('tgbot_healthcheck', stdout=out)

    assert 'at-least-once' in out.getvalue(), out.getvalue()
    assert 'scan' in calls, f'the command stopped scanning: {calls}'
    assert 'lmove' in calls, f'the command stopped probing the guarantee: {calls}'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': ''})
def test_a_missing_redis_url_reads_as_an_unreachable_redis():
    """A connection that cannot be built is a Redis this probe cannot reach.

    `build_client` raises `ImproperlyConfigured` on an empty `REDIS_URL`, which is not a
    `RedisError` — so narrowing the guard from `except Exception` turned a readable line
    into a traceback, and turned the command's `CommandError` into an
    `ImproperlyConfigured`. The old wording is what this asserts, because it is what a
    consumer's compose logs have shown for three releases.
    """
    report = check()

    assert not report.ok
    assert report.message.startswith('redis is unreachable: '), report.message
    assert 'REDIS_URL' in report.message

    with pytest.raises(CommandError, match='redis is unreachable'):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'ENABLED': False})
def test_a_disabled_process_is_not_unhealthy_and_is_not_reported_as_healthy():
    """Documented on the Deployment page and, until now, tested nowhere.

    Two things about it. It exits 0, because nothing is meant to be running here — and
    it says so *plainly*: the message goes through `self.style.SUCCESS` for a healthy
    bot and must not for this one, which examined nothing. `Report.checked` carries that
    distinction rather than the wrapper sniffing the string.
    """
    report = check()

    assert report.ok, report.message
    assert report.checked is False, 'a disabled process examined nothing, so it cannot claim to have'
    assert report.message == 'disabled in this process; nothing to check'

    out = StringIO()
    # force_color, not no_color=False: `self.style` is a no-op when the stream is not a
    # tty, so a StringIO cannot tell a styled write from a plain one otherwise — which
    # is how the first version of this test passed with the styling put back
    call_command('tgbot_healthcheck', stdout=out, force_color=True)

    assert out.getvalue().strip() == report.message, repr(out.getvalue())
    assert '\x1b[' not in out.getvalue(), 'the disabled line was coloured as a success'
