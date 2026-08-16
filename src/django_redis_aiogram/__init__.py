"""Run aiogram next to Django and send Telegram messages through a Redis queue.

Importing this package is cheap on purpose: aiogram (and the pydantic stack
underneath it) costs most of a second, and a migration container or a test run
should not pay that for a bot it never talks to. Every export resolves on first
attribute access instead (PEP 562).
"""

import threading
from typing import TYPE_CHECKING, Any

__version__ = '3.1.0'

__all__ = ('TelegramBot', '__version__', 'bot', 'conf', 'get_redis', 'redis_conn')

if TYPE_CHECKING:
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

# two threads asking for `bot` first must not each build one: the instances
# would hold separate event loops, and loop_lock would serialize nothing
_bot_guard = threading.Lock()


def __getattr__(name: str) -> Any:  # noqa: ANN401 - a module attribute is whatever the module exports
    """Resolve an export on first access, then cache it on the module."""
    if name == 'bot':
        with _bot_guard:
            if 'bot' not in globals():
                from django_redis_aiogram.client import (  # noqa: PLC0415 - the point: pay for aiogram on use, not import
                    TelegramBot,
                )

                globals()['bot'] = TelegramBot()
        return globals()['bot']
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
