"""Answer whether the bot container is doing its job, without booting Django.

``manage.py tgbot_healthcheck`` has always been correct and could not be used: a
management command runs ``django.setup()`` first, which populates the app registry
and executes every ``AppConfig.ready()`` in the *host* project. Measured in one
consumer — Django 5.2, twenty apps, one registering adapters in ``ready()`` — that
was 17.89s on top of 2.45s for the settings module, against ~0.01s for the probe's
own three Redis calls. Docker killed it at every timeout, and the container read
``unhealthy`` for the best part of an hour while the bot was fine and its heartbeat
six seconds old.

So this module is the check, and both entry points are thin:

* ``python -m django_redis_aiogram.healthcheck`` — for a container healthcheck. It
  reads ``DJANGO_SETTINGS_MODULE`` the way any Django code does and never calls
  ``django.setup()``.
* ``manage.py tgbot_healthcheck`` — unchanged, because consumers have it in compose
  files today. It pays for ``django.setup()`` like every other command, and the
  Deployment page says why you would not put it in a healthcheck.

**This module must not import anything that needs the app registry.** No models, no
aiogram, no :mod:`django_redis_aiogram.client`. Reading
``django.conf.settings.TELEGRAM_BOT`` imports the settings module and nothing more,
which is the whole saving. ``tests/test_lazy_init.py`` asserts the registry is still
unpopulated after ``main()`` returns.
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured
from redis import Redis
from redis.exceptions import RedisError, ResponseError

from django_redis_aiogram.redis import (
    get_redis,
    heartbeat_key,
    heartbeat_ttl,
    processing_key,
    processing_pattern,
    queue_key,
)
from django_redis_aiogram.settings import SETTINGS_NAME, coerce_bool, conf

logger = logging.getLogger('django_redis_aiogram')

# round trips, not keys: MATCH filters on the server but SCAN walks the whole
# keyspace either way, and this probe runs on a timer
STRANDED_SCAN_ROUNDS = 20


@dataclass(frozen=True)
class Report:
    """What the probe concluded: the verdict, the line to print, anything else to say.

    ``warnings`` is separate from ``message`` because a warning must never change the
    verdict — a stranded in-flight list may be one another worker is sending this
    second, and an exit code that said otherwise would restart a healthy container.
    """

    ok: bool
    message: str
    warnings: tuple[str, ...] = ()
    #: whether anything was actually examined. False only when this process is
    #: disabled, which is not a verdict about the bot — and is why the management
    #: command reports that one plainly rather than in success green, as it always has
    checked: bool = True


class _UnhealthyError(Exception):
    """One reason the probe is about to answer no.

    Private, and raised only between the helpers below and :func:`check`, which turns
    it back into a :class:`Report`. It exists because the check is a sequence of
    reads where any one of them ends the answer — written as early returns, that was
    twelve branches in one function and the reason for each was harder to see than the
    control flow around it.
    """


def _setting_int(key: str) -> int:
    """Read one integer setting, refusing in a line rather than a traceback.

    `E023` and `E024` say this in ``manage.py check`` — which runs under
    ``django.setup()``, the thing this entry point exists to skip. So this is the one path
    where a value like ``os.environ.get('HB', '')`` written straight into the settings
    dict reaches ``int()`` with nothing between, and the probe is what has to say so.
    """
    raw = conf[key]
    try:
        return int(raw)
    except (TypeError, ValueError) as error:
        msg = f"{SETTINGS_NAME}['{key}'] is not a number: {raw!r}"
        raise _UnhealthyError(msg) from error


def _connected() -> Redis:
    """Return the shared connection, having proved it answers.

    Two non-Redis failures are caught beside ``RedisError``, because from a probe's
    point of view a connection it cannot build is a Redis it cannot reach — which is
    what this command has always said about it, back when it caught ``Exception``:

    * ``ImproperlyConfigured`` — an empty ``REDIS_URL``, which
      :func:`~django_redis_aiogram.redis.build_client` raises on.
    * ``ValueError`` — a ``REDIS_URL`` with no scheme, which ``Redis.from_url`` rejects,
      and a non-numeric ``REDIS_TIMEOUT``, which ``read_timeout()`` does. Both are
      misconfiguration reaching us as a builtin, and both used to read as one line.

    Narrowing to ``RedisError`` alone turned those into tracebacks and, in the management
    command, into a bare exception where a ``CommandError`` belongs.
    """
    try:
        connection = get_redis()
        connection.ping()
    except (RedisError, ImproperlyConfigured, ValueError) as error:
        msg = f'redis is unreachable: {error}'
        raise _UnhealthyError(msg) from error
    return connection


def _heartbeat_age(connection: Redis, *, limit: int, ttl: int) -> int:
    """How long ago the consumer last said it was turning.

    Both numbers are needed: ``limit`` is what the verdict is judged by, and ``ttl`` is
    what the key can actually show. A limit above the TTL is unreachable — the key is
    gone by then — so the refusal names that rather than leaving an operator to wonder
    why ``--max-age 600`` still refused at a minute.
    """
    try:
        raw = connection.get(heartbeat_key())
    except (RedisError, UnicodeDecodeError) as error:
        # ping answering says nothing about the next command: a failover in between, or a
        # key this replica cannot serve. The decode error is not hypothetical either —
        # `decode_responses` in a URL shared with a cache backend makes redis-py decode
        # this key, and the bytes there are not ours to promise anything about
        msg = f'could not read the heartbeat: {error}'
        raise _UnhealthyError(msg) from error
    if raw is None:
        # the applied limit, not the TTL: with `--max-age 600` the old wording said
        # "within 30s", which contradicts the number the probe is judging by
        msg = (
            f'no heartbeat at {heartbeat_key()}: the consumer has not written one within {limit}s, or it never started'
        )
        if limit > ttl:
            msg = (
                f'{msg}. A limit over {ttl}s cannot be observed anyway, because the key expires then: '
                f"raise {SETTINGS_NAME}['HEARTBEAT_INTERVAL'] instead"
            )
        raise _UnhealthyError(msg)
    try:
        age = int(time.time()) - int(raw)
    except (TypeError, ValueError) as error:
        msg = f'the heartbeat at {heartbeat_key()} is not a timestamp'
        raise _UnhealthyError(msg) from error
    if age > limit:
        msg = f'the consumer last reported {age}s ago, over the {limit}s limit'
        raise _UnhealthyError(msg)
    return age


def _queue_depth(connection: Redis, *, limit: int) -> int:
    """How many messages are waiting, refusing when that is over the limit."""
    try:
        queued = int(connection.llen(queue_key()) or 0)
    except RedisError as error:
        msg = f'could not read the queue length: {error}'
        raise _UnhealthyError(msg) from error
    if limit and queued > limit:
        msg = f'{queued} messages are queued, over the limit of {limit}'
        raise _UnhealthyError(msg)
    return queued


def check(
    *,
    max_queue: int | None = None,
    max_age: int | None = None,
    stranded: bool = False,
    guarantee: bool = False,
) -> Report:
    """Read Redis, the consumer's heartbeat and the queue length, in that order.

    ``max_age`` defaults to the heartbeat key's own TTL — three ``HEARTBEAT_INTERVAL``s,
    so one missed refresh is not a failure — which is also the most any reader can
    observe; ``max_queue`` to ``HEALTHCHECK_MAX_QUEUE``, where 0 disables the limit.

    ``stranded`` and ``guarantee`` are off by default and the management command turns
    them on, which is the one place the two entry points differ. Both cost more than
    everything else here and neither can change the verdict: the scan is up to twenty
    ``SCAN`` rounds over a keyspace often shared with a cache backend, and the
    guarantee probe is a write — a no-op ``LMOVE`` on a missing key, but still a write,
    and on a read-only replica it answers ``unknown`` rather than the truth. Twice a
    minute for a line nobody acts on is the wrong trade for a container healthcheck and
    the right one for a command a person ran deliberately.
    """
    if not coerce_bool(conf['ENABLED'], f"{SETTINGS_NAME}['ENABLED']"):
        # nothing is meant to be running here, so nothing is wrong
        return Report(ok=True, message='disabled in this process; nothing to check', checked=False)

    try:
        # inside the guard: these two are read before Redis is touched, so an
        # unreadable one is the first thing the probe meets rather than the last
        ttl = heartbeat_ttl(max(1, _setting_int('HEARTBEAT_INTERVAL')))
        age_limit = ttl if max_age is None else max_age
        queue_limit = _setting_int('HEALTHCHECK_MAX_QUEUE') if max_queue is None else max_queue
        connection = _connected()
        age = _heartbeat_age(connection, limit=age_limit, ttl=ttl)
        queued = _queue_depth(connection, limit=queue_limit)
    except _UnhealthyError as refusal:
        return Report(ok=False, message=str(refusal))

    healthy = f'healthy: heartbeat {age}s old, {queued} queued'
    if guarantee:
        healthy = f'{healthy}, {_guarantee(connection)}'
    warnings: list[str] = []
    if stranded:
        found, swept = _stranded(connection)
        if found:
            # not a failure: another worker may be sending them right now. But an
            # invisible pile is how a stranded list stays stranded
            warnings.append(
                f'{found if swept else f"at least {found}"} message(s) are in flight under '
                'other worker names. If one of those workers is gone, '
                '`manage.py tgbot_reclaim --worker <name>` requeues them.'
            )
    return Report(ok=True, message=healthy, warnings=tuple(warnings))


def _guarantee(connection: Redis) -> str:
    """Which delivery guarantee this Redis can actually give.

    Asked of the server, not of a consumer: ``Delivery.crash_safe`` starts true and is
    only lowered by ``reclaim()``, which this probe must never call — requeueing a
    running worker's in-flight list would send those messages twice.

    It asks the same question ``reclaim()`` does, on a key that does not exist:
    rotating an empty list is a no-op on a server that has ``LMOVE``, and
    ``unknown command`` on one that does not.

    ``COMMAND INFO lmove`` would be a read rather than a write, and does not work:
    against Redis 6.0 — the servers where the answer actually differs — redis-py's own
    response parser raises ``TypeError`` on the nil entry the server returns for an
    unknown command. Catching a library's parser failing in a particular way is a
    worse dependency than a no-op write, so the write stays and the *caller* decides
    whether to pay for it.
    """
    probe = f'{queue_key()}:lmove-probe'
    try:
        connection.lmove(probe, probe, 'LEFT', 'RIGHT')
    except ResponseError as error:
        if 'unknown command' in str(error).lower():
            return 'at-most-once'
        logger.warning('could not establish which delivery guarantee is in force')
        return 'unknown'
    except RedisError:
        logger.warning('could not establish which delivery guarantee is in force')
        return 'unknown'
    return 'at-least-once'


def _stranded(connection: Redis) -> tuple[int, bool]:
    """Count what is in flight under a worker name that is not this one.

    Read rather than acted on: a message under another name may be one another worker
    is sending this second, and taking it back would send it twice.

    Bounded, and returns whether it finished. ``MATCH`` filters on the server but
    ``SCAN`` still walks the whole keyspace, and on a Redis shared with a cache
    backend — which the settings page suggests is common — an unbounded sweep is a full
    pass over someone else's keys. A partial answer is worth having; one that pretends
    to be complete is not.
    """
    pattern = processing_pattern()
    mine = processing_key()
    # SCAN may return the same key more than once when the keyspace changes
    # size mid-iteration, and counting one twice would invent a backlog
    seen: set[str] = set()
    total = 0
    cursor = 0
    try:
        for _ in range(STRANDED_SCAN_ROUNDS):
            cursor, keys = connection.scan(cursor=cursor, match=pattern, count=100)
            for key in keys:
                name = key.decode('utf-8') if isinstance(key, bytes) else str(key)
                if name == mine or name in seen:
                    continue
                seen.add(name)
                total += int(connection.llen(name) or 0)
            if cursor == 0:
                return total, True
    except (RedisError, UnicodeDecodeError):
        # the decode too: a foreign key on a shared Redis can match this pattern and hold
        # bytes that are not UTF-8, and aborting the whole probe over a warning nobody
        # acts on is the opposite of what this sweep is for
        logger.warning('could not scan for stranded in-flight lists', extra={'tg_key': pattern})
        return total, False
    return total, False


def add_limit_flags(parser: argparse.ArgumentParser) -> None:
    """Declare the two limits on any parser, so the entry points cannot drift.

    The management command adds them to the parser Django hands it, which is why this
    takes one rather than making its own: the flags, their types and their defaults are
    a property of :func:`check`, and a reader comparing ``--help`` of the two forms is
    entitled to the same answer from both.
    """
    parser.add_argument(
        '--max-queue',
        type=int,
        default=None,
        help=f"messages allowed to be waiting; defaults to {SETTINGS_NAME}['HEALTHCHECK_MAX_QUEUE'], 0 disables",
    )
    parser.add_argument(
        '--max-age',
        type=int,
        default=None,
        help=(
            f"seconds the heartbeat may be stale; defaults to three {SETTINGS_NAME}['HEARTBEAT_INTERVAL']s, "
            "which is also the key's TTL and so the most that can be observed"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Declare the container form's flags: the shared limits, plus what it leaves off."""
    parser = argparse.ArgumentParser(
        prog='python -m django_redis_aiogram.healthcheck',
        description='Exit 0 when the bot container is healthy, non-zero with a reason otherwise',
    )
    add_limit_flags(parser)
    parser.add_argument(
        '--stranded',
        action='store_true',
        help=(f'also scan for in-flight lists left by other worker names (up to {STRANDED_SCAN_ROUNDS} SCAN rounds)'),
    )
    parser.add_argument(
        '--guarantee',
        action='store_true',
        help='also report which delivery guarantee this Redis gives (issues a no-op write)',
    )
    return parser


