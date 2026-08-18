"""Run aiogram next to Django and send Telegram messages through a Redis queue.

Importing this package is cheap on purpose: aiogram (and the pydantic stack
underneath it) costs most of a second, and a migration container or a test run
should not pay that for a bot it never talks to. Every export resolves on first
attribute access instead (PEP 562).
"""

from __future__ import annotations

#: not imported from ``typing``: with annotations postponed nothing here needs that
#: module at runtime, and it was over half of what importing this package cost. Type
#: checkers understand the sentinel, and a reader can see it is always false
TYPE_CHECKING = False

__version__ = '3.1.0'

__all__ = ('TelegramBot', '__version__', 'bot', 'conf', 'get_redis', 'redis_conn')

if TYPE_CHECKING:
    from typing import Any

    from django_redis_aiogram.client import TelegramBot as TelegramBot
    from django_redis_aiogram.redis import get_redis as get_redis
    from django_redis_aiogram.redis import redis_conn as redis_conn
    from django_redis_aiogram.settings import conf as conf

    bot: TelegramBot

#: which module each lazy export lives in
_EXPORTS = {
    'TelegramBot': 'django_redis_aiogram.client',
    'get_redis': 'django_redis_aiogram.redis',
    'redis_conn': 'django_redis_aiogram.redis',
    'conf': 'django_redis_aiogram.settings',
}


def __getattr__(name: str) -> Any:  # noqa: ANN401 - a module attribute is whatever the module exports
    """Resolve an export on first access, then cache it on the module."""
    if name == 'bot':
        # `_singleton`'s module body builds the one instance, and Python's import
        # lock is what makes two threads racing here share it. That is why this
        # package holds no lock of its own: an explicit one would need `threading`
        # imported at module scope, which is most of what importing this used to cost
        from django_redis_aiogram._singleton import (  # noqa: PLC0415 - the point: pay for aiogram on use, not import
            bot,
        )

        globals()['bot'] = bot
        return bot
    if name in _EXPORTS:
        from importlib import import_module  # noqa: PLC0415 - as above

        value = getattr(import_module(_EXPORTS[name]), name)
        globals()[name] = value
        return value
    msg = f'module {__name__!r} has no attribute {name!r}'
    raise AttributeError(msg)


def __dir__() -> list[str]:
    """List the lazy exports alongside whatever is already materialised."""
    return sorted(set(globals()) | set(__all__))
