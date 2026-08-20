"""`docker ps` cannot tell whether the consumer is consuming.

The heartbeat is the only thing another process can observe about the consumer
thread, and `tgbot_healthcheck` is what reads it.
"""

import time
from io import StringIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import override_settings

# redis-py's own ConnectionError, not the built-in one: it subclasses `RedisError` and
# the built-in does not, so a fake raising the built-in was pretending to be a failure
# no real client produces — which `except Exception` in the probe used to hide
from redis.exceptions import ConnectionError, RedisError, ResponseError  # noqa: A004 - the point is to shadow it

from django_redis_aiogram.delivery import BlpopDelivery
from django_redis_aiogram.healthcheck import build_parser, check, main
from django_redis_aiogram.management.commands.tgbot_healthcheck import Command as TgbotHealthcheck

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
    # nothing was scanned, so there is no floor to report and nothing to hedge about
    assert 'in flight' not in out.getvalue(), out.getvalue()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_a_sweep_that_stops_partway_reports_what_it_did_see(redis_server, monkeypatch):
    """A count found before the failure is worth having, said as a floor.

    The old command threw it away and printed the healthy line alone, which reads as
    *no stranded messages* — the one conclusion a partial sweep cannot support. What it
    must not do is claim the number is complete, so the wording says `at least`.
    """
    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))
    redis_server.rpush(f'{QUEUE}:processing:gone', b'{}', b'{}')
    rounds = []
    real_scan = redis_server.scan

    def once_then_refuse(*args, **kwargs):
        rounds.append(1)
        if len(rounds) > 1:
            raise RedisError('NOPERM')
        # a non-zero cursor, so the sweep comes back for a second round it will not get
        return 99, real_scan(*args, **kwargs)[1]

    monkeypatch.setattr(redis_server, 'scan', once_then_refuse)
    out = StringIO()

    call_command('tgbot_healthcheck', stdout=out)

    printed = out.getvalue()
    assert 'healthy' in printed, printed
    assert 'at least 2 message(s) are in flight' in printed, printed


@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_URL': 'localhost:6379/0'})
def test_a_url_with_no_scheme_reads_as_an_unreachable_redis():
    """The other half of the empty-URL case, and the one the narrowing missed.

    `Redis.from_url` rejects a URL without a scheme with a plain `ValueError`, so
    catching `RedisError` and `ImproperlyConfigured` still left this one as a traceback
    from both entry points — while `except Exception` had always reported it as a line.
    A non-numeric `REDIS_TIMEOUT` arrives the same way.
    """
    report = check()

    assert not report.ok
    assert report.message.startswith('redis is unreachable: '), report.message

    with pytest.raises(CommandError, match='redis is unreachable'):
        healthcheck()


@override_settings(TELEGRAM_BOT={**SETTINGS, 'REDIS_TIMEOUT': 'soon'})
def test_an_unreadable_timeout_reads_as_an_unreachable_redis():
    """`read_timeout()` coerces the setting while the client is being built."""
    report = check()

    assert not report.ok
    assert report.message.startswith('redis is unreachable: '), report.message


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_heartbeat_that_cannot_be_decoded_is_reported(redis_server, monkeypatch):
    """`decode_responses` in a shared URL makes redis-py decode this key for us.

    The settings page says one Redis often serves the cache backend too, and that is how
    `decode_responses` gets into the URL. The bytes under the heartbeat key are then
    decoded by the client, and a `UnicodeDecodeError` is a `ValueError` — not a
    `RedisError`, so narrowing the guard turned a readable refusal into a traceback.
    """

    class CannotDecode:
        def ping(self):
            return True

        def get(self, *args, **kwargs):
            b'\xff'.decode('utf-8')

        def __getattr__(self, name):
            return getattr(redis_server, name)

    monkeypatch.setattr('django_redis_aiogram.healthcheck.get_redis', CannotDecode)

    with pytest.raises(CommandError, match='could not read the heartbeat'):
        healthcheck()


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
    assert calls == [], f'the container form paid for {sorted(set(calls))}'
    # the whole line, not `'once' not in report.message`: `_guarantee` answers `unknown`
    # on a read-only replica, so that substring is absent from a probe that did run
    assert report.message == 'healthy: heartbeat 0s old, 0 queued', report.message
    assert report.warnings == (), report.warnings


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_the_management_command_still_scans_and_reports_the_guarantee(redis_server, monkeypatch):
    """The other half: the command's output must not change for anyone using it.

    Without this, moving the defaults to off would silently take the guarantee line and
    the stranded warning out of a command people read by hand — which is the sort of
    quiet removal a changelog entry cannot make up for. Both halves are asserted on the
    output as well as on the calls, because a `SCAN` that is issued and then not reported
    is the same loss to the person reading it.
    """
    redis_server.rpush(f'{QUEUE}:processing:gone', b'{}')
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
    assert '1 message(s) are in flight' in out.getvalue(), out.getvalue()
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


