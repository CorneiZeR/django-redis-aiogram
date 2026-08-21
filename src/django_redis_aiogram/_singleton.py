"""The one shared :class:`~django_redis_aiogram.client.TelegramBot` for this process.

A module body, not a lock. Python guarantees a module executes once per process and
makes concurrent importers wait on that module's own import lock, so two threads
reaching for ``django_redis_aiogram.bot`` at the same moment get the same instance
without this package holding a lock of its own — and without ``__init__`` importing
``threading`` to build one, which is most of what importing the package used to cost
outside Django.

Why it matters that they get the same one: each instance builds its own event loop and
its own HTTP session, and ``loop_lock`` serializes access to *a* loop. Two bots means
two loops, and the lock that exists to keep ``run_until_complete`` from being reentered
would be guarding one of them while the other was entered.

Importing this module is what pays for aiogram, so nothing imports it at module
scope: :func:`django_redis_aiogram.__getattr__` reaches it on the first access to
``bot`` and never again.
"""

from django_redis_aiogram.client import TelegramBot

bot = TelegramBot()
