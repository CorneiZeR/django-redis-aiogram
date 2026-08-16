import fakeredis
import pytest

# `django_redis_aiogram.bot` is the singleton instance, so the class lives in
# `client`; patching the wrong one silently leaves the real connection in place.
PATCH_TARGETS = (
    'django_redis_aiogram.redis.get_redis',
    'django_redis_aiogram.delivery.get_redis',
    'django_redis_aiogram.client.get_redis',
    'django_redis_aiogram.get_redis',
    'django_redis_aiogram.management.commands.tgbot_healthcheck.get_redis',
    'django_redis_aiogram.management.commands.tgbot_reclaim.get_redis',
)


@pytest.fixture
def redis_server(monkeypatch):
    """Swap the shared connection for an in-memory one."""
    server = fakeredis.FakeRedis()
    for target in PATCH_TARGETS:
        monkeypatch.setattr(target, lambda *args, server=server, **kwargs: server)
    return server
