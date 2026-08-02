import threading
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from redis import Redis

from django_redis_aiogram.settings import SETTINGS_NAME, conf

_lock = threading.Lock()
_connection: Redis | None = None


def get_redis() -> Redis:
    """Return the shared connection, creating it on first use."""
    global _connection
    if _connection is None:
        with _lock:
            if _connection is None:
                url = conf['REDIS_URL']
                if not url:
                    raise ImproperlyConfigured(
                        f"{SETTINGS_NAME}['REDIS_URL'] is required to talk to Redis."
                    )
                _connection = Redis.from_url(url)
    return _connection


def reset_redis() -> None:
    """Drop the shared connection so the next call reconnects."""
    global _connection
    with _lock:
        connection, _connection = _connection, None
    if connection is not None:
        connection.close()


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

    def __getattr__(self, item: str) -> Any:
        return getattr(get_redis(), item)

    def __repr__(self) -> str:
        state = 'connected' if _connection is not None else 'not connected'
        return f'<RedisProxy {state}>'


redis_conn = RedisProxy()


def _reset_on_setting_change(sender: Any, setting: str, **kwargs: Any) -> None:
    if setting == SETTINGS_NAME:
        reset_redis()


setting_changed.connect(_reset_on_setting_change, dispatch_uid='django_redis_aiogram.redis')