@override_settings(TELEGRAM_BOT=SETTINGS)
def test_a_healthy_process_is_reported_in_success_green(redis_server):
    """The other half of the distinction `Report.checked` exists to carry.

    Without this the styling could be dropped from the command outright and the suite
    would stay green — the disabled case asserts only that *its* line is plain, which is
    also true of a command that has stopped colouring anything. Then `checked` becomes a
    field nothing reads.
    """
    redis_server.set(HEARTBEAT, str(int(time.time())))
    out = StringIO()

    call_command('tgbot_healthcheck', stdout=out, force_color=True)

    assert 'healthy: heartbeat' in out.getvalue(), out.getvalue()
    assert '\x1b[' in out.getvalue(), 'the healthy line lost its success colour'


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEARTBEAT_INTERVAL': 10})
def test_the_missing_heartbeat_message_names_the_limit_it_judged_by(redis_server):
    """`--max-age 600` and a message saying "within 30s" send an operator to the wrong
    number — and the one it named was a default the flag had already overridden."""
    report = check(max_age=600)

    assert not report.ok
    assert 'within 600s' in report.message, report.message
    assert 'within 30s' not in report.message, report.message


def test_a_probe_with_no_settings_module_says_so_instead_of_raising(monkeypatch, capsys):
    """The one failure this form meets that the management command cannot.

    `manage.py` sets `DJANGO_SETTINGS_MODULE` with `os.environ.setdefault` *inside its
    own process*, and a healthcheck is a different process — so a container that runs
    `manage.py` may never export it, and the recipe on the Deployment page has to put it
    in `environment:`. Without it the probe used to answer with a traceback: exit 1, so
    Docker read unhealthy, from a probe whose whole job is to say *why*.
    """

    def unreadable(*args, **kwargs):
        message = (
            'Requested setting TELEGRAM_BOT, but settings are not configured. You must '
            'either define the environment variable DJANGO_SETTINGS_MODULE or call '
            'settings.configure() before accessing settings.'
        )
        raise ImproperlyConfigured(message)

    monkeypatch.setattr('django_redis_aiogram.healthcheck.check', unreadable)

    code = main([])

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == '', captured.out
    assert captured.err.startswith('cannot read the settings: '), captured.err
    assert 'DJANGO_SETTINGS_MODULE' in captured.err


@pytest.mark.parametrize('flag', ['--max-queue', '--max-age'])
def test_both_entry_points_declare_the_same_limits(flag):
    """One form silently defaulting differently from the other is the drift to prevent.

    Both take these from `add_limit_flags`, so this fails the moment either restates a
    flag: a reader comparing `--help` of the two forms is entitled to the same answer,
    and `check()` owns what the default means.
    """
    command = TgbotHealthcheck().create_parser('manage.py', 'tgbot_healthcheck')

    module_action = next(a for a in build_parser()._actions if flag in a.option_strings)
    command_action = next(a for a in command._actions if flag in a.option_strings)

    assert module_action.default is command_action.default is None
    assert module_action.type is command_action.type is int
    assert module_action.help == command_action.help


