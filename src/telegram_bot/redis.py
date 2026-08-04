"""Deprecated alias for :mod:`django_redis_aiogram.redis`."""

from django_redis_aiogram.redis import get_db_index, get_redis, redis_conn, reset_redis

__all__ = (
    "get_db_index",
    "get_redis",
    "redis_conn",
    "reset_redis",
)