def _names_the_settings_module(error: ImportError) -> bool:
    """Whether what failed to import is the configured settings module itself.

    ``ModuleNotFoundError`` carries the dotted name it could not find, and a missing
    parent package reports the parent — so ``core.settings`` with no ``core`` on the path
    is recognised too.
    """
    missing = getattr(error, 'name', None)
    configured = os.environ.get('DJANGO_SETTINGS_MODULE')
    if not missing or not configured:
        return False
    return configured == missing or configured.startswith(f'{missing}.')


def _cannot_read(error: Exception) -> int:
    """Write the one-line refusal and give the exit code that goes with it."""
    sys.stderr.write(f'cannot read the settings: {error}\n')
    return 1


def main(argv: list[str] | None = None) -> int:
    """Run the check and print its report. Returns the exit code.

    Nothing here calls ``django.setup()``, which is the point of the module. Reading a
    setting still needs ``DJANGO_SETTINGS_MODULE`` in the environment, which a container
    running ``manage.py`` does not necessarily have — see the refusal below.
    """
    options = build_parser().parse_args(argv)
    try:
        report = check(
            max_queue=options.max_queue,
            max_age=options.max_age,
            stranded=options.stranded,
            guarantee=options.guarantee,
        )
    except ImproperlyConfigured as error:
        # the failure this form meets that the management command cannot: `manage.py` sets
        # DJANGO_SETTINGS_MODULE inside its own process, so a container running it does not
        # necessarily export the variable — and a healthcheck is a separate process. A
        # traceback here would say "unhealthy" without saying why, from a probe whose whole
        # job is to say why
        return _cannot_read(error)
    except ImportError as error:
        # the recipe asks an operator to write that module name by hand, so a mistyped one
        # is at least as likely as a missing variable, and it deserves the same line. Only
        # that one: an import the settings module itself fails at is not ours to flatten,
        # and its traceback is where the answer is
        if not _names_the_settings_module(error):
            raise
        return _cannot_read(error)
    stream = sys.stdout if report.ok else sys.stderr
    stream.write(f'{report.message}\n')
    # stdout is block-buffered when it is not a tty and stderr is not buffered at all, so
    # without this the warnings reach a merged `docker inspect` log before the verdict
    # they qualify
    stream.flush()
    for warning in report.warnings:
        sys.stderr.write(f'{warning}\n')
    return 0 if report.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