def test_a_probe_with_a_mistyped_settings_module_says_so_instead_of_raising(monkeypatch, capsys):
    """The recipe asks an operator to write that module name by hand.

    A typo is then at least as likely as a missing variable, and it arrives as
    `ModuleNotFoundError` rather than `ImproperlyConfigured` — so guarding only the
    latter left the likelier mistake answering with a traceback. Raised with `name=`,
    which is what the import machinery sets and what tells this apart from the case
    below; the first version of this test left it unset and so asserted on a shape no
    real failure has.
    """

    def mistyped(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'core.settingz'", name='core.settingz')

    monkeypatch.setenv('DJANGO_SETTINGS_MODULE', 'core.settingz')
    monkeypatch.setattr('django_redis_aiogram.healthcheck.check', mistyped)

    code = main([])

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == '', captured.out
    assert captured.err == "cannot read the settings: No module named 'core.settingz'\n", captured.err


def test_a_dependency_the_settings_module_imports_keeps_its_traceback(monkeypatch):
    """Our own failure is ours to summarise into one line. This one is not.

    A settings module that imports something uninstalled raises `ModuleNotFoundError`
    too, and flattening it would report `cannot read the settings: No module named 'yaml'`
    with no hint of where the import was — for a fault that is not the healthcheck's and
    is not fixed by anything on the Deployment page.
    """

    def missing_dependency(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'yaml'", name='yaml')

    monkeypatch.setenv('DJANGO_SETTINGS_MODULE', 'core.settings')
    monkeypatch.setattr('django_redis_aiogram.healthcheck.check', missing_dependency)

    with pytest.raises(ModuleNotFoundError, match='yaml'):
        main([])


def test_a_settings_module_whose_parent_package_is_missing_is_still_ours(monkeypatch, capsys):
    """`DJANGO_SETTINGS_MODULE=coree.settings` reports the missing *parent*.

    So comparing the name for equality alone would send the commonest typo of all — one
    in the package part — out as a traceback.
    """

    def missing_parent(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'coree'", name='coree')

    monkeypatch.setenv('DJANGO_SETTINGS_MODULE', 'coree.settings')
    monkeypatch.setattr('django_redis_aiogram.healthcheck.check', missing_parent)

    assert main([]) == 1
    assert capsys.readouterr().err == "cannot read the settings: No module named 'coree'\n"


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_a_key_that_cannot_be_decoded_does_not_abort_the_sweep(redis_server, caplog):
    """The sweep is the one part that must never fail the container over what it found.

    A foreign key on a Redis shared with a cache backend can match this pattern and hold
    bytes that are not UTF-8. Decoding it raised straight out of `check()` — a traceback
    and an unhealthy container, from an optional warning nobody acts on.
    """
    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))
    redis_server.rpush(f'{QUEUE}:processing:'.encode() + b'\xff\xfe', b'{}')

    report = check(stranded=True)

    assert report.ok, report.message
    assert 'healthy' in report.message, report.message
    assert 'could not scan for stranded in-flight lists' in caplog.text


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_the_verdict_is_flushed_before_anything_qualifies_it(redis_server, monkeypatch):
    """`docker inspect` merges the two streams, and stdout is the buffered one.

    Written to a pipe, stdout is block-buffered while stderr is not, so an unflushed
    verdict arrives *after* the warning about it — the qualifier reaching the operator
    ahead of the thing it qualifies.
    """
    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))
    redis_server.rpush(f'{QUEUE}:processing:gone', b'{}')
    events = []

    class Recording:
        def __init__(self, name):
            self.name = name

        def write(self, text):
            events.append((self.name, 'write'))

        def flush(self):
            events.append((self.name, 'flush'))

    monkeypatch.setattr('sys.stdout', Recording('out'))
    monkeypatch.setattr('sys.stderr', Recording('err'))

    assert main(['--stranded']) == 0

    assert events[0] == ('out', 'write'), events
    assert ('err', 'write') in events, events
    assert events.index(('out', 'flush')) < events.index(('err', 'write')), events


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEARTBEAT_INTERVAL': 'abc'})
def test_an_unreadable_interval_is_refused_in_a_line():
    """This entry point is the one where `manage.py check` never ran.

    `E023` and `E024` catch these values — under `django.setup()`, which the container
    form skips by design. A value like `os.environ.get('HB', '')` written straight into
    the settings dict therefore reaches `int()` with nothing in between, and a traceback
    from a probe is a container that says unhealthy without saying why.
    """
    report = check()

    assert not report.ok
    assert report.message == "TELEGRAM_BOT['HEARTBEAT_INTERVAL'] is not a number: 'abc'", report.message


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEALTHCHECK_MAX_QUEUE': 'lots'})
def test_an_unreadable_queue_limit_is_refused_in_a_line():
    """The other one read before Redis is touched. A flag still overrides it."""
    report = check()

    assert not report.ok
    assert 'HEALTHCHECK_MAX_QUEUE' in report.message, report.message


