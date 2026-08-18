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
import sys
import time
from dataclasses import dataclass, field

from django.core.exceptions import ImproperlyConfigured
from redis import Redis
from redis.exceptions import RedisError, ResponseError

from django_redis_aiogram.redis import get_redis, heartbeat_key, processing_key, queue_key
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
    warnings: tuple[str, ...] = field(default_factory=tuple)
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


def _connected() -> Redis:
    """Return the shared connection, having proved it answers.

    ``ImproperlyConfigured`` is caught beside ``RedisError`` because an empty
    ``REDIS_URL`` is what :func:`~django_redis_aiogram.redis.build_client` raises on,
    and from a probe's point of view a connection it cannot build is a Redis it cannot
    reach — which is what this command has always said about it. Narrowing to
    ``RedisError`` alone turned that readable line into a traceback and, in the
    management command, into an ``ImproperlyConfigured`` where a ``CommandError``
    belongs.
    """
    try:
        connection = get_redis()
        connection.ping()
    except (RedisError, ImproperlyConfigured) as error:
        msg = f'redis is unreachable: {error}'
        raise _UnhealthyError(msg) from error
    return connection


def _heartbeat_age(connection: Redis, *, interval: int, limit: int) -> int:
    """How long ago the consumer last said it was turning."""
    try:
        raw = connection.get(heartbeat_key())
    except RedisError as error:
        # ping answering says nothing about the next command: a failover in
        # between, or a key this replica cannot serve
        msg = f'could not read the heartbeat: {error}'
        raise _UnhealthyError(msg) from error
    if raw is None:
        msg = (
            f'no heartbeat at {heartbeat_key()}: the consumer has not written one '
            f'within {interval * 3}s, or it never started'
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

    ``max_age`` defaults to three ``HEARTBEAT_INTERVAL``s, so one missed refresh is
    not a failure; ``max_queue`` to ``HEALTHCHECK_MAX_QUEUE``, where 0 disables the
    limit.

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

    interval = max(1, int(conf['HEARTBEAT_INTERVAL']))
    age_limit = interval * 3 if max_age is None else max_age
    queue_limit = int(conf['HEALTHCHECK_MAX_QUEUE']) if max_queue is None else max_queue

    try:
        connection = _connected()
        age = _heartbeat_age(connection, interval=interval, limit=age_limit)
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
    pattern = f'{queue_key()}:processing:*'
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
    except RedisError:
        logger.warning('could not scan for stranded in-flight lists', extra={'tg_key': pattern})
        return total, False
    return total, False


def build_parser() -> argparse.ArgumentParser:
    """Declare the flags, shared with the management command so the two cannot drift."""
    parser = argparse.ArgumentParser(
        prog='python -m django_redis_aiogram.healthcheck',
        description='Exit 0 when the bot container is healthy, non-zero with a reason otherwise',
    )
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
        help=f"seconds the heartbeat may be stale; defaults to three {SETTINGS_NAME}['HEARTBEAT_INTERVAL']s",
    )
    parser.add_argument(
        '--stranded',
        action='store_true',
        help='also scan for in-flight lists left by other worker names (up to 20 SCAN rounds)',
    )
    parser.add_argument(
        '--guarantee',
        action='store_true',
        help='also report which delivery guarantee this Redis gives (issues a no-op write)',
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the check and print its report. Returns the exit code.

    Nothing here calls ``django.setup()``, which is the point of the module. Reading a
    setting still needs ``DJANGO_SETTINGS_MODULE``, and a container that runs
    ``manage.py`` already has it.
    """
    options = build_parser().parse_args(argv)
    report = check(
        max_queue=options.max_queue,
        max_age=options.max_age,
        stranded=options.stranded,
        guarantee=options.guarantee,
    )
    stream = sys.stdout if report.ok else sys.stderr
    stream.write(f'{report.message}\n')
    for warning in report.warnings:
        sys.stderr.write(f'{warning}\n')
    return 0 if report.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
