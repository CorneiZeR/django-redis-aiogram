"""Deprecated alias for :mod:`django_redis_aiogram`.

``telegram_bot`` is a very generic name for a top-level package in
site-packages and collided easily with project apps, so 2.0 renamed it. This
shim keeps existing imports and ``INSTALLED_APPS`` entries working.

Its exports are as lazy as the ones it forwards to: resolving ``bot`` here would
build the bot and import aiogram during Django startup, which is exactly what a
1.x project gains by upgrading.
"""

import warnings
from typing import TYPE_CHECKING, Any

warnings.warn(
    "The 'telegram_bot' package is deprecated, use 'django_redis_aiogram' instead. It will be removed in 3.0.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ('TelegramBot', '__version__', 'bot', 'conf', 'get_redis', 'redis_conn')

if TYPE_CHECKING:
    from django_redis_aiogram import TelegramBot as TelegramBot
    from django_redis_aiogram import __version__ as __version__
    from django_redis_aiogram import conf as conf
    from django_redis_aiogram import get_redis as get_redis
    from django_redis_aiogram import redis_conn as redis_conn

    bot: TelegramBot


def __getattr__(name: str) -> Any:  # noqa: ANN401 - a module attribute is whatever the module exports
    """Forward to the package, which resolves the export on first access."""
    if name in __all__:
        import django_redis_aiogram  # noqa: PLC0415 - forwarding must not import eagerly either

        value = getattr(django_redis_aiogram, name)
        globals()[name] = value
        return value
    msg = f'module {__name__!r} has no attribute {name!r}'
    raise AttributeError(msg)


def __dir__() -> list[str]:
    """List the forwarded exports alongside whatever is already materialised."""
    return sorted(set(globals()) | set(__all__))
