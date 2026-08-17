"""The one Redis connection this package shares.

Senders, consumers, the FSM heartbeat and the management commands all go through
:func:`get_redis`, so a process opens a single connection pool however many of
them are running. The connection is built on first use rather than at import,
because Django settings are not readable while the app registry is loading.
"""

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from redis import Redis
from redis.connection import parse_url as _parse_url

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


setting_changed.connect(_reset_on_setting_change, dispatch_uid='django_redis_aiogram.redis')
