"""The one Redis connection this package shares.

Senders, consumers, the FSM heartbeat and the management commands all go through
:func:`get_redis`, so a process opens a single connection pool however many of
them are running. The connection is built on first use rather than at import,
because Django settings are not readable while the app registry is loading.
"""

import asyncio
import threading
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.connection import parse_url as _parse_url

from django_redis_aiogram.events import worker_identity
from django_redis_aiogram.settings import SETTINGS_NAME, conf

#: redis-py ships py.typed but leaves parse_url unannotated, and strict mode refuses
#: to call it. Naming the shape here keeps the call site honest without an ignore
parse_url: Callable[[str], dict[str, Any]] = _parse_url


def read_timeout() -> int:
    """How long any single Redis call may take before the server is dead to us."""
    return max(1, int(conf['REDIS_TIMEOUT']))


def connection_kwargs() -> dict[str, Any]:
    """How every client this package builds is configured.

    Its own, rather than a detail of :func:`build_client`, because the FSM storage
    builds a second client that this package never touches again — and it went
    without any deadline at all until it was handed these.

    Note that redis-py resolves a URL *after* keyword arguments and documents that
    "querystring arguments always win", so anything here is a default a project can
    still override with a query string on ``REDIS_URL``.
    """
    timeout = read_timeout()
    return {'socket_connect_timeout': timeout, 'socket_timeout': timeout}


def queue_key() -> str:
    """Return the list queued messages are written to and read from."""
    return str(conf['REDIS_MESSAGES_KEY'])


def processing_key(worker: str | None = None) -> str:
    """Where one worker keeps the message it is sending.

    Per worker, so a restarting one reclaims only its own: a shared list would let
    a starting worker pull a message out from under another that is still sending
    it. Takes a name so `tgbot_reclaim` can address a worker that is gone.
    """
    return f'{queue_key()}:processing:{worker or worker_identity()}'


def processing_pattern() -> str:
    """Match every worker's in-flight list, this one included.

    Derived from :func:`processing_key` rather than spelled out again: the healthcheck
    scans for these, and a probe that re-wrote the scheme by hand would keep scanning the
    old one after a rename — silently reporting no stranded messages for ever, with the
    suite green because the tests that hold the literal would have been updated.

    The key is escaped because ``SCAN MATCH`` takes a glob, and a queue key is an operator's
    string: ``REDIS_MESSAGES_KEY = 'tg[staging]'`` turned into a character class that
    matches nothing this package ever writes, so the sweep reported zero stranded lists —
    and reported the scan as *complete*, which is the one answer a wrong pattern must not
    give. Only the key is escaped; the ``*`` is the wildcard this function exists for.
    """
    return f'{_escaped(queue_key())}:processing:*'


def _escaped(literal: str) -> str:
    """Quote the glob metacharacters Redis honours in a ``MATCH`` pattern.

    ``^`` is in the set and cannot change an outcome here, which is worth writing down
    rather than rediscovering: it is special only as the first character inside an
    *unescaped* ``[...]``, and ``[`` is always escaped one line above. Measured both ways
    against ``TG[^x]`` and ``TG^x`` — same match, escaped or not. Kept because the set is
    the one Redis documents, and a future caller that builds a class deliberately would
    want it; not covered by a test, because no test could fail.
    """
    return ''.join(f'\\{character}' if character in '*?[]^\\' else character for character in literal)


def heartbeat_key(worker: str | None = None) -> str:
    """Where one worker says it is still turning. Per worker, like the list above."""
    return f'{queue_key()}:heartbeat:{worker or worker_identity()}'


def heartbeat_interval() -> int:
    """How often the consumer refreshes that key, never below a second."""
    return max(1, int(conf['HEARTBEAT_INTERVAL']))


def heartbeat_ttl(interval: int | None = None) -> int:
    """How long the key survives without a refresh: three intervals.

    One place, because it is also a ceiling on what any reader can observe. A probe given
    `--max-age` above this can never see a stale heartbeat — the key is simply gone — so
    the two numbers have to be derived from each other rather than agreed on by hand in
    the writer, the probe and three pages of the wiki. Takes an interval so a caller that
    has already read and vetted the setting does not read it twice.
    """
    return (heartbeat_interval() if interval is None else interval) * 3


