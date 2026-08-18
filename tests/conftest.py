import fakeredis
import fakeredis.aioredis
import pytest

# `django_redis_aiogram.bot` is the singleton instance, so the class lives in
# `client`; patching the wrong one silently leaves the real connection in place.
PATCH_TARGETS = (
    'django_redis_aiogram.redis.get_redis',
    'django_redis_aiogram.delivery.get_redis',
    'django_redis_aiogram.client.get_redis',
    'django_redis_aiogram.get_redis',
    # the probe's decision moved out of the command in 3.1.0, so it can run without
    # django.setup(); the command is a wrapper and holds no connection of its own
    'django_redis_aiogram.healthcheck.get_redis',
    'django_redis_aiogram.management.commands.tgbot_reclaim.get_redis',
)


@pytest.fixture
def redis_server(monkeypatch):
    """Swap the shared connection for an in-memory one, sync and async alike.

    The async half patches `build_async_client` rather than `aget_redis`, so the
    per-loop registry runs for real and hands out fakes — patching the accessor
    would leave the thing under test untested. One `FakeServer` behind both, so a
    message queued through `asend` is visible to a synchronous read.
    """
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    for target in PATCH_TARGETS:
        monkeypatch.setattr(target, lambda *args, client=client, **kwargs: client)
    monkeypatch.setattr(
        'django_redis_aiogram.redis.build_async_client',
        lambda server=server: fakeredis.aioredis.FakeRedis(server=server),
    )
    return client