@override_settings(TELEGRAM_BOT={**SETTINGS, 'HEARTBEAT_INTERVAL': 10})
def test_a_limit_over_the_keys_ttl_says_it_cannot_be_observed(redis_server):
    """`--max-age 60` against a key that expires at 30s can never see a stale heartbeat.

    The Deployment page published exactly that recipe. Raising the limit past the TTL does
    nothing at all unless `HEARTBEAT_INTERVAL` goes up too, and the refusal is the only
    place an operator is looking when they find out.
    """
    over = check(max_age=60)
    within = check(max_age=20)

    assert not over.ok, over.message
    assert not within.ok, within.message
    assert 'cannot be observed anyway' in over.message, over.message
    assert "raise TELEGRAM_BOT['HEARTBEAT_INTERVAL'] instead" in over.message, over.message
    assert 'cannot be observed' not in within.message, within.message


@override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine'})
def test_the_sweep_matches_the_keys_workers_actually_write(redis_server):
    """Derived from `processing_key`, not spelled out a second time.

    A probe holding its own copy of the scheme keeps scanning the old one after a rename:
    every failing test would be one that hardcodes the literal, so updating those is how
    the rename gets done — and the probe reports no stranded messages for ever, green.
    """
    from django_redis_aiogram.redis import processing_key

    redis_server.set(f'{QUEUE}:heartbeat:mine', str(int(time.time())))
    redis_server.rpush(processing_key('gone'), b'{}')

    report = check(stranded=True)

    assert report.ok, report.message
    assert len(report.warnings) == 1, report.warnings
    assert '1 message(s) are in flight' in report.warnings[0], report.warnings


# one key per metacharacter `_escaped` quotes, and a decoy for the two that would
# otherwise match their own literal: unescaped, `TG?one` and `TG*all` are patterns that
# also select the decoy, and only a second list makes that visible
@pytest.mark.parametrize(
    ('key', 'decoy'),
    [
        ('TG_OK', None),
        ('TG[prod]', None),
        ('TG?one', 'TGXone'),
        ('TG*all', 'TGXall'),
        ('TG\\path', None),
    ],
)
def test_a_queue_key_with_glob_characters_is_still_swept(redis_server, key, decoy):
    """`SCAN MATCH` takes a glob and a queue key is an operator's string.

    `REDIS_MESSAGES_KEY = 'tg[staging]'` became a character class matching nothing this
    package writes, so the sweep found zero stranded lists — and called the scan
    *complete*, which is the one answer a wrong pattern must not give.

    Missing it in the other direction is just as wrong, and needs the decoy: unescaped,
    `TG?one` and `TG*all` still match the key they came from, so those two cases passed
    against the unescaped version. With a foreign list one character away, the unescaped
    pattern collects it and the count says so — a queue reporting another queue's
    messages as its own in flight.
    """
    with override_settings(TELEGRAM_BOT={**SETTINGS, 'WORKER_NAME': 'mine', 'REDIS_MESSAGES_KEY': key}):
        from django_redis_aiogram.healthcheck import _stranded
        from django_redis_aiogram.redis import processing_key

        redis_server.rpush(processing_key('gone'), b'{}')
        if decoy:
            redis_server.rpush(f'{decoy}:processing:gone', b'{}')

        assert _stranded(redis_server) == (1, True), f'the sweep answered wrongly under {key!r}'