def build_client() -> Redis:
    """Build a client bounded in time, so no call can hang for ever.

    redis-py only started defaulting to a read deadline in 8.0; on the 6.2 floor
    a server that accepts the connection and then stops answering blocks the
    caller until the process is killed. Blocking reads stay inside the deadline
    by asking for less than it — see :class:`~django_redis_aiogram.delivery.BlpopDelivery`.

    Commands are deliberately **not** retried. ``Redis.from_url`` builds the pool
    first, so redis-py's client-level retry default never reaches the connection
    and every command runs with ``Retry(NoBackoff(), 0)``; that was an accident,
    and this docstring is what makes it a decision. Neither command on the hot path
    is idempotent — a connection dropped after the server applied an ``RPUSH`` but
    before the reply arrived would queue the message twice, and the consumer would
    send a real person two of them. The connection-drop case is already handled
    where it can be handled safely: the consumer logs and goes round its loop
    again, and a failed ``send_redis`` records the drop and raises so the caller
    knows nothing was queued.
    """
    url = conf['REDIS_URL']
    if not url:
        msg = f"{SETTINGS_NAME}['REDIS_URL'] is required to talk to Redis."
        raise ImproperlyConfigured(msg)
    return Redis.from_url(url, **connection_kwargs())


def build_async_client() -> AsyncRedis:
    """Build the same client for a caller that is already on an event loop.

    Same URL, same deadlines from :func:`connection_kwargs`, same deliberate lack
    of retries — everything :func:`build_client` says applies here too, and
    ``redis.asyncio`` ships inside redis-py, so this needs no extra dependency.

    What differs is ownership: these connections are **loop-affine**, so one
    client cannot be shared the way the synchronous one is. :func:`aget_redis`
    keeps one per loop.
    """
    url = conf['REDIS_URL']
    if not url:
        msg = f"{SETTINGS_NAME}['REDIS_URL'] is required to talk to Redis."
        raise ImproperlyConfigured(msg)
    return AsyncRedis.from_url(url, **connection_kwargs())


def url_decodes_responses(url: str) -> bool:
    """Whether ``url`` asks redis-py to hand back ``str`` instead of ``bytes``.

    Tolerated everywhere else — :func:`as_bytes` exists for it, because one
    ``REDIS_URL`` is often shared with a cache backend that wants decoding — but
    pickled payloads cannot survive it, so check E043 refuses that one pairing.

    Asked of redis-py rather than parsed here, because the answer is surprising
    and any reimplementation would drift from it. ``decode_responses`` has no
    entry in ``URL_QUERY_ARGUMENT_PARSERS``, so it never goes through a boolean
    parser: it arrives as a raw string and reaches the connection on plain
    truthiness. ``?decode_responses=false`` and ``?decode_responses=0`` both
    **enable** decoding; only an empty value leaves it off, and only because the
    query parser drops blanks before redis-py sees them.
    """
    try:
        return bool(parse_url(url).get('decode_responses'))
    except (AttributeError, TypeError, ValueError):
        # a URL redis-py cannot read is not this check's finding: W002 covers an
        # empty one, and anything else fails at the first real connection
        return False


class _SharedConnection:
    """Holds the shared client, together with the lock that keeps it single."""

    def __init__(self) -> None:
        """Start with an empty slot; nothing connects until someone asks."""
        self._lock = threading.Lock()
        self._client: Redis | None = None

    @property
    def is_open(self) -> bool:
        """Whether a client has been built and not reset since."""
        return self._client is not None

    def get(self) -> Redis:
        """Return the client, building it at most once."""
        # one read, kept local: a reset() between two reads of the attribute
        # would otherwise let this return None
        client = self._client
        if client is None:
            with self._lock:
                client = self._client
                if client is None:
                    client = self._client = build_client()
        return client

    def reset(self) -> None:
        """Empty the slot, then close whatever was in it."""
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            # closing talks to the socket: a caller waiting to build a
            # replacement should not be held up by it
            client.close()


