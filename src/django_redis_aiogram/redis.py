"""The one Redis connection this package shares.

Senders, consumers, the FSM heartbeat and the management commands all go through
:func:`get_redis`, so a process opens a single connection pool however many of
them are running. The connection is built on first use rather than at import,
because Django settings are not readable while the app registry is loading.
"""

import threading
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from redis import Redis

from django_redis_aiogram.settings import SETTINGS_NAME, conf


def read_timeout() -> int:
    """How long any single Redis call may take before the server is dead to us."""
    return max(1, int(conf['REDIS_TIMEOUT']))


def build_client() -> Redis:
    """Build a client bounded in time, so no call can hang for ever.

    redis-py only started defaulting to a read deadline in 8.0; on the 5.0 floor
    a server that accepts the connection and then stops answering blocks the
    caller until the process is killed. Blocking reads stay inside the deadline
    by asking for less than it — see :class:`~django_redis_aiogram.delivery.BlpopDelivery`.
    """
    url = conf['REDIS_URL']
    if not url:
        msg = f"{SETTINGS_NAME}['REDIS_URL'] is required to talk to Redis."
        raise ImproperlyConfigured(msg)
    timeout = read_timeout()
    return Redis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)


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


def get_db_index() -> int:
    """Return the database number encoded in REDIS_URL."""
    return int(get_redis().connection_pool.connection_kwargs.get('db', 0) or 0)


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


redis_conn = RedisProxy()


def _reset_on_setting_change(setting: str, **_kwargs: object) -> None:
    """Reconnect after the settings change, since REDIS_URL may have moved."""
    if setting == SETTINGS_NAME:
        reset_redis()


setting_changed.connect(_reset_on_setting_change, dispatch_uid='django_redis_aiogram.redis')