class _LoopConnections:
    """One async client per event loop, and the generation that invalidates them.

    ``redis.asyncio`` connections belong to the loop that created them, so the
    single shared client the rest of this module keeps would be wrong here: two
    loops sharing one would interleave reads on the same socket.

    **A weak key cannot bound this on its own, and measuring said so.** A connected
    client holds its loop — through the connection, its writer, its transport — so
    the value keeps the key alive and the entry never dies. Measured against a real
    server: three loops used through :func:`aget_redis` and abandoned left three
    live loops and three live clients, where the same three clients dropped on the
    floor left none. That is the whole argument for the sweep in :meth:`get`, and
    the reason no death-triggered cleanup can work here: holding a loop-affine
    client at all is holding its loop.

    The weak keys stay as the cheaper half of the same job — a client built and
    never used holds nothing, so that entry does go by itself — but the sweep is
    what keeps a process that runs a loop per unit of work from accumulating a
    client, and its sockets, for every one of them.

    Invalidation cannot close: ``setting_changed`` is synchronous, ``aclose()`` is
    a coroutine, and closing a client belonging to another loop from another
    thread is exactly what these connections forbid. So a reset only bumps a
    counter, and the next caller *on that loop* awaits the close itself.
    """

    def __init__(self) -> None:
        """Start empty, at generation zero."""
        self._guard = threading.Lock()
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, AsyncRedis]]
        self._clients = weakref.WeakKeyDictionary()
        self._generation = 0

    def _forget_closed(self) -> list[AsyncRedis]:
        """Drop the entries whose loop is closed, and hand back their clients.

        Held under the guard. The clients are returned rather than dropped here so
        that whatever their collection costs happens outside it.

        They cannot be closed properly: ``aclose()`` would have to be awaited on
        the loop that is gone. Letting go is the whole of what is available, and it
        is what would have happened anyway had this registry never held them.
        """
        closed = [loop for loop in list(self._clients.keys()) if loop.is_closed()]
        return [self._clients.pop(loop)[1] for loop in closed]

    async def get(self) -> AsyncRedis:
        """Return this loop's client, building or replacing it as needed."""
        loop = _running_loop()
        # no await inside the lock, so a coroutine cannot be suspended holding it
        # and another thread's loop is only ever held off for a dict lookup
        with self._guard:
            abandoned = self._forget_closed()
            entry = self._clients.get(loop)
            if entry is not None and entry[0] == self._generation:
                stale, client = None, entry[1]
            else:
                stale = None if entry is None else entry[1]
                client = build_async_client()
                self._clients[loop] = (self._generation, client)
        # outside the guard: letting go of the abandoned clients runs whatever
        # their collection runs, and no other loop should wait behind it
        abandoned.clear()
        if stale is not None:
            # on its own loop, which is the only place it may be closed
            await stale.aclose()
        return client

    async def close(self) -> None:
        """Close and forget this loop's client, if it has one."""
        loop = _running_loop()
        with self._guard:
            entry = self._clients.pop(loop, None)
        if entry is not None:
            await entry[1].aclose()

    def invalidate(self) -> None:
        """Mark every client stale without touching any of them."""
        with self._guard:
            self._generation += 1


def _running_loop() -> asyncio.AbstractEventLoop:
    """Return the loop the caller is on, or refuse with what to call instead."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError as error:
        msg = (
            'aget_redis() needs a running event loop; call get_redis() from '
            'synchronous code instead. The async client exists for callers that '
            'are already on a loop — an ASGI view, say — and a client built off '
            'one could not be used from it.'
        )
        raise RuntimeError(msg) from error


_loops = _LoopConnections()


async def aget_redis() -> AsyncRedis:
    """Return the async client for the loop this coroutine is running on."""
    return await _loops.get()


async def aclose_redis() -> None:
    """Close this loop's async client. Django has no hook that does it for you."""
    await _loops.close()


_shared = _SharedConnection()


def get_redis() -> Redis:
    """Return the shared connection, creating it on first use."""
    return _shared.get()


def reset_redis() -> None:
    """Drop the shared connection so the next call reconnects."""
    _shared.reset()


def as_bytes(value: bytes | str) -> bytes:
    """Redis hands back str when the URL enables decode_responses."""
    return value if isinstance(value, bytes) else value.encode('utf-8')


class RedisProxy:
    """Forwards attribute access to the lazily created connection.

    Exists so `from django_redis_aiogram import redis_conn` stays a plain
    module-level import without connecting at import time.
    """

    def __getattr__(self, item: str) -> Any:  # noqa: ANN401 - a forwarded Redis method may return anything
        """Hand the attribute over to the shared client, connecting if needed."""
        return getattr(get_redis(), item)

    def __repr__(self) -> str:
        """Say whether the connection behind the proxy exists yet."""
        state = 'connected' if _shared.is_open else 'not connected'
        return f'<RedisProxy {state}>'


if TYPE_CHECKING:
    #: the proxy forwards everything through __getattr__, which types as Any — so
    #: `redis_conn.ping()` was unchecked while `get_redis().ping()` was not, and
    #: this annotation is what tells a consumer's mypy the difference
    redis_conn: Redis
else:
    redis_conn = RedisProxy()


def _reset_on_setting_change(setting: str, **_kwargs: object) -> None:
    """Reconnect after the settings change, since REDIS_URL may have moved."""
    if setting == SETTINGS_NAME:
        reset_redis()
        # marked, not closed: this runs on whatever thread changed the setting,
        # and an async client may belong to a loop on another one. The next
        # caller on that loop closes the stale client itself
        _loops.invalidate()


setting_changed.connect(_reset_on_setting_change, dispatch_uid='django_redis_aiogram.redis')
